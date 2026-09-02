"""
Utils.launchers.steam_shortcuts
Reader for Steam's non-Steam game shortcuts (userdata/<id>/config/shortcuts.vdf).

Shortcuts are stored in Valve's *binary* VDF, not the text VDF that
libraryfolders.vdf uses, so none of the Steam module's regex parsers apply. The
encoding is a type byte, a NUL-terminated key, then the value:

    0x00  nested map (recurse)      0x02  int32, little-endian
    0x01  string (NUL-terminated)   0x07  uint64
    0x08  end of map

The root map is "shortcuts" -> entries keyed "0", "1", ...

The stored "appid" is a *signed* int32; its unsigned form is the app ID Steam
uses everywhere else - steamapps/compatdata/<appid>, config.vdf's
CompatToolMapping, and grid art filenames - so find_prefix() and
find_proton_for_game() in ``Utils.launchers.steam`` works on it unchanged.

No UI, no game-specific knowledge.
"""

from __future__ import annotations

import struct
import threading
import zlib
from pathlib import Path
from typing import NamedTuple

from Utils.launchers.lutris import (
    _game_root_from_exe,
    _split_exe_rel_parts,
    _stored_exe_matches,
)
from Utils.launchers.steam import _STEAM_CANDIDATES, _stat_sig, find_prefix

# Binary-VDF value type bytes.
_T_MAP = 0x00
_T_STR = 0x01
_T_INT32 = 0x02
_T_INT64 = 0x07
_T_END = 0x08

# Guard against a corrupt file whose bytes happen to nest forever. Real
# shortcuts.vdf files are two levels deep (shortcuts -> entry), three with the
# per-entry "tags" map.
_MAX_DEPTH = 8


class Shortcut(NamedTuple):
    """One non-Steam shortcut entry."""
    appid: int              # unsigned 32-bit: keys compatdata + CompatToolMapping
    name: str
    exe: str                # unquoted path to the target executable
    start_dir: str
    launch_options: str
    icon: str
    tags: tuple[str, ...]
    user_id: str            # the userdata/<user_id> this was read from


def run_gameid(appid) -> str:
    """The 64-bit id that ``steam://rungameid/<id>`` needs for a shortcut.

    Shortcuts have no appmanifest, so Steam addresses them by a composite id
    rather than the bare app ID a store game uses.
    """
    try:
        value = int(str(appid).strip())
    except (TypeError, ValueError):
        return ""
    return str(((value & 0xFFFFFFFF) << 32) | 0x02000000)


# ---------------------------------------------------------------------------
# Binary VDF parsing
# ---------------------------------------------------------------------------

def _read_cstr(buf: bytes, i: int) -> "tuple[str, int]":
    """Read a NUL-terminated string at *i*; returns (text, next index)."""
    end = buf.index(b"\x00", i)
    return buf[i:end].decode("utf-8", "replace"), end + 1


def _parse_map(buf: bytes, i: int, depth: int = 0) -> "tuple[dict, int]":
    """Parse one binary-VDF map starting at *i*; returns (map, next index).

    Keys are lowercased: Steam's own casing varies between client versions and
    third-party writers (``openvr`` in one file on the same machine, ``OpenVR``
    in another), so every lookup here is case-insensitive by construction.
    """
    if depth > _MAX_DEPTH:
        raise ValueError("shortcut map nested too deeply")
    out: dict = {}
    while i < len(buf):
        type_byte = buf[i]
        i += 1
        if type_byte == _T_END:
            return out, i
        key, i = _read_cstr(buf, i)
        if type_byte == _T_MAP:
            value, i = _parse_map(buf, i, depth + 1)
        elif type_byte == _T_STR:
            value, i = _read_cstr(buf, i)
        elif type_byte == _T_INT32:
            value = struct.unpack_from("<i", buf, i)[0]
            i += 4
        elif type_byte == _T_INT64:
            value = struct.unpack_from("<Q", buf, i)[0]
            i += 8
        else:
            raise ValueError(
                f"unknown value type 0x{type_byte:02x} at offset {i - 1}")
        out[key.lower()] = value
    return out, i


def _unquote(value) -> str:
    """The path out of a shortcut's Exe / StartDir field.

    Steam stores these quoted (``"/path/to/game.exe"``), but not always: files
    written by third-party tools (Bottles, NonSteamLaunchers) leave bare
    values. Anything after the closing quote is a trailing argument and is
    dropped; a bare value is taken whole, since a path containing spaces can't
    be told apart from a path plus arguments.
    """
    text = str(value or "").strip()
    if text.startswith('"'):
        end = text.find('"', 1)
        return text[1:end] if end > 0 else text[1:]
    return text


def _entry_appid(entry: dict) -> int:
    """The unsigned 32-bit app ID for a shortcut entry, or 0.

    Steam stores it signed under ``appid``. Older clients and some third-party
    tools omit the key entirely (every entry of a Bottles-written file), where
    the only recourse is the legacy crc32 derivation Steam used before it
    started assigning IDs randomly. That derivation is never used to check a
    stored ID - modern IDs do not match it.
    """
    raw = entry.get("appid")
    if isinstance(raw, int) and raw:
        return raw & 0xFFFFFFFF
    exe = str(entry.get("exe", "") or "")
    name = str(entry.get("appname", "") or "")
    if not exe and not name:
        return 0
    return zlib.crc32((exe + name).encode("utf-8")) | 0x80000000


_warned_unreadable: set = set()


def _warn_unreadable(path: Path, exc: Exception) -> None:
    """Log a malformed shortcuts.vdf once per path (best-effort, UI-free)."""
    key = str(path)
    if key in _warned_unreadable:
        return
    _warned_unreadable.add(key)
    try:
        from Utils.app_log import app_log
        app_log(f"Could not read Steam shortcuts file {path}: {exc}")
    except Exception:
        pass


def _parse_file(path: Path) -> "list[Shortcut]":
    """Every usable shortcut in one shortcuts.vdf; [] if it can't be read."""
    try:
        buf = path.read_bytes()
    except OSError as exc:
        _warn_unreadable(path, exc)
        return []
    try:
        root, _ = _parse_map(buf, 0)
    except (ValueError, struct.error, IndexError) as exc:
        _warn_unreadable(path, exc)
        return []
    entries = root.get("shortcuts")
    if not isinstance(entries, dict):
        return []

    out: list[Shortcut] = []
    for entry in entries.values():
        if not isinstance(entry, dict):
            continue
        exe = _unquote(entry.get("exe"))
        appid = _entry_appid(entry)
        if not exe or not appid:
            continue
        tags = entry.get("tags")
        out.append(Shortcut(
            appid=appid,
            name=str(entry.get("appname", "") or ""),
            exe=exe,
            start_dir=_unquote(entry.get("startdir")),
            launch_options=str(entry.get("launchoptions", "") or ""),
            icon=str(entry.get("icon", "") or ""),
            tags=(tuple(str(v) for v in tags.values())
                  if isinstance(tags, dict) else ()),
            # userdata/<user_id>/config/shortcuts.vdf
            user_id=path.parent.parent.name,
        ))
    return out


# ---------------------------------------------------------------------------
# Discovery + cache
# ---------------------------------------------------------------------------

def shortcut_files() -> "list[Path]":
    """Every existing ``userdata/<id>/config/shortcuts.vdf``, deduplicated.

    One file per Steam account. ``~/.steam/steam`` is usually a symlink to the
    standard root, hence the resolve()-based dedup.
    """
    files: list[Path] = []
    seen: set = set()
    for steam_root in _STEAM_CANDIDATES:
        try:
            user_dirs = sorted(d for d in (steam_root / "userdata").iterdir()
                               if d.is_dir())
        except OSError:
            continue
        for user_dir in user_dirs:
            path = user_dir / "config" / "shortcuts.vdf"
            if not path.is_file():
                continue
            try:
                resolved = path.resolve()
            except OSError:
                resolved = path
            if resolved in seen:
                continue
            seen.add(resolved)
            files.append(path)
    return files


# load_shortcuts() cache, validated against the (st_mtime_ns, st_size) of every
# file actually read - the detection scans probe once per handler exe name, and
# a re-parse per probe would re-read every account's file each time.
_cache_lock = threading.Lock()
_cache: "tuple[tuple, tuple[Shortcut, ...]] | None" = None


def load_shortcuts() -> "list[Shortcut]":
    """Every non-Steam shortcut across all Steam accounts on this machine.

    Cached module-wide, so repeated probes cost one stat per account. Each
    call returns a fresh list, so callers can't corrupt the cache.
    """
    global _cache

    hits = [(p, sig) for p, sig in
            ((p, _stat_sig(p)) for p in shortcut_files()) if sig is not None]
    signature = tuple((str(p), sig) for p, sig in hits)

    with _cache_lock:
        cached = _cache
        if cached is not None and cached[0] == signature:
            return list(cached[1])

    out: list[Shortcut] = []
    for path, _sig in hits:
        out.extend(_parse_file(path))

    with _cache_lock:
        _cache = (signature, tuple(out))
    return list(out)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_installed_exe_index() -> "list[list[str]]":
    """Per shortcut, the lowercase path segments of its target executable.

    Mirrors the Lutris/Faugus one-pass indexes ``Utils.launchers.installed`` builds.
    """
    out: list[list[str]] = []
    for shortcut in load_shortcuts():
        parts = _split_exe_rel_parts(shortcut.exe)
        if parts:
            out.append(parts)
    return out


def find_shortcut_game_info_by_exe(
    exe_name: str,
) -> "tuple[Path, Path | None, str] | None":
    """Non-Steam-shortcut detection keyed by the handler's executable name.

    Returns ``(game_root, prefix_path | None, appid)`` or None - the same
    contract as find_lutris_game_info_by_exe / find_faugus_game_info_by_exe.

    A shortcut records the *executable*, so the game root is that path with
    the handler's declared sub-path stripped off the tail (Sims 4's
    ``Game/Bin/TS4_x64.exe`` yields the folder above ``Game``). The prefix is
    Steam's own ``compatdata/<appid>``, which exists only once the shortcut has
    actually been launched.
    """
    rel_parts = _split_exe_rel_parts(exe_name)
    if not rel_parts:
        return None
    for shortcut in load_shortcuts():
        if not _stored_exe_matches(shortcut.exe, rel_parts):
            continue
        game_root = _game_root_from_exe(Path(shortcut.exe), exe_name)
        if game_root is None:
            continue
        return (game_root, find_prefix(str(shortcut.appid)), str(shortcut.appid))
    return None


def find_shortcut_appids_by_exes(exe_names) -> "list[str]":
    """App IDs of every shortcut matching any of *exe_names*, in one pass."""
    prepared = [p for p in (_split_exe_rel_parts(e) for e in exe_names if e) if p]
    if not prepared:
        return []
    appids: list[str] = []
    for shortcut in load_shortcuts():
        appid = str(shortcut.appid)
        if appid in appids:
            continue
        if any(_stored_exe_matches(shortcut.exe, rel) for rel in prepared):
            appids.append(appid)
    return appids


def find_shortcut_by_appid(appid) -> "Shortcut | None":
    """The shortcut carrying this app ID, or None."""
    try:
        wanted = int(str(appid).strip()) & 0xFFFFFFFF
    except (TypeError, ValueError):
        return None
    if not wanted:
        return None
    for shortcut in load_shortcuts():
        if shortcut.appid == wanted:
            return shortcut
    return None


def is_shortcut_appid(appid) -> bool:
    """True when *appid* belongs to a non-Steam shortcut rather than a store app."""
    return find_shortcut_by_appid(appid) is not None
