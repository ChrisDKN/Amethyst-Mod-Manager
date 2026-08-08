"""Delegate for the Text Files tree - folder arrow + depth indent (col 0, like the
Data/Mod Files tabs) and a dim Source column. No checkboxes; clicking a file leaf
opens it in the scoped editor.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QRect, QSize
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QStyledItemDelegate

from gui_qt.theme_qt import active_palette, _c, qc
from gui_qt.icons import icon
from gui_qt.text_files_model import COL_NAME, COL_SOURCE

ARROW_SZ = 20
INDENT = 18
FONT_PX = 13          # match Mod Files / Data tabs for consistency
ROW_H = 22


class TextFilesDelegate(QStyledItemDelegate):
    def __init__(self, view, parent=None):
        super().__init__(parent or view)
        self._view = view
        p = active_palette()
        self.c_text = qc(p, "TEXT_MAIN")
        self.c_dim = qc(p, "TEXT_DIM")
        self.c_sel = qc(p, "BG_SELECT")
        self.c_arrow = _c(p, "DROPDOWN_ARROW")   # expand/collapse arrow tint

    def paint(self, p, opt, index):
        r = opt.rect
        node = index.model().node(index)
        if node is None:
            return
        if opt.state & opt.state.State_Selected:
            p.fillRect(r, self.c_sel)
        if index.column() == COL_NAME:
            self._paint_name(p, r, index, node)
        elif index.column() == COL_SOURCE and not node.is_dir and node.mod:
            p.setPen(self.c_dim)
            f = QFont(); f.setPixelSize(FONT_PX); p.setFont(f)
            rect = r.adjusted(6, 0, -8, 0)
            txt = p.fontMetrics().elidedText(node.mod, Qt.ElideRight, rect.width())
            p.drawText(rect, Qt.AlignVCenter | Qt.AlignLeft, txt)

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
        # Match the Data/Mod Files tabs: same weight for folders + files.
        p.setPen(self.c_text)
        f = QFont(); f.setPixelSize(FONT_PX); p.setFont(f)
        rect = QRect(x, r.top(), r.right() - x - 4, r.height())
        txt = p.fontMetrics().elidedText(node.name, Qt.ElideRight, rect.width())
        p.drawText(rect, Qt.AlignVCenter | Qt.AlignLeft, txt)

    def _depth(self, index) -> int:
        d = 0
        idx = index.parent()
        while idx.isValid():
            d += 1
            idx = idx.parent()
        return d

    def sizeHint(self, opt, index):
        return QSize(opt.rect.width(), ROW_H)
