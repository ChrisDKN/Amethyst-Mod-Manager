"""Shared help-marker helpers - the tooltip + bold accent "?" pattern.

Used by the Settings, exe-launch settings and Define-Custom-Game views: option
descriptions live in tooltips (on the row widgets and on a small "?" QLabel)
instead of inline hint paragraphs, so rows stay one line tall.

The host view styles the marker itself - include :func:`help_mark_qss` in its
stylesheet so the "?" picks up the theme's accent colour.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel

from gui_qt.theme_qt import _c
from gui_qt.tooltips import escaped_tooltip


def tip_text(text: str) -> str:
    """Wrap plain help text for setToolTip.

    Rich-text so the tooltip word-wraps (plain-text tooltips render as one
    long unbroken line); newlines become <br> so multi-line hints keep their
    breaks."""
    return escaped_tooltip(text)


def make_help_marker(text: str) -> QLabel:
    """A small "?" label carrying `text` as its tooltip."""
    mark = QLabel("?")
    mark.setObjectName("HelpMark")
    mark.setToolTip(tip_text(text))
    mark.setCursor(Qt.WhatsThisCursor)
    return mark


def help_mark_qss(pal) -> str:
    """The QSS rule styling markers made by :func:`make_help_marker`."""
    return (f"QLabel#HelpMark {{ color: {_c(pal, 'ACCENT')};"
            f" font-weight: bold; }}")
