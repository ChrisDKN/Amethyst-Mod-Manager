"""Qt tree model for the Text Files tab.

A QAbstractItemModel folder tree: each source ("Mod folders" / "Profile" /
"Game folder" / "My Games") is a top-level node, then the files nest into their
real folder hierarchy. A profile can have thousands of text files, so collapsible
folders keep it navigable (vs a flat list). Columns: Name, Source, Date Modified.
File leaves carry the full disk path (opened in the scoped text editor).
"""

from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtCore import (
    Qt, QAbstractItemModel, QModelIndex, QT_TRANSLATE_NOOP)

COL_NAME = 0
COL_SOURCE = 1
COL_MODIFIED = 2
# Translated at display time in headerData; register literals for lupdate.
COLUMNS = [
    QT_TRANSLATE_NOOP("TextFilesModel", "Name"),
    QT_TRANSLATE_NOOP("TextFilesModel", "Source"),
    QT_TRANSLATE_NOOP("TextFilesModel", "Date Modified"),
]

NodeRole = Qt.UserRole + 1


class _TextNode:
    __slots__ = ("name", "is_dir", "children", "parent", "full_path", "mod",
                 "rel_path", "mtime")

    def __init__(self, name, *, is_dir, parent=None,
                 full_path=None, mod="", rel_path="", mtime=None):
        self.name = name
        self.is_dir = is_dir
        self.children: list[_TextNode] = []
        self.parent = parent
        self.full_path: Path | None = full_path   # file leaves only
        self.mod = mod                            # source/mod label (leaves)
        self.rel_path = rel_path
        self.mtime = mtime

    def row(self) -> int:
        return 0 if self.parent is None else self.parent.children.index(self)


class TextFilesModel(QAbstractItemModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._root = _TextNode("", is_dir=True)

    # ---- population -------------------------------------------------------
    def set_root(self, root: _TextNode):
        self.beginResetModel()
        self._root = root
        self.endResetModel()

    def clear(self):
        self.set_root(_TextNode("", is_dir=True))

    def node(self, index: QModelIndex) -> _TextNode | None:
        return self._root if not index.isValid() else index.internalPointer()

    # ---- Qt interface -----------------------------------------------------
    def index(self, row, col, parent=QModelIndex()):
        if not self.hasIndex(row, col, parent):
            return QModelIndex()
        pnode = self.node(parent)
        if pnode is None or row >= len(pnode.children):
            return QModelIndex()
        return self.createIndex(row, col, pnode.children[row])

    def parent(self, index):
        if not index.isValid():
            return QModelIndex()
        p = index.internalPointer().parent
        if p is None or p is self._root:
            return QModelIndex()
        return self.createIndex(p.row(), 0, p)

    def rowCount(self, parent=QModelIndex()):
        pnode = self.node(parent)
        return len(pnode.children) if pnode else 0

    def columnCount(self, parent=QModelIndex()):
        return len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            if getattr(self, "_suppress_header_text", False):
                return ""
            return self.tr(COLUMNS[section])
        return None

    def flags(self, index):
        if not index.isValid():
            return Qt.NoItemFlags
        return Qt.ItemIsEnabled | Qt.ItemIsSelectable

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        node: _TextNode = index.internalPointer()
        col = index.column()
        if role == NodeRole:
            return node
        if role == Qt.DisplayRole:
            if col == COL_NAME:
                return node.name
            if col == COL_SOURCE:
                return "" if node.is_dir else node.mod
            if col == COL_MODIFIED and not node.is_dir and node.mtime is not None:
                return time.strftime("%d %b %Y  %H:%M", time.localtime(node.mtime))
        return None
