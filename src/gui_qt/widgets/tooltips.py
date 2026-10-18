"""Application-wide tooltip wrapping."""

from __future__ import annotations

import html
import re
import textwrap

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, Qt, QTimer
from PySide6.QtGui import QCursor, QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QApplication, QAbstractItemView, QHeaderView, QMenu, QTextBrowser,
    QToolTip, QWidget,
)


TOOLTIP_WRAP_CHARS = 64
_RICH_TEXT = re.compile(
    r"^\s*(?:<!doctype|<(?:qt|html|body|p|div|span|table|ul|ol|li|h[1-6]|"
    r"b|i|strong|em|pre|code)\b)",
    re.IGNORECASE,
)


def _wrap_plain_text(text: str, width: int) -> str:
    wrapped = []
    for line in text.split("\n"):
        if len(line) <= width:
            wrapped.append(line)
            continue
        lead = line[:len(line) - len(line.lstrip())]
        body = line[len(lead):]
        continuation = lead
        if body.startswith(("- ", "* ", "• ", "[")):
            continuation += "  "
        wrapped.append(textwrap.fill(
            body,
            width=width,
            initial_indent=lead,
            subsequent_indent=continuation,
            break_long_words=True,
            break_on_hyphens=False,
        ))
    return "\n".join(wrapped)


def wrap_tooltip(text: str, width: int = TOOLTIP_WRAP_CHARS) -> str:
    """Hard-wrap plain tooltip text while leaving rich text untouched."""
    if not text or _RICH_TEXT.match(text):
        return text
    return _wrap_plain_text(text, width)


def escaped_tooltip(text: str, width: int = TOOLTIP_WRAP_CHARS) -> str:
    """Wrap user-facing plain text and protect it from rich-text detection."""
    if not text:
        return ""
    text = html.escape(_wrap_plain_text(text, width)).replace("\n", "<br>")
    return f"<qt>{text}</qt>"


class InteractiveTooltip(QTextBrowser):
    def __init__(self, anchor: QWidget):
        super().__init__(anchor)
        self.setWindowFlags(Qt.ToolTip | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setFocusPolicy(Qt.NoFocus)
        self.setOpenLinks(False)
        self.anchorClicked.connect(self._open_link)
        self._anchor = anchor
        self._anchor_rect = QRect()
        self._poll = QTimer(self)
        self._poll.setInterval(50)
        self._poll.timeout.connect(self._check_hover)
        self._dismiss = QTimer(self)
        self._dismiss.setSingleShot(True)
        self._dismiss.setInterval(400)
        self._dismiss.timeout.connect(self.hide)
        for bar in (self.verticalScrollBar(), self.horizontalScrollBar()):
            bar.sliderPressed.connect(self._dismiss.stop)
        anchor.destroyed.connect(self.hide)

    def _open_link(self, url) -> None:
        if url.isRelative() and not url.path() and url.hasFragment():
            self.scrollToAnchor(url.fragment())
        else:
            QDesktopServices.openUrl(url)

    def show_text(self, text: str, pos: QPoint, rect: QRect) -> None:
        if (self.isVisible() and text == self._text
                and rect == self._anchor_rect):
            self._dismiss.stop()
            return
        from gui_qt.appearance.theme import active_palette, _c
        p = active_palette()
        self.setStyleSheet(
            f"QTextBrowser {{ background:{_c(p, 'BG_HEADER')};"
            f" color:{_c(p, 'TEXT_MAIN')};"
            f" border:1px solid {_c(p, 'ACCENT')}; padding:6px; }}")
        self._anchor_rect = QRect(rect)
        self._anchor_window = self._anchor.window()
        self._text = text
        self.setHtml(text)
        screen = QGuiApplication.screenAt(pos) or self._anchor.screen()
        bounds = screen.availableGeometry()
        width = min(420, bounds.width())
        self.ensurePolished()
        frame = 2 * self.frameWidth()
        document = self.document().clone()
        document.setTextWidth(max(1, width - frame))
        height = int(document.size().height())
        height += frame + 2
        self.resize(width, min(max(48, height), 320, bounds.height()))
        x, y = pos.x() + 12, pos.y() + 16
        if y + self.height() > bounds.bottom() + 1:
            y = pos.y() - self.height() - 8
        self.move(
            max(bounds.left(), min(x, bounds.right() - self.width() + 1)),
            max(bounds.top(), min(y, bounds.bottom() - self.height() + 1)),
        )
        QToolTip.hideText()
        self._dismiss.stop()
        self.show()
        self._filter_active = True
        QApplication.instance().installEventFilter(self)
        self._poll.start()

    def _check_hover(self) -> None:
        if not self._anchor.isVisible():
            self.hide()
            return
        pos = QCursor.pos()
        if (self.verticalScrollBar().isSliderDown()
                or self.horizontalScrollBar().isSliderDown()
                or self.rect().contains(self.mapFromGlobal(pos))
                or self._anchor_rect.contains(self._anchor.mapFromGlobal(pos))):
            self._dismiss.stop()
        elif not self._dismiss.isActive():
            self._dismiss.start()

    def eventFilter(self, watched, event):
        if not getattr(self, "_filter_active", False):
            return super().eventFilter(watched, event)
        kind = event.type()
        inside = watched is self or watched is self.windowHandle() or (
            isinstance(watched, QWidget) and self.isAncestorOf(watched))
        if ((kind in (QEvent.MouseButtonPress, QEvent.Wheel) and not inside)
                or (kind == QEvent.KeyPress and event.key() == Qt.Key_Escape)
                or kind == QEvent.ApplicationDeactivate
                or (watched in (self._anchor, self._anchor_window)
                    and kind in (QEvent.Hide, QEvent.Move, QEvent.Resize))):
            self.hide()
        return super().eventFilter(watched, event)

    def hideEvent(self, event):
        self._filter_active = False
        self._poll.stop()
        self._dismiss.stop()
        QApplication.instance().removeEventFilter(self)
        super().hideEvent(event)


class _TooltipWrapFilter(QObject):
    def eventFilter(self, watched, event):  # noqa: N802 (Qt override)
        if event.type() != QEvent.ToolTip:
            return False

        text, anchor, rect, duration = self._tooltip_at(watched, event)
        if not isinstance(text, str):
            return False
        wrapped = wrap_tooltip(text)
        if not wrapped or wrapped == text:
            return False

        QToolTip.showText(event.globalPos(), wrapped, anchor, rect, duration)
        event.accept()
        return True

    @staticmethod
    def _tooltip_at(watched, event):
        if isinstance(watched, QMenu) and watched.toolTipsVisible():
            action = watched.actionAt(event.pos())
            if action is not None:
                return (action.toolTip(), watched,
                        watched.actionGeometry(action), -1)

        if isinstance(watched, QWidget):
            view = watched.parentWidget()
            if (isinstance(view, QAbstractItemView)
                    and view.viewport() is watched):
                if isinstance(view, QHeaderView):
                    section = view.logicalIndexAt(event.pos())
                    model = view.model()
                    if section >= 0 and model is not None:
                        text = model.headerData(
                            section, view.orientation(), Qt.ToolTipRole)
                        if text:
                            return text, watched, watched.rect(), -1
                else:
                    index = view.indexAt(event.pos())
                    if index.isValid():
                        text = index.data(Qt.ToolTipRole)
                        if text:
                            return text, watched, view.visualRect(index), -1

            text = watched.toolTip()
            if text:
                return (text, watched, watched.rect(),
                        watched.toolTipDuration())

        return "", None, QRect(), -1


def install_tooltip_wrapping(app) -> None:
    if getattr(app, "_amethyst_tooltip_wrap_filter", None) is not None:
        return
    event_filter = _TooltipWrapFilter(app)
    app.installEventFilter(event_filter)
    app._amethyst_tooltip_wrap_filter = event_filter
