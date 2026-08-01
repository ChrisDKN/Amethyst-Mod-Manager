"""BG3 override-pak scan for the plugins panel's Overrides tab.

An "override" pak either has no Mods/<Folder>/meta.lsx at all, or has one but
only writes into Larian built-in module folders (same classification the
modsettings writer uses) — either way it never gets a modsettings.lsx entry
and just needs to sit in the game's Mods folder. This module lists them
per enabled mod so the GUI can show/toggle them; deployment itself is
unchanged (paks deploy via the filemap regardless).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

from Utils.app_log import app_log
from Utils.modsettings import _classify_pak_files
from Utils.pak_reader import read_pak_info

STATUS_NO_META = "no_meta"
STATUS_OVERRIDE_ONLY = "override_only"


@dataclass
class OverridePakRow:
    mod_name: str   # staging folder name (excluded_mod_files key)
    rel_key: str    # raw lowercase posix path — the exclusion key
    rel_str: str    # raw-case relative path for display
    status: str     # STATUS_NO_META | STATUS_OVERRIDE_ONLY


# Classification cache: (path, size, mtime_ns) -> status | None (not an
# override). Pak contents can't change without size/mtime changing, so hits
# skip the file open entirely.
_cache: dict[tuple[str, int, int], str | None] = {}
_cache_lock = threading.Lock()


def _classify(pak: Path) -> str | None:
    """Status for *pak*, or None when it's a normal load-order pak."""
    try:
        st = pak.stat()
        key = (str(pak), st.st_size, st.st_mtime_ns)
    except OSError:
        return None
    with _cache_lock:
        if key in _cache:
            return _cache[key]
    try:
        info = read_pak_info(pak)
    except Exception as exc:
        app_log(f"Failed to read {pak}: {exc}")
        return None
    if info.meta_xml is None:
        status = STATUS_NO_META
    else:
        overrides_builtin, has_own_data = _classify_pak_files(info.file_names)
        status = (STATUS_OVERRIDE_ONLY
                  if overrides_builtin and not has_own_data else None)
    with _cache_lock:
        _cache[key] = status
    return status


def scan_override_paks(staging_root: Path,
                       enabled_mod_names: list[str]) -> list[OverridePakRow]:
    """Override paks across *enabled_mod_names*, in modlist order.

    Disabled paks are listed too (the view shows them unchecked). rel_key is
    the RAW mod-relative path — the space excluded_mod_files keys on.
    """
    rows: list[OverridePakRow] = []
    for name in enabled_mod_names:
        mod_dir = staging_root / name
        if not mod_dir.is_dir():
            continue
        for pak in sorted(mod_dir.rglob("*.pak")):
            status = _classify(pak)
            if status is None:
                continue
            rel_str = pak.relative_to(mod_dir).as_posix()
            rows.append(OverridePakRow(name, rel_str.lower(), rel_str, status))
    return rows
