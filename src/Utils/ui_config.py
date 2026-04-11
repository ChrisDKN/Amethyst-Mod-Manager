"""
UI scaling configuration stored in ~/.config/AmethystModManager/amethyst.ini.

Users can set ui_scale (e.g. 1.0, 1.25, 1.5, 2.0) for HiDPI displays.
Set scale=auto to use automatic scaling based on screen size.
"""

import configparser
from pathlib import Path

from Utils.config_paths import get_config_dir

_INI_SECTION = "ui"
_INI_OPTION = "scale"
_INI_AUTO = "auto"
_DEFAULT_SCALE = 1.0
_MIN_SCALE = 0.5
_MAX_SCALE = 3.0

_DEFAULT_FONT_FAMILY = "Noto Sans"
_INI_FONT_OPTION = "font_family"

_ui_scale: float = _DEFAULT_SCALE
_font_family: str = _DEFAULT_FONT_FAMILY


def get_ui_config_path() -> Path:
    """Return the path to the amethyst.ini config file."""
    return get_config_dir() / "amethyst.ini"


def get_screen_info() -> tuple[int, int, float]:
    """Return (screen_width, screen_height, detected_scale) for the primary display."""
    try:
        import tkinter as _tk
        root = _tk.Tk()
        root.withdraw()
        root.update_idletasks()
        w = root.winfo_screenwidth()
        h = root.winfo_screenheight()
        # Detect if the DE/compositor is applying its own scaling.
        # Tk reports 96 DPI as default; higher values mean the DE is scaling.
        # winfo_fpixels('1i') returns pixels-per-inch as seen by Tk.
        try:
            dpi = root.winfo_fpixels('1i')
            de_scale = dpi / 96.0 if dpi > 96 else 1.0
        except Exception:
            de_scale = 1.0
        root.destroy()
    except Exception:
        return 0, 0, _DEFAULT_SCALE
    if w <= 0 or h <= 0:
        return w, h, _DEFAULT_SCALE
    # On scaled desktops, winfo_screenheight may report the virtual (scaled)
    # resolution rather than physical pixels. Divide out the DE scale to get
    # the true physical height for our scaling heuristic.
    physical_h = h / de_scale if de_scale > 1.0 else h
    # UI designed for Steam Deck (1280x800). Use height only; 800–1080 = 1.0.
    if physical_h <= 800:
        scale = max(_MIN_SCALE, physical_h / 800)
    elif physical_h >= 1080:
        scale = min(1.5, physical_h / 1080)
    else:
        scale = 1.0  # plateau: 800–1080 all use 1.0
    scale = round(scale * 20) / 20  # Snap to nearest 0.05
    return w, h, scale


def detect_hidpi_scale() -> float:
    """Detect suggested UI scale from primary screen height.

    UI designed for Steam Deck (1280x800). Heights 800–1080 → 1.0.
    Below 800 scales down; above 1080 scales up to 1.5.
    """
    _, _, scale = get_screen_info()
    return scale


def load_ui_scale() -> float:
    """Load ui_scale from INI. Returns the value, clamped to [0.5, 3.0].

    When config is missing or scale=auto, uses detect_hidpi_scale() for automatic
    scaling based on screen size.
    """
    global _ui_scale
    path = get_ui_config_path()
    if not path.is_file():
        _ui_scale = detect_hidpi_scale()
        _write_ini(path, _INI_AUTO)
        return _ui_scale
    try:
        parser = configparser.ConfigParser()
        parser.read(path)
        if parser.has_section(_INI_SECTION) and parser.has_option(_INI_SECTION, _INI_OPTION):
            raw = parser.get(_INI_SECTION, _INI_OPTION).strip().lower()
            if raw == _INI_AUTO:
                _ui_scale = detect_hidpi_scale()
            else:
                _ui_scale = _clamp(float(raw))
        else:
            _ui_scale = detect_hidpi_scale()
    except (configparser.Error, ValueError):
        _ui_scale = detect_hidpi_scale()
    return _ui_scale


def _write_ini(path: Path, scale_str: str) -> None:
    """Write the [ui] scale to amethyst.ini."""
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = configparser.ConfigParser()
    if path.is_file():
        parser.read(path)
    if _INI_SECTION not in parser:
        parser[_INI_SECTION] = {}
    parser[_INI_SECTION][_INI_OPTION] = scale_str
    with path.open("w") as f:
        parser.write(f)


def save_ui_scale(scale: float | str) -> None:
    """Write ui_scale to INI. Value is clamped to [0.5, 3.0]. Pass 'auto' for automatic."""
    global _ui_scale
    if isinstance(scale, str) and scale.strip().lower() == _INI_AUTO:
        _ui_scale = detect_hidpi_scale()
        scale_str = _INI_AUTO
    else:
        _ui_scale = _clamp(float(scale))
        scale_str = str(_ui_scale)
    _write_ini(get_ui_config_path(), scale_str)


def get_ui_scale() -> float:
    """Return the current ui_scale (call load_ui_scale first at startup)."""
    return _ui_scale


def load_font_family() -> str:
    """Load font_family from INI. Returns the value, or the default if unset."""
    global _font_family
    path = get_ui_config_path()
    if not path.is_file():
        return _font_family
    try:
        parser = configparser.ConfigParser()
        parser.read(path)
        value = parser.get(_INI_SECTION, _INI_FONT_OPTION, fallback="").strip()
        _font_family = value if value else _DEFAULT_FONT_FAMILY
    except Exception:
        pass
    return _font_family


def save_font_family(family: str) -> None:
    """Persist font_family to amethyst.ini [ui] section."""
    global _font_family
    _font_family = family.strip() or _DEFAULT_FONT_FAMILY
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = configparser.ConfigParser()
    if path.is_file():
        parser.read(path)
    if _INI_SECTION not in parser:
        parser[_INI_SECTION] = {}
    parser[_INI_SECTION][_INI_FONT_OPTION] = _font_family
    with path.open("w") as f:
        parser.write(f)


def get_font_family() -> str:
    """Return the current font family (call load_font_family first at startup)."""
    return _font_family


def _clamp(value: float) -> float:
    return max(_MIN_SCALE, min(_MAX_SCALE, value))


# ---------------------------------------------------------------------------
# Collection settings
# ---------------------------------------------------------------------------
_COLLECTIONS_SECTION = "collections"

_DEFAULT_DOWNLOAD_ORDER = "largest"   # "largest" | "smallest"
_DEFAULT_MAX_CONCURRENT = 3


def load_collection_settings() -> dict:
    """Return collection settings dict with keys: download_order, max_concurrent, check_download_locations."""
    path = get_ui_config_path()
    defaults = {
        "download_order": _DEFAULT_DOWNLOAD_ORDER,
        "max_concurrent": _DEFAULT_MAX_CONCURRENT,
        "check_download_locations": True,
    }
    if not path.is_file():
        return defaults
    try:
        parser = configparser.ConfigParser()
        parser.read(path)
        if not parser.has_section(_COLLECTIONS_SECTION):
            return defaults
        s = parser[_COLLECTIONS_SECTION]
        download_order = s.get("download_order", _DEFAULT_DOWNLOAD_ORDER).strip().lower()
        if download_order not in ("largest", "smallest"):
            download_order = _DEFAULT_DOWNLOAD_ORDER
        max_concurrent = int(s.get("max_concurrent", str(_DEFAULT_MAX_CONCURRENT)))
        max_concurrent = max(1, min(5, max_concurrent))
        check_download_locations = s.getboolean("check_download_locations", True)
        return {
            "download_order": download_order,
            "max_concurrent": max_concurrent,
            "check_download_locations": check_download_locations,
        }
    except Exception:
        return defaults


def save_collection_settings(download_order: str, max_concurrent: int,
                              check_download_locations: bool = True) -> None:
    """Persist collection settings to amethyst.ini."""
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = configparser.ConfigParser()
    if path.is_file():
        parser.read(path)
    if _COLLECTIONS_SECTION not in parser:
        parser[_COLLECTIONS_SECTION] = {}
    parser[_COLLECTIONS_SECTION]["download_order"] = download_order
    parser[_COLLECTIONS_SECTION]["max_concurrent"] = str(max(1, min(5, max_concurrent)))
    parser[_COLLECTIONS_SECTION]["check_download_locations"] = "true" if check_download_locations else "false"
    with path.open("w") as f:
        parser.write(f)


# ---------------------------------------------------------------------------
# Nexus browser settings
# ---------------------------------------------------------------------------
_NEXUS_SECTION = "nexus"


def load_nexus_show_adult() -> bool:
    """Return the persisted show_adult setting (default False)."""
    path = get_ui_config_path()
    if not path.is_file():
        return False
    try:
        parser = configparser.ConfigParser()
        parser.read(path)
        return parser.getboolean(_NEXUS_SECTION, "show_adult", fallback=False)
    except Exception:
        return False


_COLUMNS_SECTION = "columns"
_WINDOW_SECTION = "window"


def load_column_widths() -> dict[int, int]:
    """Load saved column width overrides from amethyst.ini. Returns {col_index: width}."""
    path = get_ui_config_path()
    if not path.is_file():
        return {}
    try:
        parser = configparser.ConfigParser()
        parser.read(path)
        if _COLUMNS_SECTION not in parser:
            return {}
        result = {}
        for key, val in parser[_COLUMNS_SECTION].items():
            try:
                result[int(key)] = int(val)
            except (ValueError, TypeError):
                pass
        return result
    except Exception:
        return {}


def save_column_widths(widths: dict[int, int]) -> None:
    """Persist column width overrides to amethyst.ini."""
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = configparser.ConfigParser()
    if path.is_file():
        parser.read(path)
    # Preserve column order/hidden/sort keys across the section overwrite
    existing_order = parser.get(_COLUMNS_SECTION, "order", fallback=None)
    existing_hidden = parser.get(_COLUMNS_SECTION, "hidden", fallback=None)
    existing_sort_col = parser.get(_COLUMNS_SECTION, "sort_column", fallback=None)
    existing_sort_asc = parser.get(_COLUMNS_SECTION, "sort_ascending", fallback=None)
    parser[_COLUMNS_SECTION] = {str(k): str(v) for k, v in widths.items()}
    if existing_order:
        parser[_COLUMNS_SECTION]["order"] = existing_order
    if existing_hidden is not None:
        parser[_COLUMNS_SECTION]["hidden"] = existing_hidden
    if existing_sort_col is not None:
        parser[_COLUMNS_SECTION]["sort_column"] = existing_sort_col
    if existing_sort_asc is not None:
        parser[_COLUMNS_SECTION]["sort_ascending"] = existing_sort_asc
    with path.open("w") as f:
        parser.write(f)


_DEFAULT_COL_ORDER = [2, 3, 4, 5, 6, 7]  # category, flags, conflicts, installed, priority, version


def load_column_order() -> list[int]:
    """Load saved column display order from amethyst.ini. Returns list of data col indices [2..6]."""
    path = get_ui_config_path()
    if not path.is_file():
        return list(_DEFAULT_COL_ORDER)
    try:
        parser = configparser.ConfigParser()
        parser.read(path)
        raw = parser.get(_COLUMNS_SECTION, "order", fallback=None)
        if raw is None:
            return list(_DEFAULT_COL_ORDER)
        order = [int(x) for x in raw.split(",")]
        # Drop unknown ids, de-dup, then append any new defaults the user hasn't seen yet.
        seen: set[int] = set()
        cleaned: list[int] = []
        for x in order:
            if x in _DEFAULT_COL_ORDER and x not in seen:
                cleaned.append(x)
                seen.add(x)
        for x in _DEFAULT_COL_ORDER:
            if x not in seen:
                cleaned.append(x)
                seen.add(x)
        return cleaned if cleaned else list(_DEFAULT_COL_ORDER)
    except Exception:
        return list(_DEFAULT_COL_ORDER)


def save_column_order(order: list[int]) -> None:
    """Persist column display order to amethyst.ini."""
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = configparser.ConfigParser()
    if path.is_file():
        parser.read(path)
    if _COLUMNS_SECTION not in parser:
        parser[_COLUMNS_SECTION] = {}
    parser[_COLUMNS_SECTION]["order"] = ",".join(str(x) for x in order)
    with path.open("w") as f:
        parser.write(f)


def load_column_hidden() -> set[int]:
    """Load hidden column indices from amethyst.ini. Returns set of data col indices."""
    path = get_ui_config_path()
    if not path.is_file():
        return set()
    try:
        parser = configparser.ConfigParser()
        parser.read(path)
        raw = parser.get(_COLUMNS_SECTION, "hidden", fallback=None)
        if not raw:
            return set()
        return {int(x) for x in raw.split(",") if x.strip()}
    except Exception:
        return set()


def save_column_hidden(hidden: set[int]) -> None:
    """Persist hidden column indices to amethyst.ini."""
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = configparser.ConfigParser()
    if path.is_file():
        parser.read(path)
    if _COLUMNS_SECTION not in parser:
        parser[_COLUMNS_SECTION] = {}
    parser[_COLUMNS_SECTION]["hidden"] = ",".join(str(x) for x in sorted(hidden))
    with path.open("w") as f:
        parser.write(f)


def load_sort_state() -> tuple[str | None, bool]:
    """Load saved sort column and direction from amethyst.ini.
    Returns (sort_column, ascending) where sort_column is None if no sort is active."""
    path = get_ui_config_path()
    if not path.is_file():
        return None, True
    try:
        parser = configparser.ConfigParser()
        parser.read(path)
        col = parser.get(_COLUMNS_SECTION, "sort_column", fallback=None)
        if col == "none":
            col = None
        asc = parser.get(_COLUMNS_SECTION, "sort_ascending", fallback="true").lower() == "true"
        return col, asc
    except Exception:
        return None, True


def save_sort_state(sort_column: str | None, ascending: bool) -> None:
    """Persist sort column and direction to amethyst.ini."""
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = configparser.ConfigParser()
    if path.is_file():
        parser.read(path)
    if _COLUMNS_SECTION not in parser:
        parser[_COLUMNS_SECTION] = {}
    parser[_COLUMNS_SECTION]["sort_column"] = sort_column if sort_column is not None else "none"
    parser[_COLUMNS_SECTION]["sort_ascending"] = "true" if ascending else "false"
    with path.open("w") as f:
        parser.write(f)


def load_window_geometry() -> str | None:
    """Load saved window geometry string (WxH+X+Y) from amethyst.ini."""
    path = get_ui_config_path()
    if not path.is_file():
        return None
    try:
        parser = configparser.ConfigParser()
        parser.read(path)
        return parser.get(_WINDOW_SECTION, "geometry", fallback=None)
    except Exception:
        return None


def save_window_geometry(geometry: str) -> None:
    """Persist window geometry string to amethyst.ini."""
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = configparser.ConfigParser()
    if path.is_file():
        parser.read(path)
    if _WINDOW_SECTION not in parser:
        parser[_WINDOW_SECTION] = {}
    parser[_WINDOW_SECTION]["geometry"] = geometry
    with path.open("w") as f:
        parser.write(f)


# ---------------------------------------------------------------------------
# Dev mode
# ---------------------------------------------------------------------------
_DEV_SECTION = "dev"


def load_dev_mode() -> bool:
    """Return True if [dev] devmode = true is set in amethyst.ini."""
    path = get_ui_config_path()
    if not path.is_file():
        return False
    try:
        parser = configparser.ConfigParser()
        parser.read(path)
        return parser.get(_DEV_SECTION, "devmode", fallback="false").strip().lower() == "true"
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Folder case normalisation setting
# ---------------------------------------------------------------------------
_FILEMAP_SECTION = "filemap"


def load_normalize_folder_case() -> bool:
    """Return the global normalize_folder_case setting (default True)."""
    path = get_ui_config_path()
    if not path.is_file():
        return True
    try:
        parser = configparser.ConfigParser()
        parser.read(path)
        return parser.getboolean(_FILEMAP_SECTION, "normalize_folder_case", fallback=True)
    except Exception:
        return True


def save_normalize_folder_case(value: bool) -> None:
    """Persist the normalize_folder_case setting to amethyst.ini."""
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = configparser.ConfigParser()
    if path.is_file():
        parser.read(path)
    if _FILEMAP_SECTION not in parser:
        parser[_FILEMAP_SECTION] = {}
    parser[_FILEMAP_SECTION]["normalize_folder_case"] = "true" if value else "false"
    with path.open("w") as f:
        parser.write(f)


def load_clear_archive_after_install() -> bool:
    """Return the clear_archive_after_install setting (default True)."""
    path = get_ui_config_path()
    if not path.is_file():
        return True
    try:
        parser = configparser.ConfigParser()
        parser.read(path)
        return parser.getboolean(_FILEMAP_SECTION, "clear_archive_after_install", fallback=True)
    except Exception:
        return True


def save_clear_archive_after_install(value: bool) -> None:
    """Persist the clear_archive_after_install setting to amethyst.ini."""
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = configparser.ConfigParser()
    if path.is_file():
        parser.read(path)
    if _FILEMAP_SECTION not in parser:
        parser[_FILEMAP_SECTION] = {}
    parser[_FILEMAP_SECTION]["clear_archive_after_install"] = "true" if value else "false"
    with path.open("w") as f:
        parser.write(f)


def load_keep_fomod_archives() -> bool:
    """Return the keep_fomod_archives setting (default False).

    When True, archives of mods that use a FOMOD installer are always kept
    regardless of the clear_archive_after_install setting.
    """
    path = get_ui_config_path()
    if not path.is_file():
        return False
    try:
        parser = configparser.ConfigParser()
        parser.read(path)
        return parser.getboolean(_FILEMAP_SECTION, "keep_fomod_archives", fallback=False)
    except Exception:
        return False


def save_keep_fomod_archives(value: bool) -> None:
    """Persist the keep_fomod_archives setting to amethyst.ini."""
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = configparser.ConfigParser()
    if path.is_file():
        parser.read(path)
    if _FILEMAP_SECTION not in parser:
        parser[_FILEMAP_SECTION] = {}
    parser[_FILEMAP_SECTION]["keep_fomod_archives"] = "true" if value else "false"
    with path.open("w") as f:
        parser.write(f)


def load_rename_mod_after_install() -> bool:
    """Return the rename_mod_after_install setting (default False).

    When True, a rename prompt is shown after each (non-collection) mod install.
    """
    path = get_ui_config_path()
    if not path.is_file():
        return False
    try:
        parser = configparser.ConfigParser()
        parser.read(path)
        return parser.getboolean(_FILEMAP_SECTION, "rename_mod_after_install", fallback=False)
    except Exception:
        return False


def save_rename_mod_after_install(value: bool) -> None:
    """Persist the rename_mod_after_install setting to amethyst.ini."""
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = configparser.ConfigParser()
    if path.is_file():
        parser.read(path)
    if _FILEMAP_SECTION not in parser:
        parser[_FILEMAP_SECTION] = {}
    parser[_FILEMAP_SECTION]["rename_mod_after_install"] = "true" if value else "false"
    with path.open("w") as f:
        parser.write(f)


def save_nexus_show_adult(value: bool) -> None:
    """Persist the show_adult setting to amethyst.ini."""
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = configparser.ConfigParser()
    if path.is_file():
        parser.read(path)
    if _NEXUS_SECTION not in parser:
        parser[_NEXUS_SECTION] = {}
    parser[_NEXUS_SECTION]["show_adult"] = "true" if value else "false"
    with path.open("w") as f:
        parser.write(f)


# ---------------------------------------------------------------------------
# Custom launcher paths
# ---------------------------------------------------------------------------
_PATHS_SECTION = "paths"


def load_heroic_config_path() -> str:
    """Return the user-configured Heroic config directory path, or '' if unset."""
    path = get_ui_config_path()
    if not path.is_file():
        return ""
    try:
        parser = configparser.ConfigParser()
        parser.read(path)
        return parser.get(_PATHS_SECTION, "heroic_config_path", fallback="").strip()
    except Exception:
        return ""


def save_heroic_config_path(value: str) -> None:
    """Persist the Heroic config directory path to amethyst.ini. Pass '' to clear."""
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = configparser.ConfigParser()
    if path.is_file():
        parser.read(path)
    if _PATHS_SECTION not in parser:
        parser[_PATHS_SECTION] = {}
    parser[_PATHS_SECTION]["heroic_config_path"] = value.strip()
    with path.open("w") as f:
        parser.write(f)


def load_steam_libraries_vdf_path() -> str:
    """Return the user-configured path to Steam's libraryfolders.vdf, or '' if unset."""
    path = get_ui_config_path()
    if not path.is_file():
        return ""
    try:
        parser = configparser.ConfigParser()
        parser.read(path)
        return parser.get(_PATHS_SECTION, "steam_libraries_vdf", fallback="").strip()
    except Exception:
        return ""


def save_steam_libraries_vdf_path(value: str) -> None:
    """Persist the Steam libraryfolders.vdf path to amethyst.ini. Pass '' to clear."""
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = configparser.ConfigParser()
    if path.is_file():
        parser.read(path)
    if _PATHS_SECTION not in parser:
        parser[_PATHS_SECTION] = {}
    parser[_PATHS_SECTION]["steam_libraries_vdf"] = value.strip()
    with path.open("w") as f:
        parser.write(f)
