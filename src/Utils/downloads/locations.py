"""Toolkit-neutral read/write of the download-locations settings.

Both the Tk app and the Qt app read/write the SAME file -
``~/.config/AmethystModManager/download_locations.json`` - so the Downloads tab
in either toolkit stays backward-compatible. Pure stdlib + Utils.* - no GUI
toolkit.

Format (object form; a legacy bare list of paths is auto-read + upgraded):
    {"extras": [paths], "default_disabled": bool, "cache_disabled": bool,
     "hidden_archives": [paths]}
"""

from __future__ import annotations

import json
from pathlib import Path

from Utils.atomic_write import write_atomic_text
from Utils.config_paths import get_download_locations_path
from Utils.environment.xdg import xdg_download_dir


def _read_data() -> dict:
    path = get_download_locations_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeError):
        return {}
    if isinstance(data, list):
        return {"extras": data}
    if isinstance(data, dict):
        return data
    return {}


def _write_data(data: dict) -> None:
    write_atomic_text(
        get_download_locations_path(),
        json.dumps(data, indent=2) + "\n",
    )


def read_config() -> tuple[list[str], bool, bool]:
    """Load (extras, default_disabled, cache_disabled). Supports the legacy
    bare-list form as well as the object form."""
    data = _read_data()
    raw = data.get("extras", [])
    extras = (
        [str(p).strip() for p in raw if p and str(p).strip()]
        if isinstance(raw, list) else []
    )
    return (
        extras,
        bool(data.get("default_disabled", False)),
        bool(data.get("cache_disabled", False)),
    )


def write_config(extras: list[str], default_disabled: bool,
                 cache_disabled: bool) -> None:
    data = _read_data()
    data.update({
        "extras": extras,
        "default_disabled": default_disabled,
        "cache_disabled": cache_disabled,
    })
    _write_data(data)


def archive_path_key(path) -> str:
    try:
        return str(Path(path).expanduser().resolve())
    except (OSError, RuntimeError):
        return str(Path(path).expanduser())


def load_hidden_archive_paths() -> set[str]:
    raw = _read_data().get("hidden_archives", [])
    if not isinstance(raw, list):
        return set()
    return {archive_path_key(p) for p in raw if isinstance(p, str) and p}


def save_hidden_archive_paths(paths) -> None:
    data = _read_data()
    hidden = sorted({archive_path_key(p) for p in paths if p})
    if hidden:
        data["hidden_archives"] = hidden
    else:
        data.pop("hidden_archives", None)
    _write_data(data)


def load_extra_download_locations() -> list[str]:
    """Extra scan paths only (excludes the default Downloads folder)."""
    return read_config()[0]


def get_default_downloads_dir() -> Path:
    """The system default Downloads folder (per xdg-user-dirs)."""
    return xdg_download_dir()


def is_default_downloads_disabled() -> bool:
    return read_config()[1]


def is_cache_default_disabled() -> bool:
    return read_config()[2]


def get_effective_download_locations() -> list[Path]:
    """All folders to scan: default Downloads (unless disabled) + extras,
    de-duplicated by resolved path. (Does NOT include the per-game cache, which
    needs a game name - see downloads.core.scan_download_dirs.)"""
    dirs: list[Path] = []
    seen: set[Path] = set()
    if not is_default_downloads_disabled():
        default = get_default_downloads_dir()
        try:
            key = default.resolve()
        except OSError:
            key = default
        dirs.append(default)
        seen.add(key)
    for p in load_extra_download_locations():
        path = Path(p).expanduser()
        try:
            key = path.resolve()
        except OSError:
            key = path
        if key in seen:
            continue
        dirs.append(path)
        seen.add(key)
    return dirs
