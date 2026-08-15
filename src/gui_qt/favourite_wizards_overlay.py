"""Favourite-wizard-tools picker overlay.

A dimmed borderless child overlay (NOT a top-level window - gaming-mode opens
top-levels behind the app) with a centered card: title, a scrollable checklist
of the active game's wizard tools, and Cancel / Save buttons. Checked tools are
the favourites shown at the top of the Wizard header menu.

Modeled on ``list_picker_overlay.py``. Items are ``(label, tool_id)`` pairs;
``favourites`` is the set of currently-favourited ids. Save → ``on_done(set)``;
Cancel / Esc / backdrop click → ``on_done(None)``.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QRect
from PySide6.QtGui import QPen, QBrush
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QPushButton, QStyledItemDelegate, QStyle,
)

from gui_qt.overlay_base import OverlayBase
from gui_qt.theme_qt import active_palette, bind_theme, _c, qc, qc_contrast

CHECK_BOX = 17        # same as the modlist checkbox


class _CheckDelegate(QStyledItemDelegate):
    """Paints a modlist-style checkbox (blue 17px rounded box + white tick when
    checked, BG_DEEP when off) on the left, then the item text. Mirrors
    ``mod_files_delegate._paint_check`` so this list matches the modlist."""

    def __init__(self, parent=None):
        super().__init__(parent)
        bind_theme(self, roles={
            "TEXT_MAIN", "TEXT_ON_ACCENT", "CHECK_FILL", "BORDER_FAINT",
            "BG_DEEP", "BG_SELECT", "BG_ROW_HOVER",
        })

    def refresh_theme(self, p):
        self.c_text = qc(p, "TEXT_MAIN")
        self.c_on_sel = qc(p, "TEXT_ON_ACCENT")
        self.c_tick = qc_contrast(p, "CHECK_FILL")   # tick reads on the checkbox fill
        self.c_border = qc(p, "BORDER_FAINT")
        self.c_check = qc(p, "CHECK_FILL")
        self.c_check_off = qc(p, "BG_DEEP")
        self.c_sel = qc(p, "BG_SELECT")
        self.c_hover = qc(p, "BG_ROW_HOVER")
        parent = self.parent()
        if parent is not None and hasattr(parent, "viewport"):
            parent.viewport().update()

    def paint(self, p, opt, index):
        r = opt.rect
        if opt.state & QStyle.State_Selected:
            p.fillRect(r, self.c_sel)
        elif opt.state & QStyle.State_MouseOver:
            p.fillRect(r, self.c_hover)

        pad = 10
        box = QRect(r.left() + pad, r.top() + (r.height() - CHECK_BOX) // 2,
                    CHECK_BOX, CHECK_BOX)
        p.save()
        p.setRenderHint(p.RenderHint.Antialiasing, True)
        p.setPen(QPen(self.c_border, 1))
        # data() returns an int; compare on the enum's value so the check reads
        # correctly under PySide6 (int(2) != Qt.Checked enum otherwise).
        on = int(index.data(Qt.CheckStateRole) or 0) == int(Qt.Checked.value)
        p.setBrush(QBrush(self.c_check if on else self.c_check_off))
        p.drawRoundedRect(box, 3, 3)
        if on:
            p.setPen(QPen(self.c_tick, 2))
            p.drawLine(box.left() + 4, box.center().y() + 1,
                       box.center().x() - 1, box.bottom() - 4)
            p.drawLine(box.center().x() - 1, box.bottom() - 4,
                       box.right() - 3, box.top() + 4)
        p.setRenderHint(p.RenderHint.Antialiasing, False)
        p.restore()

        text_x = box.right() + 10
        text_r = QRect(text_x, r.top(), r.right() - text_x - 6, r.height())
        sel = bool(opt.state & QStyle.State_Selected)
        p.setPen(self.c_on_sel if sel else self.c_text)
        p.drawText(text_r, Qt.AlignVCenter | Qt.AlignLeft, index.data(Qt.DisplayRole))

    def sizeHint(self, opt, index):
        s = super().sizeHint(opt, index)
        s.setHeight(max(s.height(), 32))
        return s


class FavouriteWizardsOverlay(OverlayBase):
    CARD_W = 460
    CARD_H = 460
    MIN_W = 320
    MIN_H = 240
    CLICK_OUTSIDE_CANCELS = True

    def __init__(self, host: QWidget, items, favourites, on_done):
        super().__init__(host, on_done=on_done)
        p = active_palette()

        _card, v = self._make_card("_FavCard", margins=(16, 14, 16, 14))

        hdr = QLabel(self.tr("Favourite Wizard Tools"))
        hdr.setStyleSheet(
            f"color:{_c(p,'TEXT_MAIN')}; font-weight:600; font-size:15px;")
        hdr.setWordWrap(True)
        v.addWidget(hdr)

        sub = QLabel(self.tr("Checked tools appear at the top of the Wizard menu "
                             "for quick access."))
        sub.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; font-size:12px;")
        sub.setWordWrap(True)
        v.addWidget(sub)

        self._list = QListWidget()
        self._list.setMouseTracking(True)   # so the delegate gets hover state
        self._list.setItemDelegate(_CheckDelegate(self._list))
        self._list.setStyleSheet(
            f"QListWidget {{ background:{_c(p,'BG_LIST')}; font-size:14px;"
            f" border:1px solid {_c(p,'BORDER')}; border-radius:6px; outline:none; }}")
        favs = set(favourites or ())
        for label, tool_id in items:
            it = QListWidgetItem(label)
            it.setData(Qt.UserRole, tool_id)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked if tool_id in favs else Qt.Unchecked)
            self._list.addItem(it)
        # Toggle the checkbox when the row (not just the box) is clicked.
        self._list.itemClicked.connect(self._toggle_item)
        v.addWidget(self._list, 1)

        bar = QHBoxLayout()
        bar.addStretch(1)
        cancel = QPushButton(self.tr("Cancel"))
        cancel.setObjectName("FormButton")
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.clicked.connect(lambda: self._finish(None))
        bar.addWidget(cancel)
        save = QPushButton(self.tr("Save"))
        save.setObjectName("PrimaryButton")
        save.setCursor(Qt.PointingHandCursor)
        save.clicked.connect(self._save)
        bar.addWidget(save)
        v.addLayout(bar)

        self._present()
        self._list.setFocus()

    @classmethod
    def show_over(cls, host, items, favourites, on_done):
        top = host.window() if host is not None else None
        return cls(top or host, items, favourites, on_done)

    # -- internals ----------------------------------------------------------
    def _toggle_item(self, item: QListWidgetItem):
        item.setCheckState(
            Qt.Unchecked if item.checkState() == Qt.Checked else Qt.Checked)

    def _save(self):
        chosen = set()
        for i in range(self._list.count()):
            it = self._list.item(i)
            if it.checkState() == Qt.Checked:
                chosen.add(it.data(Qt.UserRole))
        self._finish(chosen)
