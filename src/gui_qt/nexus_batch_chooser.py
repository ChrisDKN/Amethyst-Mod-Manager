"""Review files for several selected Nexus mods in one in-window overlay."""

from __future__ import annotations

from PySide6.QtCore import Qt, QEvent
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame,
    QTreeWidget, QTreeWidgetItem, QHeaderView, QTextEdit,
)

from gui_qt.theme_qt import active_palette, _c, _tinted_icon_url, contrast_text
from gui_qt.nexus_file_chooser import (
    installable_files, _fmt_size_bytes, _plain_text, _CATEGORY_LABEL,
)


def plan_mod_files(files):
    offered = installable_files(list(files or []))
    mains = [f for f in offered if (f.category_name or "").upper() == "MAIN"]
    default = mains[0] if len(mains) == 1 else offered[0] if len(offered) == 1 else None
    return offered, default


class NexusBatchChooser(QWidget):
    CARD_W = 780
    CARD_H = 660

    def __init__(self, host, mods, on_done):
        super().__init__(host)
        self._host = host
        self._on_done = on_done
        self._done = False
        self._rows = []
        p = active_palette()
        self.setObjectName("OverlayBackdrop")
        self.setStyleSheet("#OverlayBackdrop { background: rgba(0,0,0,150); }")
        self.setGeometry(host.rect())

        self._card = QFrame(self)
        self._card.setObjectName("_BatchChooserCard")
        self._card.setStyleSheet(
            f"#_BatchChooserCard {{ background:{_c(p,'BG_PANEL')};"
            f" border:1px solid {_c(p,'BORDER')}; border-radius:8px; }}")
        layout = QVBoxLayout(self._card)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(8)
        title = QLabel(
            self.tr("Download files from 1 selected mod") if len(mods) == 1 else
            self.tr("Download files from {0} selected mods").format(len(mods)))
        title.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-weight:600; font-size:16px;")
        layout.addWidget(title)
        help_text = QLabel(self.tr(
            "One main file is selected automatically. Choose a main file for "
            "mods with several variants, and check any additional files you want. "
            "Mods with no checked files are skipped."))
        help_text.setWordWrap(True)
        help_text.setStyleSheet(f"color:{_c(p,'TEXT_DIM')};")
        layout.addWidget(help_text)

        self._tree = QTreeWidget()
        self._tree.setColumnCount(3)
        self._tree.setHeaderLabels([self.tr("File"), self.tr("Version"), self.tr("Size")])
        header = self._tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        tick = _tinted_icon_url("check_white.png", contrast_text(_c(p, "CHECK_FILL")))
        closed_arrow = _tinted_icon_url("right.png", _c(p, "DROPDOWN_ARROW"))
        open_arrow = _tinted_icon_url("arrow.png", _c(p, "DROPDOWN_ARROW"))
        self._tree.setStyleSheet(
            f"QTreeWidget {{ background:{_c(p,'BG_LIST')}; color:{_c(p,'TEXT_MAIN')};"
            f" border:1px solid {_c(p,'BORDER')}; border-radius:6px; }}"
            f"QTreeWidget::item {{ padding:4px 2px; }}"
            f"QTreeWidget::indicator {{ width:16px; height:16px;"
            f" border:1px solid {_c(p,'BORDER_FAINT')}; border-radius:3px;"
            f" background:{_c(p,'BG_DEEP')}; }}"
            f"QTreeWidget::indicator:checked {{ background:{_c(p,'CHECK_FILL')};"
            f" border-color:{_c(p,'CHECK_FILL')}; image:url({tick}); }}"
            f"QTreeWidget::branch:has-children:closed {{ image:url({closed_arrow}); }}"
            f"QTreeWidget::branch:has-children:open {{ image:url({open_arrow}); }}"
            f"QTreeWidget::item:selected {{ background:{_c(p,'BG_SELECT')};"
            f" color:{_c(p,'TEXT_ON_ACCENT')}; }}")
        for entry, files in mods:
            name = entry.name or f"Mod {entry.mod_id}"
            top = QTreeWidgetItem(self._tree, [name])
            top.setFlags(Qt.ItemIsEnabled)
            top.setFirstColumnSpanned(True)
            font = top.font(0)
            font.setBold(True)
            top.setFont(0, font)
            children = []
            if files is not None:
                offered, default = plan_mod_files(files)
                for f in offered:
                    category = (f.category_name or "").upper()
                    label = self.tr(_CATEGORY_LABEL[category]) if category in _CATEGORY_LABEL else category.title()
                    size = (f.size_in_bytes or 0) or (f.size_kb * 1024 if f.size_kb else 0)
                    child = QTreeWidgetItem(top, [
                        f"[{label}] {f.name or f.file_name or f'File {f.file_id}'}",
                        f"v{f.version}" if f.version else "", _fmt_size_bytes(size)])
                    child.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsUserCheckable)
                    child.setCheckState(0, Qt.Checked if f is default else Qt.Unchecked)
                    child.setData(0, Qt.UserRole, f)
                    children.append(child)
                top.setExpanded(bool(offered))
            self._rows.append((entry, top, children, files is None))
        self._tree.itemChanged.connect(self._refresh)
        self._tree.itemClicked.connect(self._on_item_clicked)
        self._tree.currentItemChanged.connect(self._on_row_changed)
        layout.addWidget(self._tree, 1)

        self._desc = QTextEdit()
        self._desc.setReadOnly(True)
        self._desc.setFixedHeight(90)
        self._desc.setStyleSheet(
            f"QTextEdit {{ background:{_c(p,'BG_LIST')}; color:{_c(p,'TEXT_DIM')};"
            f" border:1px solid {_c(p,'BORDER')}; border-radius:6px; padding:6px; }}")
        self._desc.setPlainText(self.tr("Select a file to see its description."))
        layout.addWidget(self._desc)

        bar = QHBoxLayout()
        self._summary = QLabel()
        self._summary.setWordWrap(True)
        self._summary.setStyleSheet(f"color:{_c(p,'TEXT_DIM')};")
        bar.addWidget(self._summary, 1)
        cancel = QPushButton(self.tr("Cancel"))
        cancel.setObjectName("FormButton")
        cancel.clicked.connect(lambda: self._finish(None))
        bar.addWidget(cancel)
        self._go = QPushButton()
        self._go.setObjectName("PrimaryButton")
        self._go.clicked.connect(lambda: self._finish(self.plan()))
        bar.addWidget(self._go)
        layout.addLayout(bar)
        self._refresh()
        host.installEventFilter(self)
        self._reposition()
        self.show()
        self.raise_()
        self.setFocus()

    @classmethod
    def show_over(cls, host, mods, on_done):
        top = host.window() if host is not None else None
        return cls(top or host, mods, on_done)

    def plan(self):
        return [(entry, [child.data(0, Qt.UserRole) for child in children
                         if child.checkState(0) == Qt.Checked])
                for entry, _top, children, _failed in self._rows
                if any(child.checkState(0) == Qt.Checked for child in children)]

    def _refresh(self, *_):
        count = 0
        skipped = 0
        self._tree.blockSignals(True)
        for entry, top, children, failed in self._rows:
            checked = sum(child.checkState(0) == Qt.Checked for child in children)
            count += checked
            skipped += checked == 0
            chosen = next((child.data(0, Qt.UserRole) for child in children
                           if child.checkState(0) == Qt.Checked), None)
            if failed:
                status = self.tr("file list unavailable")
            elif not children:
                status = self.tr("no downloadable files")
            elif checked > 1:
                status = self.tr("{0} files selected").format(checked)
            elif chosen is not None:
                status = chosen.name or chosen.file_name or self.tr("1 file selected")
            else:
                status = self.tr("skipped")
            top.setText(0, f"{entry.name or f'Mod {entry.mod_id}'} — {status}")
        self._tree.blockSignals(False)
        self._summary.setText(self.tr("{0} mods skipped").format(skipped) if skipped else "")
        self._go.setText(self.tr("Download {0} files").format(count))
        self._go.setEnabled(count > 0)

    def _on_row_changed(self, current, _previous):
        f = current.data(0, Qt.UserRole) if current is not None and current.parent() else None
        self._desc.setPlainText(
            (_plain_text(getattr(f, "description", "")) or self.tr("No description provided."))
            if f is not None else self.tr("Select a file to see its description."))

    def _on_item_clicked(self, item, _column):
        if item.parent() is None and item.childCount():
            item.setExpanded(not item.isExpanded())

    def _reposition(self):
        self.setGeometry(self._host.rect())
        self._card.setFixedSize(max(360, min(self.CARD_W, self.width() - 40)),
                                max(300, min(self.CARD_H, self.height() - 40)))
        self._card.move((self.width() - self._card.width()) // 2,
                        (self.height() - self._card.height()) // 2)

    def _finish(self, result):
        if self._done:
            return
        self._done = True
        self._host.removeEventFilter(self)
        self.hide()
        self.deleteLater()
        if self._on_done is not None:
            self._on_done(result)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self._finish(None)
        else:
            super().keyPressEvent(event)

    def mousePressEvent(self, event):
        event.accept()

    def eventFilter(self, obj, event):
        if obj is self._host and event.type() == QEvent.Resize:
            self._reposition()
        return super().eventFilter(obj, event)
