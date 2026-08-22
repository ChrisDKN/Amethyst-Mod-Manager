"""CRT scanline overlay - a click-through sheet painted over a whole window.

Only themes that ask for one get one. A palette declaring ``SCANLINE_ALPHA``
above zero (currently just Pip-Boy) makes ``apply_theme`` attach an overlay to
every top-level window; every other theme creates no widget at all, so nothing
existing changes cost or behaviour.

Why an overlay widget and not a QSS ``background-image``: a stylesheet
background only reaches widgets that paint their own background. Item-view rows
come from delegates, so the QSS route covers the chrome and misses the modlist,
plugin list and trees - most of the app. A sheet on top covers everything
uniformly, text included, which is what makes it read as a CRT rather than as
striped wallpaper.

The cost is real and is why this is opt-in: the sheet overlaps its siblings, so
views underneath lose Qt's blit-scroll fast path and repaint their viewport
while scrolling.

A dark line alone is close to invisible on these themes, and the reason is
headroom rather than strength: darkening a near-black panel has almost nowhere
to go, so #0a1a0d only reaches #08150b. OLED resolves that step and a typical
LCD does not. Hence the *glow* row - a faint lit line next to the dark one.
Brightening near-black has all the headroom in the world, and it is also how a
real CRT reads: lit phosphor rows separated by dark gaps, not a grey mesh laid
over the picture. Themes that want a visible effect should set both.

``AMM_SCANLINES`` tunes the result without editing a theme: ``0`` forces the
effect off entirely, and any other number scales both opacities (``1.5`` =
half again as strong, ``0.5`` = half). Panels differ enough that this is the
practical way to find a value, then bake it into the palette.

Palette keys (all optional; absent or SCANLINE_ALPHA <= 0 means "no overlay"):
    SCANLINE_COLOR       dark line colour, "#rrggbb"      (default #000000)
    SCANLINE_ALPHA       dark line opacity, 0-255         (0 = off)
    SCANLINE_PITCH       period in logical px             (default 3)
    SCANLINE_THICKNESS   dark line height in the period   (default 1)
    SCANLINE_GLOW_COLOR  lit line colour, "#rrggbb"       (default #ffffff)
    SCANLINE_GLOW_ALPHA  lit line opacity, 0-255          (0 = no lit line)
"""

from __future__ import annotations

import os
import weakref
from typing import NamedTuple

from PySide6.QtCore import Qt, QEvent, QTimer
from PySide6.QtGui import QBrush, QColor, QPainter, QPixmap
from PySide6.QtWidgets import QMainWindow, QWidget

# Device px per tile. One column would tile correctly but costs a blit per
# pixel of width; a wider tile is the same picture with far fewer draws.
_TILE_WIDTH = 64

# A pitch beyond this stops reading as scanlines and starts reading as stripes.
_MAX_PITCH = 32


class ScanlineSpec(NamedTuple):
    """A resolved scanline description: both lines' colour/opacity, and pitch."""

    color: str
    alpha: int
    pitch: int
    thickness: int
    glow_color: str = "#ffffff"
    glow_alpha: int = 0


def _num(value, default: int) -> int:
    """Coerce a palette value to int, tolerating the str form JSON themes use."""
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def _colour(value, default: str) -> str:
    """Validate a palette colour, falling back rather than painting garbage."""
    text = str(value or "").strip()
    return text if text and QColor(text).isValid() else default


def intensity() -> float:
    """Read the AMM_SCANLINES opacity multiplier (0 means the effect is off)."""
    raw = os.environ.get("AMM_SCANLINES", "").strip()
    if not raw:
        return 1.0
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 1.0


def spec_from_palette(pal: dict | None) -> ScanlineSpec | None:
    """Resolve a palette's scanline keys, or None when the theme wants none.

    Pure data - no Qt widgets - so tests can check a theme's declaration
    without a QApplication.
    """
    scale = intensity()
    if not pal or scale <= 0:
        return None
    alpha = _num(pal.get("SCANLINE_ALPHA"), 0)
    if alpha <= 0:
        return None
    pitch = max(2, min(_MAX_PITCH, _num(pal.get("SCANLINE_PITCH"), 3)))
    thickness = max(1, min(pitch - 1, _num(pal.get("SCANLINE_THICKNESS"), 1)))
    glow_alpha = max(0, _num(pal.get("SCANLINE_GLOW_ALPHA"), 0))
    return ScanlineSpec(
        color=_colour(pal.get("SCANLINE_COLOR"), "#000000"),
        alpha=max(1, min(255, round(alpha * scale))),
        pitch=pitch,
        thickness=thickness,
        glow_color=_colour(pal.get("SCANLINE_GLOW_COLOR"), "#ffffff"),
        glow_alpha=min(255, round(glow_alpha * scale)),
    )


class ScanlineOverlay(QWidget):
    """A transparent child widget that tiles horizontal lines over its host.

    Created as a child of the window it covers (not of the central widget) so
    it also sits over the menu bar, tab bar and any in-window overlay views.
    It never takes input: WA_TransparentForMouseEvents passes every event
    through to whatever is really under the cursor.
    """

    def __init__(self, host: QWidget, spec: ScanlineSpec):
        super().__init__(host)
        self.setObjectName("ScanlineOverlay")
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setFocusPolicy(Qt.NoFocus)
        self._spec = spec
        self._brush = QBrush()
        self._raise_queued = False
        self._build_brush()
        host.installEventFilter(self)
        self._sync_geometry()
        self.show()
        self.raise_()

    def set_spec(self, spec: ScanlineSpec) -> None:
        """Adopt a new spec (theme change / live edit) and repaint."""
        if spec == self._spec:
            return
        self._spec = spec
        self._build_brush()
        self.update()

    def _build_brush(self) -> None:
        """Bake the repeating tile once; painting is then a single fillRect."""
        spec = self._spec
        dpr = self.devicePixelRatioF() or 1.0
        # Build in device pixels and tag the tile with the ratio, so the lines
        # keep their logical-pixel size instead of thinning out on HiDPI.
        pitch = max(1, round(spec.pitch * dpr))
        thickness = max(1, min(pitch, round(spec.thickness * dpr)))
        tile = QPixmap(_TILE_WIDTH, pitch)
        tile.setDevicePixelRatio(dpr)
        tile.fill(Qt.transparent)
        color = QColor(spec.color)
        color.setAlpha(spec.alpha)
        painter = QPainter(tile)
        painter.fillRect(0, 0, _TILE_WIDTH, thickness, color)
        # The lit row sits immediately below the dark one, so the two make a
        # single hard step. Spreading the glow over every remaining row instead
        # would raise the average brightness (tinting the whole UI) for less
        # visible contrast than one sharp edge buys.
        glow_rows = min(thickness, pitch - thickness)
        if spec.glow_alpha > 0 and glow_rows > 0:
            glow = QColor(spec.glow_color)
            glow.setAlpha(spec.glow_alpha)
            painter.fillRect(0, thickness, _TILE_WIDTH, glow_rows, glow)
        painter.end()
        self._brush = QBrush(tile)

    def paintEvent(self, event):                      # noqa: N802 (Qt override)
        if self._brush.style() == Qt.NoBrush:
            return
        painter = QPainter(self)
        # Crisp edges: a smoothed or antialiased tile blurs 1px lines into a
        # flat wash at anything but dpr 1.
        painter.setRenderHint(QPainter.Antialiasing, False)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, False)
        painter.fillRect(event.rect(), self._brush)

    def _sync_geometry(self) -> None:
        host = self.parentWidget()
        if host is not None:
            self.setGeometry(0, 0, host.width(), host.height())

    def _schedule_raise(self) -> None:
        """Re-raise once the current event settles, coalescing bursts.

        Widgets added to the window later (in-window overlay views, the
        detached-tab drop indicator) stack above us. A queued raise puts the
        sheet back on top without recursing through the ZOrderChange our own
        raise_() emits.
        """
        if self._raise_queued:
            return
        self._raise_queued = True
        QTimer.singleShot(0, self._do_raise)

    def _do_raise(self) -> None:
        self._raise_queued = False
        if self.parentWidget() is None:
            return
        self._sync_geometry()
        self.raise_()
        self.show()

    def eventFilter(self, obj, event):                # noqa: N802 (Qt override)
        if obj is self.parentWidget():
            kind = event.type()
            if kind == QEvent.Resize:
                self._sync_geometry()
            elif kind in (QEvent.ChildAdded, QEvent.Show,
                          QEvent.WindowActivate):
                self._schedule_raise()
        return super().eventFilter(obj, event)


# host window -> its overlay. Weak keys so a closed window takes its entry with
# it (the overlay itself is a child and dies with the host anyway).
_overlays: "weakref.WeakKeyDictionary[QWidget, ScanlineOverlay]" = \
    weakref.WeakKeyDictionary()


def attach(window: QWidget, palette: dict | None = None) -> None:
    """Give *window* a scanline sheet, or take away the one it has.

    Safe to call repeatedly - an existing overlay is retuned in place rather
    than rebuilt, and a theme without scanlines removes it.
    """
    if palette is None:
        from gui_qt.theme_qt import active_palette
        palette = active_palette()
    spec = spec_from_palette(palette)
    existing = _overlays.get(window)
    if spec is None:
        if existing is not None:
            existing.setParent(None)
            existing.deleteLater()
            _overlays.pop(window, None)
        return
    if existing is not None:
        existing.set_spec(spec)
        existing._do_raise()
        return
    _overlays[window] = ScanlineOverlay(window, spec)


def sync(app, palette: dict | None = None) -> None:
    """Attach/detach overlays across every window the app owns.

    Only QMainWindow top-levels qualify: that is the main window plus detached
    tabs. Menus, tooltips and combo popups are top-level widgets too, and a
    sheet over those would just dim them.
    """
    try:
        windows = app.topLevelWidgets()
    except Exception:
        return
    for window in windows:
        if isinstance(window, QMainWindow):
            attach(window, palette)
