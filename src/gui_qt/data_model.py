"""Qt tree model for the Data tab.

A QAbstractItemModel over the merged-deployment folder tree (what lands in the
game folder). Two columns - no checkboxes:

  0  Path         - folder / file name (the tree)
  1  Winning Mod  - the mod that owns this file in the deployed filemap

Conflict files (owned by >1 enabled mod) are tinted; the selected mod's files get
a highlight background. Mirrors gui_qt.mod_files_model but without the checkbox
columns. Display-only - all data-building lives in Utils.data_tab.
"""

from __future__ import annotations

from PySide6.QtCore import (
    Qt, QAbstractItemModel, QModelIndex, QT_TRANSLATE_NOOP,
)

from gui_qt.theme_qt import bind_theme, qc

COL_NAME = 0
COL_MOD = 1
COLUMNS = ["Path", "Winning Mod"]
# Translated at display time in headerData; register literals for lupdate
# (explicit calls - a loop variable wouldn't be statically extractable).
_COL_TR = (
    QT_TRANSLATE_NOOP("DataModel", "Path"),
    QT_TRANSLATE_NOOP("DataModel", "Winning Mod"),
)

NodeRole = Qt.UserRole + 1       # the _DataNode
ConflictRole = Qt.UserRole + 2   # 0 none, 1 winning conflict


class _DataNode:
    __slots__ = ("name", "path", "mod", "is_dir", "children", "parent",
                 "conflict")

    def __init__(self, name, path, *, is_dir, parent=None, mod="", conflict=0):
        self.name = name
        self.path = path          # canonical rel path (folder or file)
        self.mod = mod            # winning mod (files only)
        self.is_dir = is_dir
        self.children: list[_DataNode] = []
        self.parent = parent
        self.conflict = conflict  # 1 = winning conflict (tinted), 0 = none

    def row(self) -> int:
        if self.parent is None:
            return 0
        return self.parent.children.index(self)


class DataModel(QAbstractItemModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._root = _DataNode("", "", is_dir=True)
        self._highlight_mod: str | None = None
        # data() runs per visible cell; keep the QColor cached but live-refresh
        # it without resetting the model/selection.
        bind_theme(self, roles={"CONFLICT_HL_ANCHOR"})

    def refresh_theme(self, p: dict) -> None:
        self._c_highlight = qc(p, "CONFLICT_HL_ANCHOR")
        self._emit_theme_changed()

    def _emit_theme_changed(self, parent=QModelIndex()) -> None:
        rows = self.rowCount(parent)
        if not rows:
            return
        self.dataChanged.emit(
            self.index(0, 0, parent),
            self.index(rows - 1, COL_MOD, parent),
            [Qt.BackgroundRole])
        for row in range(rows):
            child = self.index(row, 0, parent)
            if self.rowCount(child):
                self._emit_theme_changed(child)

    # ---- population -------------------------------------------------------
    def set_root(self, root: _DataNode):
        self.beginResetModel()
        self._root = root
        self.endResetModel()

    def clear(self):
        self.beginResetModel()
        self._root = _DataNode("", "", is_dir=True)
        self.endResetModel()

    def node(self, index: QModelIndex) -> _DataNode | None:
        if not index.isValid():
            return self._root
        return index.internalPointer()

    def index_for_node(self, node: _DataNode, col: int = 0) -> QModelIndex:
        if node is self._root or node.parent is None:
            return QModelIndex()
        return self.createIndex(node.row(), col, node)

    def set_highlight_mod(self, mod: str | None):
        """Tint files belonging to *mod* (modlist selection cross-highlight)."""
        if mod == self._highlight_mod:
            return
        self._highlight_mod = mod
        if self.rowCount():
            self.dataChanged.emit(
                self.createIndex(0, 0, self._root.children[0]),
                self.createIndex(self.rowCount() - 1, COL_MOD,
                                 self._root.children[-1]),
                [Qt.BackgroundRole])

    # ---- Qt model interface ----------------------------------------------
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
            return self.tr(COLUMNS[section])
        return None

    def flags(self, index):
        if not index.isValid():
            return Qt.NoItemFlags
        return Qt.ItemIsEnabled | Qt.ItemIsSelectable

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        node: _DataNode = index.internalPointer()
        col = index.column()

        if role == NodeRole:
            return node
        if role == ConflictRole:
            return node.conflict
        if role == Qt.DisplayRole:
            if col == COL_NAME:
                return node.name
            if col == COL_MOD:
                return node.mod if not node.is_dir else ""
        if role == Qt.BackgroundRole and self._highlight_mod:
            if not node.is_dir and node.mod == self._highlight_mod:
                return self._c_highlight
        return None
