from __future__ import annotations

from pathlib import Path
from threading import Event

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QTreeView, QAbstractItemView,
)

from Utils.archives.listing import list_archive
from Utils.downloads.core import fmt_size
from gui_qt.widgets.eliding_label import ElidingLabel
from gui_qt.widgets.path_tree import Node, PathTreeModel, PathTreeDelegate
from gui_qt.runtime.safe_emit import safe_emit
from gui_qt.runtime.worker import LatestWorker


def _build_tree(members, query, labels, cancel):
    root = Node("", is_dir=True)
    folders = {(): root}
    explicit_dirs = set()
    for member in members:
        if cancel.is_set():
            raise InterruptedError("Archive inspection stopped")
        if query and query not in member.path.casefold():
            continue
        parts = member.path.replace("\\", "/").rstrip("/").split("/")
        parent = root
        for depth, part in enumerate(parts[:-1]):
            key = tuple(parts[:depth + 1])
            node = folders.get(key)
            if node is None:
                node = Node(part or "/", is_dir=True, parent=parent)
                parent.children.append(node)
                folders[key] = node
            parent = node
        key = tuple(parts)
        node = folders.get(key) if member.is_dir else None
        if node is None or key in explicit_dirs:
            node = Node(parts[-1] or member.path or "/", is_dir=member.is_dir,
                        parent=parent, payload=member)
            parent.children.append(node)
        if member.is_dir:
            folders.setdefault(key, node)
            explicit_dirs.add(key)
        node.payload = member
        details = []
        if not member.is_dir and member.size is not None:
            details.append(fmt_size(member.size))
        if member.is_link:
            details.append(labels["link"])
        if member.encrypted:
            details.append(labels["encrypted"])
        node.source = " · ".join(details)
    pending = [root]
    while pending:
        if cancel.is_set():
            raise InterruptedError("Archive inspection stopped")
        node = pending.pop()
        node.children.sort(key=lambda child: (not child.is_dir, child.name.casefold()))
        pending.extend(child for child in node.children if child.is_dir)
    return root


class _ContentsModel(PathTreeModel):
    def data(self, index, role=Qt.DisplayRole):
        if index.isValid() and role == Qt.ToolTipRole:
            member = self.node(index).payload
            if member is not None:
                size = f"\n{member.size:,} bytes" if member.size is not None else ""
                return member.path + size
        return super().data(index, role)


class ArchivePreview(QWidget):
    close_requested = Signal()
    _ready = Signal(int, object, object, str)

    def __init__(self, path, parent=None):
        super().__init__(parent)
        self._generation = 0
        self._cancel = Event()
        self._closing = False
        self._members = None
        self._jobs = LatestWorker("download-archive-preview")
        self._ready.connect(self._on_ready)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        toolbar = QWidget()
        toolbar.setObjectName("HeaderBar")
        bar = QHBoxLayout(toolbar)
        bar.setContentsMargins(8, 4, 8, 4)
        self._title = ElidingLabel()
        self._title.setObjectName("HeaderCaption")
        self._title.setTextFormat(Qt.PlainText)
        bar.addWidget(self._title, 1)
        for text, callback in ((self.tr("Expand all"), self._expand_all),
                               (self.tr("Collapse all"), self._collapse_all)):
            button = QPushButton(text)
            button.setObjectName("FormButton")
            button.setCursor(Qt.PointingHandCursor)
            button.clicked.connect(callback)
            bar.addWidget(button)
        from gui_qt.appearance.theme import close_button
        close = close_button()
        close.clicked.connect(self.close_requested.emit)
        bar.addWidget(close)
        layout.addWidget(toolbar)
        self._search = QLineEdit()
        self._search.setPlaceholderText(self.tr("Search archive contents…"))
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._filter)
        layout.addWidget(self._search)
        self._status = QLabel()
        self._status.setTextFormat(Qt.PlainText)
        self._status.setWordWrap(True)
        self._status.setContentsMargins(8, 4, 8, 4)
        layout.addWidget(self._status)
        self._tree = QTreeView()
        self._model = _ContentsModel(self)
        self._tree.setModel(self._model)
        self._tree.setHeaderHidden(True)
        self._tree.setRootIsDecorated(False)
        self._tree.setIndentation(0)
        self._tree.setUniformRowHeights(True)
        self._tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self._tree.setItemDelegate(PathTreeDelegate(self._tree))
        self._tree.clicked.connect(self._on_clicked)
        self._tree.expanded.connect(lambda *_: self._tree.viewport().update())
        self._tree.collapsed.connect(lambda *_: self._tree.viewport().update())
        layout.addWidget(self._tree, 1)
        self.destroyed.connect(lambda *_: self.tab_closing())
        self.set_archive(path)

    def set_archive(self, path):
        self._cancel.set()
        self._cancel = Event()
        self._generation += 1
        self._path = Path(path)
        self._members = None
        self._search.blockSignals(True)
        self._search.clear()
        self._search.blockSignals(False)
        self._search.setEnabled(False)
        self._title.setText(self._path.name)
        self._title.setToolTip(str(self._path))
        self._status.setText(self.tr("Reading archive contents…"))
        self._model.set_root(Node("", is_dir=True))
        self._submit()

    def _submit(self, query=""):
        generation = self._generation
        cancel, path, members, signal = self._cancel, self._path, self._members, self._ready
        labels = {"link": self.tr("Link"), "encrypted": self.tr("Encrypted")}

        def worker():
            try:
                rows = list_archive(path, cancel) if members is None else members
                root = _build_tree(rows, query, labels, cancel)
                safe_emit(signal, generation, rows, root, "")
            except InterruptedError:
                pass
            except Exception as exc:
                safe_emit(signal, generation, None, None, str(exc))

        self._jobs.submit(worker)

    def _filter(self, text):
        if self._members is None:
            return
        self._cancel.set()
        self._cancel = Event()
        self._generation += 1
        self._submit(text.strip().casefold())

    def _on_ready(self, generation, members, root, error):
        if self._closing or generation != self._generation:
            return
        if error:
            self._status.setText(self.tr("Cannot inspect archive: {0}").format(error))
            return
        self._members = members
        self._model.set_root(root)
        self._search.setEnabled(True)
        if not members:
            self._status.setText(self.tr("Archive is empty."))
        elif not root.children:
            self._status.setText(self.tr("No matching entries."))
        else:
            self._status.setText(self.tr("{0} archive entries").format(len(members)))
        if self._search.text().strip():
            self._tree.expandAll()

    def _on_clicked(self, index):
        if self._model.rowCount(index):
            self._tree.setExpanded(index, not self._tree.isExpanded(index))

    def _expand_all(self):
        self._tree.expandAll()

    def _collapse_all(self):
        self._tree.collapseAll()

    def tab_closing(self):
        self._closing = True
        self._cancel.set()
        self._jobs.discard_pending()
