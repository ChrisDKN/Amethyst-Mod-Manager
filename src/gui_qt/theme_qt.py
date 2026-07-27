"""Qt theming — builds a QSS stylesheet from the existing theme palettes.

The palette data in ``gui/themes/*.py`` is plain ``{KEY: "#hex"}`` dicts
(toolkit-neutral), so the Qt app reuses it directly rather than duplicating
colours. Per-theme overrides flow through the same ``THEME_DEFAULTS_OVERRIDE``
mechanism the Tk app uses.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from Utils.themes import load_palettes
from Utils.ui_config import get_appearance_mode

if TYPE_CHECKING:
    from PySide6.QtGui import QColor, QPalette


# Fallback used if a palette is missing a key, so QSS never renders with an
# empty colour string.
_FALLBACK = "#1a1a1a"

_ICONS_DIR = Path(__file__).resolve().parent.parent / "icons"


def _icon_url(name: str) -> str:
    """Forward-slash absolute path to icons/<name> for QSS `image: url(...)`
    (QSS wants POSIX separators even on the path form)."""
    return _ICONS_DIR.joinpath(name).as_posix()


def _tinted_icon_url(name: str, color: str) -> str:
    """Return a POSIX path to a recoloured copy of icons/<name> for QSS
    `image: url(...)`.

    QSS `url()` can't tint a PNG, so we bake a tinted copy once per (name,
    color) into a temp cache dir and hand back its path. The alpha shape of the
    source glyph is preserved; opaque pixels are filled with *color*.
    Falls back to the untinted icon if the source is missing or Qt can't paint.
    """
    src = _ICONS_DIR / name
    if not src.is_file():
        return _icon_url(name)
    import tempfile
    cache_dir = Path(tempfile.gettempdir()) / "amethyst_tinted_icons"
    safe_color = color.lstrip("#").lower() or "none"
    out = cache_dir / f"{Path(name).stem}_{safe_color}.png"
    if not out.is_file():
        from PySide6.QtGui import QPixmap, QPainter, QColor
        from PySide6.QtCore import Qt
        pm = QPixmap(str(src))
        if pm.isNull():
            return _icon_url(name)
        tinted = QPixmap(pm.size())
        tinted.fill(Qt.transparent)
        p = QPainter(tinted)
        p.drawPixmap(0, 0, pm)          # original — for its alpha shape
        p.setCompositionMode(QPainter.CompositionMode_SourceIn)
        p.fillRect(tinted.rect(), QColor(color))
        p.end()
        cache_dir.mkdir(parents=True, exist_ok=True)
        if not tinted.save(str(out), "PNG"):
            return _icon_url(name)
    return out.as_posix()


# The Qt app defaults to the original near-black "dark" palette. (Breeze Dark
# stays available as a selectable theme — appearance_mode = "breeze".) The blue
# checkboxes/selection come from this palette's ACCENT (#0078d4).
_QT_DEFAULT_THEME = "dark"


# Memoised active_palette() result. Model data() methods call it per cell per
# repaint, and an uncached call rescans the theme packages + parses config each
# time. Invalidated by apply_theme() and wherever the theme/appearance is
# changed (settings / theme editor save paths).
_active_palette_cache: dict | None = None


def invalidate_palette_cache() -> None:
    """Drop the memoised active palette (call after the theme changes)."""
    global _active_palette_cache
    _active_palette_cache = None


def active_palette() -> dict:
    """Return the {KEY: hex} palette for the Qt app. Defaults to the dark palette;
    an explicit saved appearance_mode theme wins when present. (Values may be str
    or (light,dark) tuples; _c() normalises them.) Memoised — see
    invalidate_palette_cache()."""
    global _active_palette_cache
    if _active_palette_cache is not None:
        return _active_palette_cache
    palettes = load_palettes()
    mode = get_appearance_mode()
    if mode and mode in palettes:
        pal = palettes[mode]
    else:
        pal = (palettes.get(_QT_DEFAULT_THEME)
               or palettes.get("dark")
               or next(iter(palettes.values()), {}))
    _active_palette_cache = pal
    return pal


def _c(pal: dict, key: str) -> str:
    val = pal.get(key, _FALLBACK)
    # Palette values may be (light, dark) tuples in some themes; take a string.
    if isinstance(val, (tuple, list)):
        val = val[-1]
    return str(val)


def build_qss(pal: dict | None = None) -> str:
    """Build the application QSS from a palette (default: active palette)."""
    p = pal or active_palette()
    c = lambda k: _c(p, k)
    # Auto-contrast text for a coloured fill: label visibility beats palette
    # choice, so button text is never editable — it's derived from the fill.
    ct = lambda k: contrast_text(_c(p, k))
    return f"""
    QWidget {{
        color: {c('TEXT_MAIN')};
        font-size: 13px;
    }}
    QMainWindow, QDialog {{ background: {c('BG_DEEP')}; }}
    /* Transparent by default so labels/checkboxes don't paint near-black boxes
       over their container — containers set their own background explicitly. */
    QLabel, QCheckBox, QRadioButton {{ background: transparent; }}
    QScrollArea, QScrollArea > QWidget > QWidget {{ background: transparent; }}

    /* Toggle indicators — blue when checked, consistent size everywhere. */
    QCheckBox::indicator {{
        width: 16px; height: 16px;
        border: 1px solid {c('BORDER_FAINT')};
        border-radius: 3px;
        background: {c('BG_DEEP')};
    }}
    QCheckBox::indicator:hover {{ border: 1px solid {c('CHECK_FILL')}; }}
    QCheckBox::indicator:checked {{
        background: {c('CHECK_FILL')};
        border: 1px solid {c('CHECK_FILL')};
        image: url({_tinted_icon_url('check_white.png', ct('CHECK_FILL'))});
    }}
    /* Radio: same look as the dropdown-menu exclusive indicator — hollow ring
       unchecked, a fully accent-filled circle when checked (border-radius = half
       the box). 14px to match QMenu::indicator. */
    QRadioButton::indicator {{
        width: 14px; height: 14px;
        border: 1px solid {c('BORDER_FAINT')};
        border-radius: 7px;
        background: {c('BG_DEEP')};
    }}
    QRadioButton::indicator:hover {{ border: 1px solid {c('ACCENT')}; }}
    QRadioButton::indicator:checked {{
        border: 1px solid {c('ACCENT')};
        border-radius: 7px;
        background: {c('ACCENT')};
    }}

    /* Toolbar */
    QToolBar {{
        background: {c('BG_HEADER')};
        border: none;
        spacing: 4px;
        padding: 4px 6px;
    }}
    QToolButton {{
        background: transparent;
        color: {c('TEXT_MAIN')};
        padding: 5px 10px;
        border-radius: 4px;
    }}
    QToolButton:hover {{ background: {c('BG_ROW_HOVER')}; }}
    QToolButton:pressed {{ background: {c('ACCENT')}; color: {ct('ACCENT')}; }}
    QToolButton::menu-button {{ width: 16px; border-left: 1px solid {c('BORDER')}; }}
    QToolButton::menu-arrow {{ width: 8px; height: 8px; }}

    QToolTip {{
        background: {c('BG_HEADER')};
        color: {c('TEXT_MAIN')};
        border: 1px solid {c('ACCENT')};
        border-radius: 4px;
        padding: 5px 8px;
        font-size: 13px;
    }}

    QMenu {{
        background: {c('BG_PANEL')};
        border: 1px solid {c('BORDER')};
        padding: 5px;
    }}
    QMenu::item {{
        padding: 7px 28px 7px 12px;
        border-radius: 4px;
        margin: 1px 2px;
    }}
    QMenu::item:selected {{ background: {c('BG_SELECT')}; color: {c('TEXT_ON_ACCENT')}; }}
    /* Menu indicators. Exclusive (selector) menus = a blue-filled dot when
       checked, a hollow ring otherwise. Non-exclusive (checkable) items use the
       same blue rounded-square box as the modlist / QCheckBox indicators. */
    QMenu::indicator {{
        width: 16px; height: 16px;
        margin-left: 4px;
    }}
    QMenu::indicator:exclusive:unchecked {{
        border: 1px solid {c('BORDER_FAINT')};
        border-radius: 8px;
        background: {c('BG_DEEP')};
    }}
    QMenu::indicator:exclusive:checked {{
        border: 1px solid {c('ACCENT')};
        border-radius: 8px;
        background: {c('ACCENT')};
    }}
    QMenu::indicator:non-exclusive:unchecked {{
        border: 1px solid {c('BORDER_FAINT')};
        border-radius: 3px;
        background: {c('BG_DEEP')};
    }}
    QMenu::indicator:non-exclusive:checked {{
        border: 1px solid {c('CHECK_FILL')};
        border-radius: 3px;
        background: {c('CHECK_FILL')};
        image: url({_tinted_icon_url('check_white.png', ct('CHECK_FILL'))});
    }}
    /* Submenu indicator — our own right-pointing arrow (matches the collapsed
       row indicator) in place of Qt's default triangle. */
    QMenu::right-arrow {{
        width: 12px; height: 12px;
        margin-right: 6px;
        image: url({_tinted_icon_url('right.png', c('DROPDOWN_ARROW'))});
    }}
    QMenu::separator {{ height: 1px; background: {c('BORDER')}; margin: 5px 8px; }}

    /* List / tree */
    QTreeView, QListView {{
        background: {c('BG_LIST')};
        alternate-background-color: {c('BG_ROW_ALT')};
        border: none;
        outline: none;
    }}
    QTreeView::item:selected, QListView::item:selected {{
        background: {c('BG_SELECT')};
        color: {c('TEXT_ON_ACCENT')};
    }}
    QHeaderView::section {{
        background: {c('BG_HEADER')};
        color: {c('TEXT_MAIN')};
        padding: 5px 8px;
        border: none;
        border-right: 1px solid {c('BORDER')};
        border-bottom: 1px solid {c('BORDER')};
    }}

    /* Detachable tabs (overlay replacement) — modern flat look: rounded top,
       no boxy borders, an accent underline on the selected tab. */
    QTabWidget::pane {{ border: none; top: 0; }}
    QTabBar {{ background: {c('BG_HEADER')}; }}
    QTabBar::tab {{
        background: transparent;
        color: {c('TEXT_DIM')};
        /* extra right padding gives the close button its own square area */
        padding: 8px 10px 8px 16px;
        margin: 3px 1px 0 1px;
        border: none;
        border-top-left-radius: 6px;
        border-top-right-radius: 6px;
        /* reserve space for the selected underline so text doesn't shift */
        border-bottom: 2px solid transparent;
    }}
    QTabBar::tab:hover {{
        background: {c('BG_ROW_HOVER')};
        color: {c('TEXT_MAIN')};
    }}
    QTabBar::tab:selected {{
        background: {c('BG_PANEL')};
        color: {c('TEXT_MAIN')};
        border-bottom: 2px solid {c('ACCENT')};
    }}
    /* Close button — a clear square on the right of the tab (matches the
       mockup). Larger hit area, subtle by default, soft rounded red on hover. */
    QTabBar::close-button {{
        image: url({_tinted_icon_url('close_white.png', c('TEXT_DIM'))});
        subcontrol-position: right;
        margin: 3px 6px 3px 4px;
        border-radius: 4px;
    }}
    QTabBar::close-button:hover {{
        background: {c('BTN_DANGER')};
        image: url({_tinted_icon_url('close_white.png', ct('BTN_DANGER'))});
    }}

    /* Slim modern scrollbars — applied globally (modlist, plugins, log, …) */
    QScrollBar:vertical {{
        background: transparent;
        width: 14px;
        margin: 0;
    }}
    QScrollBar:horizontal {{
        background: transparent;
        height: 12px;
        margin: 0;
    }}
    QScrollBar::handle:vertical, QScrollBar::handle:horizontal {{
        background: {c('BORDER_FAINT')};
        border-radius: 5px;
        min-height: 28px;
        min-width: 28px;
        margin: 2px;
    }}
    QScrollBar::handle:hover {{ background: {c('TEXT_DIM')}; }}
    QScrollBar::add-line, QScrollBar::sub-line {{
        width: 0; height: 0; background: none; border: none;
    }}
    QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

    /* Status bar + bottom bar */
    QStatusBar {{
        background: {c('BG_HEADER')};
        color: {c('TEXT_DIM')};
        border-top: 1px solid {c('BORDER')};
    }}
    QStatusBar::item {{ border: none; }}
    #BottomBar {{
        background: {c('BG_PANEL')};
        border-top: 1px solid {c('BORDER')};
    }}

    /* Generic buttons / inputs */
    QPushButton {{
        background: {c('ACCENT')};
        color: {ct('ACCENT')};
        border: none;
        padding: 6px 12px;
        border-radius: 4px;
    }}
    QPushButton:hover {{ background: {c('ACCENT_HOV')}; }}
    QComboBox, QLineEdit {{
        background: {c('BG_ROW')};
        border: 1px solid {c('BORDER')};
        border-radius: 4px;
        padding: 4px 8px;
    }}
    /* Replace Qt's default triangle drop-down indicator with arrow.png. */
    QComboBox::drop-down {{
        subcontrol-origin: padding;
        subcontrol-position: center right;
        width: 20px;
        border: none;
        background: transparent;
    }}
    QComboBox::down-arrow {{
        image: url({_tinted_icon_url('arrow.png', c('DROPDOWN_ARROW'))});
        width: 12px;
        height: 12px;
    }}
    QSplitter::handle {{ background: {c('BORDER')}; }}
    QSplitter::handle:horizontal {{ width: 6px; }}
    QSplitter::handle:vertical {{ height: 6px; }}
    /* Highlight the grip on hover/drag so a fully-collapsed panel's handle is
       easy to find and grab. */
    QSplitter::handle:hover {{ background: {c('ACCENT')}; }}
    QSplitter::handle:pressed {{ background: {c('ACCENT')}; }}
    /* FOMOD wizard divider — a visible, grabbable handle. */
    #FomodSplit::handle {{ background: {c('BORDER')}; }}
    #FomodSplit::handle:horizontal {{ width: 6px; }}
    #FomodSplit::handle:hover {{ background: {c('ACCENT')}; }}
    /* FOMOD option groups — larger text + indicators for readability. */
    #FomodGroup {{
        background: {c('BG_PANEL')};
        border: 1px solid {c('BORDER')};
        border-radius: 8px;
    }}
    #FomodGroupTitle {{ font-size: 15px; font-weight: 600; }}
    #FomodGroup QRadioButton, #FomodGroup QCheckBox {{
        font-size: 14px;
        padding: 4px 0;
        spacing: 10px;
    }}
    #FomodGroup QRadioButton::indicator,
    #FomodGroup QCheckBox::indicator {{ width: 20px; height: 20px; }}
    #FomodGroup QRadioButton::indicator,
    #FomodGroup QRadioButton::indicator:checked {{ border-radius: 10px; }}

    #StatusChip {{
        background: {c('ACCENT')};
        color: {ct('ACCENT')};
        border-radius: 3px;
        padding: 3px 8px;
    }}
    #PlaceholderPane {{
        background: {c('BG_PANEL')};
        color: {c('TEXT_FAINT')};
    }}

    /* Add-Game picker cards */
    #GameCard {{
        background: {c('BG_PANEL')};
        border: 1px solid {c('BORDER')};
        border-radius: 8px;
    }}
    #GameCard:hover {{ border: 1px solid {c('ACCENT')}; }}
    #GameCardName {{ color: {c('TEXT_MAIN')}; font-weight: 600; font-size: 12px; }}
    #GameSelectBtn {{
        background: {c('BTN_SUCCESS')}; color: {ct('BTN_SUCCESS')}; font-weight: 600;
        border: none; border-radius: 4px; padding: 5px 0;
    }}
    #GameSelectBtn:hover {{ background: {c('BTN_SUCCESS_HOV')}; }}
    #GameAddBtn {{
        background: {c('ACCENT')}; color: {ct('ACCENT')}; font-weight: 600;
        border: none; border-radius: 4px; padding: 5px 0;
    }}
    #GameAddBtn:hover {{ background: {c('ACCENT_HOV')}; }}

    /* Header bars (left two-tier header + right play bar) */
    #HeaderBar {{
        background: {c('BG_HEADER')};
        border-bottom: 1px solid {c('BORDER')};
    }}
    /* Dim caption above a tab's column header (Mod Files / Data / …). Palette-
       driven so it stays legible in light themes (was a hardcoded #aaa). */
    #HeaderCaption {{ color: {c('TEXT_DIM')}; }}
    #GroupSep {{ background: {c('BORDER')}; border: none; }}
    #ActionButton {{
        background: {c('BG_ROW')};
        color: {c('TEXT_MAIN')};
        border: 1px solid {c('BORDER')};
        border-radius: 5px;
        padding: 6px 12px;
        font-size: 14px;
    }}
    /* Split buttons (with a dropdown arrow) need extra right padding so the
       label never runs under the 22px arrow section. */
    #ActionButton[split="true"] {{ padding: 6px 28px 6px 12px; }}
    /* Deployed profile: only the TEXT goes green when the current selection is
       the deployed one (button chrome stays normal). */
    #ActionButton[deployed="true"] {{ color: {c('TEXT_OK_BRIGHT')}; }}
    #ActionButton:hover {{ background: {c('BG_ROW_HOVER')}; }}
    /* Menu open (menuOpen property) OR pressed → whole button + arrow go blue. */
    #ActionButton:pressed, #ActionButton[menuOpen="true"] {{
        background: {c('ACCENT')}; color: {ct('ACCENT')};
    }}
    /* Split-button arrow section (right of the divider), like the mockup. */
    #ActionButton::menu-button {{
        background: transparent;
        border-left: 1px solid {c('BORDER')};
        width: 22px;
        border-top-right-radius: 5px;
        border-bottom-right-radius: 5px;
    }}
    #ActionButton::menu-button:hover {{ background: {c('BG_ROW_HOVER')}; }}
    /* When the menu is open the arrow section matches the highlighted button. */
    #ActionButton[menuOpen="true"]::menu-button {{
        background: {c('ACCENT')};
        border-left: 1px solid {c('ACCENT_HOV')};
    }}
    /* Split-button dropdown arrow (QToolButton = ::menu-indicator,
       QPushButton = ::menu-arrow): use arrow.png instead of Qt's triangle. */
    #ActionButton::menu-indicator, #ActionButton::menu-arrow {{
        image: url({_tinted_icon_url('arrow.png', c('DROPDOWN_ARROW'))});
        width: 10px; height: 10px;
        subcontrol-origin: padding;
        subcontrol-position: center right;
        right: 6px;
    }}
    /* Square icon-only toolbar button (Settings). */
    #IconButton {{
        background: {c('BG_ROW')};
        border: 1px solid {c('BORDER')};
        border-radius: 5px;
    }}
    #IconButton:hover {{ background: {c('BG_ROW_HOVER')}; }}
    #IconButton:pressed {{ background: {c('ACCENT')}; }}
    /* Configure-Game form body + monospace path fields + buttons. */
    #FormBody {{ background: {c('BG_DEEP')}; }}
    /* The four bordered card panels in the Configure-Game view. */
    #ConfigPanel {{
        background: {c('BG_PANEL')};
        border: 1px solid {c('BORDER')};
        border-radius: 8px;
    }}
    #ConfigPanel QLabel {{ background: transparent; }}
    #PathEdit {{
        background: {c('BG_ROW')};
        color: {c('TEXT_MAIN')};
        border: 1px solid {c('BORDER')};
        border-radius: 4px;
        padding: 8px 10px;
    }}
    /* Consistent form button (Browse/Open/Scan/Reset, Cancel) — same height as
       the primary Save/Danger buttons so rows line up. */
    #FormButton {{
        background: {c('BG_ROW')};
        color: {c('TEXT_MAIN')};
        border: 1px solid {c('BORDER')};
        border-radius: 4px;
        padding: 0 14px;
        min-height: 30px;
        font-size: 13px;
    }}
    #FormButton:hover {{ background: {c('BG_ROW_HOVER')}; }}
    #FormButton:pressed {{ background: {c('ACCENT')}; color: {ct('ACCENT')}; }}
    #PrimaryButton {{
        background: {c('ACCENT')}; color: {ct('ACCENT')}; font-weight: 600;
        border: none; border-radius: 4px; padding: 0 18px;
        min-height: 30px; font-size: 13px;
    }}
    #PrimaryButton:hover {{ background: {c('ACCENT_HOV')}; }}
    #PrimaryButton:disabled {{ background: {c('BG_ROW')}; color: {c('TEXT_DIM')}; }}
    #DangerButton {{
        background: {c('RED_BTN')}; color: {ct('RED_BTN')}; font-weight: 600;
        border: none; border-radius: 4px; padding: 0 16px;
        min-height: 30px; font-size: 13px;
    }}
    #DangerButton:hover {{ background: {c('RED_HOV')}; }}
    #FooterButton {{
        background: {c('BG_ROW')};
        color: {c('TEXT_MAIN')};
        border: 1px solid {c('BORDER')};
        border-radius: 4px;
        padding: 4px 12px;
        font-size: 12px;
    }}
    #FooterButton:hover {{ background: {c('BG_ROW_HOVER')}; }}
    #FooterButton:pressed {{ background: {c('ACCENT')}; color: {ct('ACCENT')}; }}
    /* Filters footer button lights up while any filter is active. */
    #FooterButton[active="true"] {{
        background: {c('ACCENT')};
        color: {ct('ACCENT')};
        border: 1px solid {c('ACCENT')};
    }}
    #FooterButton[active="true"]:hover {{ background: {c('ACCENT_HOV')}; }}
    #PlayButton {{
        background: {c('BTN_SUCCESS')};
        color: {ct('BTN_SUCCESS')};
        font-weight: 600;
        font-size: 14px;
        padding: 6px 18px;
        border: none;
        border-radius: 5px;
    }}
    #PlayButton:hover {{ background: {c('BTN_SUCCESS')}; }}

    /* Bottom log panel */
    #LogBar {{
        background: {c('BG_HEADER')};
        border-top: 1px solid {c('BORDER')};
    }}
    #LogView {{
        background: {c('BG_DEEP')};
        color: {c('TEXT_MAIN')};
        border: none;
        border-top: 1px solid {c('BORDER')};
        font-family: monospace;
        font-size: 12px;
    }}

    /* Deploy/restore progress popup + notification toasts. */
    #ProgressPopup {{
        background: {c('BG_PANEL')};
        border: 1px solid {c('BORDER')};
        border-radius: 8px;
    }}
    QProgressBar {{
        background: {c('BG_DEEP')};
        border: none;
        border-radius: 4px;
    }}
    QProgressBar::chunk {{
        background: {c('ACCENT')};
        border-radius: 4px;
    }}
    #Toast {{
        background: {c('BG_PANEL')};
        border: 1px solid {c('BORDER')};
        border-radius: 8px;
    }}
    #ToastDot[state="info"] {{ color: {c('ACCENT')}; }}
    #ToastDot[state="success"] {{ color: {c('TEXT_OK_BRIGHT')}; }}
    #ToastDot[state="warning"] {{ color: {c('TEXT_WARN_BRIGHT')}; }}
    #ToastDot[state="error"] {{ color: {c('STATUS_ERR_BRIGHT')}; }}
    """


def build_qpalette(p: dict) -> "QPalette":
    """Role-based QPalette for palette *p* so stock widgets (menus, combos,
    tooltips, disabled states) read the theme colours even where QSS doesn't
    reach. Applied app-wide at startup; also set on the theme editor's preview
    subtree so its stock-widget bits track the working palette."""
    from PySide6.QtGui import QPalette, QColor

    c = lambda k: QColor(_c(p, k))
    pal = QPalette()
    pal.setColor(QPalette.Window, c("BG_DEEP"))
    pal.setColor(QPalette.WindowText, c("TEXT_MAIN"))
    pal.setColor(QPalette.Base, c("BG_LIST"))
    pal.setColor(QPalette.AlternateBase, c("BG_ROW_ALT"))
    pal.setColor(QPalette.Text, c("TEXT_MAIN"))
    pal.setColor(QPalette.Button, c("BG_HEADER"))
    pal.setColor(QPalette.ButtonText, c("TEXT_MAIN"))
    pal.setColor(QPalette.Highlight, c("BG_SELECT"))
    pal.setColor(QPalette.HighlightedText, c("TEXT_ON_ACCENT"))
    pal.setColor(QPalette.ToolTipBase, c("BG_PANEL"))
    pal.setColor(QPalette.ToolTipText, c("TEXT_MAIN"))
    pal.setColor(QPalette.PlaceholderText, c("TEXT_FAINT"))
    pal.setColor(QPalette.Link, c("LINK_BLUE"))
    pal.setColor(QPalette.BrightText, c("TEXT_WHITE"))
    # Fusion draws bevels/frames from these shade roles — seed them so panels,
    # group boxes, frames and sunken borders read on the dark theme instead of
    # the default near-black/near-white guesses.
    pal.setColor(QPalette.Light, c("BORDER_FAINT"))
    pal.setColor(QPalette.Midlight, c("BORDER_DIM"))
    pal.setColor(QPalette.Mid, c("BORDER"))
    pal.setColor(QPalette.Dark, c("BG_DEEP"))
    pal.setColor(QPalette.Shadow, c("BG_OVERLAY_DEEP"))
    # Keep selection vivid even when the window/widget isn't focused (otherwise
    # Fusion greys the Inactive-group highlight, which looks broken in lists).
    pal.setColor(QPalette.Inactive, QPalette.Highlight, c("BG_SELECT"))
    pal.setColor(QPalette.Inactive, QPalette.HighlightedText, c("TEXT_ON_ACCENT"))
    # Disabled states (greyed text) across all relevant roles.
    dim = c("TEXT_DIM")
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText,
                 QPalette.ToolTipText):
        pal.setColor(QPalette.Disabled, role, dim)
    return pal


def _apply_qpalette(app, p: dict) -> None:
    app.setPalette(build_qpalette(p))


def _make_proxy_style(base):
    """Wrap *base* QStyle in a ProxyStyle that enlarges the tab close indicator
    so the close button fills the tab height (QSS width/height alone is clamped
    by the style's PM_TabCloseIndicator metric), and shortens the tooltip
    wake-up delay app-wide (the default ~700ms feels sluggish)."""
    from PySide6.QtWidgets import QProxyStyle, QStyle

    class _TabProxyStyle(QProxyStyle):
        def pixelMetric(self, metric, option=None, widget=None):
            if metric in (QStyle.PM_TabCloseIndicatorWidth,
                          QStyle.PM_TabCloseIndicatorHeight):
                return 22
            return super().pixelMetric(metric, option, widget)

        def styleHint(self, hint, option=None, widget=None, returnData=None):
            # Show tooltips faster: the default hover-to-show delay is ~700ms.
            if hint == QStyle.SH_ToolTip_WakeUpDelay:
                return 250
            return super().styleHint(hint, option, widget, returnData)

    proxy = _TabProxyStyle(base) if base is not None else _TabProxyStyle()
    return proxy


def _resolve_base_style(p: dict):
    """Return a created QStyle for the theme's declared base style. A palette may
    set BASE_QSTYLE (MO2-style 'base style per theme'); default Fusion, and a
    real 'Breeze' QStyle is used when the plugin is installed. Falls back through
    Breeze→Fusion→whatever's available."""
    from PySide6.QtWidgets import QStyleFactory
    keys = {k.lower(): k for k in QStyleFactory.keys()}
    wanted = str(p.get("BASE_QSTYLE", "") or "").lower()
    # Default to Fusion (what the QSS was authored against) rather than any
    # system style. On the flatpak/Steam Deck a Breeze plugin is present and
    # would otherwise win, but Breeze draws its own QTabBar baseline/shape that
    # the QSS can't fully suppress (the stray white underline under the tabs).
    # Only honour Breeze when a theme opts in explicitly via BASE_QSTYLE.
    pick = (keys.get(wanted)
            or keys.get("fusion")
            or keys.get("breeze")
            or (QStyleFactory.keys()[0] if QStyleFactory.keys() else None))
    return QStyleFactory.create(pick) if pick else None


def _lighten(hex_color: str, factor: float = 0.18) -> str:
    """Return *hex_color* blended toward white by *factor* (0..1) — used for the
    hover state of danger buttons so it lifts consistently regardless of theme."""
    h = hex_color.lstrip("#")
    if len(h) != 6:
        return hex_color
    try:
        r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return hex_color
    r = int(r + (255 - r) * factor)
    g = int(g + (255 - g) * factor)
    b = int(b + (255 - b) * factor)
    return f"#{r:02x}{g:02x}{b:02x}"


def contrast_text(bg: str, dark: str = "#101010", light: str = "#ffffff") -> str:
    """Return whichever of *dark* / *light* reads best on the *bg* fill.

    Uses the WCAG relative-luminance threshold so a button label is always
    visible regardless of how light or dark its background is (e.g. a yellow
    or cyan fill gets dark text; a deep red fill gets light text). Falls back
    to *light* when *bg* can't be parsed."""
    h = bg.lstrip("#")
    if len(h) != 6:
        return light
    try:
        r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    except ValueError:
        return light
    # Linearise then weight per Rec. 709 for perceived luminance.
    def _lin(c):
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    lum = 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)
    return dark if lum > 0.4 else light


def qc(pal: dict, key: str) -> "QColor":
    """QColor for palette *key* — shorthand for ``QColor(_c(pal, key))``,
    the incantation every delegate __init__ repeats per colour."""
    from PySide6.QtGui import QColor
    return QColor(_c(pal, key))


def qc_contrast(pal: dict, key: str) -> "QColor":
    """Auto-contrasted text QColor for the fill at palette *key* (shorthand
    for ``QColor(contrast_text(_c(pal, key)))``)."""
    from PySide6.QtGui import QColor
    return QColor(contrast_text(_c(pal, key)))


# One fixed size for every in-view close button (see danger_close_button).
CLOSE_BTN_SIZE = (90, 30)


def button_qss(key: str, *, hover_key: str | None = None,
               text_key: str | None = None,
               disabled_bg_key: str = "BTN_GREY",
               disabled_fg_key: str = "TEXT_DIM",
               pal: dict | None = None,
               padding: str = "8px 24px") -> str:
    """Return a palette-driven ``QPushButton`` stylesheet string.

    Central builder so the many tab/wizard views that used to hardcode
    ``background:#2d6a9e``-style hex (blue "Select", green "Done", orange,
    red close) all pull their colours from the active theme instead — which
    is what lets a monotone / high-contrast theme actually take effect.

    *key* is the palette key for the base fill; the hover is *hover_key* when
    given, otherwise the base blended toward white via :func:`_lighten`. The
    label colour is **auto-contrasted** off the fill (:func:`contrast_text`) so
    it stays visible on any theme — button text is deliberately not editable,
    since visibility matters more than the exact colour. Pass *text_key* only
    to force a specific palette key. Disabled fill/text are palette-driven."""
    if pal is None:
        pal = active_palette()
    bg = _c(pal, key)
    hover = _c(pal, hover_key) if hover_key else _lighten(bg)
    fg = _c(pal, text_key) if text_key else contrast_text(bg)
    dis_bg = _c(pal, disabled_bg_key)
    dis_fg = _c(pal, disabled_fg_key)
    return (
        "QPushButton{background:%s; color:%s; border:none;"
        " padding:%s; border-radius:4px; font-weight:600;}"
        "QPushButton:hover{background:%s;}"
        "QPushButton:disabled{background:%s; color:%s;}"
        % (bg, fg, padding, hover, dis_bg, dis_fg))


def ok_text(pal: dict | None = None) -> str:
    """Palette colour for success/green status labels (was hardcoded #6bc76b)."""
    return _c(pal or active_palette(), "TEXT_OK_BRIGHT")


def err_text(pal: dict | None = None) -> str:
    """Palette colour for error/red status labels (was hardcoded #e06c6c)."""
    return _c(pal or active_palette(), "TEXT_ERR_BRIGHT")


def danger_close_button(text: str = "✕ Close", pal: dict | None = None):
    """Shared red close button for tab/scoped views.

    Every view that opens in a tab dismisses itself with an identical control:
    the theme's ``BTN_DANGER`` red (adapts to dark/light/breeze), a lighter
    hover, a single fixed size, rounded corners, and a pointing-hand cursor.
    Callers just connect ``.clicked``. Keeping this in one place is why the old
    per-view hardcoded ``#6b3333`` maroon buttons could all be replaced."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QPushButton
    if pal is None:
        pal = active_palette()
    danger = _c(pal, "BTN_DANGER")
    hover = _lighten(danger)
    fg = contrast_text(danger)
    btn = QPushButton(text)
    btn.setFixedSize(*CLOSE_BTN_SIZE)
    btn.setCursor(Qt.PointingHandCursor)
    btn.setStyleSheet(
        "QPushButton{background:%s; color:%s; border:none;"
        " border-radius:4px; font-weight:600;}"
        "QPushButton:hover{background:%s;}" % (danger, fg, hover))
    return btn


def apply_theme(app) -> None:
    """Apply the active theme: a base QStyle (Fusion, or the theme's declared
    BASE_QSTYLE / system Breeze when present) wrapped in a ProxyStyle (enlarged
    tab close button) + a role-based QPalette + the QSS overlay. Mirrors MO2's
    QStyle+QSS model, with QPalette added so Fusion's disabled/tooltip/frame/
    inactive states look right (MO2 leans on native styles for those)."""
    invalidate_palette_cache()
    p = active_palette()
    base = _resolve_base_style(p)
    app.setStyle(_make_proxy_style(base))
    _apply_qpalette(app, p)
    app.setStyleSheet(build_qss(p))
