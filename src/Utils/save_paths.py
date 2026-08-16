"""Resolve a game's on-disk save folders from the bundled Ludusavi data.

The manifest stores Windows-shaped paths with placeholder tokens
(``<winDocuments>/My Games/...``); on Linux those resolve either inside the
game's Wine prefix or against the real XDG dirs. Toolkit-neutral.
"""

from __future__ import annotations

import fnmatch
import glob
import os
from dataclasses import dataclass, replace
from pathlib import Path

from Utils.ludusavi_manifest import lookup


@dataclass(frozen=True)
class SaveLocation:
    """One save file or folder that exists on disk."""
    path: Path
    token_path: str      # original manifest path, tokens intact
    in_prefix: bool      # resolved inside a Wine prefix rather than natively
    store: str           # store constraint from the manifest ("" = any)
    exists: bool = True  # False = expected: parent folder exists, this doesn't
                         # yet (the game hasn't written its first save there)
    # Name globs claiming this folder's saves ("*.sav"), from a manifest path
    # like "<home>/Saved Games/.../Quake/*.sav". Empty = the whole folder is
    # saves. Anything acting on the folder (listing, export, the import backup)
    # must respect these -the folder can hold files that are not the game's.
    patterns: tuple[str, ...] = ()

    @property
    def is_dir(self) -> bool:
        return self.path.is_dir()


_GLOB_CHARS = "*?["


def _has_glob(text: str) -> bool:
    return any(ch in text for ch in _GLOB_CHARS)


def matches_patterns(name: str, patterns) -> bool:
    """Whether a directory entry is one the location's patterns claim.

    No patterns means the whole folder counts. Matching is case-insensitive:
    a Wine prefix spells names however the game wrote them.
    """
    if not patterns:
        return True
    low = name.lower()
    return any(fnmatch.fnmatchcase(low, p.lower()) for p in patterns)


# Tokens that land inside the Wine prefix. <base>/<root>/<xdg*> resolve
# through the prefix table too but point outside it, and would otherwise be
# mislabelled "[in prefix]".
_PREFIX_TOKENS = (
    "<home>", "<winDocuments>", "<winAppData>", "<winLocalAppData>",
    "<winLocalAppDataLow>", "<winPublic>", "<winProgramData>", "<winDir>",
    "<osUserName>",
)


def _drive_c(prefix_path: Path) -> "Path | None":
    """Return drive_c for a prefix given either its pfx/ dir or its parent."""
    for candidate in (prefix_path / "drive_c", prefix_path / "pfx" / "drive_c"):
        if candidate.is_dir():
            return candidate
    return None


def _prefix_users(drive_c: Path) -> list[Path]:
    """Wine user folders to try, steamuser first (handlers assume it)."""
    users = drive_c / "users"
    out: list[Path] = []
    for name in ("steamuser", os.environ.get("USER", "")):
        if not name:
            continue
        candidate = users / name
        if candidate.is_dir() and candidate not in out:
            out.append(candidate)
    return out


def _steam_roots() -> list[Path]:
    """Steam install roots (the dirs holding userdata/ and steamapps/)."""
    try:
        from Utils.steam_finder import find_steam_libraries
    except Exception:
        return []
    roots: list[Path] = []
    seen: set[str] = set()
    for common in find_steam_libraries():
        root = common.parent.parent          # <root>/steamapps/common → <root>
        key = str(root.resolve())
        if key not in seen and root.is_dir():
            seen.add(key)
            roots.append(root)
    return roots


#: SteamID64 of account 0 -the offset between the two ID forms.
_STEAM_ID64_BASE = 76561197960265728


def _store_user_ids(roots: list[Path]) -> list[str]:
    """Steam user IDs from userdata/ in both forms -the folders use the
    32-bit account ID, but a game may name its own saves with the SteamID64
    (Slay the Spire 2 does)."""
    ids: list[str] = []
    for root in roots:
        try:
            entries = [e.name for e in (root / "userdata").iterdir() if e.is_dir()]
        except OSError:
            continue
        for name in entries:
            if name == "0":
                continue
            forms = [name]
            if name.isdigit():
                account = int(name)
                forms.append(str(account + _STEAM_ID64_BASE) if account < _STEAM_ID64_BASE
                             else str(account - _STEAM_ID64_BASE))
            for form in forms:
                if form not in ids:
                    ids.append(form)
    return ids


def _ci_resolve(path: Path) -> "Path | None":
    """Resolve *path* case-insensitively, component by component, or None."""
    if not path.is_absolute():
        return None
    current = Path(path.anchor)
    for part in path.relative_to(current).parts:
        direct = current / part
        if direct.exists():
            current = direct
            continue
        try:
            matches = [c for c in current.iterdir() if c.name.lower() == part.lower()]
        except OSError:
            return None
        if len(matches) != 1:
            return None
        current = matches[0]
    return current


def _expand(token_path: str, table: dict[str, list[str]]) -> list[str]:
    """Substitute tokens in *token_path*, fanning out multi-valued ones."""
    out = [token_path]
    for token, values in table.items():
        if not any(token in candidate for candidate in out):
            continue
        if not values:
            return []
        out = [c.replace(token, v) for c in out for v in values]
    # Any token we don't know how to expand makes the path unusable.
    return [c for c in out if "<" not in c]


def _win_safe(native_table: dict[str, list[str]]) -> dict[str, list[str]]:
    """Native table minus the tokens that mean a Windows user folder."""
    return {k: v for k, v in native_table.items() if k not in ("<home>", "<osUserName>")}


def _token_tables(game_path: "Path | None", prefix_path: "Path | None") -> tuple[dict, dict]:
    """Build the (prefix, native) token substitution tables."""
    roots = [str(r) for r in _steam_roots()]
    user_ids = _store_user_ids([Path(r) for r in roots])
    base = [str(game_path)] if game_path else []

    native = {
        "<base>": base,
        "<home>": [os.path.expanduser("~")],
        "<xdgData>": [os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")],
        "<xdgConfig>": [os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")],
        "<root>": roots,
        "<storeUserId>": user_ids,
        "<osUserName>": [os.environ.get("USER", "")],
    }

    prefix: dict[str, list[str]] = {}
    drive_c = _drive_c(prefix_path) if prefix_path else None
    if drive_c is not None:
        users = _prefix_users(drive_c)
        if users:
            prefix = {
                "<base>": base,
                "<home>": [str(u) for u in users],
                "<winDocuments>": [str(u / "Documents") for u in users],
                "<winAppData>": [str(u / "AppData" / "Roaming") for u in users],
                "<winLocalAppData>": [str(u / "AppData" / "Local") for u in users],
                "<winLocalAppDataLow>": [str(u / "AppData" / "LocalLow") for u in users],
                "<winPublic>": [str(drive_c / "users" / "Public")],
                "<winProgramData>": [str(drive_c / "ProgramData")],
                "<winDir>": [str(drive_c / "windows")],
                "<root>": roots,
                "<storeUserId>": user_ids,
                "<osUserName>": [users[0].name],
            }
    return prefix, native


def resolve_save_paths(
    *,
    steam_id: str = "",
    game_name: str = "",
    game_path: "Path | None" = None,
    prefix_path: "Path | None" = None,
    store: str = "",
    existing_only: bool = True,
    extra_paths: "list[tuple[str, str, str]] | None" = None,
) -> list[SaveLocation]:
    """Return the game's save locations, disk-verified unless told otherwise."""
    entry = lookup(steam_id, game_name)
    # A handler's extra paths stand on their own: DFU has no Steam id and the
    # manifest knows only its Windows folder, so requiring a manifest hit here
    # would throw away the only location that exists on Linux.
    manifest_paths = list(entry.paths) if entry is not None else []
    all_paths = manifest_paths + list(extra_paths or [])
    if not all_paths:
        return []

    prefix_table, native_table = _token_tables(game_path, prefix_path)
    results: list[SaveLocation] = []
    index: dict[str, int] = {}

    def add(location: SaveLocation) -> None:
        """Append, or fold into the location already found at that path."""
        key = os.path.realpath(location.path)
        seat = index.get(key)
        if seat is None:
            index[key] = len(results)
            results.append(location)
            return
        # Two manifest entries can name one folder with different globs
        # ("Saves/*.ess" and "Saves/*.skse"); keeping only the first would hide
        # the rest of the saves. One of them claiming the whole folder wins.
        old = results[seat]
        merged = () if not (old.patterns and location.patterns) \
            else tuple(dict.fromkeys(old.patterns + location.patterns))
        if merged != old.patterns:
            results[seat] = replace(old, patterns=merged)

    for token_path, os_name, store_constraint in all_paths:
        if store and store_constraint and store_constraint != store:
            continue
        tables: list[tuple[dict, bool]] = []
        if os_name in ("", "windows") and prefix_table:
            tables.append((prefix_table, True))
        if os_name == "windows":
            # A Windows-tagged path can still be native (<base>/Saves is the
            # game folder either way); _expand drops it if it needs a prefix.
            tables.append((_win_safe(native_table), False))
        elif os_name in ("", "linux"):
            tables.append((native_table, False))

        for table, used_prefix_table in tables:
            in_prefix = used_prefix_table and token_path.startswith(_PREFIX_TOKENS)
            for candidate in _expand(token_path, table):
                for resolved, exists, patterns in _candidate_locations(
                        candidate, existing_only):
                    add(SaveLocation(
                        path=Path(resolved),
                        token_path=token_path,
                        in_prefix=in_prefix,
                        store=store_constraint,
                        exists=exists,
                        patterns=patterns,
                    ))
    return results


def _candidate_locations(candidate: str,
                         existing_only: bool) -> list[tuple[str, bool, tuple[str, ...]]]:
    """(path, exists, patterns) for one fully-expanded candidate path."""
    head, _sep, leaf = candidate.rpartition("/")
    if head and _has_glob(leaf):
        return _leaf_glob_locations(candidate, head, leaf, existing_only)

    hits = _matches(candidate, existing_only)
    if hits:
        return [(hit, os.path.exists(hit), ()) for hit in hits]
    # "Expected": absent, but its immediate parent exists -the game made its
    # vendor dirs and will save here on first write (Subnautica on Epic).
    # Listed so the tab points at the right place from day one.
    if (existing_only and not _has_glob(candidate)
            and os.path.isdir(os.path.dirname(candidate))):
        return [(candidate, False, ())]
    return []


def _leaf_glob_locations(candidate: str, head: str, leaf: str,
                         existing_only: bool) -> list[tuple[str, bool, tuple[str, ...]]]:
    """Locations for a candidate whose LAST component is a glob.

    The save folder is the parent; the glob only says which of its entries are
    saves (``.../Nightdive Studios/Quake/*.sav``). Returning one location per
    matched file -the first cut -made every save its own unreadable "folder",
    showed nothing at all until the first save was written, and left Export /
    Import pointed at a file instead of a folder.
    """
    hits = sorted(glob.glob(candidate))
    if hits and all(os.path.isdir(hit) for hit in hits):
        # A folder-shaped glob ("<base>/save*"): the matches ARE the locations,
        # and their parent is usually the game folder -never claim that.
        return [(hit, True, ()) for hit in hits]

    patterns = (leaf,)
    if _has_glob(head):
        return [(folder, True, patterns)
                for folder in sorted(glob.glob(head)) if os.path.isdir(folder)]

    folder = _ci_resolve(Path(head))
    if folder is not None and folder.is_dir():
        return [(str(folder), True, patterns)]
    if not existing_only or os.path.isdir(os.path.dirname(head)):
        return [(head, False, patterns)]
    return []


def _matches(candidate: str, existing_only: bool) -> list[str]:
    """Expand wildcards / fix casing, returning the paths that exist."""
    if _has_glob(candidate):
        hits = sorted(glob.glob(candidate))
        return hits if (hits or existing_only) else [candidate]
    if os.path.exists(candidate):
        return [candidate]
    fixed = _ci_resolve(Path(candidate))
    if fixed is not None and fixed.exists():
        return [str(fixed)]
    return [] if existing_only else [candidate]


def save_path_override_for_game(game) -> "SaveLocation | None":
    """Return the user's manual save folder as a location, or None if unset.

    Always returned regardless of *existing_only*: the user typed this path, so
    it belongs in the tab even before the game has written its first save -
    seeing it there is how they know the setting took.
    """
    try:
        override = game.get_save_path_override()
    except Exception:
        override = None
    if not override:
        return None
    path = Path(override)
    return SaveLocation(path=path, token_path=str(path), in_prefix=False,
                        store="", exists=path.exists())


def save_paths_for_game(game, existing_only: bool = True) -> list[SaveLocation]:
    """Resolve save locations for a loaded game handler."""
    try:
        steam_id = game.effective_steam_id() or game.steam_id
    except Exception:
        steam_id = getattr(game, "steam_id", "") or ""
    try:
        game_path = game.get_game_path()
    except Exception:
        game_path = None
    try:
        prefix_path = game.get_prefix_path()
    except Exception:
        prefix_path = None
    try:
        extra_paths = game.extra_save_paths()
    except Exception:
        extra_paths = None
    found = resolve_save_paths(
        steam_id=str(steam_id or ""),
        game_name=getattr(game, "name", "") or "",
        game_path=game_path,
        prefix_path=prefix_path,
        existing_only=existing_only,
        extra_paths=extra_paths,
    )
    override = save_path_override_for_game(game)
    if override is None:
        return found
    # The override leads -it is the answer the user gave when the manifest got
    # it wrong -and drops any manifest hit that resolved to the same folder.
    key = os.path.realpath(override.path)
    return [override] + [loc for loc in found
                         if os.path.realpath(loc.path) != key]
