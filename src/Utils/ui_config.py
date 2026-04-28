"""
UI scaling configuration stored in ~/.config/AmethystModManager/amethyst.ini.

Users can set ui_scale (e.g. 1.0, 1.25, 1.5, 2.0) for HiDPI displays.
Set scale=auto to use automatic scaling based on screen size.
"""

import configparser
import os
import re as _re
import subprocess
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


def _get_portal_scale() -> float:
    """Read the DE scale via the XDG Settings portal.

    Works inside Flatpak sandboxes and on Wayland where xrandr / kscreen-doctor
    are absent.  Reads ``org.gnome.desktop.interface`` because every major
    portal backend (xdg-desktop-portal-gnome/-kde/-hyprland/-wlr) exposes
    ``scaling-factor`` and ``text-scaling-factor`` under that namespace
    regardless of the actual DE.  Returns the larger of the two, or 1.0 if the
    portal is unreachable.
    """
    def _read(key: str) -> str:
        # Use gdbus if available, fall back to dbus-send — one of the two
        # ships with essentially every Linux distro and Flatpak runtime.
        for cmd in (
            ["gdbus", "call", "--session",
             "--dest", "org.freedesktop.portal.Desktop",
             "--object-path", "/org/freedesktop/portal/desktop",
             "--method", "org.freedesktop.portal.Settings.Read",
             "org.gnome.desktop.interface", key],
            ["dbus-send", "--session", "--print-reply=literal",
             "--dest=org.freedesktop.portal.Desktop",
             "/org/freedesktop/portal/desktop",
             "org.freedesktop.portal.Settings.Read",
             "string:org.gnome.desktop.interface", f"string:{key}"],
        ):
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=2)
                if r.returncode == 0 and r.stdout.strip():
                    return r.stdout
            except Exception:
                continue
        return ""

    scale = 1.0
    # Integer scaling-factor (uint32): 0 or 1 → no scaling, ≥2 → HiDPI
    raw = _read("scaling-factor")
    m = _re.search(r"uint32\s+(\d+)", raw) or _re.search(r"\b(\d+)\b", raw)
    if m:
        try:
            v = int(m.group(1))
            if v >= 2:
                scale = max(scale, float(v))
        except ValueError:
            pass
    # Fractional text-scaling-factor (double), e.g. 1.25
    raw = _read("text-scaling-factor")
    m = _re.search(r"([0-9]+\.[0-9]+)", raw)
    if m:
        try:
            v = float(m.group(1))
            if v > 1.0:
                scale = max(scale, v)
        except ValueError:
            pass
    return scale


def _get_compositor_scale() -> float:
    """Return the display compositor's global scale factor (>1.0 on HiDPI).

    Tries, in order:
      1. XDG Settings portal (Flatpak-safe, works on Wayland & fractional)
      2. kscreen-doctor  (KDE Plasma 6 — per-output scale from compositor)
      3. gsettings       (GNOME — integer scaling-factor)
      4. GDK_SCALE / QT_SCALE_FACTOR / GDK_DPI_SCALE environment variables

    Returns 1.0 if nothing is detected or all sources fail.
    """
    portal = _get_portal_scale()
    if portal > 1.0:
        return portal

    # KDE Plasma 6: per-output scale lives in the compositor; kscreen-doctor
    # exposes it.  Output contains ANSI colour codes so strip those first.
    try:
        r = subprocess.run(
            ["kscreen-doctor", "-o"],
            capture_output=True, text=True, timeout=3,
        )
        clean = _re.sub(r"\x1b\[[0-9;]*m", "", r.stdout)
        scales = [float(m.group(1)) for m in _re.finditer(r"Scale:\s*([\d.]+)", clean)]
        if scales:
            return max(1.0, max(scales))
    except Exception:
        pass

    # GNOME: integer scaling-factor (fractional scaling is not exposed here,
    # but integer scaling is still better than nothing).  Output looks like
    # "uint32 2" — anchor on the type prefix so the regex doesn't match the
    # "32" inside "uint32".
    try:
        r = subprocess.run(
            ["gsettings", "get", "org.gnome.desktop.interface", "scaling-factor"],
            capture_output=True, text=True, timeout=2,
        )
        m = _re.search(r"uint32\s+(\d+)", r.stdout)
        if m and int(m.group(1)) > 1:
            return float(int(m.group(1)))
    except Exception:
        pass

    # Environment variables set by some DEs / launch wrappers
    env_scale = 1.0
    for var in ("GDK_SCALE", "QT_SCALE_FACTOR"):
        try:
            v = os.environ.get(var, "").strip()
            if v:
                f = float(v)
                if f > 1.0:
                    env_scale = max(env_scale, f)
        except Exception:
            pass
    # GDK_DPI_SCALE is a fractional multiplier applied on top of GDK_SCALE
    try:
        v = os.environ.get("GDK_DPI_SCALE", "").strip()
        if v:
            f = float(v)
            if f > 1.0:
                env_scale *= f
    except Exception:
        pass
    if env_scale > 1.0:
        return env_scale

    return 1.0


def _get_primary_monitor_size() -> tuple[int, int]:
    """Return (width, height) of the primary monitor.

    On multi-monitor setups winfo_screenwidth/height returns the combined
    virtual desktop size, which inflates the auto-detected UI scale.  This
    tries xrandr first (X11), then wlr-randr (Wayland on wlroots compositors
    like sway/Hyprland/labwc).  Returns (0, 0) if both are unavailable or
    parsing fails.
    """
    try:
        result = subprocess.run(
            ["xrandr", "--current"],
            capture_output=True, text=True, timeout=3,
        )
        lines = result.stdout.splitlines()
        # Prefer the monitor explicitly marked "primary"
        for line in lines:
            if " connected " in line and "primary" in line:
                m = _re.search(r"(\d+)x(\d+)\+\d+\+\d+", line)
                if m:
                    return int(m.group(1)), int(m.group(2))
        # Fall back to the first connected monitor with a geometry
        for line in lines:
            if " connected " in line:
                m = _re.search(r"(\d+)x(\d+)\+\d+\+\d+", line)
                if m:
                    return int(m.group(1)), int(m.group(2))
    except Exception:
        pass

    # wlr-randr output: per-monitor blocks with "  1920x1080 px, 60.000000 Hz (preferred, current)"
    try:
        result = subprocess.run(
            ["wlr-randr"],
            capture_output=True, text=True, timeout=3,
        )
        for line in result.stdout.splitlines():
            m = _re.search(r"(\d+)x(\d+) px.*\bcurrent\b", line)
            if m:
                return int(m.group(1)), int(m.group(2))
    except Exception:
        pass

    return 0, 0


def get_screen_info() -> tuple[int, int, float]:
    """Return (screen_width, screen_height, detected_scale) for the primary display."""
    try:
        import tkinter as _tk
        root = _tk.Tk()
        root.withdraw()
        root.update_idletasks()
        w = root.winfo_screenwidth()
        h = root.winfo_screenheight()
        # winfo_fpixels('1i') returns pixels-per-inch as seen by Tk; higher
        # than 96 means the DE is applying its own scaling.
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

    # XDG Settings portal gives an authoritative scale on every backend that
    # supports fractional scaling (GNOME/KDE/wlroots).  When it reports a
    # value, trust it and skip the brittle "derive from monitor height" path —
    # that path exists only for compositors that don't tell us their scale.
    portal = _get_portal_scale()
    if portal > 1.0:
        scale = round(min(_MAX_SCALE, portal) * 20) / 20
        return w, h, scale

    # Xft.dpi may already have been overridden to 96 (by a previous launch),
    # hiding the true compositor scale.  Read it directly from the DE and use
    # whichever value is larger.
    de_scale = max(de_scale, _get_compositor_scale())

    # On multi-monitor setups winfo_screenwidth/height is the combined virtual
    # desktop — use xrandr to get just the primary monitor's physical size.
    # xrandr reports unscaled physical pixels, so we divide by de_scale.
    # When xrandr is unavailable (e.g. Flatpak sandbox without host xrandr),
    # Tk's winfo_screenheight on Wayland/XWayland typically reports the
    # logical (already-scaled) size, so dividing again would halve the scale.
    pm_w, pm_h = _get_primary_monitor_size()
    if pm_h > 0:
        w, h = pm_w, pm_h
        physical_h = h / de_scale if de_scale > 1.0 else h
    else:
        physical_h = h
    # UI designed for Steam Deck (1280x800). Use height only; ≤1080 = 1.0.
    # Never auto-scale below 1.0: detection is unreliable enough on Wayland /
    # Flatpak / multi-monitor that a sub-1.0 result is almost always wrong —
    # users with a genuinely tiny screen can still pick one manually.
    if physical_h >= 1080:
        scale = min(2.0, physical_h / 1080)
    else:
        scale = 1.0
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
        _seed_first_run_defaults(path)
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


def _seed_first_run_defaults(path: Path) -> None:
    """Write first-run-only defaults for collections and hidden columns.

    Called exactly once, when the INI file is first created. Existing installs
    never run this code path so their behaviour is unchanged.
    """
    try:
        parser = configparser.ConfigParser()
        if path.is_file():
            parser.read(path)
        if _COLLECTIONS_SECTION not in parser:
            parser[_COLLECTIONS_SECTION] = {}
        parser[_COLLECTIONS_SECTION]["download_order"] = _FIRST_RUN_DOWNLOAD_ORDER
        parser[_COLLECTIONS_SECTION]["max_concurrent"] = str(_FIRST_RUN_MAX_CONCURRENT)
        parser[_COLLECTIONS_SECTION]["max_extract_workers"] = str(_FIRST_RUN_MAX_EXTRACT_WORKERS)
        if _COLUMNS_SECTION not in parser:
            parser[_COLUMNS_SECTION] = {}
        parser[_COLUMNS_SECTION]["hidden"] = ",".join(str(x) for x in _FIRST_RUN_HIDDEN_COLUMNS)
        with path.open("w") as f:
            parser.write(f)
    except Exception:
        pass


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
_DEFAULT_MAX_EXTRACT_WORKERS = 4

# First-run defaults — written to the INI only when it is being created for
# the first time (see load_ui_scale). Existing installs keep whatever defaults
# they had even if they have never saved these settings explicitly.
_FIRST_RUN_DOWNLOAD_ORDER = "smallest"
_FIRST_RUN_MAX_CONCURRENT = 8
_FIRST_RUN_MAX_EXTRACT_WORKERS = 8
_FIRST_RUN_HIDDEN_COLUMNS = [2, 5]  # category, installed


def load_collection_settings() -> dict:
    """Return collection settings dict with keys: download_order, max_concurrent, max_extract_workers, check_download_locations, clear_archive_after_install."""
    path = get_ui_config_path()
    defaults = {
        "download_order": _DEFAULT_DOWNLOAD_ORDER,
        "max_concurrent": _DEFAULT_MAX_CONCURRENT,
        "max_extract_workers": _DEFAULT_MAX_EXTRACT_WORKERS,
        "check_download_locations": True,
        "clear_archive_after_install": False,
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
        max_concurrent = max(1, min(8, max_concurrent))
        max_extract_workers = int(s.get("max_extract_workers", str(_DEFAULT_MAX_EXTRACT_WORKERS)))
        max_extract_workers = max(1, min(8, max_extract_workers))
        check_download_locations = s.getboolean("check_download_locations", True)
        clear_archive_after_install = s.getboolean("clear_archive_after_install", False)
        return {
            "download_order": download_order,
            "max_concurrent": max_concurrent,
            "max_extract_workers": max_extract_workers,
            "check_download_locations": check_download_locations,
            "clear_archive_after_install": clear_archive_after_install,
        }
    except Exception:
        return defaults


def save_collection_settings(download_order: str, max_concurrent: int,
                              check_download_locations: bool = True,
                              clear_archive_after_install: bool = False,
                              max_extract_workers: int = _DEFAULT_MAX_EXTRACT_WORKERS) -> None:
    """Persist collection settings to amethyst.ini."""
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = configparser.ConfigParser()
    if path.is_file():
        parser.read(path)
    if _COLLECTIONS_SECTION not in parser:
        parser[_COLLECTIONS_SECTION] = {}
    parser[_COLLECTIONS_SECTION]["download_order"] = download_order
    parser[_COLLECTIONS_SECTION]["max_concurrent"] = str(max(1, min(8, max_concurrent)))
    parser[_COLLECTIONS_SECTION]["max_extract_workers"] = str(max(1, min(8, max_extract_workers)))
    parser[_COLLECTIONS_SECTION]["check_download_locations"] = "true" if check_download_locations else "false"
    parser[_COLLECTIONS_SECTION]["clear_archive_after_install"] = "true" if clear_archive_after_install else "false"
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


def load_force_manual_install() -> bool:
    """Return True if [dev] force_manual_install = true is set in amethyst.ini.

    When True, collection installs use the non-premium manual-download flow
    regardless of the user's actual Nexus premium status.
    """
    path = get_ui_config_path()
    if not path.is_file():
        return False
    try:
        parser = configparser.ConfigParser()
        parser.read(path)
        return parser.get(_DEV_SECTION, "force_manual_install", fallback="false").strip().lower() == "true"
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


# ---------------------------------------------------------------------------
# App update channel setting
# ---------------------------------------------------------------------------
_UPDATES_SECTION = "updates"


def load_allow_prerelease() -> bool:
    """Return the allow_prerelease setting (default False)."""
    path = get_ui_config_path()
    if not path.is_file():
        return False
    try:
        parser = configparser.ConfigParser()
        parser.read(path)
        return parser.getboolean(_UPDATES_SECTION, "allow_prerelease", fallback=False)
    except Exception:
        return False


def save_allow_prerelease(value: bool) -> None:
    """Persist the allow_prerelease setting to amethyst.ini under [updates]."""
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = configparser.ConfigParser()
    if path.is_file():
        parser.read(path)
    if _UPDATES_SECTION not in parser:
        parser[_UPDATES_SECTION] = {}
    parser[_UPDATES_SECTION]["allow_prerelease"] = "true" if value else "false"
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


def load_restore_on_close() -> bool:
    """Return the restore_on_close setting (default False).

    When True, every configured game with active deployment is restored to
    vanilla when the application window is closed.
    """
    path = get_ui_config_path()
    if not path.is_file():
        return False
    try:
        parser = configparser.ConfigParser()
        parser.read(path)
        return parser.getboolean(_FILEMAP_SECTION, "restore_on_close", fallback=False)
    except Exception:
        return False


def save_restore_on_close(value: bool) -> None:
    """Persist the restore_on_close setting to amethyst.ini."""
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = configparser.ConfigParser()
    if path.is_file():
        parser.read(path)
    if _FILEMAP_SECTION not in parser:
        parser[_FILEMAP_SECTION] = {}
    parser[_FILEMAP_SECTION]["restore_on_close"] = "true" if value else "false"
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


def load_default_staging_path() -> str:
    """Return the user-configured default mod staging folder, or '' if unset.

    When set, adding a new game uses ``<this>/<game_name>`` as its mod staging
    folder instead of the built-in default (~/.config/AmethystModManager/Profiles).
    """
    path = get_ui_config_path()
    if not path.is_file():
        return ""
    try:
        parser = configparser.ConfigParser()
        parser.read(path)
        return parser.get(_PATHS_SECTION, "default_staging_path", fallback="").strip()
    except Exception:
        return ""


def save_default_staging_path(value: str) -> None:
    """Persist the default mod staging folder to amethyst.ini. Pass '' to clear."""
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = configparser.ConfigParser()
    if path.is_file():
        parser.read(path)
    if _PATHS_SECTION not in parser:
        parser[_PATHS_SECTION] = {}
    parser[_PATHS_SECTION]["default_staging_path"] = value.strip()
    with path.open("w") as f:
        parser.write(f)


def load_download_cache_path() -> str:
    """Return the user-configured download cache root, or '' if unset.

    When set, archives downloaded for any game are stored under
    ``<this>/<game name>/`` instead of the built-in default
    (~/.config/AmethystModManager/download_cache).
    """
    path = get_ui_config_path()
    if not path.is_file():
        return ""
    try:
        parser = configparser.ConfigParser()
        parser.read(path)
        return parser.get(_PATHS_SECTION, "download_cache_path", fallback="").strip()
    except Exception:
        return ""


def save_download_cache_path(value: str) -> None:
    """Persist the download cache root to amethyst.ini. Pass '' to clear."""
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = configparser.ConfigParser()
    if path.is_file():
        parser.read(path)
    if _PATHS_SECTION not in parser:
        parser[_PATHS_SECTION] = {}
    parser[_PATHS_SECTION]["download_cache_path"] = value.strip()
    with path.open("w") as f:
        parser.write(f)


# ---------------------------------------------------------------------------
# Theme colours
# ---------------------------------------------------------------------------
_THEME_SECTION = "theme"

THEME_DEFAULTS: dict[str, str] = {
    "conflict_higher":    "#108d00",
    "conflict_lower":     "#9a0e0e",
    "plugin_mod":         "#A45500",
    "plugin_separator":   "#A45500",
    "conflict_separator": "#5A5A5A",
    "separator_bg":       "#3E3E3E",
}

# Keys whose default should swap per appearance mode. Each non-dark theme
# contributes its own overrides via a THEME_DEFAULTS_OVERRIDE dict in its
# theme file (src/gui/themes/<mode>.py); ui_config loads them lazily so the
# Utils package doesn't import the gui package at import time.
#
# User overrides in [theme] of amethyst.ini always win — except when a
# saved value exactly matches the dark default for a key that the current
# theme overrides; that's treated as legacy/uncustomised so existing ini
# files don't strand users with dark separators on a light or cyberpunk UI.
_theme_defaults_override_cache: dict[str, dict[str, str]] = {}


def _theme_defaults_override_for(mode: str) -> dict[str, str]:
    """Return {key: hex} overrides declared by the active theme file.

    Imports gui.themes.<mode> lazily. Missing file or missing dict yields {}.
    Results are cached per mode to keep the ini read path cheap.
    """
    if mode in _theme_defaults_override_cache:
        return _theme_defaults_override_cache[mode]
    result: dict[str, str] = {}
    try:
        import importlib
        mod = importlib.import_module(f"gui.themes.{mode}")
        raw = getattr(mod, "THEME_DEFAULTS_OVERRIDE", None)
        if isinstance(raw, dict):
            result = {k: v for k, v in raw.items() if k in THEME_DEFAULTS and _valid_hex(v)}
    except Exception:
        pass
    _theme_defaults_override_cache[mode] = result
    return result

_HEX_RE = _re.compile(r"^#[0-9A-Fa-f]{6}$")

_theme_colors: dict[str, str] = dict(THEME_DEFAULTS)


def _valid_hex(s: str) -> bool:
    return isinstance(s, str) and bool(_HEX_RE.match(s.strip()))


def load_theme_colors() -> dict[str, str]:
    """Load [theme] from INI, falling back to defaults for missing/invalid values.

    Theme-aware: the active theme (from get_appearance_mode()) can declare
    THEME_DEFAULTS_OVERRIDE in its theme file to replace defaults for
    user-customisable keys. User overrides in [theme] always win — except
    when a saved value exactly matches the original dark default for a key
    the current theme overrides; that's treated as legacy/uncustomised, so
    existing ini files don't strand users with dark separators on a non-dark
    theme.
    """
    global _theme_colors
    mode = get_appearance_mode()
    overrides = _theme_defaults_override_for(mode)
    result = dict(THEME_DEFAULTS)
    result.update(overrides)
    path = get_ui_config_path()
    if path.is_file():
        try:
            parser = configparser.ConfigParser()
            parser.read(path)
            if parser.has_section(_THEME_SECTION):
                for key in THEME_DEFAULTS:
                    raw = parser.get(_THEME_SECTION, key, fallback="").strip()
                    if not _valid_hex(raw):
                        continue
                    if (key in overrides
                            and raw.lower() == THEME_DEFAULTS[key].lower()):
                        continue
                    result[key] = raw
        except Exception:
            pass
    _theme_colors = result
    return _theme_colors


def save_theme_color(key: str, value: str) -> None:
    """Persist a single theme colour under [theme] in amethyst.ini.

    Silently ignores unknown keys or invalid hex values to prevent corruption.
    """
    if key not in THEME_DEFAULTS or not _valid_hex(value):
        return
    value = value.strip()
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = configparser.ConfigParser()
    if path.is_file():
        parser.read(path)
    if _THEME_SECTION not in parser:
        parser[_THEME_SECTION] = {}
    parser[_THEME_SECTION][key] = value
    with path.open("w") as f:
        parser.write(f)
    _theme_colors[key] = value


def get_theme_color(key: str) -> str:
    """Return the current cached value for *key*, or its default if unknown."""
    return _theme_colors.get(key, THEME_DEFAULTS.get(key, "#000000"))


# ---------------------------------------------------------------------------
# Appearance mode — applied at startup, requires restart.
#
# Valid values are theme IDs (filenames) under src/gui/themes/ (e.g. "dark",
# "light"). ui_config doesn't validate against that list to avoid importing
# the gui package; theme.py handles unknown IDs by falling back to dark.
# ---------------------------------------------------------------------------
_APPEARANCE_OPTION = "appearance_mode"
_APPEARANCE_DEFAULT = "dark"
_APPEARANCE_ID_RE = _re.compile(r"^[a-z0-9_][a-z0-9_-]*$")


def get_appearance_mode() -> str:
    """Return the saved appearance-mode theme ID, defaulting to 'dark'."""
    path = get_ui_config_path()
    if not path.is_file():
        return _APPEARANCE_DEFAULT
    try:
        parser = configparser.ConfigParser()
        parser.read(path)
        raw = parser.get(_INI_SECTION, _APPEARANCE_OPTION, fallback=_APPEARANCE_DEFAULT).strip().lower()
        return raw if _APPEARANCE_ID_RE.match(raw) else _APPEARANCE_DEFAULT
    except Exception:
        return _APPEARANCE_DEFAULT


def save_appearance_mode(mode: str) -> None:
    """Persist the appearance mode. Values are normalised to lowercase; any
    string that doesn't match the theme-id regex (lowercase word chars, digits,
    dashes, underscores) is silently rejected."""
    mode = mode.strip().lower()
    if not _APPEARANCE_ID_RE.match(mode):
        return
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = configparser.ConfigParser()
    if path.is_file():
        parser.read(path)
    if _INI_SECTION not in parser:
        parser[_INI_SECTION] = {}
    parser[_INI_SECTION][_APPEARANCE_OPTION] = mode
    with path.open("w") as f:
        parser.write(f)
