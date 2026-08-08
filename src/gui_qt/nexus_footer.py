"""Footer Nexus widget: shows the logged-in Nexus Mods username on the log bar;
hovering shows the API rate-limit usage in a small popup ABOVE the label.

Mirrors the Tk status bar's rate-limit feature (gui/status_bar.py) but presents
the username on the label and the hourly/daily remaining counts in the popup
(matching the requested design). Rate limits are read passively from
``api.rate_limits`` - captured from response headers on every Nexus request - so
the periodic refresh never makes a network call.

We use our OWN frameless popup rather than QToolTip: QToolTip anchors to the
cursor, so positioning it above the cursor made it flicker (cursor leaves the
tip → hide → re-show). This popup is shown on enter / hidden on leave and stays
put above the label while hovering.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, QPoint
from PySide6.QtWidgets import QLabel

from gui_qt.theme_qt import active_palette, _c


class _HoverPopup(QLabel):
    """A small frameless info window - white text, blue border, above the label."""

    def __init__(self, accent: str):
        # Qt.ToolTip = frameless, floats above, never takes focus / activation.
        super().__init__(None, Qt.ToolTip | Qt.FramelessWindowHint)
        p = active_palette()
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setStyleSheet(
            f"QLabel {{ background: {_c(p, 'BG_HEADER')}; color: {_c(p, 'TEXT_MAIN')};"
            f" border: 1px solid {accent}; border-radius: 4px;"
            f" padding: 6px 9px; font-size: 13px; }}")

    def show_above(self, anchor: QLabel) -> None:
        """Position the popup so its bottom edge sits just above *anchor*'s top,
        roughly centred on the anchor, then show it - clamped to stay within the
        application window (so a corner-anchored label doesn't push it off-screen).
        """
        self.adjustSize()
        sz = self.sizeHint()
        top = anchor.mapToGlobal(QPoint(0, 0))
        x = top.x() + (anchor.width() - sz.width()) // 2
        y = top.y() - sz.height() - 6

        # Clamp inside the app window's global rect (with a small inset).
        win = anchor.window()
        if win is not None:
            wtl = win.mapToGlobal(QPoint(0, 0))
            left, right = wtl.x() + 4, wtl.x() + win.width() - 4
            x = max(left, min(x, right - sz.width()))
            # If there's no room above (window top), drop below the anchor.
            if y < wtl.y() + 4:
                y = anchor.mapToGlobal(QPoint(0, anchor.height())).y() + 6
        self.move(x, y)
        self.show()


class NexusFooterLabel(QLabel):
    """Username label with a rate-limit hover popup.

    *get_api* is a zero-arg callable returning the current NexusAPI (or None);
    the label re-reads it each tick so it lights up once the API is created /
    validated after startup.
    """

    def __init__(self, get_api, parent=None):
        super().__init__(parent)
        self._get_api = get_api
        self._username: str | None = None
        p = active_palette()
        # Theme foreground (dark on light, near-white on dark) so the pill text
        # reads in both modes; warn/err override it when the budget runs low.
        self._col_main = _c(p, "TEXT_MAIN")
        self._col_warn = _c(p, "TEXT_WARN")
        self._col_err = _c(p, "TEXT_ERR")
        self._accent = _c(p, "ACCENT")
        self._tip_text = ""
        self._popup: _HoverPopup | None = None
        self.setObjectName("NexusFooterLabel")
        self.setAlignment(Qt.AlignCenter)
        self.setMouseTracking(True)
        self._apply_style(self._col_main)
        self._refresh()
        # Poll cached state every 10s (no network) - matches the Tk cadence.
        self._timer = QTimer(self)
        self._timer.setInterval(10_000)
        self._timer.timeout.connect(self._refresh)
        self._timer.start()

    def _apply_style(self, text_color: str) -> None:
        """White (or warn/err) text inside a rounded blue-bordered pill."""
        self.setStyleSheet(
            f"QLabel#NexusFooterLabel {{ color: {text_color};"
            f" border: 1px solid {self._accent}; border-radius: 4px;"
            f" padding: 2px 8px; }}")

    # ---- hover popup ------------------------------------------------------
    def enterEvent(self, event):
        if self._tip_text:
            if self._popup is None:
                self._popup = _HoverPopup(self._accent)
            self._popup.setText(self._tip_text)
            self._popup.show_above(self)
        super().enterEvent(event)

    def leaveEvent(self, event):
        if self._popup is not None:
            self._popup.hide()
        super().leaveEvent(event)

    def hideEvent(self, event):
        # Don't leave a stray popup floating if the bar is hidden mid-hover.
        if self._popup is not None:
            self._popup.hide()
        super().hideEvent(event)

    def set_username(self, name: str | None) -> None:
        """Set the validated Nexus username (None = unknown / logged out)."""
        self._username = name or None
        self._refresh()

    # ---- internal ---------------------------------------------------------
    def _set_tip(self, text: str) -> None:
        self._tip_text = text
        # Keep an open popup's text fresh (e.g. a refresh tick while hovering).
        if self._popup is not None and self._popup.isVisible():
            self._popup.setText(text)
            self._popup.show_above(self)

    def _refresh(self) -> None:
        api = None
        try:
            api = self._get_api()
        except Exception:
            api = None

        # Label text: username when known, else a logged-out / loading hint.
        if self._username:
            self.setText(self.tr("{0} @ NexusMods").format(self._username))
        elif api is not None:
            self.setText(self.tr("NexusMods"))
        else:
            self.setText(self.tr("Not logged in"))

        r = getattr(api, "rate_limits", None) if api is not None else None
        if r is None or (r.hourly_remaining < 0 and r.daily_remaining < 0):
            self._set_tip(self.tr("Nexus API rate limits - no data yet.\n"
                          "Values appear after the first API request."))
            self._apply_style(self._col_main)
            return

        h, d = r.hourly_remaining, r.daily_remaining
        h_str = f"{h:,}" if h >= 0 else "-"
        d_str = f"{d:,}" if d >= 0 else "-"
        self._set_tip(self.tr('Remaining API requests:\nHourly: {0}\nDaily: {1}').format(h_str, d_str))

        # White by default; amber/red as the hourly budget runs low.
        if h == 0 or d == 0:
            col = self._col_err
        elif h >= 0 and r.hourly_limit > 0 and h < r.hourly_limit * 0.1:
            col = self._col_warn
        else:
            col = self._col_main
        self._apply_style(col)
