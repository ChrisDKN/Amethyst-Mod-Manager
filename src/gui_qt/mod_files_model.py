"""Qt tree model for the Mod Files tab.

A QAbstractItemModel over a folder/file hierarchy built from a mod's raw file
listing (Utils.mod_files.build_tree). Three columns:

  0  File name  — the tree (folder/file names)
  1  Top Level  — checkbox: is this path promoted to deploy at the game root
  2  Disable    — checkbox: is this file excluded from deploy (folders = tri-state)

The model is display-only state; all persistence + the strip/exclusion
algorithms live in Utils.mod_files. The view drives saves on checkbox clicks.
"""

from __future__ import annotations

from PySide6.QtCore import (
    Qt, QAbstractItemModel, QCoreApplication, QModelIndex, QT_TRANSLATE_NOOP)

from gui_qt.theme_qt import active_palette, qc

COL_NAME = 0
COL_TOPLEVEL = 1
COL_DISABLE = 2
# Translated at display time in headerData; register literals for lupdate.
COLUMNS = [
    QT_TRANSLATE_NOOP("ModFilesModel", "File name"),
    QT_TRANSLATE_NOOP("ModFilesModel", "Top Level"),
    QT_TRANSLATE_NOOP("ModFilesModel", "Disable"),
]

# Custom roles.
NodeRole = Qt.UserRole + 1       # the _Node
ConflictRole = Qt.UserRole + 2   # 0 none, 1 win (green), -1 lose (red)


class _Node:
    __slots__ = ("name", "path", "rel_key", "rel_str", "is_dir", "children",
                 "parent", "checked", "conflict", "top_level", "synthetic",
                 "stripped", "meta")

    def __init__(self, name, path, *, is_dir, parent=None,
                 rel_key=None, rel_str=None):
        self.name = name
        self.path = path            # canonical rel path (orig case)
        self.rel_key = rel_key      # post-strip key (files only)
        self.rel_str = rel_str      # raw on-disk path (files only)
        self.is_dir = is_dir
        self.children: list[_Node] = []
        self.parent = parent
        self.checked = True         # Disable column: True = included
        self.conflict = 0           # -1 lose, 0 none, 1 win
        self.top_level = False      # Top Level column checked
        self.synthetic = False      # greyed strip placeholder
        self.stripped = False       # this path is itself stripped (greyed)
        self.meta = False           # the mod's meta.ini row (view/edit only)

    def row(self) -> int:
        if self.parent is None:
            return 0
        return self.parent.children.index(self)


class ModFilesModel(QAbstractItemModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._root = _Node("", "", is_dir=True)
        # path(lower) -> node, for ancestor/folder lookups
        self._by_path: dict[str, _Node] = {}
        # Foreground QColors cached once — data() runs per cell per repaint
        # (mirrors modlist_delegate; theme changes take effect on restart).
        p = active_palette()
        self._c_dim = qc(p, "FILE_DIM")
        self._c_win = qc(p, "FILE_WIN")
        self._c_lose = qc(p, "FILE_LOSE")

    # ---- population -------------------------------------------------------
    def set_root(self, root: _Node, by_path: dict[str, _Node]):
        self.beginResetModel()
        self._root = root
        self._by_path = by_path
        self.endResetModel()

    def clear(self):
        self.beginResetModel()
        self._root = _Node("", "", is_dir=True)
        self._by_path = {}
        self.endResetModel()

    def node(self, index: QModelIndex) -> _Node | None:
        if not index.isValid():
            return self._root
        return index.internalPointer()

    def index_for_node(self, node: _Node, col: int = 0) -> QModelIndex:
        if node is self._root or node.parent is None:
            return QModelIndex()
        return self.createIndex(node.row(), col, node)

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
        node = index.internalPointer()
        p = node.parent
        if p is None or p is self._root:
            return QModelIndex()
        return self.createIndex(p.row(), 0, p)

    def rowCount(self, parent=QModelIndex()):
        pnode = self.node(parent)
        return len(pnode.children) if pnode else 0

    def columnCount(self, parent=QModelIndex()):
        return len(COLUMNS)

    @classmethod
    def column_title(cls, section: int) -> str:
        """The translated header caption. Classmethod so the view can measure
        it for column minimums before a model exists."""
        return QCoreApplication.translate("ModFilesModel", COLUMNS[section])

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return self.column_title(section)
        return None

    def flags(self, index):
        if not index.isValid():
            return Qt.NoItemFlags
        f = Qt.ItemIsEnabled | Qt.ItemIsSelectable
        node: _Node = index.internalPointer()
        # The meta.ini row is view/edit-only — it deploys nothing, so no
        # Top Level / Disable checkboxes.
        if not getattr(node, "meta", False) and \
                index.column() in (COL_TOPLEVEL, COL_DISABLE):
            f |= Qt.ItemIsUserCheckable
        return f

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        node: _Node = index.internalPointer()
        col = index.column()

        if role == NodeRole:
            return node
        if role == ConflictRole:
            return node.conflict

        if role == Qt.DisplayRole and col == COL_NAME:
            return node.name

        if role == Qt.CheckStateRole and not node.meta:
            if col == COL_TOPLEVEL:
                return Qt.Checked if node.top_level else Qt.Unchecked
            if col == COL_DISABLE:
                return self._disable_state(node)

        if role == Qt.ForegroundRole and col == COL_NAME:
            if node.synthetic or self._is_greyed(node):
                return self._c_dim
            if node.conflict == 1:
                return self._c_win
            if node.conflict == -1:
                return self._c_lose
        return None

    # ---- check-state helpers ---------------------------------------------
    def _disable_state(self, node: _Node):
        """Disable column = checkbox 'included'. Files: checked when included.
        Folders: tri-state from their leaves."""
        if not node.is_dir:
            return Qt.Checked if node.checked else Qt.Unchecked
        leaves = self._leaves(node)
        if not leaves:
            return Qt.Checked
        on = sum(1 for l in leaves if l.checked)
        if on == len(leaves):
            return Qt.Checked
        if on == 0:
            return Qt.Unchecked
        return Qt.PartiallyChecked

    def _is_greyed(self, node: _Node) -> bool:
        """Name greys when the row (or whole folder) is disabled."""
        if not node.is_dir:
            return not node.checked
        leaves = self._leaves(node)
        return bool(leaves) and not any(l.checked for l in leaves)

    def _leaves(self, node: _Node) -> list[_Node]:
        out: list[_Node] = []
        stack = list(node.children)
        while stack:
            n = stack.pop()
            if n.is_dir:
                stack.extend(n.children)
            else:
                out.append(n)
        return out

    def leaves(self, node: _Node) -> list[_Node]:
        return self._leaves(node)

    def _descendants(self, node: _Node) -> list[_Node]:
        """All descendant nodes — folders AND files (for repaint after a
        folder-level Disable toggle so nested subfolders update too)."""
        out: list[_Node] = []
        stack = list(node.children)
        while stack:
            n = stack.pop()
            out.append(n)
            if n.is_dir:
                stack.extend(n.children)
        return out

    # ---- mutation (view calls these, then persists via Utils.mod_files) ---
    def set_disabled_subtree(self, node: _Node, included: bool):
        """Set the Disable state for a node + all descendants (folder toggle)."""
        if node.is_dir:
            for leaf in self._leaves(node):
                leaf.checked = included
        else:
            node.checked = included
        self._emit_subtree_and_ancestors(node)

    def set_disabled(self, node: _Node, included: bool):
        node.checked = included
        self._emit_subtree_and_ancestors(node)

    def _emit_subtree_and_ancestors(self, node: _Node):
        # Repaint the node, its descendants (DisplayRole grey), and its ancestors
        # (folder tri-state). Simplest: emit a broad dataChanged on the column.
        top = self.index_for_node(node, COL_NAME)
        if top.isValid():
            self.dataChanged.emit(
                self.index_for_node(node, COL_NAME),
                self.index_for_node(node, COL_DISABLE),
                [Qt.CheckStateRole, Qt.ForegroundRole])
        # Ancestors
        p = node.parent
        while p is not None and p is not self._root:
            self.dataChanged.emit(
                self.index_for_node(p, COL_NAME),
                self.index_for_node(p, COL_DISABLE),
                [Qt.CheckStateRole, Qt.ForegroundRole])
            p = p.parent
        # Descendants — every folder AND file under this node, so nested
        # subfolders' (tri-state) checkboxes + greying repaint too (not just the
        # leaves). Was the "disable a folder, subfolders don't update" bug.
        if node.is_dir:
            for child in self._descendants(node):
                ci = self.index_for_node(child, COL_NAME)
                if ci.isValid():
                    self.dataChanged.emit(ci, self.index_for_node(child, COL_DISABLE),
                                          [Qt.CheckStateRole, Qt.ForegroundRole])

    def refresh_all(self):
        """Repaint every cell (after a Top Level change recomputes top_level)."""
        if self.rowCount():
            self.dataChanged.emit(self.createIndex(0, 0, self._root.children[0]),
                                  self.createIndex(self.rowCount() - 1, COL_DISABLE,
                                                   self._root.children[-1]),
                                  [Qt.CheckStateRole, Qt.ForegroundRole, Qt.DisplayRole])
