"""Qt Data tab — the merged deployment tree (Path + Winning Mod), conflict
highlighting, file-type/only-conflicts filter, search, and image preview.

Mirrors gui_qt.mod_files_view's structure/visuals (lean: header label + QTreeView,
no embedded footer — the footer + filter panel are owned by the app) but reads the
deployed filemap via Utils.data_tab instead of a per-mod scan. Built lazily: the
tree is only (re)built when the Data sub-tab is visible (mark_dirty defers it).
"""

from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QModelIndex, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTreeView, QLabel, QAbstractItemView,
)

import Utils.data_tab as dtlogic
import Utils.mod_files as mflogic
from gui_qt.data_model import DataModel, _DataNode, COL_NAME, COL_MOD
from gui_qt.safe_emit import safe_emit


class DataView(QWidget):
    """The Data tab. configure() once, then refresh()/mark_dirty() to (re)build."""

    filetypes_changed = Signal()
    scan_status_changed = Signal(bool)         # True = build running
    _data_ready = Signal(int, object, object)  # gen, entries, contested

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scan_gen = 0                  # bumped per build → drops stale
        self._scanning = False
        self.game = None
        self.profile_dir: Path | None = None
        self.filemap_path: Path | None = None
        self.index_path: Path | None = None
        self.on_select_mod = None          # callback(mod_name | None) — highlight
        self._dirty = True
        self._is_visible = False           # is the Data sub-tab currently shown
        self._search = ""
        self._search_exts: frozenset[str] = frozenset()
        self._inc_exts: set[str] = set()
        self._exc_exts: set[str] = set()
        self._only_conflicts = False
        self._ext_counts: dict[str, int] = {}
        # Resolved-entries cache (keyed on filemap mtime+game) so filter/search
        # re-runs don't re-resolve the whole filemap (Tk _data_resolved_cache).
        self._resolved_cache: tuple | None = None
        self._build()
        self._data_ready.connect(self._on_data_ready)
        self.scan_status_changed.connect(self._on_scan_status)

    def _on_scan_status(self, running: bool):
        if running:
            self._label.setText(self.tr("Loading…"))
            self._loading_overlay.show_over()
        else:
            self._loading_overlay.hide_overlay()

    # -- context ------------------------------------------------------------
    def configure(self, game, profile_dir, filemap_path, index_path):
        self.game = game
        self.profile_dir = profile_dir
        self.filemap_path = filemap_path
        self.index_path = index_path
        self._resolved_cache = None
        self._dirty = True

    def set_visible_tab(self, visible: bool):
        """Tell the view whether the Data sub-tab is showing. Switching TO it
        triggers a deferred rebuild if dirty."""
        self._is_visible = visible
        if visible and self._dirty:
            self.refresh()

    def mark_dirty(self):
        """Deploy state changed. Rebuild now if visible, else defer."""
        self._dirty = True
        self._resolved_cache = None
        if self._is_visible:
            self.refresh()

    def refresh(self):
        self._dirty = False
        self._repopulate()

    # -- construction -------------------------------------------------------
    def _build(self):
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        tb = QWidget()
        tb.setObjectName("HeaderBar")
        tbl = QHBoxLayout(tb)
        tbl.setContentsMargins(8, 4, 8, 4)
        self._label = QLabel(self.tr("Deployed files"))
        self._label.setStyleSheet("color:#aaa;")
        tbl.addWidget(self._label, 1)
        v.addWidget(tb)

        self._model = DataModel(self)
        self._tree = QTreeView()
        self._tree.setModel(self._model)
        self._tree.setRootIsDecorated(False)
        self._tree.setIndentation(0)
        self._tree.setUniformRowHeights(True)
        self._tree.setAlternatingRowColors(False)
        self._tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self._tree.setExpandsOnDoubleClick(True)
        self._tree.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._on_context_menu)
        self._tree.clicked.connect(self._on_clicked)
        self._tree.selectionModel().selectionChanged.connect(
            lambda *_: self._on_selection_changed())
        from gui_qt.data_delegate import DataDelegate
        self._tree.setItemDelegate(DataDelegate(self._tree))

        from gui_qt.modlist_header import TkStyleHeader
        col_mins = {COL_NAME: 140, COL_MOD: 120}
        col_defaults = {COL_MOD: 200}
        hdr = TkStyleHeader(self._tree, col_mins, col_defaults)
        self._tree.setHeader(hdr)
        hdr.setMinimumSectionSize(min(col_mins.values()))
        hdr.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        for col, wdt in col_defaults.items():
            self._tree.setColumnWidth(col, wdt)
        self._tree.expanded.connect(lambda *_: self._tree.viewport().update())
        self._tree.collapsed.connect(lambda *_: self._tree.viewport().update())
        self._name_min = col_mins[COL_NAME]
        self._tree.viewport().installEventFilter(self)
        v.addWidget(self._tree, 1)

        from gui_qt.loading_overlay import LoadingOverlay
        self._loading_overlay = LoadingOverlay(self._tree)

    def eventFilter(self, obj, event):
        from PySide6.QtCore import QEvent
        if obj is self._tree.viewport() and event.type() == QEvent.Resize:
            self._fit_name_to_width()
        return super().eventFilter(obj, event)

    def _fit_name_to_width(self):
        vp = self._tree.viewport().width()
        if vp <= 0:
            return
        target = vp - self._tree.columnWidth(COL_MOD)
        if target >= self._name_min and target != self._tree.columnWidth(COL_NAME):
            self._tree.header().resizeSection(COL_NAME, target)

    # -- filter spec / state (mirrors ModFilesView) -------------------------
    @staticmethod
    def filter_spec() -> list[dict]:
        return [
            {"title": "By conflict", "type": "checks", "items": [
                ("only_conflicts", "Only conflicts", True),
            ]},
            {"title": "By file type", "type": "dynamic", "id": "filetypes"},
        ]

    def apply_filter_state(self, state: dict):
        self._only_conflicts = state.get("only_conflicts") == 1
        self._inc_exts = set(state.get("filetypes") or ())
        self._exc_exts = set(state.get("filetypes_exclude") or ())
        self._repopulate()

    def filetype_items(self) -> list[tuple]:
        items = sorted(self._ext_counts.items(), key=lambda kv: kv[0])
        return [(ext or "(none)", ext or "(no ext)", n) for ext, n in items]

    # -- population ---------------------------------------------------------
    def _resolved_entries(self):
        """Resolved [(rel_path, mod)] for the active filemap, cached on mtime."""
        if self.game is None or self.filemap_path is None or self.profile_dir is None:
            return []
        try:
            mtime = self.filemap_path.stat().st_mtime
        except OSError:
            return []
        # filemap_root.txt feeds the tree too (root-deployed games) and can
        # change alone (root-flag toggle) — key on its mtime as well.
        try:
            root_mtime = (self.filemap_path.parent
                          / "filemap_root.txt").stat().st_mtime
        except OSError:
            root_mtime = 0.0
        key = (str(self.filemap_path), mtime, root_mtime, id(self.game))
        if self._resolved_cache is not None and self._resolved_cache[0] == key:
            return self._resolved_cache[1]
        entries = dtlogic.load_data_entries(
            self.game, self.filemap_path, self.profile_dir)
        self._resolved_cache = (key, entries)
        return entries

    def _repopulate(self):
        """Resolve the filemap + conflict cache off the UI thread (the first
        build on a large modlist is CPU-heavy), then build the tree back on the
        UI thread in _on_data_ready. A generation counter drops stale results."""
        self._scan_gen += 1
        gen = self._scan_gen
        self._scanning = True
        self.scan_status_changed.emit(True)
        index_path = self.index_path
        profile_dir = self.profile_dir

        def worker():
            try:
                entries = self._resolved_entries()
                contested, _winner = mflogic.build_conflict_cache(
                    index_path, profile_dir)
            except Exception:
                import traceback
                from Utils.app_log import app_log
                # Previously a bare except with NO diagnostic output at all —
                # a real failure here surfaced only as an empty "0 files in 0
                # mods" Data tab, with nothing in any log to explain why.
                app_log(f"[data-tab] build gen={gen} FAILED:\n{traceback.format_exc()}")
                safe_emit(self._data_ready, gen, [], set())
                return
            safe_emit(self._data_ready, gen, entries, contested)

        threading.Thread(target=worker, daemon=True,
                         name="data-tab-build").start()

    def _on_data_ready(self, gen: int, entries: list, contested: set):
        if gen != self._scan_gen:
            return
        self._scanning = False
        self.scan_status_changed.emit(False)
        # Preserve expand state by path across the model reset.
        expanded = self._expanded_paths()
        # Ext counts (pre-filter) for the filter panel.
        self._ext_counts = dtlogic.filetype_counts(entries)
        self.filetypes_changed.emit()
        self._update_label(entries)

        q = self._search
        exts = self._search_exts
        keep = None
        if q or exts:
            def keep(rk, mod):
                if exts and Path(rk).suffix.lower() not in exts:
                    return False
                if q and not (q in rk or q in mod.casefold()):
                    return False
                return True
        tree_dict = dtlogic.build_data_tree(
            entries, contested,
            only_conflicts=self._only_conflicts,
            inc_exts=frozenset(self._inc_exts) or None,
            exc_exts=frozenset(self._exc_exts) or None,
            keep_extra=keep)

        root = _DataNode("", "", is_dir=True)

        def add(parent: _DataNode, subtree: dict, parent_path: str):
            for folder in sorted(k for k in subtree if k != "__files__"):
                fpath = f"{parent_path}/{folder}" if parent_path else folder
                fn = _DataNode(folder, fpath, is_dir=True, parent=parent)
                parent.children.append(fn)
                add(fn, subtree[folder], fpath)
            for fname, mod, rel_key_lower in sorted(subtree.get("__files__", [])):
                fpath = f"{parent_path}/{fname}" if parent_path else fname
                conflict = 1 if rel_key_lower in contested else 0
                parent.children.append(_DataNode(
                    fname, fpath, is_dir=False, parent=parent,
                    mod=mod, conflict=conflict))

        add(root, tree_dict, "")
        self._model.set_root(root)
        if q or exts:
            self._tree.expandAll()
        else:
            self._restore_expanded(expanded)

    def _expanded_paths(self) -> set[str]:
        out: set[str] = set()
        m = self._model

        def walk(parent_index):
            for r in range(m.rowCount(parent_index)):
                idx = m.index(r, 0, parent_index)
                node = m.node(idx)
                if node and node.is_dir and self._tree.isExpanded(idx) and node.path:
                    out.add(node.path.lower())
                walk(idx)
        walk(QModelIndex())
        return out

    def _restore_expanded(self, paths: set[str]):
        if not paths:
            return
        m = self._model

        def walk(parent_index):
            for r in range(m.rowCount(parent_index)):
                idx = m.index(r, 0, parent_index)
                node = m.node(idx)
                if node and node.is_dir and node.path and node.path.lower() in paths:
                    self._tree.expand(idx)
                walk(idx)
        walk(QModelIndex())

    def _update_label(self, entries):
        n_files = len(entries)
        n_mods = len({mod for _rk, mod in entries})
        self._label.setText(self.tr("Deployed files - {0} files in {1} mods").format(
            n_files, n_mods))

    # -- search -------------------------------------------------------------
    def _on_search(self, text: str):
        from Utils.file_search import parse_file_query
        needle, self._search_exts = parse_file_query(text)
        self._search = needle
        t = getattr(self, "_search_timer", None)
        if t is None:
            t = QTimer(self)
            t.setSingleShot(True)
            t.setInterval(150)
            t.timeout.connect(self._repopulate)
            self._search_timer = t
        t.start()

    # -- expand -------------------------------------------------------------
    def _toggle_expand_all(self) -> bool:
        first = self._model.index(0, 0) if self._model.rowCount() else None
        expanded = bool(first is not None and self._tree.isExpanded(first))
        if expanded:
            self._tree.collapseAll()
            return False
        self._tree.expandAll()
        return True

    # -- mod highlight (modlist selection cross-tint) -----------------------
    def set_highlight_mod(self, mod: str | None):
        self._model.set_highlight_mod(mod)

    # -- clicks / selection -------------------------------------------------
    def _on_clicked(self, index):
        node = self._model.node(index)
        if node is None or node is self._model._root:
            return
        # Folder name click toggles expand. File selection (handled by
        # _on_selection_changed) highlights the winning mod in the modlist —
        # Tk parity; no image preview here.
        if index.column() == COL_NAME and node.is_dir \
                and self._model.rowCount(index) > 0:
            self._tree.setExpanded(index, not self._tree.isExpanded(index))

    def _on_selection_changed(self):
        """Selecting a FILE row highlights its winning mod in the modlist; a
        folder row clears the highlight (Tk `_on_data_file_selected`)."""
        cb = self.on_select_mod
        if cb is None:
            return
        rows = self._tree.selectionModel().selectedRows()
        if not rows:
            cb(None)
            return
        node = self._model.node(rows[0])
        cb(node.mod if (node and not node.is_dir and node.mod) else None)

    def _on_context_menu(self, pos):
        pass  # Open-in-browser — wired in a later step
