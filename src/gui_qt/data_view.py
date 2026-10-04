"""Qt Data tab - the merged deployment tree (Path + Winning Mod), conflict
highlighting, filtering, search, and contextual file actions.

Mirrors gui_qt.mod_files_view's structure/visuals (lean: header label + QTreeView,
with the shared footer + filter panel owned by the app) and reads a pinned
Filegraph deployment plan instead of scanning individual mods. Built lazily: the
tree is only (re)built when the Data sub-tab is visible (mark_dirty defers it).
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QModelIndex, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTreeView, QLabel, QAbstractItemView,
)

import Utils.ui.data as dtlogic
from gui_qt.audio_preview import AUDIO_EXTS, AudioControls
from gui_qt.data_model import (
    BULK_DELTA_THRESHOLD, DataModel, _DataNode, COL_NAME, COL_MOD,
)
from gui_qt.safe_emit import safe_emit
from gui_qt.video_preview import VIDEO_EXTS


class DataView(QWidget):
    """The Data tab. configure() once, then refresh()/mark_dirty() to (re)build."""

    filetypes_changed = Signal()
    scan_status_changed = Signal(bool)         # True = build running
    _data_ready = Signal(int, object, object)  # gen, projection result, contested

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scan_gen = 0                  # bumped per build → drops stale
        self._scanning = False
        self.game = None
        self.profile_dir: Path | None = None
        self.snapshot = None
        self.on_select_mod = None          # callback(mod_name | None) - highlight
        self._dirty = True
        self._is_visible = False           # is the Data sub-tab currently shown
        self._search = ""
        self._search_exts: frozenset[str] = frozenset()
        self._inc_exts: set[str] = set()
        self._exc_exts: set[str] = set()
        self._only_conflicts = False
        self._ext_counts: dict[str, int] = {}
        # Resolved-entries cache (keyed on snapshot generation + game) so
        # filter/search changes do not rebuild the deployment projection.
        self._resolved_cache: tuple | None = None
        self._resolved_contested: set[int] = set()
        self._mod_counts: dict[str, int] = {}
        self._scan_started: dict[int, float] = {}
        self._projection = dtlogic.DestinationProjection()
        self._model_projection = self._projection
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
    def configure(self, game, profile_dir, snapshot=None):
        self._cancel_scan()
        if game is not self.game or profile_dir != self.profile_dir:
            self._audio_controls.clear_audio()
            self._model.clear()
            self._ext_counts.clear()
        self.game = game
        self.profile_dir = profile_dir
        self.snapshot = snapshot
        self._refresh_projection_context()
        self._resolved_cache = None
        self._resolved_contested.clear()
        self._mod_counts.clear()
        self._dirty = True
        self._label.setText(self._data_title())

    def _refresh_projection_context(self) -> bool:
        projection = dtlogic.DestinationProjection.from_game(self.game)
        if projection == self._projection:
            return False
        self._projection = projection
        self._resolved_cache = None
        return True

    def set_snapshot(self, snapshot):
        self.snapshot = snapshot
        self._refresh_projection_context()
        self._resolved_cache = None
        self.mark_dirty()

    def _project_entry(self, entry):
        return self._projection.project(
            entry.candidate_id, entry.mod_name, entry.target,
            entry.destination_display)

    def _adjust_cached_row(self, row, add: bool) -> None:
        _candidate_id, path, mod = row
        amount = 1 if add else -1
        extension = dtlogic.file_extension(path)
        next_ext = self._ext_counts.get(extension, 0) + amount
        if next_ext > 0:
            self._ext_counts[extension] = next_ext
        else:
            self._ext_counts.pop(extension, None)
        next_mod = self._mod_counts.get(mod, 0) + amount
        if next_mod > 0:
            self._mod_counts[mod] = next_mod
        else:
            self._mod_counts.pop(mod, None)

    def apply_resolution_delta(self, snapshot, delta) -> None:
        """Publish a native winner delta without rebuilding the whole tree."""
        if self._refresh_projection_context():
            self.set_snapshot(snapshot)
            return
        # A game-specific hidden-entry predicate may depend on a different
        # winner (Stardew's overwrite config is visible only while some mod
        # wins the sibling manifest.json).  That cross-path dependency is not
        # represented by a winner-id delta, so refresh its small projection as
        # one pinned generation instead of retaining a now-orphaned row.
        dependent_visibility = callable(
            getattr(self.game, "_orphaned_overwrite_configs", None))
        dependent_presentation = callable(
            getattr(self.game, "data_tab_display_paths", None))
        if (delta is None or delta.full_rebuild or dependent_visibility
                or dependent_presentation
                or not self._is_visible
                or self._scanning or self._resolved_cache is None
                or self._resolved_cache[0][0] != delta.base_generation):
            self.set_snapshot(snapshot)
            return

        touched = set(delta.touched_winner_ids)
        removed = set(delta.removed_winner_ids)
        _key, by_id = self._resolved_cache
        by_id = dict(by_id)
        for candidate_id in removed | touched:
            old = by_id.pop(candidate_id, None)
            if old is not None:
                self._adjust_cached_row(old, False)

        projected = []
        for entry in snapshot.deployment_entries(touched):
            row = self._project_entry(entry)
            if row is None:
                continue
            by_id[row[0]] = row
            projected.append(row)
            self._adjust_cached_row(row, True)

        self.snapshot = snapshot
        self._resolved_cache = (
            (snapshot.generation, id(self.game), self.profile_dir, self._projection.key),
            by_id)
        self._model_projection = self._projection
        self._resolved_contested.difference_update(removed | touched)
        self._resolved_contested.update(
            snapshot.contested_winner_ids(touched))
        self._dirty = False

        filtered = bool(
            self._search or self._search_exts or self._inc_exts
            or self._exc_exts or self._only_conflicts
        )
        if filtered:
            self._repopulate()
            return

        changed_rows = [
            (candidate_id, path, mod,
             candidate_id in self._resolved_contested)
            for candidate_id, path, mod in projected
        ]
        impacted = ({row[0] for row in changed_rows}
                    | ((removed | touched) & self._model._candidate_nodes.keys()))
        if len(impacted) >= BULK_DELTA_THRESHOLD:
            self._replace_model_rows([
                (candidate_id, path, mod,
                 candidate_id in self._resolved_contested)
                for candidate_id, path, mod in by_id.values()
            ])
        else:
            self._model.apply_leaf_delta(removed | touched, changed_rows)
        self.filetypes_changed.emit()
        self._update_label_counts(len(by_id), len(self._mod_counts))

    def _replace_model_rows(self, rows) -> None:
        expanded = self._expanded_paths()
        current = self._model.node(self._tree.currentIndex())
        identity = ((current.candidate_id, current.parts)
                    if current is not None else (0, ()))
        scroll = self._tree.verticalScrollBar().value()
        self._model.replace_rows(rows)
        self._restore_expanded(expanded)
        current = self._model.node_for_identity(*identity)
        if current is not None:
            self._tree.setCurrentIndex(self._model.index_for_node(current))
        self._tree.verticalScrollBar().setValue(scroll)

    def set_visible_tab(self, visible: bool):
        """Tell the view whether the Data sub-tab is showing. Switching TO it
        triggers a deferred rebuild if dirty."""
        self._is_visible = visible
        if visible and self._dirty:
            self.refresh()

    def mark_dirty(self):
        """Deploy state changed. Rebuild now if visible, else defer."""
        self._cancel_scan()
        self._dirty = True
        self._resolved_cache = None
        if self._is_visible:
            self.refresh()

    def _cancel_scan(self):
        self._scan_gen += 1
        self._scan_started.clear()
        if self._scanning:
            self._scanning = False
            self.scan_status_changed.emit(False)

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
        self._label = QLabel(self.tr("Mod destinations"))
        self._label.setObjectName("HeaderCaption")
        tbl.addWidget(self._label, 1)
        v.addWidget(tb)

        self._model = DataModel(self)
        self._model.tooltip_for_node = self._destination_tooltip
        self._tree = QTreeView()
        self._tree.setModel(self._model)
        self._model.themeChanged.connect(self._tree.viewport().update)
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

        self._audio_controls = AudioControls(self)
        v.addWidget(self._audio_controls)

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
        self._inc_exts = {
            "" if ext == "(none)" else ext for ext in state.get("filetypes") or ()}
        self._exc_exts = {
            "" if ext == "(none)" else ext
            for ext in state.get("filetypes_exclude") or ()}
        self._repopulate()

    def filetype_items(self) -> list[tuple]:
        items = sorted(self._ext_counts.items(), key=lambda kv: kv[0])
        return [(ext or "(none)", ext or "(no ext)", n) for ext, n in items]

    # -- population ---------------------------------------------------------
    def _resolved_entries(self, snapshot, game, projection, profile_dir):
        if game is None or snapshot is None or profile_dir is None:
            return [], set()
        key = (snapshot.generation, id(game), profile_dir, projection.key)
        cache = self._resolved_cache
        if cache is not None and cache[0] == key:
            return list(cache[1].values()), set(self._resolved_contested)
        records = snapshot.data_entries()
        hide_fn = getattr(game, "_orphaned_overwrite_configs", None)
        if callable(hide_fn):
            try:
                hidden = {
                    str(path).replace("\\", "/").lower()
                    for path in hide_fn(snapshot=snapshot)
                }
            except Exception:
                hidden = set()
            if hidden:
                overwrite_ids = [row[0] for row in records if row[1] == "[Overwrite]"]
                excluded = {
                    entry.candidate_id
                    for entry in snapshot.deployment_entries(overwrite_ids)
                    if entry.legacy_rel.replace("\\", "/").lower() in hidden
                }
                records = [row for row in records if row[0] not in excluded]
        rows = dtlogic.project_entries(game, projection, records)
        contested = {row[0] for row in records if row[4]}
        return rows, contested

    def _repopulate(self):
        self._refresh_projection_context()
        self._scan_gen += 1
        gen = self._scan_gen
        self._scan_started[gen] = time.perf_counter()
        self._scanning = True
        self.scan_status_changed.emit(True)
        snapshot = self.snapshot
        game = self.game
        projection = self._projection
        profile_dir = self.profile_dir
        key = (snapshot.generation if snapshot is not None else 0,
               id(game), profile_dir, projection.key)

        def worker():
            projection_started = time.perf_counter()
            try:
                resolved, contested = self._resolved_entries(
                    snapshot, game, projection, profile_dir)
            except Exception:
                safe_emit(self._data_ready, gen, (key, projection, None), set())
                return
            from Utils.diagnostics import performance as perftrace
            if perftrace.is_enabled():
                from Utils.app_log import safe_print
                safe_print(
                    f"[DATA-TIMING] gen={gen} native projection + routing "
                    f"{time.perf_counter() - projection_started:.3f}s "
                    f"({len(resolved)} rows)",
                    flush=True,
                )
            safe_emit(self._data_ready, gen, (key, projection, resolved), contested)

        threading.Thread(target=worker, daemon=True,
                         name="data-tab-build").start()

    def _on_data_ready(self, gen: int, result: tuple, contested: set):
        if gen != self._scan_gen:
            self._scan_started.pop(gen, None)
            return
        key, projection, entries = result
        self._resolved_cache = (
            (key, {row[0]: row for row in entries}) if entries is not None else None)
        entries = entries or []
        self._resolved_contested = contested
        self._model_projection = projection
        ui_started = time.perf_counter()
        self._scanning = False
        self.scan_status_changed.emit(False)
        # Preserve expand state by path across the model reset.
        expanded = self._expanded_paths()
        # Ext counts (pre-filter) for the filter panel.
        current = self._model.node(self._tree.currentIndex())
        identity = ((current.candidate_id, current.parts)
                    if current is not None else (0, ()))
        scroll = self._tree.verticalScrollBar().value()
        self._ext_counts = dtlogic.filetype_counts(entries)
        self._mod_counts = {}
        for _candidate_id, _path, mod in entries:
            self._mod_counts[mod] = self._mod_counts.get(mod, 0) + 1
        self.filetypes_changed.emit()
        self._update_label_counts(len(entries), len(self._mod_counts))
        counts_done = time.perf_counter()

        q = self._search.casefold()
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
        tree_done = time.perf_counter()

        root = _DataNode("", "", is_dir=True)

        def add(parent: _DataNode, subtree: dict, parent_path: dtlogic.DataPath):
            for folder in sorted((k for k in subtree if k != "__files__"),
                                 key=lambda name: dtlogic.name_sort_key(
                                     name, at_root=not parent_path)):
                fpath = (*parent_path, folder)
                fn = _DataNode(folder, fpath, is_dir=True, parent=parent)
                parent.children.append(fn)
                add(fn, subtree[folder], fpath)
            for fname, mod, candidate_id in sorted(
                    subtree.get("__files__", []),
                    key=lambda row: (row[0].casefold(), row[0])):
                fpath = (*parent_path, fname)
                parent.children.append(_DataNode(
                    fname, fpath, is_dir=False, parent=parent,
                    mod=mod, conflict=int(candidate_id in contested),
                    candidate_id=candidate_id))

        add(root, tree_dict, ())
        self._model.set_root(root)
        if q or exts:
            self._tree.expandAll()
        else:
            self._restore_expanded(expanded)
        current = self._model.node_for_identity(*identity)
        if current is not None:
            self._tree.setCurrentIndex(self._model.index_for_node(current))
        if not (q or exts):
            self._tree.verticalScrollBar().setValue(scroll)
        finished = time.perf_counter()
        started = self._scan_started.pop(gen, ui_started)
        from Utils.diagnostics import performance as perftrace
        if perftrace.is_enabled():
            from Utils.app_log import safe_print
            safe_print(
                f"[DATA-TIMING] gen={gen} counts "
                f"{counts_done - ui_started:.3f}s, logical tree "
                f"{tree_done - counts_done:.3f}s, Qt model "
                f"{finished - tree_done:.3f}s, total {finished - started:.3f}s",
                flush=True,
            )

    def _expanded_paths(self) -> set[dtlogic.DataPath]:
        out: set[dtlogic.DataPath] = set()
        m = self._model

        def walk(parent_index):
            for r in range(m.rowCount(parent_index)):
                idx = m.index(r, 0, parent_index)
                node = m.node(idx)
                if node and node.is_dir and self._tree.isExpanded(idx) and node.parts:
                    out.add(node.parts)
                walk(idx)
        walk(QModelIndex())
        return out

    def _restore_expanded(self, paths: set[dtlogic.DataPath]):
        if not paths:
            return
        m = self._model

        def walk(parent_index):
            for r in range(m.rowCount(parent_index)):
                idx = m.index(r, 0, parent_index)
                node = m.node(idx)
                if node and node.is_dir and node.parts in paths:
                    self._tree.expand(idx)
                walk(idx)
        walk(QModelIndex())

    def _update_label(self, entries):
        self._update_label_counts(
            len(entries), len({mod for _candidate_id, _path, mod in entries}))

    def _update_label_counts(self, n_files: int, n_mods: int):
        self._label.setText(
            self.tr("{0} - {1} files in {2} mods").format(
                self._data_title(), n_files, n_mods))

    def _data_title(self) -> str:
        return self.tr("Mod destinations")

    def _destination_tooltip(self, node: _DataNode) -> str:
        parts = node.parts
        destination = self._model_projection.absolute_path(parts)
        if destination is not None:
            return str(destination)
        if not parts:
            return ""
        if parts[0] == "<prefix>":
            return self.tr("Prefix is not configured; absolute destination unavailable.")
        if parts[0] == "<root>":
            return self.tr("Game folder is not configured; absolute destination unavailable.")
        if parts[0] == "<external>":
            return self.tr("Destinations outside the game folder and prefix")
        if not node.is_dir:
            source = self._source_path_for(node)
            if source is not None:
                return str(source)
        staging = self._model_projection.staging_root
        return (self.tr("Loaded from staging") + "\n" + str(staging)
                if staging is not None else node.path)

    # -- search -------------------------------------------------------------
    def _on_search(self, text: str):
        from Utils.text.search import parse_file_query
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
        # _on_selection_changed) highlights the winning mod in the modlist -
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

    def _source_path_for(self, node: _DataNode) -> Path | None:
        if self.game is None or self.snapshot is None or not node.candidate_id:
            return None
        try:
            entries = self.snapshot.deployment_entries((node.candidate_id,))
            entry = next(
                (item for item in entries
                 if item.candidate_id == node.candidate_id), None)
            if entry is None:
                return None
            from Utils.filegraph.service import source_path
            return source_path(self.game, entry.mod_name, entry.source_rel)
        except Exception:
            return None

    @staticmethod
    def _menu_action(menu, label, slot, enabled=True):
        action = menu.addAction(label)
        action.triggered.connect(lambda _checked=False, fn=slot: fn())
        action.setEnabled(enabled)
        return action

    def _on_context_menu(self, pos):
        index = self._tree.indexAt(pos)
        if not index.isValid():
            return
        node = self._model.node(index)
        if node is None or node.is_dir:
            return
        self._tree.setCurrentIndex(index.siblingAtColumn(COL_NAME))
        menu = self._build_context_menu(node)
        if menu is not None:
            menu.exec(self._tree.viewport().mapToGlobal(pos))

    def _build_context_menu(self, node: _DataNode):
        from PySide6.QtWidgets import QMenu
        target = self._source_path_for(node)
        if target is None:
            return None
        menu = QMenu(self._tree)
        open_browser = getattr(self, "on_open_file_browser", None)
        if open_browser is not None:
            self._menu_action(
                menu, self.tr("Open in File Browser"),
                lambda: open_browser(target.parent), target.parent.is_dir())

        ext = Path(node.path).suffix.lower()
        available = target.is_file()
        from Utils.text.files import TEXT_EXTENSIONS
        if ext in TEXT_EXTENSIONS:
            cb = getattr(self, "on_open_text", None)
            if cb is not None:
                self._menu_action(
                    menu, self.tr("Open in Text Editor"),
                    lambda: cb(target, node.path), available)
        else:
            from gui_qt.nif_preview import PREVIEW_EXTS as NIF_EXTS
            if ext in NIF_EXTS:
                cb = getattr(self, "on_open_nif", None)
                if cb is not None:
                    self._menu_action(
                        menu, self.tr("Open in NIF Viewer"),
                        lambda: cb(target, node.path), available)
            elif ext in AUDIO_EXTS:
                self._menu_action(
                    menu, self.tr("Play Audio"),
                    lambda: self._audio_controls.set_audio(target, node.path),
                    available)
            elif ext in VIDEO_EXTS:
                cb = getattr(self, "on_open_video", None)
                if cb is not None:
                    self._menu_action(
                        menu, self.tr("Play Video"),
                        lambda: cb(target, node.path), available)
            else:
                from gui_qt.bsa_preview import ARCHIVE_EXTS
                if ext in ARCHIVE_EXTS:
                    cb = getattr(self, "on_open_archive", None)
                    if cb is not None:
                        self._menu_action(
                            menu, self.tr("Inspect Archive"),
                            lambda: cb(target, node.path), available)

        return menu if menu.actions() else None
