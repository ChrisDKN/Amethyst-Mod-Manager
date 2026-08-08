"""
path_tree.py
Shared read-only virtual path tree (model + delegate) for slash-separated paths.

Extracted from the BSA/BA2 content preview so the NIF viewer can reuse the same
look as the Mod Files / Text Files trees: QTreeView with no native branch
decoration, a single column, and a delegate that draws the arrow.png/right.png
indicator, a per-depth indent, elided text and the conflict tints. Nodes carry an
optional right-aligned ``source`` label and an opaque ``payload`` for the host.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QModelIndex, QAbstractItemModel, QRect
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import QStyledItemDelegate

from gui_qt.theme_qt import active_palette, _c
from gui_qt.icons import icon

__all__ = ["Node", "node_path", "build_tree", "sort_tree", "split_row_width",
           "PathTreeModel",
           "PathTreeDelegate", "NodeRole", "ARROW_SZ", "INDENT", "FONT_PX"]

ARROW_SZ = 20
INDENT = 18
FONT_PX = 13

# Gap between a row's name and its right-aligned origin label, and the share of
# a cramped row the name may keep before the origin starts eliding.
_SRC_GAP = 10
_NAME_SHARE = 0.55

NodeRole = Qt.UserRole + 1


class Node:
    __slots__ = ("name", "is_dir", "children", "parent", "code", "source",
                 "payload")

    def __init__(self, name, *, is_dir, parent=None, source="", payload=None):
        self.name = name
        self.is_dir = is_dir
        self.children: list["Node"] = []
        self.parent = parent
        self.code = 0   # 0 none / 1 wins / -1 loses / 2 mixed (dirs only)
        self.source = source    # right-aligned origin label ("" = none)
        self.payload = payload  # host-defined; what a click on this row opens

    def row(self) -> int:
        if self.parent is None:
            return 0
        return self.parent.children.index(self)


def split_row_width(fm, name: str, source: str, avail: int) -> tuple[int, int]:
    """(name_width, source_width) for a row: neither elides while both fit;
    when cramped the name keeps up to _NAME_SHARE and the origin gets the rest.
    """
    if not source:
        return max(0, avail), 0
    # +2: elidedText() elides when the advance EQUALS the given width (right
    # bearing), so the exact measured advance still comes back elided.
    src_want = fm.horizontalAdvance(source) + 2
    name_want = fm.horizontalAdvance(name) + 2
    if src_want + _SRC_GAP + name_want <= avail:
        src_w = src_want
    else:
        name_keep = min(name_want, int(avail * _NAME_SHARE))
        src_w = max(0, min(src_want, avail - _SRC_GAP - name_keep))
    return max(0, avail - src_w - _SRC_GAP), src_w


def node_path(node: "Node") -> str:
    """Rebuild a node's full path by walking up to the root."""
    parts = []
    cur = node
    while cur is not None and cur.parent is not None:
        parts.append(cur.name)
        cur = cur.parent
    return "/".join(reversed(parts))


def build_tree(paths: list[str]) -> tuple[Node, dict[str, Node]]:
    """Turn flat 'a/b/c.dds' paths into a folder/file Node hierarchy.

    Also returns {full_path: file_node} so conflict codes can be applied
    to the right leaves later without re-walking.
    """
    root = Node("", is_dir=True)
    folders: dict[str, Node] = {}
    files: dict[str, Node] = {}
    for p in paths:
        if not p:
            continue
        norm = p.replace("\\", "/")
        parts = norm.split("/")
        parent = root
        path_so_far = ""
        for seg in parts[:-1]:
            path_so_far = f"{path_so_far}/{seg}" if path_so_far else seg
            node = folders.get(path_so_far)
            if node is None:
                node = Node(seg, is_dir=True, parent=parent)
                parent.children.append(node)
                folders[path_so_far] = node
            parent = node
        leaf = Node(parts[-1], is_dir=False, parent=parent)
        parent.children.append(leaf)
        files[norm] = leaf
    sort_tree(root)
    return root, files


def sort_tree(node: Node):
    """Folders first, then files, each alphabetical (case-insensitive).

    Only recurses into directories, so a file node's children (e.g. the several
    copies of one asset) keep the order the caller appended them in.
    """
    node.children.sort(key=lambda n: (not n.is_dir, n.name.lower()))
    for c in node.children:
        if c.is_dir:
            sort_tree(c)


class PathTreeModel(QAbstractItemModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._root = Node("", is_dir=True)

    def set_root(self, root: Node):
        self.beginResetModel()
        self._root = root
        self.endResetModel()

    def node(self, index: QModelIndex) -> Node | None:
        if not index.isValid():
            return self._root
        return index.internalPointer()

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
        return 1

    def flags(self, index):
        if not index.isValid():
            return Qt.NoItemFlags
        return Qt.ItemIsEnabled | Qt.ItemIsSelectable

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        node: Node = index.internalPointer()
        if role == NodeRole:
            return node
        if role == Qt.DisplayRole:
            return node.name
        return None


class PathTreeDelegate(QStyledItemDelegate):
    """Name-only delegate: arrow.png/right.png indicator + per-depth indent +
    elided text - same look as the Mod Files / Text Files name column.
    A node's ``source`` is drawn dim and right-aligned when set."""

    def __init__(self, view, parent=None):
        super().__init__(parent or view)
        self._view = view
        p = active_palette()
        self.c_text = QColor(_c(p, "TEXT_MAIN"))
        self.c_dim = QColor("#9a9a9a")
        self.c_sel = QColor(_c(p, "BG_SELECT"))
        self.c_arrow = _c(p, "DROPDOWN_ARROW")   # expand/collapse arrow tint
        # Same tones as the Show Conflicts tab panes.
        self.c_win = QColor("#98c379")
        self.c_lose = QColor("#e06c75")
        self.c_mixed = QColor("#e5c07b")

    def paint(self, p, opt, index):
        r = opt.rect
        node = index.model().node(index)
        if node is None:
            return
        if opt.state & opt.state.State_Selected:
            p.fillRect(r, self.c_sel)

        depth = self._depth(index)
        x = r.left() + 4 + depth * INDENT
        # Arrow for ANY node with children, not just folders: a file row holds
        # one child per copy of a contested path.
        if index.model().rowCount(index) > 0:
            a = QRect(x, r.top() + (r.height() - ARROW_SZ) // 2, ARROW_SZ, ARROW_SZ)
            expanded = self._view.isExpanded(index)
            ico = icon("arrow.png" if expanded else "right.png", ARROW_SZ,
                       color=self.c_arrow)
            if not ico.isNull():
                ico.paint(p, a)
        x += ARROW_SZ + 2

        f = QFont()
        f.setPixelSize(FONT_PX)
        p.setFont(f)
        right = r.right() - 4

        # Origin label first: it claims its width, the name elides into the rest.
        if node.source:
            fm = p.fontMetrics()
            _name_w, width = split_row_width(fm, node.name, node.source,
                                             max(0, right - x))
            p.setPen(self.c_dim)
            src_rect = QRect(right - width, r.top(), width, r.height())
            p.drawText(src_rect, Qt.AlignVCenter | Qt.AlignRight,
                       fm.elidedText(node.source, Qt.ElideLeft, width))
            right = src_rect.left() - _SRC_GAP

        code = node.code
        if code == 1:
            pen = self.c_win
        elif code == -1:
            pen = self.c_lose
        elif code == 2:
            pen = self.c_mixed
        else:
            pen = self.c_text if node.is_dir else self.c_dim
        p.setPen(pen)
        text_rect = QRect(x, r.top(), max(0, right - x), r.height())
        elided = p.fontMetrics().elidedText(node.name, Qt.ElideRight,
                                            text_rect.width())
        p.drawText(text_rect, Qt.AlignVCenter | Qt.AlignLeft, elided)

    def _depth(self, index) -> int:
        d = 0
        idx = index.parent()
        while idx.isValid():
            d += 1
            idx = idx.parent()
        return d

    def sizeHint(self, opt, index):
        s = super().sizeHint(opt, index)
        s.setHeight(max(s.height(), 22))
        return s
