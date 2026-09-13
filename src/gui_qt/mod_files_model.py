"""Qt tree model for the Mod Files tab.

A QAbstractItemModel over a folder/file hierarchy built from a mod's raw file
listing (Utils.mods.files.build_tree). Four columns:

  0  File name  - the tree (folder/file names)
  1  Top Level  - checkbox: is this path promoted to deploy at the game root
  2  Root       - checkbox: deploy this file to the game root folder (tri-state)
  3  Enabled    - checkbox: is this file included in deploy (folders = tri-state)

The model is display-only state; all persistence + the strip/exclusion
algorithms live in Utils.mods.files. The view drives saves on checkbox clicks.
"""

from __future__ import annotations

from PySide6.QtCore import (
    Qt, QAbstractItemModel, QCoreApplication, QModelIndex, QT_TRANSLATE_NOOP,
    Signal)

from gui_qt.theme_qt import bind_theme, qc
from gui_qt.tooltips import wrap_tooltip

COL_NAME = 0
COL_TOPLEVEL = 1
COL_ROOT = 2
COL_DISABLE = 3
# Translated at display time in headerData; register literals for lupdate.
COLUMNS = [
    QT_TRANSLATE_NOOP("ModFilesModel", "File name"),
    QT_TRANSLATE_NOOP("ModFilesModel", "Top Level"),
    QT_TRANSLATE_NOOP("ModFilesModel", "Root"),
    QT_TRANSLATE_NOOP("ModFilesModel", "Enabled"),
]
# Header tooltips, one per column. Translated at display time in headerData.
COLUMN_TIPS = [
    QT_TRANSLATE_NOOP(
        "ModFilesModel",
        "The mod's files and folders as they are packaged in the archive."),
    QT_TRANSLATE_NOOP(
        "ModFilesModel",
        "Promote this folder's contents up to the top of the mod, stripping "
        "the wrapper folders above it. Use this when a mod is packaged one or "
        "more folders too deep, so its files land in the right place on "
        "deploy."),
    QT_TRANSLATE_NOOP(
        "ModFilesModel",
        "Deploy this file or folder to the game's root folder (next to the "
        "game executable) instead of the game's data folder. Use this for "
        "loaders, DLLs and INIs that belong beside the .exe."),
    QT_TRANSLATE_NOOP(
        "ModFilesModel",
        "Whether this file or folder is deployed. Unchecked excludes it: it "
        "stays in the mod but is never written to the game, so it cannot win "
        "conflicts."),
]

# Custom roles.
NodeRole = Qt.UserRole + 1       # the _Node
ConflictRole = Qt.UserRole + 2   # 0 none, 1 win (green), -1 lose (red)


class _Node:
    __slots__ = ("name", "path", "raw_key", "rel_str", "is_dir",
                 "children", "parent", "checked", "conflict", "top_level",
                 "root_tag", "synthetic", "stripped", "meta", "_row",
                 "leaf_count", "checked_count", "root_count")

    def __init__(self, name, path, *, is_dir, parent=None,
                 rel_str=None, raw_key=None):
        self.name = name
        self.path = path            # canonical rel path (orig case)
        self.raw_key = raw_key      # raw on-disk key (files only) - the state key
        self.rel_str = rel_str      # raw on-disk path (files only)
        self.is_dir = is_dir
        self.children: list[_Node] = []
        self.parent = parent
        self.checked = True         # Enabled column: True = included
        self.conflict = 0           # -1 lose, 0 none, 1 win
        self.top_level = False      # Top Level column checked
        self.root_tag = False       # Root column: deploy to game root (files)
        self.synthetic = False      # greyed strip placeholder
        self.stripped = False       # this path is itself stripped (greyed)
        self.meta = False           # the mod's meta.ini row (view/edit only)
        self._row = 0
        self.leaf_count = 0
        self.checked_count = 0
        self.root_count = 0

    def row(self) -> int:
        return self._row


class ModFilesModel(QAbstractItemModel):
    themeChanged = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._root = _Node("", "", is_dir=True)
        # path(lower) -> node, for ancestor/folder lookups
        self._by_path: dict[str, _Node] = {}
        # data() runs per visible cell; keep QColors cached but refresh them on
        # a live theme change rather than rebuilding the model.
        bind_theme(self, roles={"FILE_DIM", "FILE_LOSE", "FILE_WIN"})

    def refresh_theme(self, p: dict) -> None:
        self._c_dim = qc(p, "FILE_DIM")
        self._c_win = qc(p, "FILE_WIN")
        self._c_lose = qc(p, "FILE_LOSE")
        self.themeChanged.emit()

    # ---- population -------------------------------------------------------
    def set_root(self, root: _Node, by_path: dict[str, _Node]):
        self._prepare_tree(root)
        self.beginResetModel()
        self._root = root
        self._by_path = by_path
        self.endResetModel()

    @staticmethod
    def _prepare_tree(root: _Node) -> None:
        stack = [(root, False)]
        while stack:
            node, visited = stack.pop()
            if not node.is_dir:
                node.leaf_count = 1
                node.checked_count = int(node.checked)
                node.root_count = int(node.root_tag)
                continue
            if not visited:
                stack.append((node, True))
                for row in range(len(node.children) - 1, -1, -1):
                    child = node.children[row]
                    child._row = row
                    stack.append((child, False))
                continue
            node.leaf_count = sum(child.leaf_count for child in node.children)
            node.checked_count = sum(child.checked_count for child in node.children)
            node.root_count = sum(child.root_count for child in node.children)

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
        if parent.isValid() and parent.column() != 0:
            return 0
        pnode = self.node(parent)
        return len(pnode.children) if pnode else 0

    def columnCount(self, parent=QModelIndex()):
        return len(COLUMNS)

    @classmethod
    def column_title(cls, section: int) -> str:
        """The translated header caption. Classmethod so the view can measure
        it for column minimums before a model exists."""
        return QCoreApplication.translate("ModFilesModel", COLUMNS[section])

    @classmethod
    def column_tip(cls, section: int) -> str:
        """The translated header tooltip, hard-wrapped to a readable width.

        Qt lays a tooltip out on one line however long it gets (and ignores CSS
        widths in its rich-text subset), so we insert the breaks ourselves.
        """
        text = QCoreApplication.translate("ModFilesModel", COLUMN_TIPS[section])
        return wrap_tooltip(text)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal:
            if role == Qt.DisplayRole:
                return self.column_title(section)
            if role == Qt.ToolTipRole:
                return self.column_tip(section)
        return None

    def flags(self, index):
        if not index.isValid():
            return Qt.NoItemFlags
        f = Qt.ItemIsEnabled | Qt.ItemIsSelectable
        node: _Node = index.internalPointer()
        # The meta.ini row is view/edit-only - it deploys nothing, so it gets
        # no checkboxes.
        if not getattr(node, "meta", False) and \
                index.column() in (COL_TOPLEVEL, COL_ROOT, COL_DISABLE):
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
            if col == COL_ROOT:
                return self._root_state(node)
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
        """Enabled column. Files are checked when included.
        Folders: tri-state from their leaves."""
        if not node.is_dir:
            return Qt.Checked if node.checked else Qt.Unchecked
        if not node.leaf_count:
            return Qt.Checked
        if node.checked_count == node.leaf_count:
            return Qt.Checked
        if node.checked_count == 0:
            return Qt.Unchecked
        return Qt.PartiallyChecked

    def _root_state(self, node: _Node):
        """Root column: files checked when routed to the game root.
        Folders: tri-state from their leaves."""
        if not node.is_dir:
            return Qt.Checked if node.root_tag else Qt.Unchecked
        if not node.leaf_count:
            return Qt.Unchecked
        if node.root_count == node.leaf_count:
            return Qt.Checked
        if node.root_count == 0:
            return Qt.Unchecked
        return Qt.PartiallyChecked

    def _is_greyed(self, node: _Node) -> bool:
        """Name greys when the row (or whole folder) is disabled."""
        if not node.is_dir:
            return not node.checked
        return bool(node.leaf_count) and node.checked_count == 0

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

    # ---- mutation (view calls these, then persists via Utils.mods.files) ---
    def set_disabled_subtree(self, node: _Node, included: bool):
        """Set the Enabled state for a node + all descendants."""
        if node.is_dir:
            old_count = node.checked_count
            value = node.leaf_count if included else 0
            stack = [node]
            while stack:
                descendant = stack.pop()
                if descendant.is_dir:
                    descendant.checked_count = (
                        descendant.leaf_count if included else 0)
                    stack.extend(descendant.children)
                else:
                    descendant.checked = included
                    descendant.checked_count = int(included)
            node.checked_count = value
            self._adjust_ancestors(node.parent, "checked_count", value - old_count)
        else:
            self._set_leaf_state(node, "checked", "checked_count", included)
        self._emit_subtree_and_ancestors(node)

    def set_disabled(self, node: _Node, included: bool):
        self._set_leaf_state(node, "checked", "checked_count", included)
        self._emit_subtree_and_ancestors(node)

    def set_root_subtree(self, node: _Node, tagged: bool):
        """Set the Root state for a node + all descendants (folder toggle)."""
        if node.is_dir:
            old_count = node.root_count
            value = node.leaf_count if tagged else 0
            stack = [node]
            while stack:
                descendant = stack.pop()
                if descendant.is_dir:
                    descendant.root_count = (
                        descendant.leaf_count if tagged else 0)
                    stack.extend(descendant.children)
                else:
                    descendant.root_tag = tagged
                    descendant.root_count = int(tagged)
            node.root_count = value
            self._adjust_ancestors(node.parent, "root_count", value - old_count)
        else:
            self._set_leaf_state(node, "root_tag", "root_count", tagged)
        self._emit_subtree_and_ancestors(node)

    def set_root_tag(self, node: _Node, tagged: bool):
        self._set_leaf_state(node, "root_tag", "root_count", tagged)
        self._emit_subtree_and_ancestors(node)

    @staticmethod
    def _adjust_ancestors(node: _Node | None, field: str, delta: int) -> None:
        while node is not None:
            setattr(node, field, getattr(node, field) + delta)
            node = node.parent

    def _set_leaf_state(self, node: _Node, value_field: str,
                        count_field: str, value: bool) -> None:
        old = bool(getattr(node, value_field))
        value = bool(value)
        if old == value:
            return
        setattr(node, value_field, value)
        setattr(node, count_field, int(value))
        self._adjust_ancestors(node.parent, count_field,
                               int(value) - int(old))

    def _emit_subtree_and_ancestors(self, node: _Node):
        # Repaint the node, its descendants, and its tri-state ancestors.
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
        # Descendants - one contiguous signal per parent keeps nested folder
        # tri-states current without sending one Qt event per file.
        if node.is_dir:
            stack = [node]
            while stack:
                parent = stack.pop()
                if not parent.children:
                    continue
                first = parent.children[0]
                last = parent.children[-1]
                self.dataChanged.emit(
                    self.createIndex(first._row, COL_NAME, first),
                    self.createIndex(last._row, COL_DISABLE, last),
                    [Qt.CheckStateRole, Qt.ForegroundRole])
                stack.extend(child for child in parent.children if child.is_dir)

    def refresh_all(self):
        """Repaint every cell (after a Top Level change recomputes top_level)."""
        if self.rowCount():
            self.dataChanged.emit(self.createIndex(0, 0, self._root.children[0]),
                                  self.createIndex(self.rowCount() - 1, COL_DISABLE,
                                                   self._root.children[-1]),
                                  [Qt.CheckStateRole, Qt.ForegroundRole, Qt.DisplayRole])
