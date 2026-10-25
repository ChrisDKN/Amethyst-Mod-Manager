"""
deployment/wine_dll.py
Wine / Proton DLL override management and emergency deployed-file cleanup.

Extracted from the deployment façade during the 2026-04 refactor.
"""

from __future__ import annotations

import os
import shutil
import time as _time
from pathlib import Path

from Utils.app_log import safe_log as _safe_log
from Utils.atomic_write import write_atomic_text
from Utils.wine.dll_config import _merge_overrides
from Utils.wine.registry import guard_user_hive


# ---------------------------------------------------------------------------
# Emergency cleanup - remove hardlinked / symlinked mod files
# ---------------------------------------------------------------------------

def remove_deployed_files(game_dir: Path, log_fn=None, *,
                          source_roots=(), core_dirs=None) -> int:
    """Remove links to staged files and restore the specified vanilla backups.

    Hardlink counts alone do not establish mod ownership. Unrecorded copies
    and links to files outside the staging roots are preserved.
    """
    import stat
    from Utils.deployment.shared import (
        _TRASH_INFIX, _prune_empty_dirs, _restore_backup_dir)

    _log = _safe_log(log_fn)
    game_dir = Path(game_dir)
    if game_dir.is_symlink() or game_dir.name.endswith("_Core"):
        raise RuntimeError(f"Refusing to scan a symlink or vanilla backup: {game_dir}")
    root = game_dir.resolve()
    sources = {Path(path).resolve() for path in source_roots}
    for source in sources:
        if source.is_relative_to(root) or root.is_relative_to(source):
            raise RuntimeError(f"Mod storage overlaps the game folder: {source}")
    cores = list(core_dirs) if core_dirs is not None else [
        game_dir.with_name(game_dir.name + "_Core")]
    for core in cores:
        core = Path(core)
        destination = core.with_name(core.name.removesuffix("_Core"))
        if (not core.name.endswith("_Core") or core.is_symlink()
                or not destination.resolve().is_relative_to(root)):
            raise RuntimeError(f"Unsafe vanilla backup location: {core}")

    def walk(path):
        def fail(exc):
            raise exc
        return os.walk(path, topdown=True, followlinks=False, onerror=fail)

    candidates = []
    links = []
    if game_dir.exists():
        for parent, dirs, files in walk(game_dir):
            base = Path(parent)
            for name in list(dirs):
                child = base / name
                if child.is_symlink():
                    dirs.remove(name)
                    links.append(child)
                elif name.endswith("_Core") or _TRASH_INFIX in name:
                    dirs.remove(name)
            for name in files:
                child = base / name
                info = child.lstat()
                if stat.S_ISLNK(info.st_mode):
                    links.append(child)
                elif stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
                    candidates.append((child, (info.st_dev, info.st_ino)))

    wanted = {identity for _path, identity in candidates}
    owned = set()
    if wanted:
        for source in sources:
            if not source.exists():
                continue
            for parent, _dirs, files in walk(source):
                for name in files:
                    info = (Path(parent) / name).lstat()
                    identity = (info.st_dev, info.st_ino)
                    if stat.S_ISREG(info.st_mode) and identity in wanted:
                        owned.add(identity)

    removed = 0
    emptied = set()
    for link in links:
        target = link.resolve(strict=False)
        if any(target.is_relative_to(source) for source in sources):
            link.unlink()
            removed += 1
            emptied.add(link.parent)
    for path, identity in candidates:
        if identity in owned:
            info = path.lstat()
            if (info.st_dev, info.st_ino) != identity:
                raise RuntimeError(f"File changed during cleanup: {path}")
            path.unlink()
            removed += 1
            emptied.add(path.parent)

    _prune_empty_dirs(emptied, stop_dirs={game_dir})
    for core in map(Path, cores):
        if not core.exists():
            continue
        destination = core.with_name(core.name[:-len("_Core")])
        if not destination.exists():
            core.rename(destination)
        else:
            for parent, dirs, files in walk(core):
                base = Path(parent)
                for name in (*dirs, *files):
                    target = destination / (base / name).relative_to(core)
                    if (not target.parent.resolve().is_relative_to(root)
                            or (name in dirs and target.is_symlink()
                                and not (base / name).is_symlink())):
                        raise RuntimeError(f"Vanilla restore escapes the game folder: {target}")
                for name in dirs:
                    source = base / name
                    if not source.is_symlink():
                        (destination / source.relative_to(core)).mkdir(
                            parents=True, exist_ok=True)
            _restore_backup_dir(core, destination, _log)
            if core.exists():
                raise RuntimeError(f"Vanilla backup could not be fully restored: {core}")
        _log(f"Restored vanilla backup: {core}")

    _log(f"Removed {removed} deployed link(s) from {game_dir}")
    return removed


def clean_game_folder(game, target: Path, log_fn=None) -> int:
    """Recover physical deployment leftovers under the shared operation lock."""
    import json
    from Utils.deployment.game_root import (
        _FILEMAP_BACKUP_DIR, _FILEMAP_LOG_NAME, restore_filemap_from_root)
    from Utils.deployment.locking import coordinated_game_mutation
    from Utils.deployment.root import (
        _ROOT_BACKUP_NAME, _ROOT_IDENTITY_NAME, _ROOT_LOG_NAME,
        restore_root_folder_for_game)
    from Utils.deployment.shared import remove_case_alias_links, remove_probe_stub_dirs
    from Utils.deployment.standard import restore_data_core
    from Utils.vfs import has_any_deployment_state

    _log = _safe_log(log_fn)
    with coordinated_game_mutation(game, "clean-game-folder"):
        if has_any_deployment_state(game):
            raise RuntimeError("Cannot clean the game folder while a VFS deployment exists. "
                               "Recover missing game files before using Restore.")
        game_root = Path(game.get_game_path())
        target = Path(target)
        data_root = game.get_mod_data_path() or game_root
        if (not game_root.is_dir() or game_root.is_symlink()
                or target.is_symlink()
                or target.resolve() != Path(data_root).resolve()):
            raise RuntimeError(f"Unsafe or unavailable game folder: {target}")
        scan_roots = [game_root]
        if not target.resolve().is_relative_to(game_root.resolve()):
            scan_roots.append(target)
        filemap = game.get_effective_filemap_path()
        get_context = getattr(game, "get_deployment_context", None)
        context = get_context() if callable(get_context) else {}
        if context:
            recorded_root = context.get("game_root") or {}
            if (Path(context["filemap_path"]).resolve() != filemap.resolve()
                    or Path(recorded_root["path"]).resolve() != game_root.resolve()):
                raise RuntimeError("Select the deployed profile and its original game folder "
                                   "before cleaning, or use Restore.")
        sources = {
            game.get_effective_mod_staging_path(),
            game.get_effective_overwrite_path(),
            game.get_effective_root_folder_path(),
        }
        for source in sources:
            if any(source.resolve().is_relative_to(root.resolve())
                   or root.resolve().is_relative_to(source.resolve()) for root in scan_roots):
                raise RuntimeError(f"Mod storage overlaps the game folder: {source}")
        core = target.with_name(target.name + "_Core") if target != game_root else None
        if core is not None and core.is_symlink():
            raise RuntimeError(f"Unsafe vanilla backup location: {core}")
        metadata = game.get_effective_root_folder_path().parent
        identities = metadata / _ROOT_IDENTITY_NAME
        if identities.is_file():
            recorded = json.loads(identities.read_text(encoding="utf-8"))
            recorded_root = recorded.get("root")
            if recorded_root and Path(recorded_root).resolve() != game_root.resolve():
                raise RuntimeError("Root Folder recovery belongs to a different game folder. "
                                   "Use Restore with the original installation.")
        destinations = []
        for journal in (filemap.parent / _FILEMAP_LOG_NAME, metadata / _ROOT_LOG_NAME):
            if journal.is_file():
                destinations.extend(line for line in journal.read_text(
                    encoding="utf-8", errors="surrogateescape").splitlines()
                    if line and line != "---dirs---")
        for backup in (filemap.parent / _FILEMAP_BACKUP_DIR, metadata / _ROOT_BACKUP_NAME):
            if backup.is_symlink():
                raise RuntimeError(f"Unsafe recovery backup: {backup}")
            if backup.is_dir():
                destinations.extend(str(path.relative_to(backup)) for path in backup.rglob("*"))
        for relative in destinations:
            path = Path(relative)
            if (path.is_absolute() or ".." in path.parts
                    or not (game_root / path).parent.resolve().is_relative_to(game_root.resolve())):
                raise RuntimeError(f"Cleanup recovery path escapes the game folder: {relative}")
        removed = restore_filemap_from_root(
            filemap, game_root, log_fn=_log, move_runtime_files=False)
        aliases = getattr(game, "case_alias_dirs", ())
        removed += remove_case_alias_links(game_root, aliases, log_fn=_log, strict=True)
        if core is not None and core.exists():
            whitelist = getattr(game, "restore_whitelist_matcher", None)
            relative = (target.relative_to(game_root).as_posix().rstrip("/") + "/"
                        if target.is_relative_to(game_root) else "")
            restore_data_core(
                target, core_dir=core,
                overwrite_dir=game.get_effective_overwrite_path(),
                restore_whitelist=(whitelist(rel_prefix=relative)
                                   if callable(whitelist) else None),
                log_fn=_log, game=game,
                profile_dir=getattr(game, "_active_profile_dir", None))
            if core.exists():
                raise RuntimeError(f"Vanilla backup could not be fully restored: {core}")
        for root in scan_roots:
            removed += remove_deployed_files(
                root, log_fn=_log, source_roots=sources, core_dirs=[])
        removed += restore_root_folder_for_game(game, game_root=game_root, log_fn=_log)
        removed += remove_case_alias_links(game_root, aliases, log_fn=_log, strict=True)
        remove_probe_stub_dirs(game_root, getattr(game, "probe_stub_dirs", ()), log_fn=_log)
        hook = getattr(game, "post_clean_game_folder", None)
        if callable(hook) and hook(log_fn=_log) is False:
            raise RuntimeError("The game's cleanup hook did not complete.")
        return removed


# ---------------------------------------------------------------------------
# Wine / Proton prefix helpers
# ---------------------------------------------------------------------------

@guard_user_hive
def apply_wine_dll_overrides(
    prefix_path: Path,
    overrides: dict[str, str],
    log_fn=None,
) -> bool:
    """Write DLL override entries into the Proton prefix's user.reg.

    *prefix_path* is the ``pfx/`` directory (the one that contains
    ``drive_c/`` and ``user.reg``).

    *overrides* maps DLL name → load order string, e.g.
    ``{"winhttp": "native,builtin"}``.

    The function locates (or creates) the
    ``[Software\\\\Wine\\\\DllOverrides]`` section in ``user.reg`` and
    inserts/updates each key.  The file is written atomically so a crash
    mid-write cannot corrupt the prefix.

    Returns False if the prefix registry cannot be read or written.
    """
    _log = _safe_log(log_fn)

    if not overrides:
        return True
    overrides = _merge_overrides(overrides)

    # Accept either the pfx/ directory directly or its parent (compatdata/<id>/)
    if not (prefix_path / "user.reg").is_file() and (prefix_path / "pfx" / "user.reg").is_file():
        prefix_path = prefix_path / "pfx"
    user_reg = prefix_path / "user.reg"
    if not user_reg.is_file():
        _log(f"Warning: user.reg not found at {user_reg}; skipping DLL overrides.")
        return False

    try:
        text = user_reg.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        _log(f"Warning: could not read user.reg: {exc}")
        return False

    lines = text.splitlines(keepends=True)
    section_header = "[Software\\\\Wine\\\\DllOverrides]"

    # Locate the section (case-insensitive header match)
    section_start: int | None = None
    section_end: int | None = None  # index of first line after this section
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.lower().startswith(section_header.lower()):
            section_start = i
        elif section_start is not None and stripped.startswith("["):
            section_end = i
            break

    # Timestamps used when creating a new section or adding new entries only.
    # Section headers in Wine's .reg format use a decimal Unix timestamp;
    # #time= lines use a hex Windows FILETIME (100ns ticks since 1601-01-01).
    _unix_ts   = int(_time.time())
    _filetime_hex = format(int((_unix_ts + 11644473600) * 1e7), "x")

    if section_start is None:
        # Section doesn't exist - append it at the end
        _log(r"[Software\\Wine\\DllOverrides] not found; appending to user.reg.")
        if lines and not lines[-1].endswith("\n"):
            lines.append("\n")
        lines.append("\n")
        lines.append(f"{section_header} {_unix_ts}\n")
        lines.append(f"#time={_filetime_hex}\n")
        for dll, value in sorted(overrides.items()):
            lines.append(f'"{dll}"="{value}"\n')
            _log(f"  DLL override set: {dll} = {value}")
    else:
        # Section exists - find existing keys and add/update
        body_start = section_start + 1
        body_end = section_end if section_end is not None else len(lines)
        key_lines = lines[body_start:body_end]

        # Separate trailing blank lines (section terminators in Wine's .reg
        # format) from the actual key/value body.  New entries must go into
        # the body, never after the trailing blanks.
        trailing: list[str] = []
        while key_lines and not key_lines[-1].strip():
            trailing.insert(0, key_lines.pop())

        def _sorted_insert_pos(entry_line: str) -> int:
            """Return the index where *entry_line* should be inserted to keep
            the section body in alphabetical order (matching winecfg behaviour).
            Skips non-key lines (#time=, blank, etc.)."""
            entry_key = entry_line.split("=", 1)[0].strip().lower()
            for idx, kl in enumerate(key_lines):
                kl_stripped = kl.strip()
                if not kl_stripped or kl_stripped.startswith("#"):
                    continue
                existing_key = kl_stripped.split("=", 1)[0].strip().lower()
                if existing_key > entry_key:
                    return idx
            return len(key_lines)

        changed = False
        for dll, value in overrides.items():
            key_lower = f'"{dll.lower()}"'
            expected_line = f'"{dll}"="{value}"\n'
            found_at: int | None = None
            for j, kline in enumerate(key_lines):
                if kline.lower().startswith(key_lower + "="):
                    found_at = j
                    break
            if found_at is not None:
                if key_lines[found_at] == expected_line:
                    pass  # correct value, correct position - nothing to do
                else:
                    # Value is wrong - update in place (position is already sorted)
                    key_lines[found_at] = expected_line
                    changed = True
                    _log(f"  DLL override updated: {dll} = {value}")
            else:
                # New entry - insert in sorted position (matching winecfg)
                pos = _sorted_insert_pos(expected_line)
                key_lines.insert(pos, expected_line)
                changed = True
                _log(f"  DLL override set: {dll} = {value}")

        if not changed:
            # All overrides already present with the correct values - leave
            # user.reg completely untouched so Wine's own state is preserved.
            _log("  DLL overrides already set correctly; skipping write.")
            return True

        # Re-append trailing blank lines to preserve section terminator.
        key_lines.extend(trailing)

        # Only update the section header / #time= timestamps when we actually
        # make a change, and use the formats Wine expects.
        lines[section_start] = f"{section_header} {_unix_ts}\n"
        for j, kline in enumerate(key_lines):
            if kline.lower().startswith("#time="):
                key_lines[j] = f"#time={_filetime_hex}\n"
                break

        lines[body_start:body_end] = key_lines

    try:
        write_atomic_text(user_reg, "".join(lines))
    except OSError as exc:
        _log(f"Warning: could not write user.reg: {exc}")
        return False
    return True


@guard_user_hive
def set_show_dot_files(prefix_path: Path, log_fn=None) -> bool:
    """Set ``[Software\\\\Wine]`` ``"ShowDotFiles"="Y"`` in the prefix's user.reg.

    This is winecfg's "Show dot files" checkbox: without it Wine hides every
    Unix dotfile/dot-directory, so file dialogs inside the prefix cannot browse
    to ``~/.local/share/...`` or ``~/.config/...`` - which is where the mod
    manager keeps profiles, staged mods and tool data.  Tools that ask the user
    to pick a Data folder are unusable in a prefix without it.

    Written straight into user.reg rather than through ``reg add`` because the
    caller may be on the UI thread: a ``reg add`` costs a full wine startup
    (seconds), while this is a small text edit.  Returns True when the value is
    already correct or was written, False when the prefix couldn't be touched.

    Wine reads user.reg when a wineserver starts and rewrites it on shutdown,
    so this must run while no process is live in the prefix - which is the case
    at env-resolution time, before the tool is launched.
    """
    _log = _safe_log(log_fn)

    # Accept either the pfx/ directory directly or its parent (compatdata/<id>/)
    prefix_path = Path(prefix_path)
    if not (prefix_path / "user.reg").is_file() and (prefix_path / "pfx" / "user.reg").is_file():
        prefix_path = prefix_path / "pfx"
    user_reg = prefix_path / "user.reg"
    if not user_reg.is_file():
        _log(f"Show dot files: user.reg not found at {user_reg}; skipping.")
        return False

    try:
        text = user_reg.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        _log(f"Show dot files: could not read user.reg: {exc}")
        return False

    lines = text.splitlines(keepends=True)
    section_header = "[Software\\\\Wine]"
    entry_line = '"ShowDotFiles"="Y"\n'

    # Locate the section. The header match must be exact up to the closing
    # bracket - a plain startswith() would also match [Software\\Wine\\Explorer]
    # and friends, and the value would land in the wrong key.
    section_start: int | None = None
    section_end: int | None = None  # index of first line after this section
    for i, line in enumerate(lines):
        stripped = line.strip()
        if section_start is None:
            if stripped.lower().startswith(section_header.lower() + " ") \
                    or stripped.lower() == section_header.lower():
                section_start = i
        elif stripped.startswith("["):
            section_end = i
            break

    _unix_ts = int(_time.time())
    _filetime_hex = format(int((_unix_ts + 11644473600) * 1e7), "x")

    if section_start is None:
        if lines and not lines[-1].endswith("\n"):
            lines.append("\n")
        lines.append("\n")
        lines.append(f"{section_header} {_unix_ts}\n")
        lines.append(f"#time={_filetime_hex}\n")
        lines.append(entry_line)
        _log(r"Show dot files: added [Software\\Wine] to user.reg.")
    else:
        body_start = section_start + 1
        body_end = section_end if section_end is not None else len(lines)
        key_lines = lines[body_start:body_end]

        # Trailing blanks terminate the section in Wine's .reg format - a new
        # entry has to go before them, not after.
        trailing: list[str] = []
        while key_lines and not key_lines[-1].strip():
            trailing.insert(0, key_lines.pop())

        found_at: int | None = None
        for j, kline in enumerate(key_lines):
            if kline.strip().lower().startswith('"showdotfiles"='):
                found_at = j
                break

        if found_at is not None and key_lines[found_at] == entry_line:
            # Already correct - leave user.reg completely untouched so Wine's
            # own state (and its mtime) is preserved.
            return True
        if found_at is not None:
            key_lines[found_at] = entry_line
        else:
            key_lines.append(entry_line)

        key_lines.extend(trailing)
        lines[section_start] = f"{section_header} {_unix_ts}\n"
        for j, kline in enumerate(key_lines):
            if kline.lower().startswith("#time="):
                key_lines[j] = f"#time={_filetime_hex}\n"
                break
        lines[body_start:body_end] = key_lines
        _log("Show dot files: enabled in user.reg.")

    try:
        write_atomic_text(user_reg, "".join(lines))
    except OSError as exc:
        _log(f"Show dot files: could not write user.reg: {exc}")
        return False
    return True


@guard_user_hive
def remove_wine_dll_overrides(
    prefix_path: Path,
    dlls: "list[str] | set[str]",
    log_fn=None,
) -> bool:
    """Remove Wine DLL override entries from the Proton prefix's user.reg.

    *dlls* is a collection of DLL names whose ``[Software\\\\Wine\\\\DllOverrides]``
    entries should be deleted.  Entries not present in the file are silently
    skipped.  The file is written atomically.
    """
    _log = _safe_log(log_fn)

    if not dlls:
        return True

    dlls_lower = {d.lower() for d in dlls}

    # Accept either the pfx/ directory directly or its parent
    if not (prefix_path / "user.reg").is_file() and (prefix_path / "pfx" / "user.reg").is_file():
        prefix_path = prefix_path / "pfx"
    user_reg = prefix_path / "user.reg"
    if not user_reg.is_file():
        _log(f"Warning: user.reg not found at {user_reg}; skipping DLL override removal.")
        return False

    try:
        text = user_reg.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        _log(f"Warning: could not read user.reg: {exc}")
        return False

    lines = text.splitlines(keepends=True)
    section_header = "[Software\\\\Wine\\\\DllOverrides]"

    section_start: int | None = None
    section_end: int | None = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.lower().startswith(section_header.lower()):
            section_start = i
        elif section_start is not None and stripped.startswith("["):
            section_end = i
            break

    if section_start is None:
        return True

    body_start = section_start + 1
    body_end = section_end if section_end is not None else len(lines)
    key_lines = lines[body_start:body_end]

    removed_count = 0
    new_key_lines = []
    for kline in key_lines:
        stripped = kline.strip()
        if stripped.startswith('"'):
            # Extract the key name between the first pair of quotes
            end_quote = stripped.find('"', 1)
            if end_quote > 1:
                key_name = stripped[1:end_quote].lower()
                if key_name in dlls_lower:
                    _log(f"  DLL override removed: {stripped[1:end_quote]}")
                    removed_count += 1
                    continue  # drop this line
        new_key_lines.append(kline)

    if removed_count == 0:
        return True

    # Fix up the section header and #time= timestamps to use the formats
    # Wine expects: decimal Unix seconds for the header, hex Windows FILETIME
    # for the #time= line.
    _unix_ts = int(_time.time())
    _filetime_hex = format(int((_unix_ts + 11644473600) * 1e7), "x")
    lines[section_start] = f"{section_header} {_unix_ts}\n"
    for j, kline in enumerate(new_key_lines):
        if kline.lower().startswith("#time="):
            new_key_lines[j] = f"#time={_filetime_hex}\n"
            break

    lines[body_start:body_end] = new_key_lines

    try:
        write_atomic_text(user_reg, "".join(lines))
    except OSError as exc:
        _log(f"Warning: could not write user.reg: {exc}")
        return False
    return True


__all__ = [
    "clean_game_folder",
    "remove_deployed_files",
    "apply_wine_dll_overrides",
    "remove_wine_dll_overrides",
    "set_show_dot_files",
]
