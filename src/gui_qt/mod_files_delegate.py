"""Delegate for the Mod Files tree - draws modlist-style checkboxes (blue, 17px,
centred in the Top Level / Root / Disable columns) and uses the separator arrow
assets (arrow.png / right.png) for the expand/collapse indicator.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QRect
from PySide6.QtGui import QPen, QBrush, QFont
from PySide6.QtWidgets import QStyledItemDelegate

from gui_qt.theme_qt import active_palette, _c, qc, qc_contrast
from gui_qt.icons import icon
from gui_qt.mod_files_model import COL_NAME, COL_TOPLEVEL, COL_ROOT, COL_DISABLE

CHECK_BOX = 17        # same as the modlist checkbox
ARROW_SZ = 20         # same as the modlist separator arrow
INDENT = 18           # per-depth indent for the tree column
FONT_PX = 13


class ModFilesDelegate(QStyledItemDelegate):
    def __init__(self, view, parent=None):
        super().__init__(parent or view)
        self._view = view
        p = active_palette()
        self.c_text = qc(p, "TEXT_MAIN")
        self.c_dim = qc(p, "FILE_DIM")
        self.c_win = qc(p, "FILE_WIN")
        self.c_lose = qc(p, "FILE_LOSE")
        self.c_border = qc(p, "BORDER_FAINT")
        self.c_check = qc(p, "CHECK_FILL")
        self.c_check_off = qc(p, "BG_DEEP")
        self.c_tick = qc_contrast(p, "CHECK_FILL")   # tick reads on the checkbox fill
        self.c_sel = qc(p, "BG_SELECT")
        self.c_part = qc(p, "ACCENT")
        self.c_arrow = _c(p, "DROPDOWN_ARROW")   # expand/collapse arrow tint

    def paint(self, p, opt, index):
        r = opt.rect
        col = index.column()
        model = index.model()
        node = model.node(index)
        if node is None:
            return

        # Selection background spans the whole row (drawn per-cell).
        if opt.state & opt.state.State_Selected:
            p.fillRect(r, self.c_sel)

        if col == COL_NAME:
            self._paint_name(p, r, index, node)
        elif node.meta:
            # meta.ini deploys nothing → no Top Level / Root / Disable checkboxes.
            return
        elif col == COL_TOPLEVEL:
            self._paint_check(p, r, Qt.Checked if node.top_level else Qt.Unchecked,
                              greyed=node.synthetic)
        elif col in (COL_ROOT, COL_DISABLE):
            state = model.data(index, Qt.CheckStateRole)
            self._paint_check(p, r, state, greyed=node.synthetic)

    # -- name column (arrow + indent + coloured text) ----------------------
    def _paint_name(self, p, r, index, node):
        depth = self._depth(index)
        x = r.left() + 4 + depth * INDENT

        # Expand/collapse arrow for folders (arrow.png expanded, right.png not).
        if node.is_dir and self._view.model().rowCount(index) > 0:
            a = QRect(x, r.top() + (r.height() - ARROW_SZ) // 2, ARROW_SZ, ARROW_SZ)
            expanded = self._view.isExpanded(index)
            ico = icon("arrow.png" if expanded else "right.png", ARROW_SZ,
                       color=self.c_arrow)
            if not ico.isNull():
                ico.paint(p, a)
        x += ARROW_SZ + 2

        # Text colour: grey if disabled/synthetic/stripped, else conflict tint.
        if node.synthetic or node.stripped or self._is_greyed(node):
            color = self.c_dim
        elif node.conflict == 1:
            color = self.c_win
        elif node.conflict == -1:
            color = self.c_lose
        else:
            color = self.c_text
        p.setPen(color)
        f = QFont(); f.setPixelSize(FONT_PX); p.setFont(f)
        text_rect = QRect(x, r.top(), r.right() - x - 4, r.height())
        fm = p.fontMetrics()
        elided = fm.elidedText(node.name, Qt.ElideRight, text_rect.width())
        p.drawText(text_rect, Qt.AlignVCenter | Qt.AlignLeft, elided)

    def _is_greyed(self, node) -> bool:
        if not node.is_dir:
            return not node.checked
        leaves = self._view.model().leaves(node)
        return bool(leaves) and not any(l.checked for l in leaves)

    def _depth(self, index) -> int:
        d = 0
        idx = index.parent()
        while idx.isValid():
            d += 1
            idx = idx.parent()
        return d

    # -- checkbox column (modlist style, centred) --------------------------
    def _paint_check(self, p, r, state, greyed=False):
        box = QRect(r.center().x() - CHECK_BOX // 2,
                    r.top() + (r.height() - CHECK_BOX) // 2,
                    CHECK_BOX, CHECK_BOX)
        p.setRenderHint(p.RenderHint.Antialiasing, True)
        p.setPen(QPen(self.c_border, 1))
        on = state == Qt.Checked
        partial = state == Qt.PartiallyChecked
        fill = self.c_check if (on or partial) else self.c_check_off
        if greyed:
            fill = self.c_check_off
        p.setBrush(QBrush(fill))
        p.drawRoundedRect(box, 3, 3)
        if on and not greyed:
            p.setPen(QPen(self.c_tick, 2))
            p.drawLine(box.left() + 4, box.center().y() + 1,
                       box.center().x() - 1, box.bottom() - 4)
            p.drawLine(box.center().x() - 1, box.bottom() - 4,
                       box.right() - 3, box.top() + 4)
        elif partial and not greyed:
            p.setPen(QPen(self.c_tick, 2))
            p.drawLine(box.left() + 4, box.center().y(),
                       box.right() - 4, box.center().y())
        p.setRenderHint(p.RenderHint.Antialiasing, False)

    def sizeHint(self, opt, index):
        s = super().sizeHint(opt, index)
        s.setHeight(max(s.height(), 22))
        return s
