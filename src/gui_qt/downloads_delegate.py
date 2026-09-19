"""Delegate for the Downloads list - modlist-style blue checkbox, bold section
headers, right-aligned size, and an install-state button per archive row
(painted + hit-tested here). Visual language matches the other Qt tabs.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QRect, QEvent, QSize
from PySide6.QtGui import QPen, QBrush, QFont
from PySide6.QtWidgets import QStyledItemDelegate

from gui_qt.theme_qt import bind_theme, qc, qc_contrast
from gui_qt.downloads_model import (
    COL_CHECK, COL_NAME, COL_SIZE, COL_DOWNLOADED, COL_INSTALL,
    ActiveDownload, EntryRole, InstallStateRole, HiddenRole,
)
from Utils.downloads.core import ARCHIVE_INSTALLED, ARCHIVE_UNINSTALLED

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
        self.on_cancel = None
        self.on_toggle_section = None  # callback(header_row) - select-all toggle
        bind_theme(self, roles={
            "TEXT_MAIN", "TEXT_DIM", "BORDER_FAINT", "CHECK_FILL",
            "BG_DEEP", "BG_SELECT", "BG_HEADER", "BTN_SUCCESS",
            "BTN_WARN", "BTN_INFO", "BTN_DANGER", "ACCENT",
        })

    def refresh_theme(self, p: dict) -> None:
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
        self.c_uninstalled = qc(p, "BTN_INFO")
        self.c_blue = qc(p, "ACCENT")          # Select-all button
        self.c_cancel = qc(p, "BTN_DANGER")
        self.c_cancel_text = qc_contrast(p, "BTN_DANGER")
        # Button label colours are auto-contrasted off each button's own fill so
        # they stay readable on any theme (e.g. a bright-yellow BTN_WARN needs
        # dark text, not white). Text visibility beats palette choice.
        self.c_install_text = qc_contrast(p, "BTN_SUCCESS")
        self.c_reinstall_text = qc_contrast(p, "BTN_WARN")
        self.c_uninstalled_text = qc_contrast(p, "BTN_INFO")
        self.c_selall_text = qc_contrast(p, "ACCENT")
        self._view.viewport().update()

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
            elif col == COL_INSTALL and not index.model().is_downloading_section(index.row()):
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

        if isinstance(e, ActiveDownload):
            if col == COL_NAME:
                p.setPen(self.c_text)
                f = QFont(); f.setPixelSize(FONT_PX); p.setFont(f)
                rect = r.adjusted(6, 0, -4, -6)
                name = index.model().data(index, Qt.DisplayRole) or ""
                p.drawText(rect, Qt.AlignVCenter | Qt.AlignLeft,
                           p.fontMetrics().elidedText(name, Qt.ElideRight, rect.width()))
                bar = QRect(r.left() + 6, r.bottom() - 5, max(0, r.width() - 10), 4)
                p.fillRect(bar, self.c_check_off)
                if e.total > 0:
                    if e.done > 0:
                        filled = min(bar.width(),
                                     max(1, e.done * bar.width() // e.total))
                        p.fillRect(QRect(bar.left(), bar.top(), filled, bar.height()),
                                   self.c_blue)
                else:
                    p.fillRect(bar, QBrush(self.c_blue, Qt.Dense4Pattern))
            elif col in (COL_SIZE, COL_DOWNLOADED):
                p.setPen(self.c_dim)
                f = QFont(); f.setPixelSize(FONT_PX); p.setFont(f)
                alignment = (Qt.AlignVCenter | Qt.AlignRight if col == COL_SIZE
                             else Qt.AlignCenter)
                p.drawText(r.adjusted(4, 0, -8, 0), alignment,
                           index.model().data(index, Qt.DisplayRole) or "")
            elif col == COL_INSTALL:
                rect = self._button_rect(r)
                p.setRenderHint(p.RenderHint.Antialiasing, True)
                p.setPen(Qt.NoPen)
                p.setBrush(self.c_cancel if e.cancellable and not e.cancelling
                           else self.c_border)
                p.drawRoundedRect(rect, 4, 4)
                p.setPen(self.c_cancel_text if e.cancellable and not e.cancelling
                         else self.c_dim)
                f = QFont(); f.setPixelSize(BTN_FONT_PX); f.setBold(True); p.setFont(f)
                p.drawText(rect, Qt.AlignCenter,
                           self.tr("Cancelling…") if e.cancelling
                           else self.tr("Cancel"))
                p.setRenderHint(p.RenderHint.Antialiasing, False)
            return

        if opt.state & opt.state.State_Selected:
            p.fillRect(r, self.c_sel)

        if col == COL_CHECK:
            self._paint_check(p, r, index.model().data(index, Qt.CheckStateRole))
        elif col == COL_NAME:
            hidden = index.model().data(index, HiddenRole)
            p.setPen(self.c_dim if hidden else self.c_text)
            f = QFont(); f.setPixelSize(FONT_PX); p.setFont(f)
            rect = r.adjusted(6, 0, -4, 0)
            name = e.path.name if e.path else ""
            if hidden:
                name = self.tr("{0} (hidden)").format(name)
            txt = p.fontMetrics().elidedText(
                name, Qt.ElideRight, rect.width())
            p.drawText(rect, Qt.AlignVCenter | Qt.AlignLeft, txt)
        elif col == COL_SIZE:
            p.setPen(self.c_dim)
            f = QFont(); f.setPixelSize(FONT_PX); p.setFont(f)
            p.drawText(r.adjusted(0, 0, -8, 0),
                       Qt.AlignVCenter | Qt.AlignRight, e.size_str)
        elif col == COL_DOWNLOADED:
            p.setPen(self.c_dim)
            f = QFont(); f.setPixelSize(FONT_PX); p.setFont(f)
            p.drawText(r.adjusted(4, 0, -4, 0), Qt.AlignCenter,
                       index.model().data(index, Qt.DisplayRole) or "")
        elif col == COL_INSTALL:
            self._paint_button(p, r, index.model().data(index, InstallStateRole))

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

    def _paint_button(self, p, r, state):
        installed = state == ARCHIVE_INSTALLED
        uninstalled = state == ARCHIVE_UNINSTALLED
        rect = self._button_rect(r)
        p.setRenderHint(p.RenderHint.Antialiasing, True)
        p.setPen(Qt.NoPen)
        fill = (self.c_reinstall if installed else
                self.c_uninstalled if uninstalled else self.c_install)
        text_colour = (self.c_reinstall_text if installed else
                       self.c_uninstalled_text if uninstalled
                       else self.c_install_text)
        p.setBrush(fill)
        p.drawRoundedRect(rect, 4, 4)
        p.setPen(text_colour)
        f = QFont(); f.setPixelSize(BTN_FONT_PX); f.setBold(True); p.setFont(f)
        text = (self.tr("Reinstall") if installed else
                self.tr("Uninstalled") if uninstalled else self.tr("Install"))
        p.drawText(rect, Qt.AlignCenter, text)
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
            if (col == COL_INSTALL
                    and not model.is_downloading_section(index.row())
                    and self.on_toggle_section is not None) \
                    and self._button_rect(opt.rect).contains(
                        event.position().toPoint()):
                self.on_toggle_section(index.row())
                return True
            return False
        if isinstance(e, ActiveDownload):
            if (col == COL_INSTALL and e.cancellable and not e.cancelling
                    and self.on_cancel is not None
                    and self._button_rect(opt.rect).contains(
                        event.position().toPoint())):
                self.on_cancel(e.key)
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
