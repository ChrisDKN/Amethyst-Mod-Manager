"""Read-only Overwrite-log viewer - borderless in-window overlay.

Shows the files swept into the deploy target's ``overwrite/`` per restore, newest
restore first, parsed from ``.mm_overwrite_log.txt``. Qt port of
``gui/modlist_panel._show_overwrite_log``. A dimmed child overlay (see
gui_qt/overlay_base.py) with a scrollable body and a Close button. All widgets
built once with real parents.
"""

from __future__ import annotations

from html import escape

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QLabel, QPushButton, QTextEdit,
)

from gui_qt.overlay_base import OverlayBase
from gui_qt.theme_qt import active_palette, bind_theme, close_button, _c


def parse_overwrite_log(text: str) -> "list[tuple[str, list[str]]]":
    """Split the overwrite log into (header, files) sections, newest first.
    Faithful port of gui/modlist_panel._parse_overwrite_log."""
    sections: "list[tuple[str, list[str]]]" = []
    cur_header: "str | None" = None
    cur_files: "list[str]" = []
    for raw in (text or "").splitlines():
        line = raw.rstrip("\n")
        if line.startswith("# "):
            if cur_header is not None:
                sections.append((cur_header, cur_files))
            cur_header = line[2:].strip()
            cur_files = []
        elif line.strip():
            if cur_header is not None:
                cur_files.append(line)
    if cur_header is not None:
        sections.append((cur_header, cur_files))
    sections.reverse()
    return sections


class OverwriteLogOverlay(OverlayBase):
    CARD_W = 640
    CARD_H = 520
    MIN_W = 360
    MIN_H = 240

    def __init__(self, host: QWidget, sections: "list[tuple[str, list[str]]]",
                 title: "str | None" = None):
        super().__init__(host)
        p = active_palette()
        self._sections = sections

        _card, v = self._make_card("_OvlLogCard", margins=(16, 14, 16, 14))

        header = QHBoxLayout()
        title_lbl = QLabel(
            title or self.tr("Files swept into Overwrite (newest restore first)"))
        title_lbl.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-weight:600; font-size:14px;")
        header.addWidget(title_lbl)
        header.addStretch(1)
        close = close_button(self.tr("Close"), pal=p)
        close.clicked.connect(lambda: self._finish())
        header.addWidget(close)
        v.addLayout(header)

        self._body = QTextEdit()
        self._body.setReadOnly(True)
        self._body.setStyleSheet(
            f"QTextEdit {{ background:{_c(p,'BG_LIST')}; color:{_c(p,'TEXT_MAIN')};"
            f" border:1px solid {_c(p,'BORDER')}; border-radius:4px; }}")
        self._body.setHtml(self._render(sections, p))
        v.addWidget(self._body, 1)

        bind_theme(self, roles={"TEXT_MAIN", "TEXT_DIM"})
        self._present()
        self.setFocus()

    def refresh_theme(self, palette):
        self._body.setHtml(self._render(self._sections, palette))

    @classmethod
    def show_over(cls, host, sections, title=None):
        top = host.window() if host is not None else None
        return cls(top or host, sections, title=title)

    @staticmethod
    def _render(sections, p) -> str:
        dim = _c(p, "TEXT_DIM")
        main = _c(p, "TEXT_MAIN")
        if not sections:
            return (f"<div style='color:{dim}'>No files have entered overwrite "
                    "yet.</div>")
        parts: list[str] = []
        for hdr, files in sections:
            parts.append(f"<div style='color:{main};font-weight:600;"
                         f"margin-top:8px'>{escape(hdr)}</div>")
            for f in files:
                parts.append(f"<div style='color:{dim};margin-left:12px'>"
                             f"{escape(f)}</div>")
        return "".join(parts)
