"""
deploy_custom_rules.py
Custom routing rules (flexible file routing used by Bethesda + others).

Extracted from deploy.py during the 2026-04 refactor. No behaviour changes.
"""

from __future__ import annotations

import concurrent.futures
import fnmatch
import os
import shutil
from pathlib import Path

from Utils.app_log import safe_log as _safe_log
from Utils.deploy_shared import (
    CustomRule,
    LinkMode,
    _deploy_workers,
    _do_link,
    _path_under_root,
    _resolve_source,
)


_CUSTOM_RULES_LOG_NAME = "custom_rules_deployed.txt"
_CUSTOM_RULES_BACKUP_DIR = "custom_rules_backup"


def deploy_custom_rules(
    filemap_path: Path,
    game_root: Path,
    staging_root: Path,
    rules: list[CustomRule],
    mode: LinkMode = LinkMode.HARDLINK,
    strip_prefixes: set[str] | None = None,
    per_mod_strip_prefixes: dict[str, list[str]] | None = None,
    log_fn=None,
    progress_fn=None,
) -> set[str]:
    """Deploy filemap entries that match a CustomRule to their designated dirs.

    Matching logic (first matching rule wins): file matches a rule by folder
    (any path segment in rule.folders), extension (rule.extensions), or
    filename (rule.filenames). Placement under ``game_root / rule.dest``
    depends on rule.flatten:
    - flatten=False (default) — preserve the full mod-relative path under dest
    - flatten=True + folder match — strip the prefix above the matched folder,
      keep matched folder + contents under dest
    - flatten=True + ext/filename match — bare filename under dest

    Returns the set of lowercased rel_paths that were handled so the caller
    can exclude them from the normal deploy step.

    A log of placed absolute paths is written to
    filemap_path.parent / "custom_rules_deployed.txt" for use by
    restore_custom_rules().
    """
    if not rules:
        return set()

    _log = _safe_log(log_fn)
    _strip = {p.lower() for p in strip_prefixes} if strip_prefixes else set()
    _per_mod_strip = per_mod_strip_prefixes or {}
    overwrite_dir = staging_root.parent / "overwrite"
    _overwrite_str = str(overwrite_dir)
    _staging_str   = str(staging_root)
    nocase_cache: dict[Path, dict[str, list[Path]]] = {}
    sorted_strip   = sorted(_strip) if _strip else []

    # Pre-process rules into normalised form for fast matching.
    # Extensions are kept as a list sorted longest-first so that multi-dot
    # extensions like ".dekcns.json" win over their plain ".json" suffix.
    _rules: list[tuple[CustomRule, set[str], list[str], set[str]]] = []
    for rule in rules:
        ext_list = sorted({e.lower() for e in rule.extensions}, key=len, reverse=True)
        _rules.append((
            rule,
            {f.lower() for f in rule.folders},
            ext_list,
            {n.lower() for n in rule.filenames},
        ))

    def _ext_match(filename: str, exts: list[str]) -> str | None:
        """Return the longest extension in ``exts`` that ``filename`` ends with
        (as ``.something``), or None. ``exts`` must be sorted longest-first.
        """
        for e in exts:
            if filename.endswith(e) and len(filename) > len(e):
                return e
        return None

    def _name_match(filename: str, names: set[str]) -> bool:
        """Match ``filename`` (lowercased) against the rule's filenames.
        Glob characters (``*``, ``?``, ``[seq]``) are honoured so a rule can
        target e.g. ``*.dekcns.json``; plain entries match by exact equality.
        """
        for n in names:
            if any(c in n for c in "*?["):
                if fnmatch.fnmatchcase(filename, n):
                    return True
            elif filename == n:
                return True
        return False

    def _match_rule(rel_lower: str) -> tuple[CustomRule, int, str] | None:
        """Return (rule, strip_len, matched_ext) for the first match, or None.

        strip_len is the number of leading characters to strip from rel_str
        so the folder itself (and its contents) are preserved under dest.
        For extension/filename matches strip_len is -1 (sentinel meaning
        "no folder anchor"); placement is then driven by rule.flatten —
        True flattens to the bare filename, False preserves the full
        mod-relative path under dest.

        matched_ext is the extension that matched (e.g. ".dekcns.json"), or
        an empty string for folder/filename-only matches. Used to derive the
        correct stem for companion-file resolution.

        Example with folder "logicmods":
          "logicmods/file.pak"              → strip_len=0 (keep as-is)
          "paks/logicmods/file.pak"         → strip_len=5 (strip "paks/")
          "content/paks/logicmods/file.pak" → strip_len=13 (strip "content/paks/")
        """
        parts = rel_lower.split("/")
        filename = parts[-1]
        is_loose = len(parts) == 1
        for rule, folders, exts, filenames in _rules:
            strip_len = -1
            folder_hit = False
            if folders:
                for f in folders:
                    if "/" in f:
                        # Multi-segment folder: find it anywhere as a
                        # contiguous segment sequence.
                        idx = rel_lower.find(f + "/")
                        if idx < 0 and rel_lower.endswith(f):
                            idx = len(rel_lower) - len(f)
                        if idx >= 0 and (idx == 0 or rel_lower[idx - 1] == "/"):
                            strip_len = idx
                            folder_hit = True
                            break
                    else:
                        # Single-segment: find it as any directory segment.
                        for pi, seg in enumerate(parts[:-1]):
                            if seg == f:
                                # Strip everything before this segment.
                                strip_len = sum(len(parts[j]) + 1 for j in range(pi))
                                folder_hit = True
                                break
                        if folder_hit:
                            break
                # loose_only on folder rules: matched folder must itself be
                # at the top level (no parent above it).
                if folder_hit and rule.loose_only and strip_len != 0:
                    continue
            matched_ext = _ext_match(filename, exts) if exts else None
            if folder_hit and (not exts or matched_ext is not None):
                return rule, strip_len, matched_ext or ""
            # For ext/filename-only rules, loose_only means the file itself
            # has no directory components.
            if rule.loose_only and not is_loose:
                continue
            if matched_ext is not None and not folders and not filenames:
                return rule, -1, matched_ext
            if filenames and _name_match(filename, filenames):
                return rule, -1, ""
        return None

    already_seen: set[str] = set()
    tasks: list[tuple[Path, Path]] = []   # (src, dst)
    handled_lower: set[str] = set()
    # Retain primary matches and all entries so we can resolve companions
    # after the first pass.
    # primary_matches: rel_lower -> (rule, strip_len, rel_str, mod_name, matched_ext)
    primary_matches: dict[str, tuple[CustomRule, int, str, str, str]] = {}
    # entries_by_parent: parent_lower -> list of (rel_str, mod_name, name_lower)
    # We index by parent only and resolve siblings by name suffix at companion
    # time, since a multi-dot extension means a single file has multiple valid
    # "stems" (e.g. "foo.dekcns.json" has stems "foo" and "foo.dekcns").
    entries_by_parent: dict[str, list[tuple[str, str, str]]] = {}

    with filemap_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if "\t" not in line:
                continue
            rel_str, mod_name = line.split("\t", 1)
            rel_lower = rel_str.lower()
            if rel_lower in already_seen:
                continue
            already_seen.add(rel_lower)

            # Index every entry by parent dir for companion resolution.
            parent_lower, _, name_lower = rel_lower.rpartition("/")
            entries_by_parent.setdefault(parent_lower, []).append(
                (rel_str, mod_name, name_lower)
            )

            match = _match_rule(rel_lower)
            if match is None:
                continue
            rule, strip_len, matched_ext = match
            primary_matches[rel_lower] = (rule, strip_len, rel_str, mod_name, matched_ext)

            src_str = _resolve_source(
                mod_name, rel_str, rel_lower, overwrite_dir, staging_root,
                _overwrite_str, _staging_str, sorted_strip, _per_mod_strip,
                nocase_cache,
            )
            if src_str is None:
                _log(f"  WARN: source not found — {rel_str} ({mod_name})")
                continue
            src = Path(src_str)

            dest_base = game_root / rule.dest if rule.dest else game_root
            if rule.flatten:
                if strip_len >= 0:
                    # Folder match + flatten=True: strip the prefix above the
                    # matched folder so the folder + contents land under dest.
                    #   strip_len=0: LogicMods/f → dest/LogicMods/f
                    #   strip_len=5: Paks/LogicMods/f → dest/LogicMods/f
                    kept = rel_str[strip_len:].lstrip("/")
                    dst = dest_base / kept if kept else dest_base
                else:
                    # Ext/filename-only + flatten=True: bare filename under dest.
                    dst = dest_base / src.name
            else:
                # flatten=False (any match type): preserve the full
                # mod-relative path under dest.
                #   folders=["LogicMods"] dest="" file=Paks/LogicMods/x.pak
                #     → Paks/LogicMods/x.pak (at game root)
                #   exts=[".dll"] dest="win64/mods/dlls" file=folder1/x.dll
                #     → win64/mods/dlls/folder1/x.dll
                dst = dest_base / rel_str
            tasks.append((src, dst))
            handled_lower.add(rel_lower)

    # Second pass: companion files ride along with their primary match.
    # Companions are matched longest-first too so a ".dekcns.json" companion
    # would beat a ".json" one.
    for rel_lower, (rule, strip_len, rel_str, _mod_name, matched_ext) in list(primary_matches.items()):
        companions = sorted(
            {c.lower() for c in rule.companion_extensions}, key=len, reverse=True
        )
        if not companions:
            continue
        parent_lower, _, name_lower = rel_lower.rpartition("/")
        # Stem is the primary filename minus the extension that matched.
        # Falls back to splitext when there was no extension match (folder/
        # filename rules) — companions remain stem-relative in that case.
        if matched_ext and name_lower.endswith(matched_ext):
            stem_lower = name_lower[: -len(matched_ext)]
        else:
            stem_lower, _ = os.path.splitext(name_lower)
        siblings = entries_by_parent.get(parent_lower, ())
        stem_dot = stem_lower + "."
        for sib_rel_str, sib_mod_name, sib_name_lower in siblings:
            sib_lower = sib_rel_str.lower()
            if sib_lower in handled_lower:
                continue
            if not sib_name_lower.startswith(stem_dot):
                continue
            sib_ext = None
            for c in companions:
                if sib_name_lower.endswith(c) and len(sib_name_lower) > len(c):
                    sib_ext = c
                    break
            if sib_ext is None:
                continue
            src_str = _resolve_source(
                sib_mod_name, sib_rel_str, sib_lower, overwrite_dir, staging_root,
                _overwrite_str, _staging_str, sorted_strip, _per_mod_strip,
                nocase_cache,
            )
            if src_str is None:
                _log(f"  WARN: source not found — {sib_rel_str} ({sib_mod_name})")
                continue
            src = Path(src_str)
            dest_base = game_root / rule.dest if rule.dest else game_root
            if rule.flatten:
                if strip_len >= 0:
                    kept = sib_rel_str[strip_len:].lstrip("/")
                    dst = dest_base / kept if kept else dest_base
                else:
                    dst = dest_base / src.name
            else:
                dst = dest_base / sib_rel_str
            tasks.append((src, dst))
            handled_lower.add(sib_lower)

    if not tasks:
        return handled_lower

    # Backup directory for vanilla files that will be overwritten
    backup_dir = filemap_path.parent / _CUSTOM_RULES_BACKUP_DIR
    if backup_dir.exists():
        shutil.rmtree(backup_dir)

    # Create destination directories
    needed_dirs: set[Path] = {dst.parent for _, dst in tasks}
    for d in needed_dirs:
        d.mkdir(parents=True, exist_ok=True)

    placed_abs: list[str] = []
    total = len(tasks)
    _game_root = game_root

    # Back up any vanilla files we are about to overwrite (must be serial).
    for src, dst in tasks:
        if dst.exists() and not dst.is_symlink():
            try:
                rel = dst.relative_to(_game_root)
                bak = backup_dir / rel
                bak.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(dst), str(bak))
            except (ValueError, OSError) as e:
                _log(f"  WARN: could not back up {dst}: {e}")
        elif dst.is_symlink():
            dst.unlink()

    # Transfer files in parallel.
    transfer_tasks: list[tuple[str, str]] = [(str(s), str(d)) for s, d in tasks]

    def _do_custom(item: tuple[str, str]) -> tuple[str | None, tuple[str, OSError] | None]:
        src_s, dst_s = item
        err = _do_link(src_s, dst_s, mode)
        if err is None:
            return dst_s, None
        return None, (dst_s, err)

    done_count = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=_deploy_workers()) as pool:
        for result, err in pool.map(_do_custom, transfer_tasks):
            done_count += 1
            if result is not None:
                placed_abs.append(result)
            elif err is not None:
                dst_err, exc = err
                _log(f"  WARN: could not transfer {dst_err}: {exc}")
            if progress_fn is not None and (done_count % 200 == 0 or done_count == total):
                progress_fn(done_count, total)

    log_path = filemap_path.parent / _CUSTOM_RULES_LOG_NAME
    try:
        if placed_abs:
            log_path.write_text("\n".join(placed_abs), encoding="utf-8")
        elif log_path.exists():
            log_path.unlink()
    except OSError:
        pass

    _log(f"  Custom rules: placed {len(placed_abs)} file(s).")
    return handled_lower


def restore_custom_rules(
    filemap_path: Path,
    game_root: Path,
    rules: list[CustomRule],
    log_fn=None,
) -> int:
    """Remove files placed by deploy_custom_rules() and prune empty dest dirs.

    Reads filemap_path.parent / "custom_rules_deployed.txt", deletes every
    listed absolute path, then tries to rmdir each rule's destination directory
    (silently ignored if non-empty).  Returns the number of files removed.
    """
    _log = _safe_log(log_fn)
    log_path = filemap_path.parent / _CUSTOM_RULES_LOG_NAME
    backup_dir = filemap_path.parent / _CUSTOM_RULES_BACKUP_DIR

    if not log_path.is_file():
        return 0

    placed = [p for p in log_path.read_text(encoding="utf-8").splitlines() if p]
    removed = 0
    dirs_to_prune: set[Path] = set()
    _game_root_resolved = game_root.resolve()
    for abs_str in placed:
        p = Path(abs_str)
        # Use the unresolved path for the under-root check so symlinks
        # (whose targets live outside game_root) are not incorrectly blocked.
        try:
            p.relative_to(game_root)
        except ValueError:
            try:
                p.resolve().relative_to(_game_root_resolved)
            except ValueError:
                _log(f"  SKIP: path traversal blocked — {abs_str}")
                continue
        if p.is_file() or p.is_symlink():
            p.unlink()
            removed += 1
        # Collect parent dirs for pruning (stop at game_root, unresolved check)
        parent = p.parent
        while parent != game_root:
            try:
                parent.relative_to(game_root)
            except ValueError:
                break
            dirs_to_prune.add(parent)
            parent = parent.parent

    # Restore backed-up vanilla files
    if backup_dir.is_dir():
        for bak_src in backup_dir.rglob("*"):
            if not bak_src.is_file():
                continue
            rel = bak_src.relative_to(backup_dir)
            orig = game_root / rel
            if not _path_under_root(orig, game_root):
                _log(f"  SKIP: path traversal blocked — {rel}")
                continue
            orig.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(bak_src), str(orig))
            _log(f"  Restored {rel} from custom_rules_backup/")
        shutil.rmtree(backup_dir, ignore_errors=True)

    # Prune empty subdirectories deepest-first; never touch game_root itself
    for d in sorted(dirs_to_prune, key=lambda x: len(x.parts), reverse=True):
        try:
            d.rmdir()
        except OSError:
            pass

    log_path.unlink()
    _log(f"  Custom rules restore: removed {removed} file(s).")
    return removed


__all__ = [
    "_CUSTOM_RULES_LOG_NAME",
    "_CUSTOM_RULES_BACKUP_DIR",
    "deploy_custom_rules",
    "restore_custom_rules",
]
