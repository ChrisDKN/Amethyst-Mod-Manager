"""
Shared theme constants and icon loader for the GUI.
Used by gui.py and all gui submodules.
"""

from pathlib import Path

import customtkinter as ctk
from PIL import Image as PilImage

from Utils.ui_config import (
    get_ui_scale, get_font_family, load_font_family,
    load_theme_colors, get_theme_color,
)

# ---------------------------------------------------------------------------
# Color palette
# ---------------------------------------------------------------------------
BG_DEEP    = "#1a1a1a"
BG_PANEL   = "#252526"
BG_HEADER  = "#2a2a2b"
BG_ROW     = "#2d2d2d"
BG_ROW_ALT = "#303030"
BG_SEP     = "#383838"  # overridden below via load_theme_colors()
BG_HOVER   = "#094771"
BG_SELECT  = "#0f5fa3"
BG_HOVER_ROW = "#3d3d3d"
ACCENT     = "#0078d4"
ACCENT_HOV = "#1084d8"
TEXT_MAIN  = "#d4d4d4"
TEXT_DIM   = "#858585"
TEXT_SEP   = "#b0b0b0"
TEXT_OK    = "#98c379"
TEXT_ERR   = "#e06c75"
TEXT_WARN  = "#e5c07b"
BORDER     = "#444444"
RED_BTN    = "#a83232"
RED_HOV    = "#c43c3c"

# ---------------------------------------------------------------------------
# Contrast helper
# ---------------------------------------------------------------------------
def contrasting_text_color(hex_bg: str) -> str:
    """Return '#111111' or '#eeeeee' (dark/light) based on the luminance of
    *hex_bg* (e.g. '#3a7bd5') so text always stays readable."""
    try:
        hex_bg = hex_bg.lstrip("#")
        r, g, b = (int(hex_bg[i:i+2], 16) / 255.0 for i in (0, 2, 4))
        # Convert sRGB to linear light (WCAG 2 formula)
        def _lin(c: float) -> float:
            return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
        lum = 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)
        # Threshold: use dark text on light backgrounds, light text on dark
        return "#111111" if lum > 0.179 else "#eeeeee"
    except Exception:
        return TEXT_SEP


def hover_tint(hex_bg: str, amount: int = 20) -> str:
    """Return a hover variant of *hex_bg* — brighter by *amount* per channel,
    or darker if brightening would clip (channel already near 255).

    When darkening a near-saturated colour, a plain ``-amount`` on one channel
    is barely perceptible (e.g. pure red #ff0000 only shifts luminance by ~8%).
    So darkening scales all channels proportionally toward black to produce a
    shift roughly comparable to the lighten case.
    """
    try:
        h = hex_bg.lstrip("#")
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        # If any channel would clip when brightening, darken instead.
        if max(r, g, b) + amount > 255:
            # Scale toward black by ~15% — perceptible regardless of hue.
            factor = 0.82
            r = int(r * factor)
            g = int(g * factor)
            b = int(b * factor)
        else:
            r = min(255, r + amount)
            g = min(255, g + amount)
            b = min(255, b + amount)
        return f"#{r:02x}{g:02x}{b:02x}"
    except Exception:
        return hex_bg

# Highlight colours — user-customisable via Settings → Theme, persisted in amethyst.ini.
load_theme_colors()
plugin_separator   = get_theme_color("plugin_separator")
plugin_mod         = get_theme_color("plugin_mod")
conflict_separator = get_theme_color("conflict_separator")
conflict_higher    = get_theme_color("conflict_higher")
conflict_lower     = get_theme_color("conflict_lower")
BG_SEP             = get_theme_color("separator_bg")


def refresh_theme_colors() -> None:
    """Re-read theme colours from amethyst.ini and rebind the module globals.

    Callers that captured names via `from gui.theme import X` will still see
    their old binding — rendering sites should use `theme.X` attribute access
    for the values to update live.
    """
    global plugin_separator, plugin_mod, conflict_separator, conflict_higher, conflict_lower, BG_SEP
    load_theme_colors()
    plugin_separator   = get_theme_color("plugin_separator")
    plugin_mod         = get_theme_color("plugin_mod")
    conflict_separator = get_theme_color("conflict_separator")
    conflict_higher    = get_theme_color("conflict_higher")
    conflict_lower     = get_theme_color("conflict_lower")
    BG_SEP             = get_theme_color("separator_bg")

# ---------------------------------------------------------------------------
# Fonts
# ---------------------------------------------------------------------------
# Font families — loaded from amethyst.ini at startup via load_font_family().
load_font_family()
FONT_FAMILY = get_font_family()
FONT_MONO_FAMILY = "Liberation Mono"

# Base sizes are tuned for Windows/SteamOS at 96 DPI (tk scaling ~1.33).
# Point sizes are scaled by ui_scale so fonts scale on HiDPI; CustomTkinter
# does not scale user-provided font tuples.
def _font_pt(base: int) -> int:
    """Return scaled point size for font tuples."""
    return max(8, round(base * get_ui_scale()))


def font_sized(name: str, base_pt: int, *styles: str) -> tuple:
    """Return a font tuple (name, size, *styles) with size scaled by ui_scale.
    Use for one-off fonts instead of hardcoding e.g. font=(FONT_FAMILY, 11)."""
    return (name, _font_pt(base_pt), *styles)


def font_sized_px(name: str, base_pt: int, *styles: str) -> tuple:
    """Return a font tuple with pixel size (negative) for tk widgets.
    Use for tk.Label/tk.Button where point sizes may not scale on Linux HiDPI."""
    return (name, _pt_to_px(base_pt), *styles)


FONT_NORMAL = (FONT_FAMILY, _font_pt(14))
FONT_BOLD   = (FONT_FAMILY, _font_pt(14), "bold")
FONT_SMALL  = (FONT_FAMILY, _font_pt(12))
FONT_MONO   = (FONT_MONO_FAMILY, _font_pt(14))
FONT_SEP    = (FONT_FAMILY, _font_pt(12), "bold")
FONT_HEADER = (FONT_FAMILY, _font_pt(12), "bold")

# Pixel sizes for tk.Label / canvas create_text / ttk.Style font= args.
# Negative values tell Tk to treat them as pixels rather than points,
# bypassing Tk's own DPI scaling (which would double-scale on HiDPI).
# The pixel count is fixed at what the design looks like at 96 DPI
# (tk scaling 1.3333): e.g. 11pt * 1.3333 ≈ 15px.
# Multiplied by get_ui_scale() for HiDPI support.
_BASELINE = 1.3333  # 96 DPI / 72 pt

def _pt_to_px(pt: int) -> int:
    """Convert a design point size to a negative-pixel size (96 DPI baseline)."""
    return -max(8, round(pt * _BASELINE * get_ui_scale()))

FS9  = _pt_to_px(9)
FS10 = _pt_to_px(10)
FS11 = _pt_to_px(11)
FS12 = _pt_to_px(12)
FS13 = _pt_to_px(13)
FS16 = _pt_to_px(16)


def init_fonts(widget) -> None:
    """Set Tk scaling to design baseline. Font scaling is done via explicit
    point sizes in FONT_* and FS* (not via tk scaling) so it works on Linux
    where CustomTkinter doesn't scale user-provided fonts.
    """
    try:
        widget.tk.call("tk", "scaling", _BASELINE)
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Icons (package-relative: src/gui/theme.py -> src/icons)
# ---------------------------------------------------------------------------
_ICONS_DIR = Path(__file__).resolve().parent.parent / "icons"


def load_icon(name: str, size: tuple[int, int] = (16, 16)) -> ctk.CTkImage | None:
    """Load a CTkImage from the icons directory. Returns None if file not found.

    Icon size is scaled by ui_scale for HiDPI displays.
    """
    path = _ICONS_DIR / name
    if not path.is_file():
        return None
    scale = get_ui_scale()
    scaled_size = (max(1, round(size[0] * scale)), max(1, round(size[1] * scale)))
    img = PilImage.open(path).convert("RGBA")
    return ctk.CTkImage(light_image=img, dark_image=img, size=scaled_size)


def scaled(px: int | float) -> int:
    """Scale a pixel value by the current UI scale. Use for layout constants
    (row heights, paddings, icon sizes) that CustomTkinter does not scale."""
    return max(1, round(float(px) * get_ui_scale()))


def scaled_layout_minsize(base: int | float) -> int:
    """Scale a layout minsize so it preserves weight ratios at low scales.

    At 1x scale returns *base* unchanged. Below 1x uses scale² so minsize
    shrinks more aggressively and doesn't force ~50/50 when weights expect 5:4.
    Use for grid minsize in weighted layouts (e.g. plugin panel).
    """
    s = get_ui_scale()
    if s >= 1.0:
        return max(1, round(float(base) * s))
    # Below 1x: use scale² so 0.5x → base*0.25, 0.75x → base*0.56
    return max(1, round(float(base) * s * s))
