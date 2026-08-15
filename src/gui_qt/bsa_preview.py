"""Panel-scoped BSA / BA2 / UE pak content preview for the Mod Files tab.

Reads the archive's table-of-contents (Utils.bsa_reader for BSA/BA2,
Utils.ue_pak_reader for Unreal .pak/.utoc, Utils.pak_reader for Baldur's
Gate 3 LSPK .pak - the two .pak formats are told apart by magic, not by
extension; TOC only, no file-data decompression) and shows
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
The lookup runs on a daemon thread - the tree shows immediately, tints
arrive when ready.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QTreeView, QAbstractItemView, QSizePolicy,
)

from gui_qt.safe_emit import safe_emit
from gui_qt.path_tree import (
    Node as _Node, PathTreeDelegate as _ArchiveDelegate,
    PathTreeModel as _ArchiveModel, build_tree as _build_tree,
    node_path as _node_path,
)
from gui_qt.theme_qt import active_palette, _c

# Archive extensions that get a content-preview tab instead of an image preview.
ARCHIVE_EXTS = {".bsa", ".ba2", ".pak", ".utoc"}


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
        self._path: "Path | None" = None
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
        self._header.setStyleSheet(
            f"color:{_c(active_palette(), 'TEXT_MAIN')}; font-weight:600;")
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
        if node is None:
            return
        if node.is_dir and self._model.rowCount(index) > 0:
            self._tree.setExpanded(index, not self._tree.isExpanded(index))
            return
        if not node.is_dir:
            self._open_file_node(node)

    def _open_file_node(self, node):
        """Hand a previewable entry to the host, identified by its inner path."""
        from gui_qt.nif_preview import PREVIEW_EXTS as NIF_EXTS
        rel = _node_path(node)
        ext = ("." + rel.rsplit(".", 1)[-1].lower()) if "." in rel else ""
        if ext in NIF_EXTS:
            cb = getattr(self, "on_open_nif", None)
            if cb is not None and self._path is not None:
                cb(self._path, rel)

    def set_archive(self, path: Path, display_name: str = ""):
        """Load (or swap) the previewed archive in place."""
        self._header.setText(display_name or path.name)
        self._path = path
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
            from Utils.pak_reader import is_lspk_file, read_lspk_file_list
            suffix = Path(path).suffix.lower()
            if suffix == ".pak" and is_lspk_file(path):
                # Baldur's Gate 3 paks are Larian LSPK, not Unreal - same
                # extension, unrelated format.
                paths = read_lspk_file_list(path)
            elif suffix in UE_ARCHIVE_EXTENSIONS:
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
