"""
UI scaling configuration stored in ~/.config/AmethystModManager/amethyst.ini.

Users can set ui_scale (e.g. 1.0, 1.25, 1.5, 2.0) for HiDPI displays.
Set scale=auto to use automatic scaling based on screen size.
"""

import configparser
import io
import os
import re as _re
import subprocess
import threading
from pathlib import Path

from Utils.atomic_write import write_atomic_text
from Utils.config_paths import get_config_dir

_INI_SECTION = "ui"

# Bumped when amethyst.ini's schema changes incompatibly. On startup,
# ensure_ini_version() wipes a file whose [meta] version differs (or is absent)
# so the Qt build starts from a clean ini - this clears Tk-era ini files when
# users move over.
_META_SECTION = "meta"
_APP_INI_VERSION = 2
# Last-used game + profile (restored at startup).
_SESSION_SECTION = "session"
# First-run onboarding completion flag (missing/0 → show onboarding on launch).
_ONBOARDING_SECTION = "onboarding"
_INI_OPTION = "scale"
_INI_AUTO = "auto"
_DEFAULT_SCALE = 1.0
_MIN_SCALE = 0.5
_MAX_SCALE = 3.0
# Auto-detection caps out lower than the manual ceiling: HiDPI/portal readings
# can be over-eager (e.g. 4K TVs reporting 2.0) and the UI is still legible at
# 1.5x. Users who want larger can still pick it manually.
_AUTO_MAX_SCALE = 1.5

_DEFAULT_FONT_FAMILY = "Noto Sans"
_INI_FONT_OPTION = "font_family"

# Language / locale. Empty string ("") means "follow the system locale"; any
# other value is a locale code (e.g. "en", "de", "fr") matching a compiled
# translations/amethyst_<code>.qm file.
_DEFAULT_LANGUAGE = ""
_INI_LANGUAGE_OPTION = "language"

_ui_scale: float = _DEFAULT_SCALE
_font_family: str = _DEFAULT_FONT_FAMILY
_language: str = _DEFAULT_LANGUAGE


def get_ui_config_path() -> Path:
    """Return the path to the amethyst.ini config file."""
    return get_config_dir() / "amethyst.ini"


def _new_parser() -> "configparser.ConfigParser":
    """A ConfigParser tolerant of a duplicated option/section (last value wins
    instead of raising). amethyst.ini is shared with column_state.py, which uses
    a case-preserving optionxform; a legacy key that differs only in case (e.g.
    ``w_Mod Name`` vs ``w_mod name`` in [qt_columns]) would otherwise make EVERY
    read here raise DuplicateOptionError and break all saves. strict=False lets
    the file still load so settings can be written."""
    return configparser.ConfigParser(strict=False)


# ---------------------------------------------------------------------------
# Shared ini read cache + atomic writer
# ---------------------------------------------------------------------------
# Parsed-parser cache keyed by path, validated by (st_mtime_ns, st_size) so an
# external edit is picked up. Read-only call sites share the cached parser via
# _read_ini and must never mutate it; save paths build their OWN parser from a
# fresh disk read, mutate that, and hand it to _write_ini - which writes
# atomically and refreshes the cache with exactly what was written.
_ini_cache: dict[str, tuple[tuple[int, int], "configparser.ConfigParser"]] = {}
_ini_cache_lock = threading.Lock()


def _ini_stat_key(path: Path) -> tuple[int, int]:
    """(st_mtime_ns, st_size) cache-validity key for *path*."""
    st = path.stat()
    return (st.st_mtime_ns, st.st_size)


def _read_ini(path: Path) -> "configparser.ConfigParser":
    """Cached parse of *path* - treat the returned parser as read-only."""
    try:
        key = _ini_stat_key(path)
    except OSError:
        return _new_parser()
    path_str = str(path)
    with _ini_cache_lock:
        cached = _ini_cache.get(path_str)
        if cached is not None and cached[0] == key:
            return cached[1]
    parser = _new_parser()
    parser.read(path)
    with _ini_cache_lock:
        _ini_cache[path_str] = (key, parser)
    return parser


def _write_ini(parser: "configparser.ConfigParser", path: Path) -> None:
    """Serialize *parser* to *path* atomically and refresh the read cache."""
    buf = io.StringIO()
    parser.write(buf)
    write_atomic_text(path, buf.getvalue())
    path_str = str(path)
    with _ini_cache_lock:
        try:
            _ini_cache[path_str] = (_ini_stat_key(path), parser)
        except OSError:
            _ini_cache.pop(path_str, None)


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
        # Use gdbus if available, fall back to dbus-send - one of the two
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
      2. kscreen-doctor  (KDE Plasma 6 - per-output scale from compositor)
      3. gsettings       (GNOME - integer scaling-factor)
      4. GDK_SCALE / QT_SCALE_FACTOR / GDK_DPI_SCALE environment variables

    Returns 1.0 if nothing is detected or all sources fail.
    """
    portal = _get_portal_scale()
    if portal > 1.0:
        return portal

    # KDE Plasma 6: per-output scale lives in the compositor; kscreen-doctor
    # exposes it.  Output contains ANSI colour codes so strip those first.
    # The binary isn't in Flatpak runtimes, so retry via flatpak-spawn --host
    # (--directory=/ because the sandbox cwd doesn't exist on the host) so
    # Flatpak and AppImage detect the same scale on the same machine.
    out = _run_capture(["kscreen-doctor", "-o"])
    if not out:
        out = _run_capture(["flatpak-spawn", "--host", "--directory=/", "kscreen-doctor", "-o"])
    if out:
        clean = _re.sub(r"\x1b\[[0-9;]*m", "", out)
        scales = [float(m.group(1)) for m in _re.finditer(r"Scale:\s*([\d.]+)", clean)]
        if scales:
            return max(1.0, max(scales))

    # GNOME: integer scaling-factor (fractional scaling is not exposed here,
    # but integer scaling is still better than nothing).  Output looks like
    # "uint32 2" - anchor on the type prefix so the regex doesn't match the
    # "32" inside "uint32".  Inside Flatpak, sandboxed gsettings reads default
    # (empty) dconf, so ask the host when the sandbox copy reports nothing.
    for argv in (
        ["gsettings", "get", "org.gnome.desktop.interface", "scaling-factor"],
        ["flatpak-spawn", "--host", "--directory=/",
         "gsettings", "get", "org.gnome.desktop.interface", "scaling-factor"],
    ):
        out = _run_capture(argv, timeout=2)
        m = _re.search(r"uint32\s+(\d+)", out)
        if m and int(m.group(1)) > 1:
            return float(int(m.group(1)))

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


def _parse_xrandr(stdout: str) -> tuple[int, int]:
    """Pick (w, h) of the 'primary' monitor from xrandr output, else first connected."""
    lines = stdout.splitlines()
    for line in lines:
        if " connected " in line and "primary" in line:
            m = _re.search(r"(\d+)x(\d+)\+\d+\+\d+", line)
            if m:
                return int(m.group(1)), int(m.group(2))
    for line in lines:
        if " connected " in line:
            m = _re.search(r"(\d+)x(\d+)\+\d+\+\d+", line)
            if m:
                return int(m.group(1)), int(m.group(2))
    return 0, 0


def _parse_wlr_randr(stdout: str) -> tuple[int, int]:
    """Pick (w, h) of the first 'current' mode line from wlr-randr output."""
    for line in stdout.splitlines():
        m = _re.search(r"(\d+)x(\d+) px.*\bcurrent\b", line)
        if m:
            return int(m.group(1)), int(m.group(2))
    return 0, 0


def _run_capture(argv: list[str], timeout: int = 3) -> str:
    """Run argv, return stdout on success, '' on any failure."""
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        if r.returncode == 0:
            return r.stdout
    except Exception:
        pass
    return ""


def _get_primary_monitor_size() -> tuple[int, int]:
    """Return (width, height) of the primary monitor.

    On multi-monitor setups winfo_screenwidth/height returns the combined
    virtual desktop size, which inflates the auto-detected UI scale.  Tries
    xrandr (X11), then wlr-randr (wlroots Wayland).  Inside a Flatpak sandbox
    neither binary is in the runtime, so each call is also retried via
    ``flatpak-spawn --host`` so the host's binaries can answer instead.
    """
    # xrandr - sandbox first, then host (--directory=/ because the sandbox
    # cwd /app/... doesn't exist on the host, and the portal Spawn inherits cwd).
    out = _run_capture(["xrandr", "--current"])
    if not out:
        out = _run_capture(["flatpak-spawn", "--host", "--directory=/", "xrandr", "--current"])
    if out:
        wh = _parse_xrandr(out)
        if wh != (0, 0):
            return wh

    # wlr-randr - sandbox first, then host
    out = _run_capture(["wlr-randr"])
    if not out:
        out = _run_capture(["flatpak-spawn", "--host", "--directory=/", "wlr-randr"])
    if out:
        wh = _parse_wlr_randr(out)
        if wh != (0, 0):
            return wh

    return 0, 0


# Toolkit-specific display probe → (width, height, de_scale). Injected by the
# GUI (set_screen_probe) so this module needn't import a GUI toolkit. When
# unset, get_screen_info falls back to the xrandr/wlr-randr + portal path.
_screen_probe: "callable | None" = None


def set_screen_probe(fn: "callable | None") -> None:
    """Register a callable() -> (width, height, de_scale) display probe."""
    global _screen_probe
    _screen_probe = fn


def get_screen_info() -> tuple[int, int, float]:
    """Return (screen_width, screen_height, detected_scale) for the primary display."""
    if _screen_probe is not None:
        try:
            w, h, de_scale = _screen_probe()
        except Exception:
            return 0, 0, _DEFAULT_SCALE
    else:
        # No GUI toolkit attached: derive size from xrandr/wlr-randr and let the
        # portal/compositor path below supply the scale.
        w, h = _get_primary_monitor_size()
        de_scale = 1.0
    if w <= 0 or h <= 0:
        return w, h, _DEFAULT_SCALE

    # Wayland guard: Tk is X11-only, so on Wayland we run under XWayland and
    # most compositors (GNOME always, KDE in "Scaled by the system" mode)
    # already upscale the window. Scaling again on top would double-scale.
    # The reliable tell is Xft.dpi: when the session expects X11 apps to
    # scale *themselves*, the DE raises Xft.dpi (e.g. 192 at 200%); when the
    # compositor scales them, Xft.dpi stays at ~96. So only trust a reported
    # compositor scale if Xft.dpi confirms we are expected to apply it.
    on_wayland = bool(os.environ.get("WAYLAND_DISPLAY"))
    compositor = max(_get_portal_scale(), _get_compositor_scale())
    if on_wayland and compositor > 1.0 and de_scale <= 1.05:
        return w, h, 1.0

    # XDG Settings portal / compositor gives an authoritative scale on every
    # backend that supports fractional scaling (GNOME/KDE/wlroots). When it
    # reports a value, trust it and skip the brittle "derive from monitor
    # height" path - that exists only for setups that don't tell us their
    # scale.
    if compositor > 1.0:
        scale = round(min(_AUTO_MAX_SCALE, compositor) * 20) / 20
        return w, h, scale
    if on_wayland:
        return w, h, 1.0

    # On multi-monitor setups winfo_screenwidth/height is the combined virtual
    # desktop - use xrandr to get just the primary monitor's physical size.
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
    # Resolution-only fallback. A bare resolution is a weak scaling signal -
    # a 27" 1440p desktop monitor at 100% DE scale wants 1.0 - so only kick
    # in above 1600p (e.g. 4K), capped at _AUTO_MAX_SCALE. Never auto-scale
    # below 1.0: detection is unreliable enough on Wayland / Flatpak /
    # multi-monitor that a sub-1.0 result is almost always wrong.
    if physical_h > 1600:
        scale = min(_AUTO_MAX_SCALE, physical_h / 1080)
    else:
        scale = 1.0
    scale = round(scale * 20) / 20  # Snap to nearest 0.05
    return w, h, scale


def detect_hidpi_scale() -> float:
    """Detect suggested UI scale (compositor-reported, else screen height).

    Prefers the DE/compositor's own scale (portal, kscreen-doctor, gsettings,
    env vars), skipped when the Wayland compositor already upscales XWayland.
    Resolution fallback: heights ≤1600 → 1.0; above scales by h/1080, capped
    at _AUTO_MAX_SCALE (1.5x); manual selection can still go higher.
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
        _write_scale_ini(path, _INI_AUTO)
        _seed_first_run_defaults(path)
        return _ui_scale
    try:
        parser = _read_ini(path)
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


def _write_scale_ini(path: Path, scale_str: str) -> None:
    """Write the [ui] scale to amethyst.ini."""
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = _new_parser()
    if path.is_file():
        parser.read(path)
    if _INI_SECTION not in parser:
        parser[_INI_SECTION] = {}
    parser[_INI_SECTION][_INI_OPTION] = scale_str
    _write_ini(parser, path)


def _seed_first_run_defaults(path: Path) -> None:
    """Write first-run-only defaults for collections and hidden columns.

    Called exactly once, when the INI file is first created. Existing installs
    never run this code path so their behaviour is unchanged.
    """
    try:
        parser = _new_parser()
        if path.is_file():
            parser.read(path)
        if _COLLECTIONS_SECTION not in parser:
            parser[_COLLECTIONS_SECTION] = {}
        parser[_COLLECTIONS_SECTION]["max_concurrent"] = str(_FIRST_RUN_MAX_CONCURRENT)
        parser[_COLLECTIONS_SECTION]["max_extract_workers"] = str(_FIRST_RUN_MAX_EXTRACT_WORKERS)
        if _COLUMNS_SECTION not in parser:
            parser[_COLUMNS_SECTION] = {}
        parser[_COLUMNS_SECTION]["hidden"] = ",".join(str(x) for x in _FIRST_RUN_HIDDEN_COLUMNS)
        parser[_COLUMNS_SECTION]["introduced"] = ",".join(str(x) for x in _FIRST_RUN_HIDDEN_COLUMNS)
        _write_ini(parser, path)
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
    _write_scale_ini(get_ui_config_path(), scale_str)


def get_ui_scale() -> float:
    """Return the current ui_scale (call load_ui_scale first at startup)."""
    return _ui_scale


def load_ui_scale_is_auto() -> bool:
    """Return True when [ui] scale is literally 'auto' (or unset → auto default).

    load_ui_scale() resolves 'auto' into a concrete float, so the Settings UI
    needs this to know whether to show the Auto checkbox as ticked. A missing
    file / section / key counts as auto (that is the first-run default written
    by load_ui_scale)."""
    path = get_ui_config_path()
    if not path.is_file():
        return True
    try:
        parser = _read_ini(path)
        raw = parser.get(_INI_SECTION, _INI_OPTION, fallback=_INI_AUTO).strip().lower()
        return raw == _INI_AUTO
    except Exception:
        return True


def load_font_family() -> str:
    """Load font_family from INI. Returns the value, or the default if unset."""
    global _font_family
    path = get_ui_config_path()
    if not path.is_file():
        return _font_family
    try:
        parser = _read_ini(path)
        value = parser.get(_INI_SECTION, _INI_FONT_OPTION, fallback="").strip()
        _font_family = value if value else _DEFAULT_FONT_FAMILY
    except Exception:
        pass
    return _font_family


def load_language() -> str:
    """Load the UI language code from amethyst.ini [ui] language.

    Returns "" (follow system locale) when unset. The value is cached so
    get_language() can be called cheaply after this runs once at startup.
    """
    global _language
    path = get_ui_config_path()
    if not path.is_file():
        return _language
    try:
        parser = _read_ini(path)
        _language = parser.get(
            _INI_SECTION, _INI_LANGUAGE_OPTION, fallback="").strip()
    except Exception:
        pass
    return _language


def save_language(code: str) -> None:
    """Persist the UI language code to amethyst.ini [ui] language.

    Pass "" to follow the system locale. Takes effect on next launch.
    """
    global _language
    _language = (code or "").strip()
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = _new_parser()
    if path.is_file():
        parser.read(path)
    if _INI_SECTION not in parser:
        parser[_INI_SECTION] = {}
    parser[_INI_SECTION][_INI_LANGUAGE_OPTION] = _language
    _write_ini(parser, path)


def get_language() -> str:
    """Return the current language code (call load_language first at startup)."""
    return _language


# ---------------------------------------------------------------------------
# Tab pins - per-view preferred presentation mode (full / modlist / plugins).
# A view is identified by its tab `key` (e.g. "change_version", "nexus_browser").
# When a user re-pins a tab we remember the choice here so the same view reopens
# in that mode next time. Section is [tab_pins]; value is one of the mode names.
# ---------------------------------------------------------------------------
_TAB_PINS_SECTION = "tab_pins"
_VALID_TAB_MODES = ("full", "modlist", "plugins")


def get_tab_pin(key: str) -> "str | None":
    """Return the saved presentation mode for the view *key*, or None if unset
    (view opens in whatever mode its call site requests)."""
    if not key:
        return None
    path = get_ui_config_path()
    if not path.is_file():
        return None
    try:
        parser = _read_ini(path)
        val = parser.get(_TAB_PINS_SECTION, key, fallback="").strip()
        return val if val in _VALID_TAB_MODES else None
    except Exception:
        return None


def save_tab_pin(key: str, mode: str) -> None:
    """Persist the preferred presentation *mode* for the view *key* to
    amethyst.ini [tab_pins]. No-op for an empty key or unknown mode."""
    if not key or mode not in _VALID_TAB_MODES:
        return
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = _new_parser()
    if path.is_file():
        parser.read(path)
    if _TAB_PINS_SECTION not in parser:
        parser[_TAB_PINS_SECTION] = {}
    parser[_TAB_PINS_SECTION][key] = mode
    _write_ini(parser, path)


# ---------------------------------------------------------------------------
# Windows-filesystem deploy warning acknowledgements - per-game fingerprint of
# the drive layout the user chose to "Deploy anyway" on (see Utils.fs_check).
# Stored so the advisory shows once per game, re-arming if the drives change.
# ---------------------------------------------------------------------------
_FS_WARNINGS_SECTION = "fs_warnings"


def get_fs_warning_ack(game_name: str) -> str:
    """Return the acknowledged fs_check fingerprint for *game_name* ("" if
    the user has never confirmed the Windows-filesystem warning for it)."""
    if not game_name:
        return ""
    path = get_ui_config_path()
    if not path.is_file():
        return ""
    try:
        parser = _read_ini(path)
        return parser.get(_FS_WARNINGS_SECTION, game_name, fallback="").strip()
    except Exception:
        return ""


def save_fs_warning_ack(game_name: str, fingerprint: str) -> None:
    """Persist the acknowledged Windows-filesystem fingerprint for *game_name*."""
    if not game_name or not fingerprint:
        return
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = _new_parser()
    if path.is_file():
        parser.read(path)
    if _FS_WARNINGS_SECTION not in parser:
        parser[_FS_WARNINGS_SECTION] = {}
    # Escape % for ConfigParser interpolation (mount paths may contain it);
    # parser.get() on read resolves %% back to %.
    parser[_FS_WARNINGS_SECTION][game_name] = fingerprint.replace("%", "%%")
    _write_ini(parser, path)


def _clamp(value: float) -> float:
    return max(_MIN_SCALE, min(_MAX_SCALE, value))


# ---------------------------------------------------------------------------
# Collection settings
# ---------------------------------------------------------------------------
_COLLECTIONS_SECTION = "collections"

# Download order is no longer configurable - collection downloads always run
# strictly smallest→largest (unknown-size mods last). Defaults are 8/8: more
# concurrency past this gives little practical benefit on typical hardware.
_DEFAULT_MAX_CONCURRENT = 8
_DEFAULT_MAX_EXTRACT_WORKERS = 8

# Upper clamps for the user-configurable concurrency (Settings ▸ Downloads).
_MAX_CONCURRENT_CEILING = 12
_MAX_EXTRACT_WORKERS_CEILING = 8

# First-run defaults - written to the INI only when it is being created for
# the first time (see load_ui_scale). Existing installs keep whatever defaults
# they had even if they have never saved these settings explicitly.
_FIRST_RUN_MAX_CONCURRENT = 8
_FIRST_RUN_MAX_EXTRACT_WORKERS = 8
_FIRST_RUN_HIDDEN_COLUMNS = [2, 5, 8]  # category, installed, size


def load_collection_settings() -> dict:
    """Return collection settings dict with keys: max_concurrent, max_extract_workers, check_download_locations, clear_archive_after_install, download_order.

    NB *download_order* is retained ONLY for the legacy Tk settings dialog
    (gui/status_bar.py) - the Qt download scheduler ignores it (downloads always
    use the double-ended big-first + small-first policy)."""
    path = get_ui_config_path()
    defaults = {
        "max_concurrent": _DEFAULT_MAX_CONCURRENT,
        "max_extract_workers": _DEFAULT_MAX_EXTRACT_WORKERS,
        "check_download_locations": True,
        "clear_archive_after_install": False,
        "download_order": "largest",   # legacy Tk key; unused by Qt
    }
    if not path.is_file():
        return defaults
    try:
        parser = _read_ini(path)
        if not parser.has_section(_COLLECTIONS_SECTION):
            return defaults
        s = parser[_COLLECTIONS_SECTION]
        max_concurrent = int(s.get("max_concurrent", str(_DEFAULT_MAX_CONCURRENT)))
        max_concurrent = max(1, min(_MAX_CONCURRENT_CEILING, max_concurrent))
        max_extract_workers = int(s.get("max_extract_workers", str(_DEFAULT_MAX_EXTRACT_WORKERS)))
        max_extract_workers = max(1, min(_MAX_EXTRACT_WORKERS_CEILING, max_extract_workers))
        check_download_locations = s.getboolean("check_download_locations", True)
        clear_archive_after_install = s.getboolean("clear_archive_after_install", False)
        download_order = s.get("download_order", "largest").strip().lower()
        if download_order not in ("largest", "smallest"):
            download_order = "largest"
        return {
            "max_concurrent": max_concurrent,
            "max_extract_workers": max_extract_workers,
            "check_download_locations": check_download_locations,
            "clear_archive_after_install": clear_archive_after_install,
            "download_order": download_order,
        }
    except Exception:
        return defaults


# Download speed limit (all Nexus downloads, MB/s; 0 = unlimited). Stored in
# the [collections] section because the collection installer is its main UI
# entry point, but the cap applies to every download the app performs.
_DL_SPEED_LIMIT_KEY = "download_speed_limit"
_DL_SPEED_LIMIT_CEILING = 1000.0


def load_download_speed_limit() -> float:
    """Return the download speed limit in MB/s (0 = unlimited)."""
    path = get_ui_config_path()
    if not path.is_file():
        return 0.0
    try:
        parser = _read_ini(path)
        if not parser.has_section(_COLLECTIONS_SECTION):
            return 0.0
        raw = parser[_COLLECTIONS_SECTION].get(_DL_SPEED_LIMIT_KEY, "0")
        return max(0.0, min(_DL_SPEED_LIMIT_CEILING, float(raw)))
    except Exception:
        return 0.0


def save_download_speed_limit(mbps: float) -> None:
    """Persist the download speed limit (MB/s, 0 = unlimited) to amethyst.ini."""
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = _new_parser()
    if path.is_file():
        parser.read(path)
    if _COLLECTIONS_SECTION not in parser:
        parser[_COLLECTIONS_SECTION] = {}
    value = max(0.0, min(_DL_SPEED_LIMIT_CEILING, float(mbps or 0)))
    parser[_COLLECTIONS_SECTION][_DL_SPEED_LIMIT_KEY] = f"{value:g}"
    _write_ini(parser, path)


def save_collection_settings(max_concurrent: int,
                              check_download_locations: bool = True,
                              clear_archive_after_install: bool = False,
                              max_extract_workers: int = _DEFAULT_MAX_EXTRACT_WORKERS,
                              download_order: str | None = None) -> None:
    """Persist collection settings to amethyst.ini.

    *download_order* is accepted for backward-compatibility (the Tk settings
    dialog still passes it) but the Qt download scheduler ignores it - downloads
    always use the double-ended (big-first + small-first) policy. When given, it
    is still written through so the Tk UI round-trips.
    """
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = _new_parser()
    if path.is_file():
        parser.read(path)
    if _COLLECTIONS_SECTION not in parser:
        parser[_COLLECTIONS_SECTION] = {}
    if download_order is not None:
        parser[_COLLECTIONS_SECTION]["download_order"] = download_order
    parser[_COLLECTIONS_SECTION]["max_concurrent"] = str(max(1, min(_MAX_CONCURRENT_CEILING, max_concurrent)))
    parser[_COLLECTIONS_SECTION]["max_extract_workers"] = str(max(1, min(_MAX_EXTRACT_WORKERS_CEILING, max_extract_workers)))
    parser[_COLLECTIONS_SECTION]["check_download_locations"] = "true" if check_download_locations else "false"
    parser[_COLLECTIONS_SECTION]["clear_archive_after_install"] = "true" if clear_archive_after_install else "false"
    _write_ini(parser, path)


# ---------------------------------------------------------------------------
# Extraction resource limits
# ---------------------------------------------------------------------------
# Applies to EVERY archive extraction (single installs, Downloads tab and
# collection installs) - unlike [collections] max_extract_workers, which only
# caps how many extractions run at once during a collection install.
_EXTRACTION_SECTION = "extraction"

# 0 = no cap (7z -mmt=on, all cores). Clamp guards a hand-edited INI.
_EXTRACTION_THREADS_CEILING = 128


def load_extraction_settings() -> dict:
    """Return extraction resource settings with keys:

    * ``cpu_threads`` - int; max CPU threads per extractor process
      (0 = use all cores).
    * ``low_priority`` - bool; run extractor processes at low CPU + disk
      priority so foreground applications stay responsive.
    """
    path = get_ui_config_path()
    defaults = {"cpu_threads": 0, "low_priority": False}
    if not path.is_file():
        return defaults
    try:
        parser = _read_ini(path)
        if not parser.has_section(_EXTRACTION_SECTION):
            return defaults
        s = parser[_EXTRACTION_SECTION]
        cpu_threads = int(s.get("cpu_threads", "0"))
        cpu_threads = max(0, min(_EXTRACTION_THREADS_CEILING, cpu_threads))
        low_priority = s.getboolean("low_priority", False)
        return {"cpu_threads": cpu_threads, "low_priority": low_priority}
    except Exception:
        return defaults


def _save_extraction_option(key: str, value: str) -> None:
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = _new_parser()
    if path.is_file():
        parser.read(path)
    if _EXTRACTION_SECTION not in parser:
        parser[_EXTRACTION_SECTION] = {}
    parser[_EXTRACTION_SECTION][key] = value
    _write_ini(parser, path)


def save_extraction_cpu_threads(threads: int) -> None:
    """Persist the per-extraction CPU thread cap (0 = use all cores)."""
    threads = max(0, min(_EXTRACTION_THREADS_CEILING, int(threads)))
    _save_extraction_option("cpu_threads", str(threads))


def save_extraction_low_priority(value: bool) -> None:
    """Persist whether extractor processes run at low CPU/disk priority."""
    _save_extraction_option("low_priority", "true" if value else "false")


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
        parser = _read_ini(path)
        return parser.getboolean(_NEXUS_SECTION, "show_adult", fallback=False)
    except Exception:
        return False


def load_nexus_last_premium() -> "bool | None":
    """Last successfully-validated Nexus premium status, or None if never
    recorded. Fallback for the collection-install premium gate when a live
    validate() errors (network hiccup / rate limit) - a transient failure must
    not silently demote a premium user to manual mode (GH#278)."""
    path = get_ui_config_path()
    if not path.is_file():
        return None
    try:
        parser = _read_ini(path)
        raw = parser.get(_NEXUS_SECTION, "last_known_premium", fallback="")
        if raw.strip().lower() in ("true", "false"):
            return raw.strip().lower() == "true"
        return None
    except Exception:
        return None


def save_nexus_last_premium(value: bool) -> None:
    """Persist the last successfully-validated Nexus premium status."""
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = _new_parser()
    if path.is_file():
        parser.read(path)
    if _NEXUS_SECTION not in parser:
        parser[_NEXUS_SECTION] = {}
    parser[_NEXUS_SECTION]["last_known_premium"] = "true" if value else "false"
    _write_ini(parser, path)


_COLUMNS_SECTION = "columns"
_WINDOW_SECTION = "window"


def load_qt_window_state() -> dict:
    """Return the saved Qt main-window state from amethyst.ini [window]:

    * ``geometry`` - base64 str of QMainWindow.saveGeometry() (position, size
      and maximized flag), or None if never saved.
    * ``body_split`` - [left, right] pixel sizes of the modlist ║ plugins
      splitter, or None if never saved / unparseable.
    """
    result = {"geometry": None, "body_split": None}
    path = get_ui_config_path()
    if not path.is_file():
        return result
    try:
        parser = _read_ini(path)
        geo = parser.get(_WINDOW_SECTION, "qt_geometry", fallback="").strip()
        if geo:
            result["geometry"] = geo
        raw = parser.get(_WINDOW_SECTION, "body_split", fallback="").strip()
        if raw:
            sizes = [int(x) for x in raw.split(",")]
            if len(sizes) == 2 and all(s >= 0 for s in sizes) and sum(sizes) > 0:
                result["body_split"] = sizes
    except Exception:
        pass
    return result


def save_qt_window_state(geometry_b64: str, body_split: "list[int] | None") -> None:
    """Persist the Qt main-window geometry (base64 of saveGeometry()) and the
    modlist ║ plugins splitter sizes to amethyst.ini [window] in one write."""
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = _new_parser()
    if path.is_file():
        parser.read(path)
    if _WINDOW_SECTION not in parser:
        parser[_WINDOW_SECTION] = {}
    parser[_WINDOW_SECTION]["qt_geometry"] = geometry_b64
    if body_split and len(body_split) == 2:
        parser[_WINDOW_SECTION]["body_split"] = ",".join(str(int(s)) for s in body_split)
    _write_ini(parser, path)


# ---------------------------------------------------------------------------
# Dev mode
# ---------------------------------------------------------------------------
_DEV_SECTION = "dev"

DEV_MODE_ENV = "AMM_DEV_MODE"
FORCE_MANUAL_INSTALL_ENV = "AMM_FORCE_MANUAL_INSTALL"


def _env_flag(name: str) -> "bool | None":
    """Read *name* as a boolean, or None when it isn't set."""
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return None
    return raw in ("1", "true", "yes", "on")


def _dev_flag(key: str, env_name: str) -> bool:
    """True if *env_name* is set truthy, else [dev] *key* = true in the ini."""
    override = _env_flag(env_name)
    if override is not None:
        return override
    path = get_ui_config_path()
    if not path.is_file():
        return False
    try:
        parser = _read_ini(path)
        return parser.get(_DEV_SECTION, key, fallback="false").strip().lower() == "true"
    except Exception:
        return False


def load_dev_mode() -> bool:
    """True with AMM_DEV_MODE=1 or [dev] devmode = true in amethyst.ini."""
    return _dev_flag("devmode", DEV_MODE_ENV)


def load_force_manual_install() -> bool:
    """True with AMM_FORCE_MANUAL_INSTALL=1 or [dev] force_manual_install = true;
    Nexus installs then use the non-premium manual-download flow."""
    return _dev_flag("force_manual_install", FORCE_MANUAL_INSTALL_ENV)


# ---------------------------------------------------------------------------
# Flatpak 32-bit self-heal warning suppression
# ---------------------------------------------------------------------------
_FLATPAK_SECTION = "flatpak"


def load_suppress_i386_warning() -> bool:
    """Return True if the user has dismissed the missing-32-bit-support warning.

    Set when the user chose "Don't show again" on the failed-auto-install
    notice - they've opted to handle it themselves (or don't run Windows tools),
    so the startup self-heal stays silent.
    """
    path = get_ui_config_path()
    if not path.is_file():
        return False
    try:
        parser = _read_ini(path)
        return parser.getboolean(_FLATPAK_SECTION, "suppress_i386_warning", fallback=False)
    except Exception:
        return False


def save_suppress_i386_warning(value: bool) -> None:
    """Persist the suppress_i386_warning setting to amethyst.ini."""
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = _new_parser()
    if path.is_file():
        parser.read(path)
    if _FLATPAK_SECTION not in parser:
        parser[_FLATPAK_SECTION] = {}
    parser[_FLATPAK_SECTION]["suppress_i386_warning"] = "true" if value else "false"
    _write_ini(parser, path)


# ---------------------------------------------------------------------------
# Folder case normalisation setting
# ---------------------------------------------------------------------------
_FILEMAP_SECTION = "filemap"


# ---------------------------------------------------------------------------
# .nif preview settings
# ---------------------------------------------------------------------------
_NIF_SECTION = "nif_preview"


def load_nif_background() -> str:
    """Return the .nif viewer background preset key (default "light")."""
    path = get_ui_config_path()
    if not path.is_file():
        return "light"
    try:
        parser = _read_ini(path)
        return parser.get(_NIF_SECTION, "background", fallback="light")
    except Exception:
        return "light"


def load_nif_brightness() -> int:
    """Return the .nif viewer brightness percent (default 100 = neutral)."""
    path = get_ui_config_path()
    if not path.is_file():
        return 100
    try:
        parser = _read_ini(path)
        return parser.getint(_NIF_SECTION, "brightness", fallback=100)
    except Exception:
        return 100


def save_nif_brightness(percent: int) -> None:
    """Persist the .nif viewer brightness percent to amethyst.ini."""
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = _new_parser()
    if path.is_file():
        parser.read(path)
    if _NIF_SECTION not in parser:
        parser[_NIF_SECTION] = {}
    parser[_NIF_SECTION]["brightness"] = str(int(percent))
    _write_ini(parser, path)


def load_nif_cull_backfaces() -> bool:
    """Return whether the .nif viewer culls backfaces (default False)."""
    path = get_ui_config_path()
    if not path.is_file():
        return False
    try:
        parser = _read_ini(path)
        return parser.getboolean(_NIF_SECTION, "cull_backfaces", fallback=False)
    except Exception:
        return False


def save_nif_cull_backfaces(value: bool) -> None:
    """Persist the .nif viewer backface-culling setting to amethyst.ini."""
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = _new_parser()
    if path.is_file():
        parser.read(path)
    if _NIF_SECTION not in parser:
        parser[_NIF_SECTION] = {}
    parser[_NIF_SECTION]["cull_backfaces"] = "true" if value else "false"
    _write_ini(parser, path)


def load_nif_invert_mouse() -> bool:
    """Return whether the .nif viewer inverts orbit/pan (default True)."""
    path = get_ui_config_path()
    if not path.is_file():
        return True
    try:
        parser = _read_ini(path)
        return parser.getboolean(_NIF_SECTION, "invert_mouse", fallback=True)
    except Exception:
        return True


def save_nif_invert_mouse(value: bool) -> None:
    """Persist the .nif viewer mouse-inversion setting to amethyst.ini."""
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = _new_parser()
    if path.is_file():
        parser.read(path)
    if _NIF_SECTION not in parser:
        parser[_NIF_SECTION] = {}
    parser[_NIF_SECTION]["invert_mouse"] = "true" if value else "false"
    _write_ini(parser, path)


def save_nif_background(key: str) -> None:
    """Persist the .nif viewer background preset to amethyst.ini."""
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = _new_parser()
    if path.is_file():
        parser.read(path)
    if _NIF_SECTION not in parser:
        parser[_NIF_SECTION] = {}
    parser[_NIF_SECTION]["background"] = str(key)
    _write_ini(parser, path)


def load_normalize_folder_case() -> bool:
    """Return the global normalize_folder_case setting (default True)."""
    path = get_ui_config_path()
    if not path.is_file():
        return True
    try:
        parser = _read_ini(path)
        return parser.getboolean(_FILEMAP_SECTION, "normalize_folder_case", fallback=True)
    except Exception:
        return True


def save_normalize_folder_case(value: bool) -> None:
    """Persist the normalize_folder_case setting to amethyst.ini."""
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = _new_parser()
    if path.is_file():
        parser.read(path)
    if _FILEMAP_SECTION not in parser:
        parser[_FILEMAP_SECTION] = {}
    parser[_FILEMAP_SECTION]["normalize_folder_case"] = "true" if value else "false"
    _write_ini(parser, path)


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
        parser = _read_ini(path)
        return parser.getboolean(_UPDATES_SECTION, "allow_prerelease", fallback=False)
    except Exception:
        return False


def save_allow_prerelease(value: bool) -> None:
    """Persist the allow_prerelease setting to amethyst.ini under [updates]."""
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = _new_parser()
    if path.is_file():
        parser.read(path)
    if _UPDATES_SECTION not in parser:
        parser[_UPDATES_SECTION] = {}
    parser[_UPDATES_SECTION]["allow_prerelease"] = "true" if value else "false"
    _write_ini(parser, path)


def load_update_notifications() -> bool:
    """Whether to show the app-update banner on startup (default True)."""
    path = get_ui_config_path()
    if not path.is_file():
        return True
    try:
        parser = _read_ini(path)
        return parser.getboolean(_UPDATES_SECTION, "update_notifications",
                                 fallback=True)
    except Exception:
        return True


def save_update_notifications(value: bool) -> None:
    """Persist the update_notifications setting under [updates]."""
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = _new_parser()
    if path.is_file():
        parser.read(path)
    if _UPDATES_SECTION not in parser:
        parser[_UPDATES_SECTION] = {}
    parser[_UPDATES_SECTION]["update_notifications"] = ("true" if value
                                                        else "false")
    _write_ini(parser, path)


# ---------------------------------------------------------------------------
# Favourite wizard tools (global, shown at the top of the Wizard header menu)
# ---------------------------------------------------------------------------
# Stored as one key per favourited tool id under [wizard_favourites] (value is
# ignored, presence = favourited). Global across games - a tool id is unique
# per tool, and the Wizard menu only lists tools applicable to the active game
# anyway, so unrelated ids simply never match.
_WIZARD_FAV_SECTION = "wizard_favourites"


def load_favourite_wizards() -> set[str]:
    """Return the set of favourited wizard-tool ids (empty if none/unset)."""
    path = get_ui_config_path()
    if not path.is_file():
        return set()
    try:
        parser = _read_ini(path)
        if _WIZARD_FAV_SECTION not in parser:
            return set()
        return {k for k, v in parser[_WIZARD_FAV_SECTION].items()
                if str(v).strip().lower() in ("1", "true", "yes", "on", "")}
    except Exception:
        return set()


def save_favourite_wizards(tool_ids) -> None:
    """Persist the set of favourited wizard-tool ids under [wizard_favourites],
    replacing any previous contents."""
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = _new_parser()
    if path.is_file():
        parser.read(path)
    # Rewrite the section from scratch so unchecked tools are dropped.
    parser[_WIZARD_FAV_SECTION] = {tid: "true" for tid in sorted(set(tool_ids))}
    _write_ini(parser, path)


def load_clear_archive_after_install() -> bool:
    """Return the clear_archive_after_install setting (default True)."""
    path = get_ui_config_path()
    if not path.is_file():
        return True
    try:
        parser = _read_ini(path)
        return parser.getboolean(_FILEMAP_SECTION, "clear_archive_after_install", fallback=True)
    except Exception:
        return True


def save_clear_archive_after_install(value: bool) -> None:
    """Persist the clear_archive_after_install setting to amethyst.ini."""
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = _new_parser()
    if path.is_file():
        parser.read(path)
    if _FILEMAP_SECTION not in parser:
        parser[_FILEMAP_SECTION] = {}
    parser[_FILEMAP_SECTION]["clear_archive_after_install"] = "true" if value else "false"
    _write_ini(parser, path)


def load_install_mods_disabled() -> bool:
    """Return the install_mods_disabled setting (default False). When True,
    newly installed mods land in the modlist disabled instead of enabled.
    Applies to every install path except collection installs."""
    path = get_ui_config_path()
    if not path.is_file():
        return False
    try:
        parser = _read_ini(path)
        return parser.getboolean(_FILEMAP_SECTION, "install_mods_disabled", fallback=False)
    except Exception:
        return False


def save_install_mods_disabled(value: bool) -> None:
    """Persist the install_mods_disabled setting to amethyst.ini."""
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = _new_parser()
    if path.is_file():
        parser.read(path)
    if _FILEMAP_SECTION not in parser:
        parser[_FILEMAP_SECTION] = {}
    parser[_FILEMAP_SECTION]["install_mods_disabled"] = "true" if value else "false"
    _write_ini(parser, path)


def load_keep_fomod_archives() -> bool:
    """Return the keep_fomod_archives setting (default False).

    When True, archives of mods that use a FOMOD installer are always kept
    regardless of the clear_archive_after_install setting.
    """
    path = get_ui_config_path()
    if not path.is_file():
        return False
    try:
        parser = _read_ini(path)
        return parser.getboolean(_FILEMAP_SECTION, "keep_fomod_archives", fallback=False)
    except Exception:
        return False


def save_keep_fomod_archives(value: bool) -> None:
    """Persist the keep_fomod_archives setting to amethyst.ini."""
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = _new_parser()
    if path.is_file():
        parser.read(path)
    if _FILEMAP_SECTION not in parser:
        parser[_FILEMAP_SECTION] = {}
    parser[_FILEMAP_SECTION]["keep_fomod_archives"] = "true" if value else "false"
    _write_ini(parser, path)


def load_show_summary_tooltips() -> bool:
    """Return the show_summary_tooltips setting (default True)."""
    path = get_ui_config_path()
    if not path.is_file():
        return True
    try:
        parser = _read_ini(path)
        return parser.getboolean(_FILEMAP_SECTION, "show_summary_tooltips", fallback=True)
    except Exception:
        return True


def save_show_summary_tooltips(value: bool) -> None:
    """Persist the show_summary_tooltips setting to amethyst.ini."""
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = _new_parser()
    if path.is_file():
        parser.read(path)
    if _FILEMAP_SECTION not in parser:
        parser[_FILEMAP_SECTION] = {}
    parser[_FILEMAP_SECTION]["show_summary_tooltips"] = "true" if value else "false"
    _write_ini(parser, path)


def load_hide_bsa_conflicts() -> bool:
    """Return the hide_bsa_conflicts setting (default False).

    When True, the modlist panel hides BSA/BA2 archive conflict flags and the
    BSA conflict parsing is skipped entirely (for better performance).
    """
    path = get_ui_config_path()
    if not path.is_file():
        return False
    try:
        parser = _read_ini(path)
        return parser.getboolean(_FILEMAP_SECTION, "hide_bsa_conflicts", fallback=False)
    except Exception:
        return False


def save_hide_bsa_conflicts(value: bool) -> None:
    """Persist the hide_bsa_conflicts setting to amethyst.ini."""
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = _new_parser()
    if path.is_file():
        parser.read(path)
    if _FILEMAP_SECTION not in parser:
        parser[_FILEMAP_SECTION] = {}
    parser[_FILEMAP_SECTION]["hide_bsa_conflicts"] = "true" if value else "false"
    _write_ini(parser, path)


def load_rename_mod_after_install() -> bool:
    """Return the rename_mod_after_install setting (default False).

    When True, a rename prompt is shown after each (non-collection) mod install.
    """
    path = get_ui_config_path()
    if not path.is_file():
        return False
    try:
        parser = _read_ini(path)
        return parser.getboolean(_FILEMAP_SECTION, "rename_mod_after_install", fallback=False)
    except Exception:
        return False


def save_rename_mod_after_install(value: bool) -> None:
    """Persist the rename_mod_after_install setting to amethyst.ini."""
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = _new_parser()
    if path.is_file():
        parser.read(path)
    if _FILEMAP_SECTION not in parser:
        parser[_FILEMAP_SECTION] = {}
    parser[_FILEMAP_SECTION]["rename_mod_after_install"] = "true" if value else "false"
    _write_ini(parser, path)


def load_restore_on_close() -> bool:
    """Return the restore_on_close setting (default False).

    When True, every configured game with active deployment is restored to
    vanilla when the application window is closed.
    """
    path = get_ui_config_path()
    if not path.is_file():
        return False
    try:
        parser = _read_ini(path)
        return parser.getboolean(_FILEMAP_SECTION, "restore_on_close", fallback=False)
    except Exception:
        return False


def save_restore_on_close(value: bool) -> None:
    """Persist the restore_on_close setting to amethyst.ini."""
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = _new_parser()
    if path.is_file():
        parser.read(path)
    if _FILEMAP_SECTION not in parser:
        parser[_FILEMAP_SECTION] = {}
    parser[_FILEMAP_SECTION]["restore_on_close"] = "true" if value else "false"
    _write_ini(parser, path)


def save_nexus_show_adult(value: bool) -> None:
    """Persist the show_adult setting to amethyst.ini."""
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = _new_parser()
    if path.is_file():
        parser.read(path)
    if _NEXUS_SECTION not in parser:
        parser[_NEXUS_SECTION] = {}
    parser[_NEXUS_SECTION]["show_adult"] = "true" if value else "false"
    _write_ini(parser, path)


def load_nexus_page_size(default: int = 30) -> int:
    """Return the persisted Nexus browser 'shown per page' setting."""
    path = get_ui_config_path()
    if not path.is_file():
        return default
    try:
        parser = _read_ini(path)
        val = parser.getint(_NEXUS_SECTION, "page_size", fallback=default)
        return val if val in (20, 30, 40, 50) else default
    except Exception:
        return default


def save_nexus_page_size(value: int) -> None:
    """Persist the Nexus browser 'shown per page' setting to amethyst.ini."""
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = _new_parser()
    if path.is_file():
        parser.read(path)
    if _NEXUS_SECTION not in parser:
        parser[_NEXUS_SECTION] = {}
    parser[_NEXUS_SECTION]["page_size"] = str(int(value))
    _write_ini(parser, path)


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
        parser = _read_ini(path)
        return parser.get(_PATHS_SECTION, "heroic_config_path", fallback="").strip()
    except Exception:
        return ""


def save_heroic_config_path(value: str) -> None:
    """Persist the Heroic config directory path to amethyst.ini. Pass '' to clear."""
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = _new_parser()
    if path.is_file():
        parser.read(path)
    if _PATHS_SECTION not in parser:
        parser[_PATHS_SECTION] = {}
    parser[_PATHS_SECTION]["heroic_config_path"] = value.strip()
    _write_ini(parser, path)


def load_lutris_data_path() -> str:
    """Return the user-configured Lutris data directory path, or '' if unset."""
    path = get_ui_config_path()
    if not path.is_file():
        return ""
    try:
        parser = _read_ini(path)
        return parser.get(_PATHS_SECTION, "lutris_data_path", fallback="").strip()
    except Exception:
        return ""


def save_lutris_data_path(value: str) -> None:
    """Persist the Lutris data directory path to amethyst.ini. Pass '' to clear."""
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = _new_parser()
    if path.is_file():
        parser.read(path)
    if _PATHS_SECTION not in parser:
        parser[_PATHS_SECTION] = {}
    parser[_PATHS_SECTION]["lutris_data_path"] = value.strip()
    _write_ini(parser, path)


def load_lutris_appimage_path() -> str:
    """Return the user-configured Lutris AppImage path, or '' if unset."""
    path = get_ui_config_path()
    if not path.is_file():
        return ""
    try:
        parser = _read_ini(path)
        return parser.get(_PATHS_SECTION, "lutris_appimage_path", fallback="").strip()
    except Exception:
        return ""


def save_lutris_appimage_path(value: str) -> None:
    """Persist the Lutris AppImage path to amethyst.ini. Pass '' to clear."""
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = _new_parser()
    if path.is_file():
        parser.read(path)
    if _PATHS_SECTION not in parser:
        parser[_PATHS_SECTION] = {}
    parser[_PATHS_SECTION]["lutris_appimage_path"] = value.strip()
    _write_ini(parser, path)


def load_faugus_data_path() -> str:
    """Return the user-configured Faugus data directory path, or '' if unset."""
    path = get_ui_config_path()
    if not path.is_file():
        return ""
    try:
        parser = _read_ini(path)
        return parser.get(_PATHS_SECTION, "faugus_data_path", fallback="").strip()
    except Exception:
        return ""


def save_faugus_data_path(value: str) -> None:
    """Persist the Faugus data directory path to amethyst.ini. Pass '' to clear."""
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = _new_parser()
    if path.is_file():
        parser.read(path)
    if _PATHS_SECTION not in parser:
        parser[_PATHS_SECTION] = {}
    parser[_PATHS_SECTION]["faugus_data_path"] = value.strip()
    _write_ini(parser, path)


def load_faugus_appimage_path() -> str:
    """Return the user-configured Faugus AppImage path, or '' if unset."""
    path = get_ui_config_path()
    if not path.is_file():
        return ""
    try:
        parser = _read_ini(path)
        return parser.get(_PATHS_SECTION, "faugus_appimage_path", fallback="").strip()
    except Exception:
        return ""


def save_faugus_appimage_path(value: str) -> None:
    """Persist the Faugus AppImage path to amethyst.ini. Pass '' to clear."""
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = _new_parser()
    if path.is_file():
        parser.read(path)
    if _PATHS_SECTION not in parser:
        parser[_PATHS_SECTION] = {}
    parser[_PATHS_SECTION]["faugus_appimage_path"] = value.strip()
    _write_ini(parser, path)


def load_steam_libraries_vdf_path() -> str:
    """Return the user-configured path to Steam's libraryfolders.vdf, or '' if unset."""
    path = get_ui_config_path()
    if not path.is_file():
        return ""
    try:
        parser = _read_ini(path)
        return parser.get(_PATHS_SECTION, "steam_libraries_vdf", fallback="").strip()
    except Exception:
        return ""


def save_steam_libraries_vdf_path(value: str) -> None:
    """Persist the Steam libraryfolders.vdf path to amethyst.ini. Pass '' to clear."""
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = _new_parser()
    if path.is_file():
        parser.read(path)
    if _PATHS_SECTION not in parser:
        parser[_PATHS_SECTION] = {}
    parser[_PATHS_SECTION]["steam_libraries_vdf"] = value.strip()
    _write_ini(parser, path)


def load_default_staging_path() -> str:
    """Return the user-configured default mod staging folder, or '' if unset.

    When set, adding a new game uses ``<this>/<game_name>`` as its mod staging
    folder instead of the built-in default (~/.config/AmethystModManager/Profiles).
    """
    path = get_ui_config_path()
    if not path.is_file():
        return ""
    try:
        parser = _read_ini(path)
        return parser.get(_PATHS_SECTION, "default_staging_path", fallback="").strip()
    except Exception:
        return ""


def save_default_staging_path(value: str) -> None:
    """Persist the default mod staging folder to amethyst.ini. Pass '' to clear."""
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = _new_parser()
    if path.is_file():
        parser.read(path)
    if _PATHS_SECTION not in parser:
        parser[_PATHS_SECTION] = {}
    parser[_PATHS_SECTION]["default_staging_path"] = value.strip()
    _write_ini(parser, path)


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
        parser = _read_ini(path)
        return parser.get(_PATHS_SECTION, "download_cache_path", fallback="").strip()
    except Exception:
        return ""


def save_download_cache_path(value: str) -> None:
    """Persist the download cache root to amethyst.ini. Pass '' to clear."""
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = _new_parser()
    if path.is_file():
        parser.read(path)
    if _PATHS_SECTION not in parser:
        parser[_PATHS_SECTION] = {}
    parser[_PATHS_SECTION]["download_cache_path"] = value.strip()
    _write_ini(parser, path)


def load_onboarding_complete() -> bool:
    """Return True once first-run onboarding has been finished/dismissed.

    False when the file / section / key is missing (covers 'missing or 0'), so
    a fresh amethyst.ini shows the onboarding on launch exactly once.
    """
    path = get_ui_config_path()
    if not path.is_file():
        return False
    try:
        parser = _read_ini(path)
        return parser.getboolean(_ONBOARDING_SECTION, "complete", fallback=False)
    except Exception:
        return False


def save_onboarding_complete(value: bool) -> None:
    """Persist the onboarding completion flag to amethyst.ini."""
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = _new_parser()
    if path.is_file():
        parser.read(path)
    if _ONBOARDING_SECTION not in parser:
        parser[_ONBOARDING_SECTION] = {}
    parser[_ONBOARDING_SECTION]["complete"] = "1" if value else "0"
    _write_ini(parser, path)


# ---------------------------------------------------------------------------
# Custom install-name regex patterns
#
# User-defined search/replace rules applied to a downloaded archive's filename
# stem *before* the built-in Nexus/mod.io parsers, so users can adapt to a new
# download-name format (Nexus changes these often) without a code change. Each
# rule is a {"search": <regex>, "replace": <repl>, "enabled": bool} dict;
# ``re.sub(search, replace, stem)`` is applied in order. Stored as a JSON list
# under one option so the ordered pairs round-trip cleanly through configparser.
# ---------------------------------------------------------------------------
_NAME_PATTERNS_SECTION = "install_name_patterns"
_NAME_PATTERNS_OPTION = "patterns"
# One-shot marker: the built-in default rules are seeded into `patterns` exactly
# once (the first time the rules are read). Without this, a user who deletes all
# the default rows would get them re-added on the next launch.
_NAME_PATTERNS_SEEDED_OPTION = "seeded"
# Comma-separated ids of the default rules already introduced to this config.
# A default id not in this list is injected once (at its canonical position) so a
# NEW built-in rule shipped in an update reaches users who were seeded earlier -
# without re-adding a default the user deliberately deleted (its id is recorded
# here from the first seed, so it won't come back).
_NAME_PATTERNS_INTRODUCED_OPTION = "introduced"


def _read_install_name_patterns_raw(parser) -> list[dict] | None:
    """Parse the stored JSON list, or None if the option is absent/blank/bad."""
    import json
    raw = parser.get(_NAME_PATTERNS_SECTION, _NAME_PATTERNS_OPTION, fallback="").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except Exception:
        return None
    if not isinstance(data, list):
        return None
    rules: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        search = str(item.get("search", ""))
        if not search:
            continue
        rule = {
            "search": search,
            "replace": str(item.get("replace", "")),
            "enabled": bool(item.get("enabled", True)),
        }
        # id/label are carried for built-in default rows so the editor can offer
        # a reset; user-added rows simply omit them.
        if item.get("id"):
            rule["id"] = str(item["id"])
        if item.get("label"):
            rule["label"] = str(item["label"])
        rules.append(rule)
    return rules


def load_install_name_patterns() -> list[dict]:
    """Return the install-name rules (built-in defaults + user rules), in order.

    Each item is ``{"search", "replace", "enabled"[, "id", "label"]}``. On the
    very first read the built-in defaults (``mod_name_utils.default_install_name_rules``)
    are seeded into the config, so callers and the editor always see the same
    list. Returns ``[]`` only when the config is unreadable.
    """
    path = get_ui_config_path()
    if not path.is_file():
        # Config not created yet - return defaults without writing (the ini is
        # written on first save/seed once load_ui_scale has created the file).
        try:
            from Utils.mod_name_utils import default_install_name_rules
            return default_install_name_rules()
        except Exception:
            return []
    try:
        from Utils.mod_name_utils import default_install_name_rules
        defaults = default_install_name_rules()
    except Exception:
        defaults = []
    try:
        parser = _read_ini(path)
        rules = _read_install_name_patterns_raw(parser)
        already_seeded = parser.getboolean(
            _NAME_PATTERNS_SECTION, _NAME_PATTERNS_SEEDED_OPTION, fallback=False)
        if rules is None and not already_seeded:
            # First run: seed the built-in defaults and persist them so they show
            # (editable) in the editor and run at install time.
            save_install_name_patterns(defaults)
            return defaults
        rules = rules or []
        # Migration: inject any default rule NOT yet introduced to this config
        # (a new built-in shipped in an update), at its canonical position, once.
        intro_raw = parser.get(
            _NAME_PATTERNS_SECTION, _NAME_PATTERNS_INTRODUCED_OPTION, fallback="")
        introduced = {x.strip() for x in intro_raw.split(",") if x.strip()}
        # Legacy configs seeded before the `introduced` marker existed: treat the
        # ids currently present as already-introduced so we don't re-add a rule
        # the user deleted from that original set.
        if not introduced:
            introduced = {str(r.get("id")) for r in rules if r.get("id")}
        new_defaults = [d for d in defaults
                        if d.get("id") and d["id"] not in introduced]
        if new_defaults:
            merged = _merge_new_defaults(rules, defaults, new_defaults)
            save_install_name_patterns(merged)
            return merged
        return rules
    except Exception:
        return []


def _merge_new_defaults(rules: list[dict], defaults: list[dict],
                        new_defaults: list[dict]) -> list[dict]:
    """Insert *new_defaults* into *rules* at their canonical position (relative
    to the other defaults in *defaults*), leaving existing rules and their order
    otherwise untouched."""
    default_order = [d.get("id") for d in defaults]
    merged = list(rules)
    for nd in new_defaults:
        nid = nd.get("id")
        try:
            di = default_order.index(nid)
        except ValueError:
            merged.append(dict(nd))
            continue
        # Find the insert point: after the last already-present default that
        # precedes this one in the canonical order.
        insert_at = len(merged)
        prior_ids = {default_order[j] for j in range(di)}
        last_prior = -1
        for idx, r in enumerate(merged):
            if r.get("id") in prior_ids:
                last_prior = idx
        if last_prior >= 0:
            insert_at = last_prior + 1
        else:
            # No preceding default present → put it before the first custom row
            # (i.e. before the first row without a known default id).
            default_ids = set(default_order)
            for idx, r in enumerate(merged):
                if r.get("id") not in default_ids:
                    insert_at = idx
                    break
        merged.insert(insert_at, dict(nd))
    return merged


def save_install_name_patterns(rules: list[dict]) -> None:
    """Persist the install-name rules to amethyst.ini as a JSON list.

    Rules with an empty ``search`` are dropped. Order is preserved (rules are
    applied top-to-bottom at install time). Carries the optional ``id``/``label``
    on built-in default rows so the editor can offer a reset. Always sets the
    ``seeded`` marker so defaults are seeded at most once (see load_*)."""
    import json
    cleaned = []
    for r in rules:
        if not str(r.get("search", "")).strip():
            continue
        item = {
            "search": str(r.get("search", "")),
            "replace": str(r.get("replace", "")),
            "enabled": bool(r.get("enabled", True)),
        }
        if r.get("id"):
            item["id"] = str(r["id"])
        if r.get("label"):
            item["label"] = str(r["label"])
        cleaned.append(item)
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = _new_parser()
    if path.is_file():
        parser.read(path)
    if _NAME_PATTERNS_SECTION not in parser:
        parser[_NAME_PATTERNS_SECTION] = {}
    parser[_NAME_PATTERNS_SECTION][_NAME_PATTERNS_OPTION] = json.dumps(cleaned)
    parser[_NAME_PATTERNS_SECTION][_NAME_PATTERNS_SEEDED_OPTION] = "true"
    # Record every shipped default id as "introduced" (union with what's already
    # there), so a default remains introduced even after the user deletes it -
    # it won't be re-added by the load-time migration.
    try:
        from Utils.mod_name_utils import default_install_name_rules
        default_ids = {str(d["id"]) for d in default_install_name_rules() if d.get("id")}
    except Exception:
        default_ids = set()
    prior_intro = {
        x.strip()
        for x in parser.get(_NAME_PATTERNS_SECTION,
                            _NAME_PATTERNS_INTRODUCED_OPTION, fallback="").split(",")
        if x.strip()
    }
    parser[_NAME_PATTERNS_SECTION][_NAME_PATTERNS_INTRODUCED_OPTION] = \
        ",".join(sorted(prior_intro | default_ids))
    _write_ini(parser, path)


# ---------------------------------------------------------------------------
# App environment variables (Settings ▸ Advanced) - applied to our OWN process
# at startup by Utils.app_env.apply_saved_env, so a change needs a restart.
#
# Stored as a JSON list under one option rather than one option per variable:
# configparser lower-cases option names, which would mangle PATH-style names,
# and JSON keeps order + the enabled flag. `%` is escaped to `%%` on write so
# BasicInterpolation round-trips a literal percent (a Wine-style value like
# %USERPROFILE% otherwise raises on save).
# ---------------------------------------------------------------------------
_APP_ENV_SECTION = "app_env"
_APP_ENV_OPTION = "vars"


def load_app_env_vars() -> list[dict]:
    """Return the saved app environment variables in order.

    Each item is ``{"name": str, "value": str, "enabled": bool}``. Returns []
    when nothing is configured or the config is unreadable.
    """
    import json
    path = get_ui_config_path()
    if not path.is_file():
        return []
    try:
        parser = _read_ini(path)
        raw = parser.get(_APP_ENV_SECTION, _APP_ENV_OPTION, fallback="").strip()
    except Exception:
        return []
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    out: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        out.append({
            "name": name,
            "value": str(item.get("value", "")),
            "enabled": bool(item.get("enabled", True)),
        })
    return out


def save_app_env_vars(entries: list[dict]) -> None:
    """Persist the app environment variables. Unnamed entries are dropped; a
    repeated name keeps its last occurrence (the environment can only hold one
    value per name, and that's what apply_saved_env would end up with)."""
    import json
    cleaned: list[dict] = []
    seen: dict[str, int] = {}
    for e in entries or []:
        name = str(e.get("name", "")).strip()
        if not name:
            continue
        item = {
            "name": name,
            "value": str(e.get("value", "")),
            "enabled": bool(e.get("enabled", True)),
        }
        if name in seen:
            cleaned[seen[name]] = item
        else:
            seen[name] = len(cleaned)
            cleaned.append(item)
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = _new_parser()
    if path.is_file():
        parser.read(path)
    if _APP_ENV_SECTION not in parser:
        parser[_APP_ENV_SECTION] = {}
    parser[_APP_ENV_SECTION][_APP_ENV_OPTION] = json.dumps(cleaned).replace("%", "%%")
    _write_ini(parser, path)


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
# User overrides in [theme] of amethyst.ini always win - except when a
# saved value exactly matches the dark default for a key that the current
# theme overrides; that's treated as legacy/uncustomised so existing ini
# files don't strand users with dark separators on a light or cyberpunk UI.
_theme_defaults_override_cache: dict[str, dict[str, str]] = {}


# Resolver that maps a theme *mode* name → {key: hex} overrides. Injected by
# the GUI (set_theme_override_resolver) so this module needn't import the GUI's
# theme package. Headless / non-GUI builds leave it unset → no overrides.
_theme_override_resolver: "callable | None" = None


def set_theme_override_resolver(fn: "callable | None") -> None:
    """Register a callable(mode: str) -> dict[str, str] of theme overrides.

    Clears the per-mode cache so a freshly-registered resolver takes effect.
    """
    global _theme_override_resolver
    _theme_override_resolver = fn
    _theme_defaults_override_cache.clear()


def _theme_defaults_override_for(mode: str) -> dict[str, str]:
    """Return {key: hex} overrides declared by the active theme.

    Delegates to the GUI-registered resolver (set_theme_override_resolver);
    yields {} when no resolver is attached or it fails. Results are cached per
    mode to keep the ini read path cheap.
    """
    if mode in _theme_defaults_override_cache:
        return _theme_defaults_override_cache[mode]
    result: dict[str, str] = {}
    if _theme_override_resolver is not None:
        try:
            raw = _theme_override_resolver(mode)
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
    user-customisable keys. User overrides in [theme] always win - except
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
            parser = _read_ini(path)
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


# ---------------------------------------------------------------------------
# Appearance mode - applied at startup, requires restart.
#
# Valid values are theme IDs (filenames) under src/gui/themes/ (e.g. "dark",
# "light"). ui_config doesn't validate against that list to avoid importing
# the gui package; theme.py handles unknown IDs by falling back to dark.
# ---------------------------------------------------------------------------
_APPEARANCE_OPTION = "appearance_mode"
_APPEARANCE_DEFAULT = "dark"
# Theme ids are lowercase word chars/digits/dashes/underscores; user-authored
# JSON themes add a single "custom:" namespace prefix (see Utils.custom_themes),
# so a colon is permitted between the prefix and the slug.
_APPEARANCE_ID_RE = _re.compile(r"^[a-z0-9_][a-z0-9_:-]*$")


def get_appearance_mode() -> str:
    """Return the saved appearance-mode theme ID, defaulting to 'dark'."""
    path = get_ui_config_path()
    if not path.is_file():
        return _APPEARANCE_DEFAULT
    try:
        parser = _read_ini(path)
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
    parser = _new_parser()
    if path.is_file():
        parser.read(path)
    if _INI_SECTION not in parser:
        parser[_INI_SECTION] = {}
    parser[_INI_SECTION][_APPEARANCE_OPTION] = mode
    _write_ini(parser, path)


# ---------------------------------------------------------------------------
# Last session (game + profile) - restored at startup
# ---------------------------------------------------------------------------

def load_last_session() -> "tuple[str | None, str | None]":
    """Return (last_game, last_profile) from amethyst.ini, each None if unset."""
    path = get_ui_config_path()
    if not path.is_file():
        return (None, None)
    try:
        parser = _read_ini(path)
        g = parser.get(_SESSION_SECTION, "last_game", fallback="") or ""
        p = parser.get(_SESSION_SECTION, "last_profile", fallback="") or ""
        return (g or None, p or None)
    except Exception:
        return (None, None)


def save_last_session(game: "str | None", profile: "str | None") -> None:
    """Persist the last-used game + profile to amethyst.ini ([session])."""
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        parser = _new_parser()
        if path.is_file():
            parser.read(path)
        if _SESSION_SECTION not in parser:
            parser[_SESSION_SECTION] = {}
        parser[_SESSION_SECTION]["last_game"] = game or ""
        parser[_SESSION_SECTION]["last_profile"] = profile or ""
        _write_ini(parser, path)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# amethyst.ini schema version gate (migration wipe)
# ---------------------------------------------------------------------------

def ensure_ini_version() -> None:
    """Ensure amethyst.ini matches the current schema version.

    If the file exists but its ``[meta] version`` is missing or != _APP_INI_VERSION
    (an old Tk-era ini), DELETE it so the Qt build starts fresh. Then make sure a
    file exists stamping the current version. amethyst.ini only - other config
    (last_game.json, games/, profiles, caches) is left untouched.

    Call this ONCE at the very start of startup, before anything reads the ini.
    Best-effort: any error falls back to wiping + rewriting so a corrupt/locked
    ini can never block startup.
    """
    path = get_ui_config_path()
    try:
        needs_wipe = False
        if path.is_file():
            try:
                parser = _new_parser()
                parser.read(path)
                ver = parser.getint(_META_SECTION, "version", fallback=0)
            except Exception:
                ver = -1   # unreadable → treat as outdated
            if ver != _APP_INI_VERSION:
                needs_wipe = True
        if needs_wipe:
            try:
                path.unlink()
            except OSError:
                pass
        if not path.is_file():
            # Fresh stamp (brand-new install or just-wiped).
            path.parent.mkdir(parents=True, exist_ok=True)
            parser = _new_parser()
            parser[_META_SECTION] = {"version": str(_APP_INI_VERSION)}
            _write_ini(parser, path)
        else:
            # File is current but make sure the version key is present/correct.
            parser = _new_parser()
            parser.read(path)
            if (not parser.has_section(_META_SECTION)
                    or parser.get(_META_SECTION, "version", fallback="")
                    != str(_APP_INI_VERSION)):
                if _META_SECTION not in parser:
                    parser[_META_SECTION] = {}
                parser[_META_SECTION]["version"] = str(_APP_INI_VERSION)
                _write_ini(parser, path)
    except Exception:
        # Last resort: try a clean rewrite; swallow anything so startup proceeds.
        try:
            parser = _new_parser()
            parser[_META_SECTION] = {"version": str(_APP_INI_VERSION)}
            _write_ini(parser, path)
        except Exception:
            pass
