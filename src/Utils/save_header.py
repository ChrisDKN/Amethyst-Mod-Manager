"""Read the metadata header out of a Bethesda save file.

Dispatch is on MAGIC BYTES, not the game handler, so nothing in Games/ needs to
know about this. Nothing raises: a corrupt or truncated file degrades to a
partial result, an unknown one to None.

Layouts (each parser below documents its own field walk):
    TESV_SAVEGAME  Skyrim LE/SE/VR, Enderal    .ess   wstrings, body may be compressed
    FO4_SAVEGAME   Fallout 4, Fallout 4 VR     .fos   wstrings, never compressed
    FO3SAVEGAME    Fallout 3, New Vegas        .fos   pipe-delimited strings
    TES4SAVEGAME   Oblivion                    .ess   bzstrings

Text is in the Windows system codepage, decoded as cp1252 — a save from a
localised (e.g. Russian cp1251) build shows mojibake, the same limitation MO2
has, since the file records no encoding.
"""

from __future__ import annotations

import os
import re
import struct
import zlib
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

# Save text is the Windows system codepage; cp1252 covers Western installs.
_ENCODING = "cp1252"

# Extensions worth sniffing at all. Anything else is not a save we parse.
SAVE_EXTS = (".ess", ".fos")

# Refuse absurd screenshots rather than allocating on a corrupt size field.
# The largest real one is Fallout 4's 640x384 RGBA (~0.9 MB).
_MAX_SHOT_BYTES = 64 * 1024 * 1024

# Plugin lists are small; a bad count field must not spin for a million reads.
_MAX_PLUGINS = 4096

# How much of a compressed body to inflate. The plugin lists are at its head;
# 4096 plugins of 255 chars is ~1 MB, so this has generous headroom.
_BODY_HEAD = 4 * 1024 * 1024

# Bytes read when only the header is wanted. Every format puts the metadata
# well inside the first page or two; the screenshot is what follows.
_HEADER_PROBE = 8192

# Bytes read when the screenshot is wanted but the plugin list isn't — the
# largest real screenshot is Fallout 4's 640x384 RGBA (~1 MB).
_SHOT_PROBE = 4 * 1024 * 1024

# One segment of Fallout 4's compact play time ("2d", "10m").
_COMPACT_UNIT = re.compile(r"^\d+[dhms]$")

# Windows FILETIME epoch (1601-01-01) offset from the Unix epoch, in seconds.
_FILETIME_EPOCH_DELTA = 11644473600


class _Reader:
    """Bounds-checked little-endian cursor over a bytes buffer."""

    def __init__(self, buf: bytes, pos: int = 0):
        self.buf = buf
        self.pos = pos

    def _need(self, count: int) -> None:
        if count < 0 or self.pos + count > len(self.buf):
            raise _SaveParseError(
                f"read of {count} at {self.pos} past end ({len(self.buf)})")

    def bytes(self, count: int) -> bytes:
        self._need(count)
        out = self.buf[self.pos:self.pos + count]
        self.pos += count
        return out

    def unpack(self, fmt: str):
        size = struct.calcsize(fmt)
        self._need(size)
        out = struct.unpack_from("<" + fmt, self.buf, self.pos)
        self.pos += size
        return out

    def u8(self) -> int:
        return self.unpack("B")[0]

    def u16(self) -> int:
        return self.unpack("H")[0]

    def u32(self) -> int:
        return self.unpack("I")[0]

    def u64(self) -> int:
        return self.unpack("Q")[0]

    def f32(self) -> float:
        return self.unpack("f")[0]

    def wstring(self) -> str:
        """uint16 length + that many characters (Creation Engine)."""
        return _decode(self.bytes(self.u16()))

    def bzstring(self) -> str:
        """uint8 length INCLUDING a trailing NUL (Oblivion)."""
        return _decode(self.bytes(self.u8()))

    def expect(self, literal: bytes) -> None:
        got = self.bytes(len(literal))
        if got != literal:
            raise _SaveParseError(f"expected {literal!r} at "
                                  f"{self.pos - len(literal)}, got {got!r}")

    def pipe_string(self) -> str:
        """'|' + uint16 length + '|' + characters (Fallout 3 / New Vegas)."""
        self.expect(b"|")
        count = self.u16()
        self.expect(b"|")
        return _decode(self.bytes(count))

    def pipe_u32(self) -> int:
        self.expect(b"|")
        return self.u32()


class _SaveParseError(Exception):
    """Internal: a read ran off the end or a field didn't look like itself."""


def _decode(raw: bytes) -> str:
    return raw.split(b"\x00")[0].decode(_ENCODING, "replace")


def _filetime_to_unix(filetime: int) -> "float | None":
    """Windows FILETIME (100 ns ticks since 1601) → unix timestamp."""
    if filetime <= 0:
        return None
    stamp = filetime / 10_000_000 - _FILETIME_EPOCH_DELTA
    # Anything outside 1970..2100 is a misread field, not a save date.
    return stamp if 0 < stamp < 4102444800 else None


def _systemtime_to_unix(raw: bytes) -> "float | None":
    """Windows SYSTEMTIME (8 uint16: y, m, dow, d, h, min, s, ms) → unix ts."""
    try:
        year, month, _dow, day, hour, minute, second, _ms = struct.unpack("<8H", raw)
        return datetime(year, month, day, hour, minute, second,
                        tzinfo=timezone.utc).timestamp()
    except Exception:
        return None


@dataclass(frozen=True)
class SaveScreenshot:
    """Raw pixel data from the save, ready to hand to QImage."""

    width: int
    height: int
    channels: int   # 3 = RGB888, 4 = RGBA8888
    data: bytes


@dataclass(frozen=True)
class SaveHeader:
    """What a save file says about itself."""

    kind: str                 # "TESV" | "FO4" | "FO3" | "TES4"
    version: int = 0
    save_number: int = 0
    character: str = ""
    level: int = 0
    location: str = ""
    play_time: str = ""       # the engine's own "hhh.mm.ss" string
    race: str = ""
    #: Fallout 3 / New Vegas only — the character's class + karma line.
    title: str = ""
    sex: str = ""
    saved_at: "float | None" = None
    game_version: str = ""    # Fallout 4 only
    screenshot: "SaveScreenshot | None" = None
    plugins: tuple = ()
    light_plugins: tuple = ()
    #: A later section failed to parse; the fields above it are still good.
    partial: bool = False


# Magic → (kind, magic length). Longest first so no prefix shadows another.
_MAGICS = (
    (b"TESV_SAVEGAME", "TESV"),
    (b"TES4SAVEGAME", "TES4"),
    (b"FO4_SAVEGAME", "FO4"),
    (b"FO3SAVEGAME", "FO3"),
)

_CACHE: dict = {}
# Screenshots dominate an entry (~0.25-1 MB), so this stays small — it only
# exists to make re-selecting the same row instant.
_CACHE_MAX = 24


def _cache_get(key: str, mtime_ns: int, size: int):
    entry = _CACHE.get(key)
    if entry is None:
        return None
    c_mtime, c_size, header = entry
    return header if (c_mtime == mtime_ns and c_size == size) else None


def _cache_put(key: str, mtime_ns: int, size: int, header: SaveHeader) -> None:
    if len(_CACHE) >= _CACHE_MAX:
        for stale in list(_CACHE.keys())[: _CACHE_MAX // 4]:
            _CACHE.pop(stale, None)
    _CACHE[key] = (mtime_ns, size, header)


def clear_cache() -> None:
    """Drop the parsed-header cache (tests; a save folder replaced wholesale)."""
    _CACHE.clear()


def save_kind(path) -> str:
    """Return the format id for *path* ("TESV", "FO4", …), or "" if unknown."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(16)
    except OSError:
        return ""
    for magic, kind in _MAGICS:
        if head.startswith(magic):
            return kind
    return ""


def is_save_file(path) -> bool:
    """Whether *path* parses as a save. Extension first, so a folder of
    non-saves costs no opens."""
    try:
        p = Path(path)
    except (TypeError, ValueError):
        return False
    if p.suffix.lower() not in SAVE_EXTS:
        return False
    return bool(save_kind(p))


def parse_save(path, *, want_screenshot: bool = True,
               want_plugins: bool = True) -> "SaveHeader | None":
    """Parse *path*, or None if it isn't a save we know. Turning off
    *want_screenshot* / *want_plugins* keeps the read to the first few KB."""
    key = str(path)
    try:
        st = os.stat(key)
    except OSError:
        return None

    full = want_screenshot and want_plugins
    if full:
        cached = _cache_get(key, st.st_mtime_ns, st.st_size)
        if cached is not None:
            return cached

    try:
        with open(key, "rb") as handle:
            head = handle.read(16)
            kind = ""
            for magic, name in _MAGICS:
                if head.startswith(magic):
                    kind = name
                    break
            if not kind:
                return None
            handle.seek(0)
            # Only the plugin list (past the screenshot) needs the whole file.
            if want_plugins:
                buf = handle.read()
            elif want_screenshot:
                buf = handle.read(_SHOT_PROBE)
            else:
                buf = handle.read(_HEADER_PROBE)
    except OSError:
        return None

    parser = {"TESV": _parse_tesv, "FO4": _parse_fo4,
              "FO3": _parse_fo3, "TES4": _parse_tes4}[kind]
    try:
        header = parser(buf, want_screenshot, want_plugins)
    except Exception:
        # Magic matched, layout didn't — report the format so the UI can say
        # "unreadable save" rather than "not a save".
        return SaveHeader(kind=kind, partial=True)

    if full:
        _cache_put(key, st.st_mtime_ns, st.st_size, header)
    return header


# ---- screenshot -----------------------------------------------------------
def _read_screenshot(rdr: _Reader, width: int, height: int, channels: int,
                     want: bool) -> "SaveScreenshot | None":
    """Consume the pixel block, returning it only when *want*."""
    if width <= 0 or height <= 0 or channels not in (3, 4):
        raise _SaveParseError(f"bad screenshot dims {width}x{height}x{channels}")
    count = width * height * channels
    if count > _MAX_SHOT_BYTES:
        raise _SaveParseError(f"screenshot of {count} bytes is implausible")
    if not want:
        # Step over, don't read: a metadata-only caller never buffered the
        # pixels, and demanding them would flag a healthy save partial.
        rdr.pos += count
        return None
    return SaveScreenshot(width=width, height=height, channels=channels,
                          data=rdr.bytes(count))


# ---- plugin lists ---------------------------------------------------------
def _read_plugin_list(rdr: _Reader, count: int, reader_name: str) -> tuple:
    if count > _MAX_PLUGINS:
        raise _SaveParseError(f"plugin count {count} is implausible")
    read = getattr(rdr, reader_name)
    return tuple(read() for _ in range(count))


# ---- Skyrim / Enderal (TESV_SAVEGAME) -------------------------------------
def _parse_tesv(buf: bytes, want_shot: bool, want_plugins: bool) -> SaveHeader:
    """magic[13], headerSize u32, header fields, screenshot, [compressed] body."""
    rdr = _Reader(buf, 13)
    header_size = rdr.u32()
    body_start = rdr.pos
    version = rdr.u32()
    save_number = rdr.u32()
    character = rdr.wstring()
    level = rdr.u32()
    location = rdr.wstring()
    play_time = rdr.wstring()
    race = rdr.wstring()
    sex = rdr.u16()
    rdr.f32()   # current experience
    rdr.f32()   # experience needed for the next level
    saved_at = _filetime_to_unix(rdr.u64())
    shot_w = rdr.u32()
    shot_h = rdr.u32()
    compression = rdr.u16() if version >= 12 else 0

    # The format states its own header length: landing anywhere else means the
    # layout assumption is wrong and everything past here is garbage.
    if rdr.pos - body_start != header_size:
        raise _SaveParseError(
            f"TESV header walk consumed {rdr.pos - body_start}, "
            f"declared {header_size}")

    base = SaveHeader(
        kind="TESV", version=version, save_number=save_number,
        character=character, level=level, location=location,
        play_time=play_time, race=race, sex=_sex_label(sex),
        saved_at=saved_at,
    )

    try:
        shot = _read_screenshot(rdr, shot_w, shot_h,
                                4 if version >= 12 else 3, want_shot)
    except _SaveParseError:
        return _partial(base)
    base = _with(base, screenshot=shot)
    if not want_plugins:
        return base

    try:
        body = _tesv_body(rdr, compression)
        plugins, light, _gv, partial = _creation_plugin_lists(
            _Reader(body), gameversion=False, esl_form_version=78)
    except Exception:
        return _partial(base)
    return _with(base, plugins=plugins, light_plugins=light, partial=partial)


def _tesv_body(rdr: _Reader, compression: int) -> bytes:
    """The bytes after the screenshot, decompressed when Skyrim SE compressed them."""
    if compression == 0:
        return rdr.buf[rdr.pos:]
    uncompressed_len = rdr.u32()
    compressed_len = rdr.u32()
    blob = rdr.bytes(compressed_len)
    if compression == 1:
        # Stop at _BODY_HEAD: SE compresses the whole rest of the save, and a
        # late-game file inflates to hundreds of MB — the plugin lists we want
        # sit in the first few KB of it.
        return zlib.decompressobj().decompress(blob, _BODY_HEAD)
    if compression == 2:
        # lz4 is already a hard dependency (skygen_core, pak_reader). Its block
        # API has no partial mode, so this one pays for the full inflate.
        import lz4.block
        return lz4.block.decompress(blob, uncompressed_size=uncompressed_len)
    raise _SaveParseError(f"unknown save compression {compression}")


def _creation_plugin_lists(rdr: _Reader, *, gameversion: bool,
                           esl_form_version: int) -> tuple:
    """formVersion + plugin lists, shared by Skyrim and Fallout 4.

    *esl_form_version* is where the format grew the light-plugin list (78 SE,
    68 FO4); below it, reading a count would invent names out of the body.
    """
    form_version = rdr.u8()
    game_version = rdr.wstring() if gameversion else ""
    rdr.u32()   # pluginInfoSize — the lists below are self-describing
    plugins = _read_plugin_list(rdr, rdr.u8(), "wstring")
    light, partial = (), False
    if form_version >= esl_form_version:
        try:
            light = _read_plugin_list(rdr, rdr.u16(), "wstring")
        except _SaveParseError:
            # Claimed a light list and didn't deliver — say so rather than
            # quietly reporting the save as having no ESLs.
            partial = True
    return plugins, light, game_version, partial


# ---- Fallout 4 (FO4_SAVEGAME) ---------------------------------------------
def _parse_fo4(buf: bytes, want_shot: bool, want_plugins: bool) -> SaveHeader:
    """As TESV through shotHeight, but no compression field and RGBA always."""
    rdr = _Reader(buf, 12)
    header_size = rdr.u32()
    body_start = rdr.pos
    version = rdr.u32()
    save_number = rdr.u32()
    character = rdr.wstring()
    level = rdr.u32()
    location = rdr.wstring()
    play_time = rdr.wstring()
    race = rdr.wstring()
    sex = rdr.u16()
    rdr.f32()
    rdr.f32()
    saved_at = _filetime_to_unix(rdr.u64())
    shot_w = rdr.u32()
    shot_h = rdr.u32()

    if rdr.pos - body_start != header_size:
        raise _SaveParseError(
            f"FO4 header walk consumed {rdr.pos - body_start}, "
            f"declared {header_size}")

    base = SaveHeader(
        kind="FO4", version=version, save_number=save_number,
        character=character, level=level, location=location,
        play_time=play_time, race=race, sex=_sex_label(sex),
        saved_at=saved_at,
    )

    try:
        shot = _read_screenshot(rdr, shot_w, shot_h, 4, want_shot)
    except _SaveParseError:
        return _partial(base)
    base = _with(base, screenshot=shot)
    if not want_plugins:
        return base

    try:
        plugins, light, game_version, partial = _creation_plugin_lists(
            rdr, gameversion=True, esl_form_version=68)
    except Exception:
        return _partial(base)
    return _with(base, plugins=plugins, light_plugins=light,
                 game_version=game_version, partial=partial)


# ---- Fallout 3 / New Vegas (FO3SAVEGAME) ----------------------------------
def _parse_fo3(buf: bytes, want_shot: bool, want_plugins: bool) -> SaveHeader:
    # New Vegas carries a 64-byte language block; Fallout 3 may not. The
    # declared header length tells us which walk was right, so try both.
    last_error: Exception = _SaveParseError("no attempt made")
    for language_bytes in (64, 0):
        try:
            return _parse_fo3_variant(buf, want_shot, want_plugins, language_bytes)
        except _SaveParseError as exc:
            last_error = exc
    raise last_error


def _parse_fo3_variant(buf: bytes, want_shot: bool, want_plugins: bool,
                       language_bytes: int) -> SaveHeader:
    rdr = _Reader(buf, 11)
    header_size = rdr.u32()
    # headerSize is measured from here — the version field is inside it.
    header_end = rdr.pos + header_size
    version = rdr.u32()

    if language_bytes:
        rdr.expect(b"|")
        rdr.bytes(language_bytes)
    shot_w = rdr.pipe_u32()
    shot_h = rdr.pipe_u32()
    save_number = rdr.pipe_u32()
    character = rdr.pipe_string()
    title = rdr.pipe_string()          # "Courier" / class + karma line
    level = rdr.pipe_u32()
    location = rdr.pipe_string()
    play_time = rdr.pipe_string()
    rdr.expect(b"|")

    if rdr.pos != header_end:
        raise _SaveParseError(
            f"FO3 header walk ended at {rdr.pos}, declared {header_end}")

    base = SaveHeader(
        kind="FO3", version=version, save_number=save_number,
        character=character, level=level, location=location,
        play_time=play_time, title=title,
    )

    try:
        shot = _read_screenshot(rdr, shot_w, shot_h, 3, want_shot)
    except _SaveParseError:
        return _partial(base)
    base = _with(base, screenshot=shot)
    if not want_plugins:
        return base

    try:
        rdr.u8()    # formVersion
        rdr.u32()   # pluginInfoSize
        plugins = _read_plugin_list(rdr, rdr.u8(), "pipe_string")
    except Exception:
        return _partial(base)
    return _with(base, plugins=plugins)


# ---- Oblivion (TES4SAVEGAME) ----------------------------------------------
def _parse_tes4(buf: bytes, want_shot: bool, want_plugins: bool) -> SaveHeader:
    """Shares nothing with the others: bzstrings, SYSTEMTIME dates, u16 level."""
    rdr = _Reader(buf, 12)
    major = rdr.u8()
    rdr.u8()                      # minor version
    rdr.bytes(16)                 # exe timestamp (SYSTEMTIME)
    rdr.u32()                     # header version
    header_size = rdr.u32()
    header_end = rdr.pos
    save_number = rdr.u32()
    character = rdr.bzstring()
    level = rdr.u16()
    location = rdr.bzstring()
    rdr.f32()                     # elapsed in-game days (not play time)
    game_ticks = rdr.u32()
    saved_at = _systemtime_to_unix(rdr.bytes(16))
    shot_size = rdr.u32()
    shot_w = rdr.u32()
    shot_h = rdr.u32()

    # saveHeaderSize spans saveNum..screenshot; screenshotSize spans the pixels
    # plus its two dimension fields. Both cross-check the walk.
    if rdr.pos - header_end != header_size:
        raise _SaveParseError(
            f"TES4 header walk consumed {rdr.pos - header_end}, "
            f"declared {header_size}")
    if shot_size != shot_w * shot_h * 3 + 8:
        raise _SaveParseError(
            f"TES4 screenshot size {shot_size} != {shot_w}x{shot_h} RGB + 8")

    base = SaveHeader(
        kind="TES4", version=major, save_number=save_number,
        character=character, level=level, location=location,
        play_time=_oblivion_play_time(game_ticks),
        saved_at=saved_at,
    )

    try:
        shot = _read_screenshot(rdr, shot_w, shot_h, 3, want_shot)
    except _SaveParseError:
        return _partial(base)
    base = _with(base, screenshot=shot)
    if not want_plugins:
        return base

    try:
        plugins = _read_plugin_list(rdr, rdr.u8(), "bzstring")
    except Exception:
        return _partial(base)
    return _with(base, plugins=plugins)


def _oblivion_play_time(game_ticks: int) -> str:
    """Oblivion has no play-time string — render its ms counter as hhh.mm.ss
    so callers stay format-agnostic."""
    if game_ticks <= 0:
        return ""
    total = game_ticks // 1000
    return f"{total // 3600:03d}.{total // 60 % 60:02d}.{total % 60:02d}"


# ---- small helpers --------------------------------------------------------
def _sex_label(raw: int) -> str:
    return {0: "Male", 1: "Female"}.get(raw, "")


def _with(header: SaveHeader, **changes) -> SaveHeader:
    return replace(header, **changes)


def _partial(header: SaveHeader) -> SaveHeader:
    return _with(header, partial=True)


def format_play_time(raw: str) -> str:
    """Render an engine play-time string for display.

    Two shapes occur: "hhh.mm.ss" (Skyrim, Fallout 3/NV — the units are pinned
    down by New Vegas writing the same value into its save FILENAMES), and
    Fallout 4's "2d.2h.10m.2 days.2 hours.10 minutes", where a compact form is
    followed by a spelled-out one. Anything else passes through untouched.
    """
    parts = raw.split(".")
    if len(parts) == 3 and all(p.strip().isdigit() for p in parts):
        hours, minutes, seconds = (int(p) for p in parts)
        if hours:
            return f"{hours}h {minutes}m"
        if minutes:
            return f"{minutes}m {seconds}s"
        return f"{seconds}s"
    compact = []
    for part in parts:
        if not _COMPACT_UNIT.match(part.strip()):
            break
        compact.append(part.strip())
    return " ".join(compact) if compact else raw
