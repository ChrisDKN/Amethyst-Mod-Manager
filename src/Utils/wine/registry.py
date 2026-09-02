"""Read-only reader for Wine's on-disk registry hives (system.reg / user.reg).

Wine stores each hive as a plain UTF-8 text file with a ``WINE REGISTRY Version
2`` header. Two properties of that format trip up every naive reader, and both
are handled here:

  * Key paths are **hive-relative** (no ``HKLM\\`` prefix) and every backslash
    is **doubled**, so ``HKLM\\Software\\Wow6432Node\\X`` is written on disk as
    ``[Software\\\\Wow6432Node\\\\X] 1783922856`` (with a trailing timestamp).
  * Wine **lowercases value names**. The Bethesda install path really is stored
    as ``"installed path"="Z:\\..."``, never ``"Installed Path"``. Matching is
    therefore case-insensitive on both the key path and the value name, and
    :func:`read_values` returns lowercased names.

This module only reads. The in-place section *editors* -
``deploy_wine_dll.apply_wine_dll_overrides`` and
``Utils.bethesda.xedit.set_winxp_compat``
- keep their own parsers on purpose: their reading is inseparable from sorted
insertion, ``#time=`` FILETIME rewriting and atomic writes, and reworking them
would risk corrupting user prefixes for no user-visible gain.

Nothing here raises: unreadable or absent hives read as ``None``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterator

HIVE_SYSTEM = "system.reg"
HIVE_USER = "user.reg"
HIVE_DEFAULT_USER = "userdef.reg"

# "[Software\\Wine\\DllOverrides] 1783922856" - the trailing decimal timestamp
# is optional (hand-written hives and some Wine versions omit it).
_SECTION_RE = re.compile(r"^\[(?P<key>.*?)\](?:\s+\d+)?\s*$")
# '"installed path"="Z:\\games\\Skyrim\\"' - the name may contain escapes.
_VALUE_RE = re.compile(r'^"(?P<name>(?:[^"\\]|\\.)*)"=(?P<val>.*)$')
_DEFAULT_VALUE_RE = re.compile(r"^@=(?P<val>.*)$")

# Hive text cache keyed by (path, st_mtime_ns, st_size). system.reg runs to
# 5-10 MB, and a health-check rescan reads it several times in a row.
_CACHE_LIMIT = 4
_cache: dict[tuple, str] = {}


def normalize_pfx(prefix_path: Path) -> Path:
    """Return the directory that actually holds ``user.reg`` / ``drive_c``.

    Accepts either the ``pfx/`` directory or its compatdata parent, mirroring
    the idiom in ``deploy_wine_dll`` / ``Utils.bethesda.xedit``. A path that is neither
    is returned unchanged - the caller's own existence checks then report it.
    """
    prefix_path = Path(prefix_path)
    if (not (prefix_path / HIVE_USER).is_file()
            and (prefix_path / "pfx" / HIVE_USER).is_file()):
        return prefix_path / "pfx"
    return prefix_path


def read_hive_text(pfx: Path, hive: str = HIVE_SYSTEM) -> str | None:
    """UTF-8 text of ``<pfx>/<hive>``, or None when absent/unreadable."""
    path = Path(pfx) / hive
    try:
        st = path.stat()
    except OSError:
        return None
    key = (str(path), st.st_mtime_ns, st.st_size)
    cached = _cache.get(key)
    if cached is not None:
        return cached
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if len(_cache) >= _CACHE_LIMIT:
        _cache.clear()
    _cache[key] = text
    return text


def escape_key(key_path: str) -> str:
    r"""``Software\Wow6432Node\X`` -> ``Software\\Wow6432Node\\X`` (on-disk form)."""
    return key_path.replace("\\", "\\\\")


def _unescape(raw: str) -> str:
    r"""Unescape a quoted .reg string body (``\\`` -> ``\``, ``\"`` -> ``"``)."""
    out: list[str] = []
    i = 0
    while i < len(raw):
        ch = raw[i]
        if ch == "\\" and i + 1 < len(raw):
            nxt = raw[i + 1]
            out.append({"n": "\n", "r": "\r", "t": "\t", "0": "\0"}.get(nxt, nxt))
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _parse_value(raw: str) -> str:
    """Decode the right-hand side of a value line.

    Quoted strings are unquoted and unescaped; ``dword:`` / ``hex:`` and any
    other typed form is returned verbatim - no caller here interprets them.
    """
    raw = raw.strip()
    if len(raw) >= 2 and raw.startswith('"') and raw.endswith('"'):
        return _unescape(raw[1:-1])
    return raw


def iter_sections(text: str) -> Iterator[tuple[str, list[str]]]:
    """Yield ``(raw_key_path, body_lines)`` for each ``[...]`` section.

    The key path is the raw on-disk text (backslashes still doubled). A section
    ends at the next line starting with ``[``; ``#time=`` and blank lines stay
    in the body for the caller to filter.
    """
    key: str | None = None
    body: list[str] = []
    for line in text.splitlines():
        if line.startswith("["):
            m = _SECTION_RE.match(line)
            if m is not None:
                if key is not None:
                    yield key, body
                key = m.group("key")
                body = []
                continue
        if key is not None:
            body.append(line)
    if key is not None:
        yield key, body


def _values_from_body(body: list[str]) -> dict[str, str]:
    """Value map for one section body. Names are lowercased; default is '@'."""
    values: dict[str, str] = {}
    for line in body:
        line = line.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        m = _VALUE_RE.match(line)
        if m is not None:
            values[_unescape(m.group("name")).lower()] = _parse_value(m.group("val"))
            continue
        m = _DEFAULT_VALUE_RE.match(line)
        if m is not None:
            values["@"] = _parse_value(m.group("val"))
    return values


def find_sections(
    pfx: Path,
    key_regex: "str | re.Pattern",
    *,
    hive: str = HIVE_SYSTEM,
) -> list[tuple[str, dict[str, str]]]:
    """Sections whose raw (doubled-backslash) key path matches *key_regex*.

    The pattern is fullmatched case-insensitively against the text inside the
    brackets, so it must be written in the doubled-backslash on-disk form -
    build it with :func:`escape_key` plus ``re.escape`` where it is literal.
    """
    text = read_hive_text(pfx, hive)
    if text is None:
        return []
    pattern = (re.compile(key_regex, re.IGNORECASE)
               if isinstance(key_regex, str) else key_regex)
    out: list[tuple[str, dict[str, str]]] = []
    for key, body in iter_sections(text):
        if pattern.fullmatch(key):
            out.append((key, _values_from_body(body)))
    return out


def read_values(
    pfx: Path, key_path: str, *, hive: str = HIVE_SYSTEM,
) -> dict[str, str] | None:
    """All values of one key, or None when the key is absent.

    *key_path* is given in normal single-backslash form and matched
    case-insensitively. Returned names are lowercased (Wine's own casing);
    the default value is under ``"@"``.
    """
    text = read_hive_text(pfx, hive)
    if text is None:
        return None
    wanted = escape_key(key_path).lower()
    for key, body in iter_sections(text):
        if key.lower() == wanted:
            return _values_from_body(body)
    return None


def read_value(
    pfx: Path, key_path: str, value_name: str, *, hive: str = HIVE_SYSTEM,
) -> str | None:
    """One value, matched case-insensitively, or None if key/value is absent."""
    values = read_values(pfx, key_path, hive=hive)
    if values is None:
        return None
    return values.get(value_name.lower())


def key_exists(pfx: Path, key_path: str, *, hive: str = HIVE_SYSTEM) -> bool:
    """True when *key_path* is present in the hive (case-insensitive)."""
    return read_values(pfx, key_path, hive=hive) is not None


def hive_contains(pfx: Path, needle: str, *, hive: str = HIVE_SYSTEM) -> bool:
    """Case-insensitive substring scan of the hive text.

    For cheap "is this file referenced anywhere" probes (COM registrations
    naming a DLL) where the exact CLSID keys are not worth enumerating.
    """
    text = read_hive_text(pfx, hive)
    if text is None:
        return False
    return needle.lower() in text.lower()
