"""Toolkit-neutral logic for the Data tab (Tk + Qt share this).

The Data tab shows the MERGED deployment layout: every file from filemap.txt as a
folder tree, with the winning mod per file and conflict highlighting - "what
actually lands in the game folder". The intricate bit is resolving each filemap
entry to its real deploy destination (UE5 rule resolution + custom routing rules
with include_siblings / flatten / prefix+root hiding). That logic is lifted almost
verbatim from the Tk ModFiles… er, Data mixin (gui/plugin_panel_data.py) so the Qt
Data tab stays in lockstep. Pure stdlib + Utils.*/Games.* - no GUI toolkit.

Conflict data (contested keys + filemap winner) is provided by
Utils.mod_files.build_conflict_cache - reused, not duplicated here.
"""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path


def deploys_to_subfolder(game) -> bool:
    """True when mods deploy into a SUBFOLDER of the game root (Skyrim's Data/,
    Morrowind's Data Files/) - the Data tab shows that subfolder, so files that
    deploy to the game root fall outside its scope and must be hidden. False
    for root-deployed games (deploy dir == game root, e.g. Witcher 3), where
    root-bound files land inside the shown tree. Falls back to the mods_dir
    property when the paths aren't configured."""
    try:
        gp = game.get_game_path()
        dp = game.get_mod_data_path()
    except Exception:
        gp = dp = None
    if gp is not None and dp is not None:
        return Path(dp) != Path(gp)
    return bool((getattr(game, "mods_dir", None) or "").strip("/ "))


# ---------------------------------------------------------------------------
# Front half: parse filemap.txt + drop hidden mods, then resolve destinations
# ---------------------------------------------------------------------------
def parse_filemap(filemap_path: Path) -> list[tuple[str, str]]:
    """Parse filemap.txt → [(rel_path, mod_name)] (rel_path raw, tab-separated)."""
    entries: list[tuple[str, str]] = []
    try:
        # surrogateescape: filemap.txt rel paths derive from on-disk filenames
        # whose non-UTF-8 bytes decode to surrogate code points - a plain utf-8
        # read raises on them.
        with filemap_path.open(encoding="utf-8", errors="surrogateescape") as f:
            for line in f:
                line = line.rstrip("\n")
                if "\t" not in line:
                    continue
                rel_path, mod_name = line.split("\t", 1)
                entries.append((rel_path, mod_name))
    except OSError:
        return []
    return entries


def load_data_entries(game, filemap_path: Path,
                      profile_dir: Path) -> list[tuple[str, str]]:
    """Return the RESOLVED [(rel_path, mod_name)] for the Data tree.

    Parses filemap.txt, drops mods that deploy to a custom separator location
    (they land elsewhere) and game-specific orphaned entries, then resolves each
    entry to its real deploy destination. Mirrors the front half of Tk
    _refresh_data_tab (plugin_panel_data.py:438-516)."""
    if not filemap_path.is_file():
        return []
    raw_entries = parse_filemap(filemap_path)

    # Drop mods belonging to a separator with a custom deploy location.
    custom_deploy_mods: set[str] = set()
    modlist_path = profile_dir / "modlist.txt"
    if modlist_path.is_file():
        try:
            from Utils.modlist import read_modlist
            from Utils.deploy import (
                load_separator_deploy_paths, expand_separator_deploy_paths)
            sep_paths = load_separator_deploy_paths(profile_dir)
            if sep_paths:
                ml = read_modlist(modlist_path)
                custom_deploy_mods = set(
                    expand_separator_deploy_paths(sep_paths, ml).keys())
        except Exception:
            custom_deploy_mods = set()
    if custom_deploy_mods:
        raw_entries = [(p, m) for p, m in raw_entries
                       if m not in custom_deploy_mods]

    # Game-specific hidden entries (e.g. Stardew orphaned overwrite config.json).
    hide_fn = getattr(game, "_orphaned_overwrite_configs", None)
    if callable(hide_fn):
        try:
            hidden = hide_fn(filemap_path)
        except Exception:
            hidden = set()
        if hidden:
            raw_entries = [(p, m) for p, m in raw_entries
                           if p.lower() not in hidden]

    resolved = resolve_data_entries(game, raw_entries, profile_dir)

    # Root-flagged mods (meta rootFolder=true) live in filemap_root.txt and
    # deploy VERBATIM to the game root. For a root-deployed game that root IS
    # the tree the Data tab shows, so merge them all in. For subfolder-deploy
    # games (Skyrim's Data/, Morrowind's Data Files/) only the entries UNDER
    # the deploy subfolder land inside the shown tree - keep those with the
    # prefix stripped ('Data Files/foo.esp' → 'foo.esp'); the rest deploy to
    # the game root and stay hidden, same as root-routed rule files. Merged
    # entries win over same-path normal entries (root deploy runs after the
    # main deploy and backs the existing file up).
    root_entries = parse_filemap(filemap_path.parent / "filemap_root.txt")
    if root_entries and deploys_to_subfolder(game):
        try:
            from Utils.game_helpers import game_data_subpath
            prefix = game_data_subpath(game).lower()
        except Exception:
            prefix = ""
        kept: list[tuple[str, str]] = []
        if prefix:
            plen = len(prefix) + 1
            for p, m in root_entries:
                norm = p.replace("\\", "/")
                if norm.lower().startswith(prefix + "/") and len(norm) > plen:
                    kept.append((norm[plen:], m))
        root_entries = kept
    if root_entries:
        shadowed = {p.replace("\\", "/").lower() for p, _m in root_entries}
        resolved = [(p, m) for p, m in resolved
                    if p.replace("\\", "/").lower() not in shadowed]
        resolved.extend(root_entries)
    return resolved


# ---------------------------------------------------------------------------
# Deploy-destination resolution (UE5 + custom routing rules) - verbatim port of
# Tk _resolve_data_entries (plugin_panel_data.py:518-805).
# ---------------------------------------------------------------------------
def resolve_data_entries(game, entries: list[tuple[str, str]],
                         profile_dir: Path) -> list[tuple[str, str]]:
    """Prefix each entry's path with its resolved deploy destination so the Data
    tab shows where files will actually land in the game.

    UE5 games use their own _resolve_filemap_entries. Other games with
    custom_routing_rules use the same folder-match logic as deploy_custom_rules
    (first matching rule wins, full path preserved under dest)."""
    from Games.ue5_game import UE5Game
    if isinstance(game, UE5Game):
        # Build priority map so flatten collisions show only the winner.
        priority_map: dict[str, int] = {}
        modlist_path = profile_dir / "modlist.txt"
        if modlist_path.is_file():
            try:
                from Utils.modlist import read_modlist
                for rank, e in enumerate(read_modlist(modlist_path)):
                    priority_map[e.name] = rank
            except Exception:
                pass
        prefix_skip_dest = getattr(game, "_PREFIX_SKIP_DEST", None)
        winners: dict[str, tuple[int, str, str]] = {}
        for rel_path, mod_name, dest, final_rel in game._resolve_filemap_entries(
                list(entries)):
            if prefix_skip_dest is not None and dest == prefix_skip_dest:
                continue
            full_path = dest + "/" + final_rel if dest else final_rel
            rank = priority_map.get(mod_name, 1 << 30)
            existing = winners.get(full_path)
            if existing is None or rank < existing[0]:
                winners[full_path] = (rank, full_path, mod_name)
        return [(p, m) for _r, p, m in winners.values()]

    rules = getattr(game, "custom_routing_rules", None)
    if not rules:
        return entries

    # Pre-process rules (mirrors deploy_custom_rules). Extensions sorted
    # longest-first so multi-dot extensions win over their plain suffix.
    _rules = [
        (r,
         {f.lower() for f in r.folders},
         sorted({e.lower() for e in r.extensions}, key=len, reverse=True),
         {n.lower() for n in r.filenames})
        for r in rules
    ]

    def _ext_match(filename: str, exts: list[str]) -> str | None:
        for e in exts:
            if filename.endswith(e) and len(filename) > len(e):
                return e
        return None

    def _name_match(filename: str, names: set[str]) -> bool:
        for n in names:
            if any(c in n for c in "*?["):
                if fnmatch.fnmatchcase(filename, n):
                    return True
            elif filename == n:
                return True
        return False

    def _match_one(rel_lower, rule, folders, exts, filenames):
        parts = rel_lower.split("/")
        filename = parts[-1]
        if rule.exclude_extensions:
            for e in rule.exclude_extensions:
                if filename.endswith(e.lower()):
                    return None
        is_loose = len(parts) == 1
        strip_len = -1
        folder_hit = False
        if folders:
            for f in folders:
                if "/" in f:
                    idx = rel_lower.find(f + "/")
                    if idx < 0 and rel_lower.endswith(f):
                        idx = len(rel_lower) - len(f)
                    if idx >= 0 and (idx == 0 or rel_lower[idx - 1] == "/"):
                        strip_len = idx
                        folder_hit = True
                        break
                else:
                    for pi, seg in enumerate(parts[:-1]):
                        if seg == f:
                            strip_len = sum(len(parts[j]) + 1 for j in range(pi))
                            folder_hit = True
                            break
                    if folder_hit:
                        break
            if folder_hit and rule.loose_only and strip_len != 0:
                return None
        matched_ext = _ext_match(filename, exts) if exts else None
        if folder_hit and (not exts or matched_ext is not None):
            return strip_len, matched_ext or ""
        if rule.loose_only and not is_loose:
            return None
        if matched_ext is not None and not folders and not filenames:
            return -1, matched_ext
        if filenames and _name_match(filename, filenames):
            return -1, ""
        return None

    primary_rules: dict[int, tuple] = {}
    entries_by_parent: dict[str, list[tuple[int, str]]] = {}
    normalised: list[str] = []
    for idx, (rel_path, _mod_name) in enumerate(entries):
        rel_norm = rel_path.replace("\\", "/")
        normalised.append(rel_norm)
        rel_lower = rel_norm.lower()
        parent_lower, _, _name_lower = rel_lower.rpartition("/")
        entries_by_parent.setdefault(parent_lower, []).append((idx, _name_lower))

    sibling_overrides: dict[int, str] = {}
    prefix_hidden: set[int] = set()
    _subfolder_deploy = deploys_to_subfolder(game)
    root_hidden: set[int] = set()

    def _routes_to_root(rule) -> bool:
        return _subfolder_deploy and not getattr(rule, "to_prefix", False) and not rule.dest

    from Utils.deploy_custom_rules import (
        _sibling_container, canonicalize_declared_folders,
    )
    claimed: set[int] = set()
    for rule, folders, exts, filenames in _rules:
        new_primary_idxs: list[int] = []
        for idx, (rel_path, mod_name) in enumerate(entries):
            if idx in claimed:
                continue
            rel_lower = normalised[idx].lower()
            hit = _match_one(rel_lower, rule, folders, exts, filenames)
            if hit is None:
                continue
            strip_len, matched_ext = hit
            primary_rules[idx] = (rule, strip_len, matched_ext)
            claimed.add(idx)
            new_primary_idxs.append(idx)
            if getattr(rule, "to_prefix", False):
                prefix_hidden.add(idx)
            elif _routes_to_root(rule):
                root_hidden.add(idx)
        if not getattr(rule, "include_siblings", False) or not new_primary_idxs:
            continue
        drags: list[tuple[str, str, str, bool]] = []
        for pidx in new_primary_idxs:
            _r, sl, _me = primary_rules[pidx]
            rn = normalised[pidx]; pmod = entries[pidx][1]
            info = _sibling_container(rn, sl, pmod)
            if info is None:
                continue
            cont, cname = info
            is_whole = cont == ""
            drags.append((cont.lower(), cname, pmod, is_whole))
            tail = rn if is_whole else rn[len(cont) + 1:]
            sibling_overrides[pidx] = (cname + "/" + tail) if cname else tail
        drags.sort(key=lambda t: (0 if t[3] else 1, -len(t[0])))
        seen_drags: set[tuple[str, str]] = set()
        for cont_lower, cname, pmod, is_whole in drags:
            key = (cont_lower, pmod)
            if key in seen_drags:
                continue
            seen_drags.add(key)
            prefix_lower = cont_lower + "/" if cont_lower else ""
            for sib_idx, (rel_path, sib_mod) in enumerate(entries):
                if sib_idx in claimed:
                    continue
                if sib_mod != pmod:
                    continue
                sn = normalised[sib_idx]; slow = sn.lower()
                if is_whole:
                    ric = sn
                else:
                    if not slow.startswith(prefix_lower):
                        continue
                    ric = sn[len(cont_lower) + 1:]
                sibling_overrides[sib_idx] = (cname + "/" + ric) if cname else ric
                primary_rules[sib_idx] = (rule, -2, "")
                claimed.add(sib_idx)
                if getattr(rule, "to_prefix", False):
                    prefix_hidden.add(sib_idx)
                elif _routes_to_root(rule):
                    root_hidden.add(sib_idx)

    # Second pass: mark companions (same folder, same stem, companion ext).
    for idx, (rule, strip_len, matched_ext) in list(primary_rules.items()):
        companions = sorted(
            {c.lower() for c in getattr(rule, "companion_extensions", [])},
            key=len, reverse=True,
        )
        if not companions:
            continue
        rel_norm = normalised[idx]
        rel_lower = rel_norm.lower()
        parent_lower, _, name_lower = rel_lower.rpartition("/")
        if matched_ext and name_lower.endswith(matched_ext):
            stem_lower = name_lower[: -len(matched_ext)]
        else:
            stem_lower, _ = os.path.splitext(name_lower)
        stem_dot = stem_lower + "."
        for sib_idx, sib_name_lower in entries_by_parent.get(parent_lower, ()):
            if sib_idx == idx:
                continue
            if sib_idx in primary_rules:
                continue
            if not sib_name_lower.startswith(stem_dot):
                continue
            for c in companions:
                if sib_name_lower.endswith(c) and len(sib_name_lower) > len(c):
                    primary_rules[sib_idx] = (rule, strip_len, c)
                    if getattr(rule, "to_prefix", False):
                        prefix_hidden.add(sib_idx)
                    elif _routes_to_root(rule):
                        root_hidden.add(sib_idx)
                    break

    resolved = []
    for idx, (rel_path, mod_name) in enumerate(entries):
        if idx in prefix_hidden or idx in root_hidden:
            continue
        rel_norm = normalised[idx]
        match = primary_rules.get(idx)
        if match is not None:
            rule, strip_len, _matched_ext = match
            dest = rule.dest
            override = sibling_overrides.get(idx)
            if override is not None:
                tail = override
            elif rule.flatten:
                if strip_len >= 0:
                    tail = rel_norm[strip_len:].lstrip("/")
                else:
                    tail = rel_norm.split("/")[-1]
            else:
                tail = rel_norm
            # Same canonicalisation the deploy applies, so the tree shows the
            # folder the files actually land in.
            tail = canonicalize_declared_folders(tail, tuple(rule.folders))
            full_path = dest + "/" + tail if dest else tail
            _mods_dir = getattr(game, "mods_dir", None)
            if _mods_dir:
                _prefix = _mods_dir.rstrip("/") + "/"
                if full_path.lower().startswith(_prefix.lower()):
                    full_path = full_path[len(_prefix):]
        else:
            full_path = rel_norm
        resolved.append((full_path, mod_name))
    return resolved


# ---------------------------------------------------------------------------
# Tree dict (with the file-type / only-conflicts filter applied)
# ---------------------------------------------------------------------------
def build_data_tree(entries: list[tuple[str, str]],
                    contested_keys: set[str] | None = None, *,
                    only_conflicts: bool = False,
                    inc_exts: frozenset | None = None,
                    exc_exts: frozenset | None = None,
                    keep_extra=None) -> dict:
    """Build the nested tree dict from resolved [(rel_path, mod_name)] entries.

    Folders are sub-dicts; files live in a "__files__" list of
    (fname, mod_name, rel_key_lower). Mirrors Tk _build_data_tree_from_entries
    (plugin_panel_data.py:879-903). only_conflicts / inc_exts / exc_exts apply the
    filter side panel; keep_extra(rel_key_lower, mod) is an optional extra
    predicate (used for the search box)."""
    contested_keys = contested_keys or set()
    inc_exts = inc_exts or frozenset()
    exc_exts = exc_exts or frozenset()
    tree: dict = {}
    for rel_path, mod_name in entries:
        rel_norm = rel_path.replace("\\", "/")
        rel_key_lower = rel_norm.lower()
        if only_conflicts and rel_key_lower not in contested_keys:
            continue
        if inc_exts or exc_exts:
            dot = rel_key_lower.rfind(".")
            slash = rel_key_lower.rfind("/")
            if dot <= slash:
                if inc_exts:
                    continue
            else:
                ext = rel_key_lower[dot:]
                if inc_exts and ext not in inc_exts:
                    continue
                if exc_exts and ext in exc_exts:
                    continue
        if keep_extra is not None and not keep_extra(rel_key_lower, mod_name):
            continue
        parts = rel_norm.split("/")
        node = tree
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node.setdefault("__files__", []).append(
            (parts[-1], mod_name, rel_key_lower))
    return tree


def filetype_counts(entries: list[tuple[str, str]]) -> dict[str, int]:
    """Map extension (lower, with dot) → file count across resolved entries."""
    counts: dict[str, int] = {}
    for rel_path, _mod in entries:
        rl = rel_path.replace("\\", "/").lower()
        dot = rl.rfind(".")
        slash = rl.rfind("/")
        if dot > slash:
            ext = rl[dot:]
            counts[ext] = counts.get(ext, 0) + 1
    return counts
