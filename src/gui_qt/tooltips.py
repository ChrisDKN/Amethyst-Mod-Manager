"""Application-wide tooltip wrapping."""

from __future__ import annotations

import html
import re
import textwrap

from PySide6.QtCore import QEvent, QObject, QRect, Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QHeaderView, QMenu, QToolTip, QWidget,
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
