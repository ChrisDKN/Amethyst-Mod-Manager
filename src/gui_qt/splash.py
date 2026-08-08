"""Transparent, correctly-centred startup splash.

Replaces the old Tkinter splash, which was GTK-backed and relied on window-
manager hints to centre itself - those hints were ignored on some setups, so
the splash landed in a corner. Here we centre explicitly against the screen
geometry, which does not depend on the WM cooperating.

Per-pixel alpha (soft edges/shadow) needs a running compositor; on the Deck
under Gamescope/KWin that is always present. Where it is not, ``setMask`` clips
the window to the logo's alpha shape as a hard-edged fallback so the splash
never shows an opaque rectangle behind the artwork.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import QColor, QCursor, QGuiApplication, QPainter, QPixmap
from PySide6.QtWidgets import QWidget

from gui_qt.theme_qt import active_palette, _c


_LOGO = Path(__file__).resolve().parent.parent / "icons" / "Logo.png"

# Padding around the logo so a drop shadow / rounded card has room to breathe.
_PAD = 28
_RADIUS = 18


class Splash(QWidget):
    """Frameless, translucent splash showing the app logo and a status line."""

    def __init__(self, message: str | None = None) -> None:
        super().__init__(
            None,
            Qt.SplashScreen | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint,
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_DeleteOnClose, True)

        self._pal = active_palette()
        self._message = message if message is not None else self.tr("Starting Amethyst…")

        self._logo = QPixmap(str(_LOGO)) if _LOGO.exists() else QPixmap()
        if not self._logo.isNull():
            # Draw the logo at a sensible on-screen size regardless of the
            # source resolution.
            side = 160
            self._logo = self._logo.scaled(
                side, side,
                Qt.KeepAspectRatio, Qt.SmoothTransformation,
            )

        logo_w = self._logo.width() if not self._logo.isNull() else 160
        logo_h = self._logo.height() if not self._logo.isNull() else 160
        # Extra height for the status line beneath the logo.
        self.resize(logo_w + _PAD * 2, logo_h + _PAD * 2 + 26)

        # Hard-edge fallback for compositor-less X11: clip to the rounded card.
        # (Where per-pixel alpha works this mask is a no-op visually.)
        self._apply_mask()

    def _apply_mask(self) -> None:
        from PySide6.QtGui import QRegion
        r = self.rect().adjusted(0, 0, -1, -1)
        self.setMask(QRegion(r, QRegion.Rectangle))

    def set_message(self, text: str) -> None:
        self._message = text
        self.repaint()   # synchronous: startup thread is busy, no event loop churn

    def center_on_cursor(self) -> None:
        """Centre on whichever screen the cursor is on (the one the user is
        looking at), using availableGeometry so we clear panels/taskbars.

        On a docked Deck the *primary* screen may not be the active one, so we
        prefer the screen under the cursor and fall back to primary."""
        screen = QGuiApplication.screenAt(QCursor.pos())
        if screen is None:
            screen = QGuiApplication.primaryScreen()
        geo = screen.availableGeometry()
        self.move(geo.center() - self.rect().center())

    # -- painting -----------------------------------------------------------

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)

        c = lambda k: _c(self._pal, k)

        # Rounded card behind the logo.
        card = QColor(c("BG_HEADER"))
        card.setAlpha(235)
        p.setPen(Qt.NoPen)
        p.setBrush(card)
        rect = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        p.drawRoundedRect(rect, _RADIUS, _RADIUS)

        # Logo, centred horizontally, near the top.
        if not self._logo.isNull():
            x = (self.width() - self._logo.width()) // 2
            p.drawPixmap(x, _PAD, self._logo)

        # Status line.
        if self._message:
            p.setPen(QColor(c("TEXT_MAIN")))
            f = p.font()
            f.setPointSize(9)
            p.setFont(f)
            text_rect = self.rect().adjusted(
                _PAD, self.height() - _PAD - 20, -_PAD, -_PAD + 6)
            p.drawText(text_rect, Qt.AlignHCenter | Qt.AlignVCenter, self._message)

        p.end()


def splash_disabled() -> bool:
    """Whether the splash should be skipped for this launch."""
    if "--no-splash" in sys.argv:
        return True
    if os.environ.get("AMETHYST_NO_SPLASH", "").strip().lower() in (
            "1", "true", "yes", "on"):
        return True
    # Niri has no X compositing for our (XWayland) windows, so the per-pixel
    # alpha this splash relies on renders as an opaque black rectangle (GH#328).
    desktops = ":".join((
        os.environ.get("XDG_CURRENT_DESKTOP", ""),
        os.environ.get("XDG_SESSION_DESKTOP", ""),
    )).lower()
    if "niri" in desktops:
        return True
    return False


def show_splash(message: str | None = None) -> Splash | None:
    """Build, centre and show a splash. Call after the QApplication exists.

    Returns the widget; the caller must keep a reference and call ``.close()``
    (or ``finish(window)``) once the main window is up. Returns ``None`` when
    the splash is disabled (``--no-splash``, ``AMETHYST_NO_SPLASH=1``, or a
    compositor known to render it as a black rectangle)."""
    if splash_disabled():
        return None
    s = Splash(message)
    s.center_on_cursor()
    s.show()
    # Force an immediate paint before the caller blocks on slow startup work.
    s.repaint()
    QGuiApplication.processEvents()
    return s
