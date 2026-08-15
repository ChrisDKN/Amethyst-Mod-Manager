"""Delegate for the Data tree - same visual language as the Mod Files tree (depth
indent + arrow.png/right.png expand arrow, conflict text tint) but two text
columns and no checkboxes. Col 0 = Path (tree), col 1 = Winning Mod.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QRect
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QStyledItemDelegate

from gui_qt.theme_qt import bind_theme, _c, qc
from gui_qt.icons import icon
from gui_qt.data_model import COL_NAME, COL_MOD

ARROW_SZ = 20         # same as the modlist separator arrow
INDENT = 18           # per-depth indent (matches Mod Files)
FONT_PX = 13


class DataDelegate(QStyledItemDelegate):
    def __init__(self, view, parent=None):
        super().__init__(parent or view)
        self._view = view
        bind_theme(self, roles={
            "TEXT_MAIN", "TEXT_DIM", "BG_SELECT", "DROPDOWN_ARROW",
            "FILE_WIN",
        })

    def refresh_theme(self, p: dict) -> None:
        self.c_text = qc(p, "TEXT_MAIN")
        self.c_dim = qc(p, "TEXT_DIM")
        self.c_win = qc(p, "FILE_WIN")
        self.c_sel = qc(p, "BG_SELECT")
        self.c_arrow = _c(p, "DROPDOWN_ARROW")   # expand/collapse arrow tint
        self._view.viewport().update()

    def paint(self, p, opt, index):
        r = opt.rect
        node = index.model().node(index)
        if node is None:
            return
        if opt.state & opt.state.State_Selected:
            p.fillRect(r, self.c_sel)
        if index.column() == COL_NAME:
            self._paint_name(p, r, index, node)
        elif index.column() == COL_MOD:
            self._paint_mod(p, r, node)

    def _paint_name(self, p, r, index, node):
        depth = self._depth(index)
        x = r.left() + 4 + depth * INDENT
        if node.is_dir and self._view.model().rowCount(index) > 0:
            a = QRect(x, r.top() + (r.height() - ARROW_SZ) // 2, ARROW_SZ, ARROW_SZ)
            ico = icon("arrow.png" if self._view.isExpanded(index) else "right.png",
                       ARROW_SZ, color=self.c_arrow)
            if not ico.isNull():
                ico.paint(p, a)
        x += ARROW_SZ + 2
        # Conflict winners are tinted green (Tk "conflict_win"); folders neutral.
        color = self.c_win if (not node.is_dir and node.conflict == 1) else self.c_text
        p.setPen(color)
        f = QFont(); f.setPixelSize(FONT_PX); p.setFont(f)
        rect = QRect(x, r.top(), r.right() - x - 4, r.height())
        elided = p.fontMetrics().elidedText(node.name, Qt.ElideRight, rect.width())
        p.drawText(rect, Qt.AlignVCenter | Qt.AlignLeft, elided)

    def _paint_mod(self, p, r, node):
        if node.is_dir or not node.mod:
            return
        p.setPen(self.c_dim)
        f = QFont(); f.setPixelSize(FONT_PX); p.setFont(f)
        rect = QRect(r.left() + 6, r.top(), r.width() - 10, r.height())
        elided = p.fontMetrics().elidedText(node.mod, Qt.ElideRight, rect.width())
        p.drawText(rect, Qt.AlignVCenter | Qt.AlignLeft, elided)

    def _depth(self, index) -> int:
        d = 0
        idx = index.parent()
        while idx.isValid():
            d += 1
            idx = idx.parent()
        return d

    def sizeHint(self, opt, index):
        s = super().sizeHint(opt, index)
        s.setHeight(max(s.height(), 22))
        return s
