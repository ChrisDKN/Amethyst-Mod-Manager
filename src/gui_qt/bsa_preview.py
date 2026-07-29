"""Panel-scoped BSA / BA2 / UE pak content preview for the Mod Files tab.

Reads the archive's table-of-contents (Utils.bsa_reader for BSA/BA2,
Utils.ue_pak_reader for .pak/.utoc — TOC only, no decompression) and shows
the internal file structure as a read-only tree. Uses the same visual recipe as the Mod Files / Text Files
trees (QTreeView, no native branch decoration, TkStyleHeader-less single
column, custom delegate drawing the arrow.png/right.png indicator + indent) so
it looks consistent with the rest of the app. Replaces the old (removed) Tk
"Archive" tab.

Conflict tints: when the host supplies ``conflict_fn`` (app-side lookup of
the archive-conflict winner map), contested files are coloured green (this
mod's copy wins) or red (another mod's archive wins) using the same tones
as the Show Conflicts tab; folders roll up their children's states (yellow
= contains both winners and losers) so collapsed branches stay visible.
The lookup runs on a daemon thread — the tree shows immediately, tints
arrive when ready.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QModelIndex, QAbstractItemModel, QRect, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QTreeView, QAbstractItemView, QSizePolicy,
    QStyledItemDelegate,
)

from gui_qt.safe_emit import safe_emit
from gui_qt.theme_qt import active_palette, _c
from gui_qt.icons import icon

# Archive extensions that get a content-preview tab instead of an image preview.
ARCHIVE_EXTS = {".bsa", ".ba2", ".pak", ".utoc"}

ARROW_SZ = 20
INDENT = 18
FONT_PX = 13

NodeRole = Qt.UserRole + 1


class _Node:
    __slots__ = ("name", "is_dir", "children", "parent", "code")

    def __init__(self, name, *, is_dir, parent=None):
        self.name = name
        self.is_dir = is_dir
        self.children: list["_Node"] = []
        self.parent = parent
        self.code = 0   # 0 none / 1 wins / -1 loses / 2 mixed (dirs only)

    def row(self) -> int:
        if self.parent is None:
            return 0
        return self.parent.children.index(self)


def _build_tree(paths: list[str]) -> tuple[_Node, dict[str, _Node]]:
    """Turn flat 'a/b/c.dds' paths into a folder/file _Node hierarchy.

    Also returns {full_path: file_node} so conflict codes can be applied
    to the right leaves later without re-walking.
    """
    root = _Node("", is_dir=True)
    folders: dict[str, _Node] = {}
    files: dict[str, _Node] = {}
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
                node = _Node(seg, is_dir=True, parent=parent)
                parent.children.append(node)
                folders[path_so_far] = node
            parent = node
        leaf = _Node(parts[-1], is_dir=False, parent=parent)
        parent.children.append(leaf)
        files[norm] = leaf
    _sort(root)
    return root, files


def _sort(node: _Node):
    # Folders first, then files, each alphabetical (case-insensitive).
    node.children.sort(key=lambda n: (not n.is_dir, n.name.lower()))
    for c in node.children:
        if c.is_dir:
            _sort(c)


class _ArchiveModel(QAbstractItemModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._root = _Node("", is_dir=True)

    def set_root(self, root: _Node):
        self.beginResetModel()
        self._root = root
        self.endResetModel()

    def node(self, index: QModelIndex) -> _Node | None:
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
        node: _Node = index.internalPointer()
        if role == NodeRole:
            return node
        if role == Qt.DisplayRole:
            return node.name
        return None


class _ArchiveDelegate(QStyledItemDelegate):
    """Name-only delegate: arrow.png/right.png indicator + per-depth indent +
    elided text — same look as the Mod Files / Text Files name column."""

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
        if node.is_dir and index.model().rowCount(index) > 0:
            a = QRect(x, r.top() + (r.height() - ARROW_SZ) // 2, ARROW_SZ, ARROW_SZ)
            expanded = self._view.isExpanded(index)
            ico = icon("arrow.png" if expanded else "right.png", ARROW_SZ,
                       color=self.c_arrow)
            if not ico.isNull():
                ico.paint(p, a)
        x += ARROW_SZ + 2

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
        f = QFont(); f.setPixelSize(FONT_PX); p.setFont(f)
        text_rect = QRect(x, r.top(), r.right() - x - 4, r.height())
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


class BsaPreview(QWidget):
    """Read-only preview of a BSA/BA2/pak archive's internal file tree with
    optional per-file conflict tints (see module docstring)."""

    close_requested = Signal()
    _conflicts_ready = Signal(int, dict)   # (generation, {path: code})

    def __init__(self, path: Path, display_name: str = "", parent=None,
                 conflict_fn=None):
        super().__init__(parent)
        self.setObjectName("BsaPreview")
        self._conflict_fn = conflict_fn
        self._gen = 0
        self._paths: list[str] = []
        self._codes: dict[str, int] = {}
        self._file_nodes: dict[str, "_Node"] = {}
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        tb = QWidget()
        tb.setObjectName("HeaderBar")
        from PySide6.QtWidgets import QHBoxLayout, QPushButton, QCheckBox
        tbl = QHBoxLayout(tb)
        tbl.setContentsMargins(8, 4, 8, 4)
        self._header = QLabel(display_name or path.name)
        self._header.setStyleSheet("color:#ddd; font-weight:600;")
        self._header.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        tbl.addWidget(self._header, 1)

        self._only_conflicts = QCheckBox(self.tr("Only conflicts"))
        self._only_conflicts.setEnabled(False)   # armed once codes arrive
        self._only_conflicts.toggled.connect(lambda _on: self._rebuild_tree())
        tbl.addWidget(self._only_conflicts, 0)

        expand_btn = QPushButton(self.tr("Expand All"))
        expand_btn.setCursor(Qt.PointingHandCursor)
        expand_btn.clicked.connect(lambda: self._tree.expandAll())
        tbl.addWidget(expand_btn, 0)
        collapse_btn = QPushButton(self.tr("Collapse All"))
        collapse_btn.setCursor(Qt.PointingHandCursor)
        collapse_btn.clicked.connect(lambda: self._tree.collapseAll())
        tbl.addWidget(collapse_btn, 0)

        from gui_qt.theme_qt import danger_close_button
        close_btn = danger_close_button()
        close_btn.clicked.connect(self.close_requested.emit)
        tbl.addWidget(close_btn, 0)
        v.addWidget(tb)
        self._conflicts_ready.connect(self._on_conflicts_ready)

        self._model = _ArchiveModel(self)
        self._tree = QTreeView()
        self._tree.setModel(self._model)
        self._tree.setHeaderHidden(True)
        self._tree.setRootIsDecorated(False)
        self._tree.setIndentation(0)
        self._tree.setUniformRowHeights(True)
        self._tree.setAlternatingRowColors(False)
        self._tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self._tree.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._tree.setItemDelegate(_ArchiveDelegate(self._tree))
        # We draw our own arrow, so a name-column click toggles expand.
        self._tree.clicked.connect(self._on_clicked)
        self._tree.expanded.connect(lambda *_: self._tree.viewport().update())
        self._tree.collapsed.connect(lambda *_: self._tree.viewport().update())
        v.addWidget(self._tree, 1)

        self.set_archive(path, display_name)

    def _on_clicked(self, index):
        node = self._model.node(index)
        if node is not None and node.is_dir and self._model.rowCount(index) > 0:
            self._tree.setExpanded(index, not self._tree.isExpanded(index))

    def set_archive(self, path: Path, display_name: str = ""):
        """Load (or swap) the previewed archive in place."""
        self._header.setText(display_name or path.name)
        self._gen += 1
        self._codes = {}
        self._only_conflicts.blockSignals(True)
        self._only_conflicts.setChecked(False)
        self._only_conflicts.setEnabled(False)
        self._only_conflicts.blockSignals(False)
        try:
            from Utils.ue_pak_reader import (
                UE_ARCHIVE_EXTENSIONS, read_ue_archive_file_list,
            )
            if Path(path).suffix.lower() in UE_ARCHIVE_EXTENSIONS:
                paths = read_ue_archive_file_list(path)
            else:
                from Utils.bsa_reader import read_bsa_file_list
                paths = read_bsa_file_list(path)
        except Exception:
            paths = []
        self._paths = paths
        if not paths:
            self._file_nodes = {}
            empty = _Node("", is_dir=True)
            empty.children.append(
                _Node(self.tr("(archive is empty or unreadable)"), is_dir=False,
                      parent=empty))
            self._model.set_root(empty)
            return
        self._rebuild_tree()
        self._tree.collapseAll()
        # Conflict lookup off-thread; tints land via _conflicts_ready.
        if self._conflict_fn is not None:
            import threading
            gen = self._gen
            fn = self._conflict_fn

            def worker():
                try:
                    codes = fn(path) or {}
                except Exception:
                    codes = {}
                safe_emit(self._conflicts_ready, gen, codes)

            threading.Thread(target=worker, daemon=True).start()

    def _rebuild_tree(self):
        """(Re)build the tree from the current paths, honouring the
        Only-conflicts filter, and re-apply any known conflict codes."""
        paths = self._paths
        if self._only_conflicts.isChecked() and self._codes:
            paths = [p for p in paths if self._codes.get(p)]
        root, files = _build_tree(paths)
        self._root = root
        self._file_nodes = files
        self._apply_codes()
        self._model.set_root(root)
        if self._only_conflicts.isChecked():
            self._tree.expandAll()

    def _on_conflicts_ready(self, gen: int, codes: dict):
        if gen != self._gen:
            return   # a different archive was swapped in meanwhile
        self._codes = {p: c for p, c in codes.items() if c}
        self._only_conflicts.setEnabled(bool(self._codes))
        if self._apply_codes():
            self._tree.viewport().update()

    def _apply_codes(self) -> bool:
        """Set leaf codes from self._codes and roll folder states up
        (green = only winners inside, red = only losers, yellow = both).
        Returns True when at least one node was tinted."""
        hit = False
        for fp, code in self._codes.items():
            node = self._file_nodes.get(fp)
            if node is not None:
                node.code = code
                hit = True
        if not hit:
            return False

        def roll(node: "_Node") -> None:
            win = lose = False
            for c in node.children:
                if c.is_dir:
                    roll(c)
                if c.code in (1, 2):
                    win = True
                if c.code in (-1, 2):
                    lose = True
            node.code = 2 if (win and lose) else (1 if win else (-1 if lose else 0))

        root = getattr(self, "_root", None)
        if root is not None:
            roll(root)
        return True
