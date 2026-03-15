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
        root.destroy()
    except Exception:
        return 0, 0, _DEFAULT_SCALE
    if w <= 0 or h <= 0:
        return w, h, _DEFAULT_SCALE
    # UI designed for Steam Deck (1280x800). Use height only; 800–1080 = 1.0.
    if h <= 800:
        scale = max(_MIN_SCALE, h / 800)
    elif h >= 1080:
        scale = min(1.5, h / 1080)
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
