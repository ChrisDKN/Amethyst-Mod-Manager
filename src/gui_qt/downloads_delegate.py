"""Delegate for the Downloads list - modlist-style blue checkbox, bold section
headers, right-aligned size, and an Install/Reinstall button per archive row
(painted + hit-tested here). Visual language matches the other Qt tabs.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QRect, QEvent, QSize
from PySide6.QtGui import QPen, QBrush, QFont
from PySide6.QtWidgets import QStyledItemDelegate

from gui_qt.theme_qt import active_palette, qc, qc_contrast
from gui_qt.downloads_model import (
    COL_CHECK, COL_NAME, COL_SIZE, COL_INSTALL, EntryRole, InstalledRole,
)

CHECK_BOX = 18
FONT_PX = 14          # bigger row text
BTN_FONT_PX = 12
ROW_H = 34           # taller rows so the buttons read like the footer buttons
BTN_W = 92
BTN_H = 28           # match the footer tool-button height


class DownloadsDelegate(QStyledItemDelegate):
    def __init__(self, view, parent=None):
        super().__init__(parent or view)
        self._view = view
        self.on_install = None       # callback(path) when an Install button hit
        self.on_toggle_section = None  # callback(header_row) - select-all toggle
        p = active_palette()
        self.c_text = qc(p, "TEXT_MAIN")
        self.c_dim = qc(p, "TEXT_DIM")
        self.c_border = qc(p, "BORDER_FAINT")
        self.c_check = qc(p, "CHECK_FILL")
        self.c_check_off = qc(p, "BG_DEEP")
        self.c_check_tick = qc_contrast(p, "CHECK_FILL")  # tick on the checkbox fill
        self.c_sel = qc(p, "BG_SELECT")
        self.c_header_bg = qc(p, "BG_HEADER")
        self.c_install = qc(p, "BTN_SUCCESS")
        self.c_reinstall = qc(p, "BTN_WARN")   # orange (already installed)
        self.c_blue = qc(p, "ACCENT")          # Select-all button
        # Button label colours are auto-contrasted off each button's own fill so
        # they stay readable on any theme (e.g. a bright-yellow BTN_WARN needs
        # dark text, not white). Text visibility beats palette choice.
        self.c_install_text = qc_contrast(p, "BTN_SUCCESS")
        self.c_reinstall_text = qc_contrast(p, "BTN_WARN")
        self.c_selall_text = qc_contrast(p, "ACCENT")

    # -- paint --------------------------------------------------------------
    def paint(self, p, opt, index):
        r = opt.rect
        e = index.model().data(index, EntryRole)
        if e is None:
            return
        col = index.column()
        if e.is_section_header:
            p.fillRect(r, self.c_header_bg)
            if col == COL_NAME:
                p.setPen(self.c_text)
                f = QFont(); f.setPixelSize(FONT_PX); f.setBold(True); p.setFont(f)
                p.drawText(r.adjusted(8, 0, -4, 0),
                           Qt.AlignVCenter | Qt.AlignLeft, e.section_name)
            elif col == COL_INSTALL:
                # "Select all" - a blue button, same size/position as the per-row
                # Install button so it reads as a clear action.
                rect = self._button_rect(r)
                p.setRenderHint(p.RenderHint.Antialiasing, True)
                p.setPen(Qt.NoPen)
                p.setBrush(self.c_blue)
                p.drawRoundedRect(rect, 4, 4)
                p.setPen(self.c_selall_text)
                f = QFont(); f.setPixelSize(BTN_FONT_PX); f.setBold(True); p.setFont(f)
                p.drawText(rect, Qt.AlignCenter, self.tr("Select all"))
                p.setRenderHint(p.RenderHint.Antialiasing, False)
            return

        if opt.state & opt.state.State_Selected:
            p.fillRect(r, self.c_sel)

        if col == COL_CHECK:
            self._paint_check(p, r, index.model().data(index, Qt.CheckStateRole))
        elif col == COL_NAME:
            p.setPen(self.c_text)
            f = QFont(); f.setPixelSize(FONT_PX); p.setFont(f)
            rect = r.adjusted(6, 0, -4, 0)
            txt = p.fontMetrics().elidedText(
                e.path.name if e.path else "", Qt.ElideRight, rect.width())
            p.drawText(rect, Qt.AlignVCenter | Qt.AlignLeft, txt)
        elif col == COL_SIZE:
            p.setPen(self.c_dim)
            f = QFont(); f.setPixelSize(FONT_PX); p.setFont(f)
            p.drawText(r.adjusted(0, 0, -8, 0),
                       Qt.AlignVCenter | Qt.AlignRight, e.size_str)
        elif col == COL_INSTALL:
            self._paint_button(p, r, index.model().data(index, InstalledRole))

    def _paint_check(self, p, r, state):
        box = QRect(r.center().x() - CHECK_BOX // 2,
                    r.top() + (r.height() - CHECK_BOX) // 2, CHECK_BOX, CHECK_BOX)
        p.setRenderHint(p.RenderHint.Antialiasing, True)
        p.setPen(QPen(self.c_border, 1))
        on = state == Qt.Checked
        p.setBrush(QBrush(self.c_check if on else self.c_check_off))
        p.drawRoundedRect(box, 3, 3)
        if on:
            p.setPen(QPen(self.c_check_tick, 2))
            p.drawLine(box.left() + 4, box.center().y() + 1,
                       box.center().x() - 1, box.bottom() - 4)
            p.drawLine(box.center().x() - 1, box.bottom() - 4,
                       box.right() - 3, box.top() + 4)
        p.setRenderHint(p.RenderHint.Antialiasing, False)

    def _button_rect(self, r) -> QRect:
        y = r.top() + (r.height() - BTN_H) // 2
        return QRect(r.right() - BTN_W - 6, y, BTN_W, BTN_H)

    def _paint_button(self, p, r, installed):
        rect = self._button_rect(r)
        p.setRenderHint(p.RenderHint.Antialiasing, True)
        p.setPen(Qt.NoPen)
        p.setBrush(self.c_reinstall if installed else self.c_install)
        p.drawRoundedRect(rect, 4, 4)
        p.setPen(self.c_reinstall_text if installed else self.c_install_text)
        f = QFont(); f.setPixelSize(BTN_FONT_PX); f.setBold(True); p.setFont(f)
        p.drawText(rect, Qt.AlignCenter,
                   self.tr("Reinstall") if installed else self.tr("Install"))
        p.setRenderHint(p.RenderHint.Antialiasing, False)

    def sizeHint(self, opt, index):
        return QSize(opt.rect.width(), ROW_H)

    # -- interaction --------------------------------------------------------
    def editorEvent(self, event, model, opt, index):
        if event.type() != QEvent.MouseButtonRelease:
            return False
        e = model.data(index, EntryRole)
        if e is None:
            return False
        col = index.column()
        shift = bool(event.modifiers() & Qt.ShiftModifier)
        if e.is_section_header:
            # Only the "Select all" button rect toggles the section.
            if col == COL_INSTALL and self.on_toggle_section is not None \
                    and self._button_rect(opt.rect).contains(
                        event.position().toPoint()):
                self.on_toggle_section(index.row())
                return True
            return False
        # Checkbox OR name click toggles selection (no drag/reorder here, so the
        # whole name is a select target - user request).
        if col in (COL_CHECK, COL_NAME):
            model.toggle_check(index.row(), shift=shift)
            return True
        if col == COL_INSTALL:
            if self._button_rect(opt.rect).contains(event.position().toPoint()):
                if self.on_install is not None and e.path is not None:
                    self.on_install(e.path)
                return True
        return False
