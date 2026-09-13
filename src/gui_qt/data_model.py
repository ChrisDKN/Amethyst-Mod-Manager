"""Qt tree model for the Data tab.

A QAbstractItemModel over the merged-deployment folder tree (what lands in the
game folder). Two columns - no checkboxes:

  0  Path         - folder / file name (the tree)
  1  Winning Mod  - the mod that owns this file in the deployed filemap

Conflict files (owned by >1 enabled mod) are tinted; the selected mod's files get
a highlight background. Mirrors gui_qt.mod_files_model but without the checkbox
columns. Display-only - all data-building lives in Utils.ui.data.
"""

from __future__ import annotations

from PySide6.QtCore import (
    Qt, QAbstractItemModel, QModelIndex, QT_TRANSLATE_NOOP, Signal,
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
BULK_DELTA_THRESHOLD = 512


class _DataNode:
    __slots__ = ("name", "path", "mod", "is_dir", "children", "parent",
                 "conflict", "candidate_id", "_row")

    def __init__(self, name, path, *, is_dir, parent=None, mod="", conflict=0,
                 candidate_id=0):
        self.name = name
        self.path = path          # canonical rel path (folder or file)
        self.mod = mod            # winning mod (files only)
        self.is_dir = is_dir
        self.children: list[_DataNode] = []
        self.parent = parent
        self.conflict = conflict  # 1 = winning conflict (tinted), 0 = none
        self.candidate_id = int(candidate_id)
        self._row = 0

    def row(self) -> int:
        return self._row


class DataModel(QAbstractItemModel):
    themeChanged = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._root = _DataNode("", "", is_dir=True)
        self._candidate_nodes: dict[int, _DataNode] = {}
        self._folder_nodes: dict[str, _DataNode] = {}
        self._path_nodes: dict[str, _DataNode] = {}
        self._highlight_mod: str | None = None
        # data() runs per visible cell; keep the QColor cached but live-refresh
        # it without resetting the model/selection.
        bind_theme(self, roles={"CONFLICT_HL_ANCHOR"})

    def refresh_theme(self, p: dict) -> None:
        self._c_highlight = qc(p, "CONFLICT_HL_ANCHOR")
        self.themeChanged.emit()

    # ---- population -------------------------------------------------------
    def set_root(self, root: _DataNode):
        candidates, folders, paths = self._index_tree(root)
        self.beginResetModel()
        self._root = root
        self._candidate_nodes = candidates
        self._folder_nodes = folders
        self._path_nodes = paths
        self.endResetModel()

    @staticmethod
    def _index_tree(root: _DataNode):
        candidates = {}
        folders = {}
        paths = {}
        stack = [root]
        while stack:
            parent = stack.pop()
            for row, child in enumerate(parent.children):
                child._row = row
                paths[child.path.casefold()] = child
                if child.is_dir:
                    folders[child.path] = child
                    stack.append(child)
                elif child.candidate_id:
                    candidates[child.candidate_id] = child
        return candidates, folders, paths

    def replace_rows(self, rows) -> None:
        """Replace the tree from ``(candidate_id, path, mod, conflict)`` rows."""
        root = _DataNode("", "", is_dir=True)
        folders: dict[str, _DataNode] = {}
        for candidate_id, path, mod, conflict in rows:
            parts = [part for part in path.replace("\\", "/").split("/") if part]
            if not parts:
                continue
            parent = root
            parent_path = ""
            for part in parts[:-1]:
                parent_path = f"{parent_path}/{part}" if parent_path else part
                node = folders.get(parent_path)
                if node is None:
                    node = _DataNode(part, parent_path, is_dir=True, parent=parent)
                    parent.children.append(node)
                    folders[parent_path] = node
                parent = node
            parent.children.append(_DataNode(
                parts[-1], path, is_dir=False, parent=parent, mod=mod,
                conflict=int(bool(conflict)), candidate_id=int(candidate_id)))
        stack = [root]
        while stack:
            parent = stack.pop()
            parent.children.sort(key=self._sort_key)
            stack.extend(child for child in parent.children if child.is_dir)
        self.set_root(root)

    def clear(self):
        self.beginResetModel()
        self._root = _DataNode("", "", is_dir=True)
        self._candidate_nodes = {}
        self._folder_nodes = {}
        self._path_nodes = {}
        self.endResetModel()

    @staticmethod
    def _sort_key(node: _DataNode):
        return (not node.is_dir, node.name.casefold(), node.name)

    def _insert_position(self, parent: _DataNode, node: _DataNode) -> int:
        key = self._sort_key(node)
        low, high = 0, len(parent.children)
        while low < high:
            middle = (low + high) // 2
            if self._sort_key(parent.children[middle]) <= key:
                low = middle + 1
            else:
                high = middle
        return low

    @staticmethod
    def _reindex_children(parent: _DataNode, start: int = 0) -> None:
        for row in range(start, len(parent.children)):
            parent.children[row]._row = row

    def _remove_leaf(self, node: _DataNode) -> None:
        parent = node.parent
        if parent is None:
            return
        row = node._row
        self.beginRemoveRows(self.index_for_node(parent), row, row)
        parent.children.pop(row)
        self._reindex_children(parent, row)
        self.endRemoveRows()
        self._candidate_nodes.pop(node.candidate_id, None)
        self._path_nodes.pop(node.path.casefold(), None)
        while parent is not self._root and not parent.children:
            empty = parent
            parent = empty.parent
            if parent is None:
                break
            row = empty._row
            self.beginRemoveRows(self.index_for_node(parent), row, row)
            parent.children.pop(row)
            self._reindex_children(parent, row)
            self.endRemoveRows()
            self._folder_nodes.pop(empty.path, None)
            self._path_nodes.pop(empty.path.casefold(), None)

    def _ensure_folder(self, parent: _DataNode, name: str, path: str) -> _DataNode:
        existing = self._folder_nodes.get(path)
        if existing is not None:
            return existing
        node = _DataNode(name, path, is_dir=True, parent=parent)
        row = self._insert_position(parent, node)
        self.beginInsertRows(self.index_for_node(parent), row, row)
        parent.children.insert(row, node)
        self._reindex_children(parent, row)
        self._folder_nodes[path] = node
        self._path_nodes[path.casefold()] = node
        self.endInsertRows()
        return node

    def _insert_leaf(self, candidate_id: int, path: str, mod: str,
                     conflict: int) -> None:
        parts = [part for part in path.replace("\\", "/").split("/") if part]
        if not parts:
            return
        parent = self._root
        parent_path = ""
        for part in parts[:-1]:
            parent_path = f"{parent_path}/{part}" if parent_path else part
            parent = self._ensure_folder(parent, part, parent_path)
        node = _DataNode(
            parts[-1], path, is_dir=False, parent=parent, mod=mod,
            conflict=int(bool(conflict)), candidate_id=candidate_id,
        )
        row = self._insert_position(parent, node)
        self.beginInsertRows(self.index_for_node(parent), row, row)
        parent.children.insert(row, node)
        self._reindex_children(parent, row)
        self._candidate_nodes[candidate_id] = node
        self._path_nodes[path.casefold()] = node
        self.endInsertRows()

    def apply_leaf_delta(self, removed_ids, changed) -> None:
        """Apply ``(candidate_id, display_path, mod, conflict)`` rows locally."""
        changed_by_id = {int(row[0]): row for row in changed}
        for candidate_id in set(removed_ids or ()) | set(changed_by_id):
            node = self._candidate_nodes.get(int(candidate_id))
            replacement = changed_by_id.get(int(candidate_id))
            if node is None:
                continue
            if replacement is None or node.path != replacement[1]:
                self._remove_leaf(node)
        for candidate_id, path, mod, conflict in changed_by_id.values():
            node = self._candidate_nodes.get(int(candidate_id))
            if node is None:
                self._insert_leaf(int(candidate_id), path, mod, conflict)
                continue
            changed_roles = []
            if node.mod != mod:
                node.mod = mod
                changed_roles.append(Qt.DisplayRole)
            value = int(bool(conflict))
            if node.conflict != value:
                node.conflict = value
                changed_roles.extend((ConflictRole, Qt.BackgroundRole))
            if changed_roles:
                self.dataChanged.emit(
                    self.index_for_node(node, COL_NAME),
                    self.index_for_node(node, COL_MOD),
                    list(dict.fromkeys(changed_roles)),
                )

    def node(self, index: QModelIndex) -> _DataNode | None:
        if not index.isValid():
            return self._root
        return index.internalPointer()

    def index_for_node(self, node: _DataNode, col: int = 0) -> QModelIndex:
        if node is self._root or node.parent is None:
            return QModelIndex()
        return self.createIndex(node.row(), col, node)

    def node_for_identity(self, candidate_id: int, path: str):
        if candidate_id:
            node = self._candidate_nodes.get(candidate_id)
            if node is not None:
                return node
        return self._path_nodes.get(path.casefold())

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
        if parent.isValid() and parent.column() != 0:
            return 0
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
