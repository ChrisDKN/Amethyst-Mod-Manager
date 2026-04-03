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

_ui_scale: float = _DEFAULT_SCALE


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


def _clamp(value: float) -> float:
    return max(_MIN_SCALE, min(_MAX_SCALE, value))


# ---------------------------------------------------------------------------
# Collection settings
# ---------------------------------------------------------------------------
_COLLECTIONS_SECTION = "collections"

_DEFAULT_DOWNLOAD_ORDER = "largest"   # "largest" | "smallest"
_DEFAULT_MAX_CONCURRENT = 3


def load_collection_settings() -> dict:
    """Return collection settings dict with keys: download_order, max_concurrent."""
    path = get_ui_config_path()
    defaults = {
        "download_order": _DEFAULT_DOWNLOAD_ORDER,
        "max_concurrent": _DEFAULT_MAX_CONCURRENT,
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
        return {
            "download_order": download_order,
            "max_concurrent": max_concurrent,
        }
    except Exception:
        return defaults


def save_collection_settings(download_order: str, max_concurrent: int) -> None:
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
    # Preserve column order key across the section overwrite
    existing_order = parser.get(_COLUMNS_SECTION, "order", fallback=None)
    parser[_COLUMNS_SECTION] = {str(k): str(v) for k, v in widths.items()}
    if existing_order:
        parser[_COLUMNS_SECTION]["order"] = existing_order
    with path.open("w") as f:
        parser.write(f)


_DEFAULT_COL_ORDER = [2, 3, 4, 5, 6]  # category, flags, conflicts, installed, priority


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
        if sorted(order) == sorted(_DEFAULT_COL_ORDER):
            return order
        return list(_DEFAULT_COL_ORDER)
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
