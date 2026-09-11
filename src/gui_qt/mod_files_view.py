"""Qt Mod Files tab - per-mod file tree with Top Level + Root + Enabled columns.

Reuses Utils.mods.files for every bit of logic (file listing, conflict cache, the
strip-prefix promotion/demotion algorithm, the exclusion save-merge) so it stays
in lockstep with the Tk tab. This module is the Qt presentation: a QTreeView +
ModFilesModel, a toolbar (Expand all / Filters), a search box, and a footer
(Pack / Unpack BSA).
"""

from __future__ import annotations

from pathlib import Path
import shutil
import threading

from PySide6.QtCore import Qt, QTimer, Signal, Slot
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTreeView, QLabel, QAbstractItemView,
    QMenu,
)

import Utils.mods.files as mflogic
from gui_qt.mod_files_model import (
    ModFilesModel, _Node, COL_NAME, COL_TOPLEVEL, COL_ROOT, COL_DISABLE,
)
from gui_qt.audio_preview import AUDIO_EXTS, AudioControls
from gui_qt.video_preview import VIDEO_EXTS
from gui_qt.safe_emit import safe_emit


def _build_file_tree(files, folders, conflicts, stripped, excluded, root_tags,
                     filters):
    search, search_exts, inc_exts, exc_exts, want_win, want_lose = filters

    def keep(rel_key, rel_str):
        if search_exts or inc_exts or exc_exts:
            ext = Path(rel_key).suffix.lower()
            if ext in exc_exts or (inc_exts and ext not in inc_exts):
                return False
            if search_exts and ext not in search_exts:
                return False
        if want_win or want_lose:
            conflict = conflicts.get(rel_key.lower(), 0)
            if not ((want_win and conflict == 1) or (want_lose and conflict == -1)):
                return False
        return not search or search in rel_str.lower()

    tree_dict = mflogic.build_tree(files, keep_rel_key=keep)
    if not (search_exts or inc_exts or exc_exts or want_win or want_lose):
        for rel_str in folders.values():
            if search and search not in rel_str.lower():
                continue
            subtree = tree_dict
            for part in rel_str.replace("\\", "/").split("/"):
                subtree = subtree.setdefault(part, {})
    root = _Node("", "", is_dir=True)
    by_path = {}

    def add_nodes(parent, subtree, parent_path):
        for folder in sorted(k for k in subtree if k != "__files__"):
            path = f"{parent_path}/{folder}" if parent_path else folder
            node = _Node(folder, path, is_dir=True, parent=parent)
            parent.children.append(node)
            by_path[path.lower()] = node
            add_nodes(node, subtree[folder], path)
        for name, rel_key, rel_str in sorted(subtree.get("__files__", [])):
            path = f"{parent_path}/{name}" if parent_path else name
            node = _Node(name, path, is_dir=False, parent=parent,
                         rel_str=rel_str, raw_key=rel_key)
            node.checked = rel_key not in excluded
            node.root_tag = rel_key in root_tags
            node.conflict = conflicts.get(rel_key.lower(), 0)
            parent.children.append(node)
            by_path[path.lower()] = node

    add_nodes(root, tree_dict, "")
    for node in by_path.values():
        node.stripped = node.path.lower() in stripped
        node.top_level = (not node.stripped
                          and mflogic.is_top_level(node.path, stripped))
    return root, by_path


class ModFilesView(QWidget):
    """Self-contained Mod Files tab widget. Call show_mod(mod_name) to populate.
    Emits changed() after state or disk edits so the host can rebuild the
    filemap."""

    changed = Signal()
    filetypes_changed = Signal()   # the ext-count list changed (refresh panel)
    mod_changed = Signal(object)   # the shown mod name (or None) changed
    _files_ready = Signal(int, object, object)
    _tree_ready = Signal(int, object, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        # Host-provided context (set via configure()).
        self.game = None
        self.profile_dir: Path | None = None
        self._snapshot = None
        self._mod_name: str | None = None
        self._stripped: set[str] = set()      # lower strip entries for this mod
        self._search = ""
        self._search_exts: frozenset[str] = frozenset()
        self._inc_exts: set[str] = set()
        self._exc_exts: set[str] = set()
        self._ext_counts: dict[str, int] = {}
        self._needs_repopulate = False
        self._context_generation = 0
        self._build_generation = 0
        self._worker_running = False
        self._files_cache = None
        self._folders_cache = None
        self._pending_expanded: set[str] = set()
        self._build()
        self._files_ready.connect(self._on_files_ready)
        self._tree_ready.connect(self._on_tree_ready)
        self._repop_timer = QTimer(self)
        self._repop_timer.setSingleShot(True)
        self._repop_timer.timeout.connect(self._start_repopulate)

    # -- context ------------------------------------------------------------
    def configure(self, game, profile_dir):
        if game is not self.game or profile_dir != self.profile_dir:
            self._audio_controls.clear_audio()
            self._folders_cache = None
            self.show_mod(None)
        self.game = game
        self.profile_dir = profile_dir
        self._snapshot = None
        self._invalidate_files()
        # The Root column is meaningless when the normal deploy already targets
        # the game root (no Data subfolder) - hide it for those games.
        hide_root = True
        if game is not None:
            try:
                from Utils.games.registry import game_data_subpath
                hide_root = not game_data_subpath(game)
            except Exception:
                hide_root = True
        self._tree.setColumnHidden(COL_ROOT, hide_root)

    def set_snapshot(self, snapshot):
        if snapshot is self._snapshot:
            return
        self._snapshot = snapshot
        self._invalidate_files()
        if self._mod_name is not None:
            self._request_repopulate()

    def _invalidate_files(self):
        self._context_generation += 1
        self._build_generation += 1
        self._files_cache = None

    # -- construction -------------------------------------------------------
    def _build(self):
        # The tool footer
        # (Pack/Unpack + search + Filters/Expand), and the filter side panel,
        # are owned by the app so they sit in the shared column footer and the
        # window-left filter slot (matching the modlist).
        self._filter_state: dict = {}
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        tb = QWidget()
        tb.setObjectName("HeaderBar")
        tbl = QHBoxLayout(tb)
        tbl.setContentsMargins(8, 4, 8, 4)
        self._label = QLabel(self.tr("(no mod selected)"))
        self._label.setObjectName("HeaderCaption")
        tbl.addWidget(self._label, 1)
        v.addWidget(tb)

        self._model = ModFilesModel(self)
        self._tree = QTreeView()
        self._tree.setModel(self._model)
        self._model.themeChanged.connect(self._tree.viewport().update)
        # We draw our own arrow + indent in the delegate, so kill the native
        # branch decoration (root decoration off; indentation handled by us).
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
        from gui_qt.mod_files_delegate import ModFilesDelegate
        self._tree.setItemDelegate(ModFilesDelegate(self._tree))

        # Tk-style column resize (boundary drag, constant total) - same as the
        # modlist/plugins panels.
        from gui_qt.modlist_header import TkStyleHeader
        col_mins = {
            COL_NAME: 120,
            COL_TOPLEVEL: self._header_min(COL_TOPLEVEL, 60),
            COL_ROOT: self._header_min(COL_ROOT, 50),
            COL_DISABLE: self._header_min(COL_DISABLE, 55),
        }
        col_defaults = {COL_TOPLEVEL: max(70, col_mins[COL_TOPLEVEL]),
                        COL_ROOT: max(60, col_mins[COL_ROOT]),
                        COL_DISABLE: max(60, col_mins[COL_DISABLE])}
        hdr = TkStyleHeader(self._tree, col_mins, col_defaults)
        self._tree.setHeader(hdr)
        hdr.setMinimumSectionSize(min(col_mins.values()))
        hdr.setDefaultAlignment(Qt.AlignCenter)
        for col, wdt in col_defaults.items():
            self._tree.setColumnWidth(col, wdt)
        # Repaint the arrow column when a folder expands/collapses.
        self._tree.expanded.connect(lambda *_: self._tree.viewport().update())
        self._tree.collapsed.connect(lambda *_: self._tree.viewport().update())
        self._name_min = col_mins[COL_NAME]
        # Name column absorbs leftover width on resize (modlist parity).
        self._tree.viewport().installEventFilter(self)
        v.addWidget(self._tree, 1)

        self._audio_controls = AudioControls(self)
        v.addWidget(self._audio_controls)

    def _header_min(self, col: int, floor: int) -> int:
        """Width that fits this column's header caption in full.

        Qt paints header labels inset by the style's header margin on each
        side; we add that plus a couple of px of slack so the text sits at the
        minimum width without eliding.
        """
        from PySide6.QtWidgets import QStyle
        text = ModFilesModel.column_title(col)
        pad = self.style().pixelMetric(QStyle.PM_HeaderMargin, None, self) * 2 + 6
        return max(floor, self.fontMetrics().horizontalAdvance(text) + pad)

    def eventFilter(self, obj, event):
        from PySide6.QtCore import QEvent
        if obj is self._tree.viewport() and event.type() == QEvent.Resize:
            self._fit_name_to_width()
        return super().eventFilter(obj, event)

    def _fit_name_to_width(self):
        vp = self._tree.viewport().width()
        if vp <= 0:
            return
        others = (self._tree.columnWidth(COL_TOPLEVEL)
                  + self._tree.columnWidth(COL_ROOT)
                  + self._tree.columnWidth(COL_DISABLE))
        target = vp - others
        if target >= self._name_min and target != self._tree.columnWidth(COL_NAME):
            self._tree.header().resizeSection(COL_NAME, target)

    @staticmethod
    def filter_spec() -> list[dict]:
        """Spec for the Mod Files filter side panel (the app builds the panel in
        the window-left slot and feeds state back via apply_filter_state)."""
        return [
            {"title": "By conflict", "type": "checks", "items": [
                ("mf_win", "Winning conflicts", True),
                ("mf_lose", "Losing conflicts", True),
            ]},
            {"title": "By file type", "type": "dynamic", "id": "filetypes"},
        ]

    def apply_filter_state(self, state: dict):
        """Apply filter state from the external panel and repopulate."""
        self._filter_state = state
        self._inc_exts = set(state.get("filetypes") or ())
        self._exc_exts = set(state.get("filetypes_exclude") or ())
        self._repopulate()

    def filetype_items(self) -> list[tuple]:
        """Current (ext, label, count) list for the filter panel's dynamic list."""
        items = sorted(self._ext_counts.items(), key=lambda kv: kv[0])
        return [(ext or "(none)", ext or "(no ext)", n) for ext, n in items]

    # -- population ---------------------------------------------------------
    def show_mod(self, mod_name: str | None):
        if mod_name != self._mod_name:
            self._pending_expanded = (self._pending_expanded or self._expanded_paths()) \
                if mod_name else set()
            self._audio_controls.clear_audio()
            self._folders_cache = None
            self._model.clear()
        self._mod_name = mod_name
        self._invalidate_files()
        self.mod_changed.emit(mod_name)
        if mod_name is None:
            self._label.setText(self.tr("(no mod selected)"))
            self._model.clear()
            self._tree.setEnabled(True)
            self._ext_counts = {}
            self._needs_repopulate = False
            self._repop_timer.stop()
            self.filetypes_changed.emit()
            return
        self._stripped = mflogic.read_strip_prefixes(self.profile_dir, mod_name)
        self._request_repopulate()

    def _request_repopulate(self, delay=0):
        if self._mod_name is None:
            return
        self._build_generation += 1
        self._needs_repopulate = True
        self._tree.setEnabled(False)
        self._label.setText(self.tr("{0} — Loading files…").format(self._mod_name))
        if self.isVisible():
            self._repop_timer.start(delay)
        else:
            self._repop_timer.stop()

    def showEvent(self, event):
        super().showEvent(event)
        if self._needs_repopulate:
            self._request_repopulate()

    def _repopulate(self):
        self._request_repopulate()

    def _start_repopulate(self):
        if (self._worker_running or not self._needs_repopulate
                or not self.isVisible() or self._mod_name is None
                or self._repop_timer.isActive()):
            return
        if self._files_cache is not None:
            self._start_tree_build()
            return
        context = self._context_generation
        snapshot = self._snapshot
        mod_name = self._mod_name
        mod_dir = self._mod_root_dir()
        excluded_dirs = tuple(getattr(self.game, "filemap_exclude_dirs", ()) or ())
        folders_cache = self._folders_cache
        ready = self._files_ready
        self._worker_running = True

        def worker():
            try:
                if snapshot is not None:
                    records = snapshot.iter_mod_files(mod_name)
                    files = {}
                    conflicts = {}
                    for record in records:
                        key = record.source_rel.decode("utf-8", "surrogateescape").lower()
                        files[key] = record.source
                        conflicts[key] = record.conflict_status
                    folders = (folders_cache if folders_cache is not None
                               else mflogic.scan_mod_dirs(
                                   mod_dir, excluded_dirs))
                else:
                    files, folders = mflogic.scan_mod_tree(
                        mod_dir, excluded_dirs)
                    conflicts = {}
                counts = {}
                for key in files:
                    ext = Path(key).suffix.lower()
                    counts[ext] = counts.get(ext, 0) + 1
                safe_emit(ready, context, (files, folders, conflicts, counts), None)
            except Exception as exc:
                safe_emit(ready, context, None, str(exc))

        threading.Thread(target=worker, daemon=True, name="mod-files-query").start()

    @Slot(int, object, object)
    def _on_files_ready(self, context, result, error):
        self._worker_running = False
        if context == self._context_generation:
            if error is not None:
                self._load_failed(error)
            else:
                self._files_cache = result
                self._folders_cache = result[1]
        self._start_repopulate()

    def _start_tree_build(self):
        generation = self._build_generation
        files, folders, conflicts, counts = self._files_cache
        self._needs_repopulate = False
        self._ext_counts = counts
        self.filetypes_changed.emit()
        if generation != self._build_generation:
            return
        try:
            self._prune_orphan_strips(files)
            mflogic.migrate_root_tags_to_raw(
                self.profile_dir, self._mod_name, files, self._stripped)
            mflogic.migrate_exclusions_to_raw(
                self.profile_dir, self._mod_name, files, self._stripped)
            mflogic.prune_orphan_root_tags(self.profile_dir, self._mod_name, set(files))
            excluded = mflogic.read_exclusions(self.profile_dir, self._mod_name)
            root_tags = mflogic.read_root_tags(self.profile_dir, self._mod_name)
        except Exception as exc:
            self._load_failed(str(exc))
            return
        stripped = set(self._stripped)
        filters = (self._search, self._search_exts, set(self._inc_exts),
                   set(self._exc_exts), self._filter_state.get("mf_win") == 1,
                   self._filter_state.get("mf_lose") == 1)
        ready = self._tree_ready
        self._worker_running = True

        def worker():
            try:
                result = _build_file_tree(
                    files, folders, conflicts, stripped, excluded, root_tags,
                    filters)
                safe_emit(ready, generation, result, None)
            except Exception as exc:
                safe_emit(ready, generation, None, str(exc))

        threading.Thread(target=worker, daemon=True, name="mod-files-tree").start()

    @Slot(int, object, object)
    def _on_tree_ready(self, generation, result, error):
        self._worker_running = False
        if generation == self._build_generation and self._mod_name is not None:
            if not self.isVisible():
                self._needs_repopulate = True
            elif error is not None:
                self._load_failed(error)
            else:
                expanded = self._pending_expanded or self._expanded_paths()
                selected = self._model.node(self._tree.currentIndex())
                selected_path = ("\x00meta.ini" if selected is not None and selected.meta
                                 else selected.path.lower() if selected is not None else None)
                root, by_path = result
                self._add_strip_placeholders(root, by_path)
                self._add_meta_ini_row(root, by_path)
                self._model.set_root(root, by_path)
                self._pending_expanded = set()
                self._label.setText(self._mod_name)
                self._tree.setEnabled(True)
                if self._search or self._search_exts:
                    self._tree.expandAll()
                else:
                    self._restore_expanded(expanded, top_level_open=False)
                node = by_path.get(selected_path)
                if node is not None:
                    self._tree.setCurrentIndex(self._model.createIndex(node.row(), 0, node))
        self._start_repopulate()

    def _load_failed(self, error):
        from Utils.app_log import app_log
        app_log(f"Mod Files: {self._mod_name}: {error}")
        self._needs_repopulate = False
        self._repop_timer.stop()
        self._model.clear()
        self._tree.setEnabled(True)
        self._ext_counts = {}
        self._label.setText(self.tr("{0} — Unable to load files").format(self._mod_name))
        self.filetypes_changed.emit()

    def _add_strip_placeholders(self, root: _Node, by_path: dict):
        """Synthetic greyed rows for strip entries not otherwise in the tree so
        the user can un-strip them (Tk parity)."""
        for entry_l in sorted(self._stripped):
            if not entry_l or entry_l in by_path:
                continue
            display = entry_l
            sp_map = mflogic.read_mod_strip_prefixes(self.profile_dir, None) \
                if self.profile_dir is not None else {}
            for e in sp_map.get(self._mod_name or "", []):
                if e.lower() == entry_l:
                    display = e
                    break
            node = _Node(display, display, is_dir=True, parent=root)
            node.synthetic = True
            node.stripped = True
            root.children.insert(0, node)
            by_path[entry_l] = node

    def _mod_root_dir(self) -> Path | None:
        """The mod's own staging folder (where meta.ini lives). Distinct from a
        file's deploy path - meta.ini sits at the folder root, not in the tree."""
        return mflogic._mod_dir_for(self.game, self._mod_name) \
            if self._mod_name is not None else None

    def _add_meta_ini_row(self, root: _Node, by_path: dict):
        """If the mod folder has a meta.ini (MO2 metadata, excluded from the
        deploy scan), add a top row that opens it in the text editor. Hidden
        while a filter/search is active so it doesn't pollute filtered results."""
        if self._search or self._search_exts or self._inc_exts or self._exc_exts \
                or self._filter_state.get("mf_win") == 1 \
                or self._filter_state.get("mf_lose") == 1:
            return
        mod_dir = self._mod_root_dir()
        if mod_dir is None:
            return
        meta_path = mod_dir / "meta.ini"
        if not meta_path.is_file():
            return
        node = _Node("meta.ini", "meta.ini", is_dir=False, parent=root,
                     rel_str="meta.ini")
        node.meta = True
        root.children.insert(0, node)
        by_path["\x00meta.ini"] = node   # reserved key: never collides with a real path

    def _prune_orphan_strips(self, files: dict):
        """Remove strip entries that aren't a real ancestor folder of any file
        in this mod (legacy corruption). Persists the cleaned set if it changed
        so the orphan rows stop appearing."""
        if not self._stripped:
            return
        # All real ancestor folder paths (lower) across every file.
        valid: set[str] = set()
        for rel_str in files.values():
            p = rel_str.replace("\\", "/")
            segs = p.split("/")[:-1]
            cur = ""
            for s in segs:
                cur = f"{cur}/{s}" if cur else s
                valid.add(cur.lower())
        pruned = {s for s in self._stripped if s in valid}
        if pruned != self._stripped:
            self._stripped = pruned
            if self.profile_dir is not None and self._mod_name is not None:
                mflogic.save_strip_prefixes(
                    self.profile_dir, self._mod_name, self._stripped)

    # -- search (driven by the app's column-footer search box) --------------
    def _on_search(self, text: str):
        from Utils.text.search import parse_file_query
        needle, self._search_exts = parse_file_query(text)
        self._search = needle
        self._request_repopulate(delay=150)

    # -- expand (driven by the app's footer Expand-all button) -------------
    def _toggle_expand_all(self) -> bool:
        """Toggle expand/collapse all; returns True if now expanded."""
        first = self._model.index(0, 0) if self._model.rowCount() else None
        expanded = bool(first is not None and self._tree.isExpanded(first))
        if expanded:
            self._tree.collapseAll()
            return False
        self._tree.expandAll()
        return True

    def _expanded_paths(self) -> set[str]:
        return {
            path for path, node in self._model._by_path.items()
            if node.is_dir and self._tree.isExpanded(
                self._model.createIndex(node.row(), 0, node))
        }

    def _restore_expanded(self, paths: set[str], top_level_open: bool):
        if top_level_open:
            paths = paths | {
                node.path.lower() for node in self._model._root.children if node.is_dir
            }
        for path in sorted(paths):
            node = self._model._by_path.get(path)
            if node is not None and node.is_dir:
                self._tree.expand(self._model.createIndex(node.row(), 0, node))

    # -- checkbox clicks ----------------------------------------------------
    def _on_clicked(self, index):
        node = self._model.node(index)
        if node is None or node is self._model._root:
            return
        col = index.column()
        if col == COL_DISABLE:
            self._toggle_disable(node)
        elif col == COL_ROOT:
            self._toggle_root(node)
        elif col == COL_TOPLEVEL:
            self._toggle_top_level(node)
        elif col == COL_NAME and node.is_dir and self._model.rowCount(index) > 0:
            # Toggle expand/collapse on a folder-name click (we draw the arrow
            # ourselves; the native branch click is disabled).
            self._tree.setExpanded(index, not self._tree.isExpanded(index))
        elif col == COL_NAME and not node.is_dir:
            self._maybe_open_file(node)

    def _maybe_open_file(self, node: _Node):
        """Single-click a file to preview supported content."""
        if node.rel_str is None:
            return
        ext = Path(node.rel_str).suffix.lower()
        from gui_qt.bsa_preview import ARCHIVE_EXTS
        if ext in ARCHIVE_EXTS:
            target = self._disk_path_for(node)
            if target is None or not target.is_file():
                return
            cb = getattr(self, "on_open_archive", None)
            if cb is not None:
                cb(target, node.rel_str)
            return
        from gui_qt.nif_preview import PREVIEW_EXTS as NIF_EXTS
        if ext in NIF_EXTS:
            target = self._disk_path_for(node)
            if target is None or not target.is_file():
                return
            cb = getattr(self, "on_open_nif", None)
            if cb is not None:
                cb(target, node.rel_str)
            return
        from gui_qt.image_preview import PREVIEW_EXTS
        if ext in PREVIEW_EXTS:
            target = self._disk_path_for(node)
            if target is None or not target.is_file():
                return
            cb = getattr(self, "on_open_image", None)
            if cb is not None:
                cb(target, node.rel_str)
            return
        if ext in AUDIO_EXTS:
            target = self._disk_path_for(node)
            if target is None or not target.is_file():
                return
            self._audio_controls.set_audio(target, node.rel_str)
            return
        if ext in VIDEO_EXTS:
            target = self._disk_path_for(node)
            if target is None or not target.is_file():
                return
            cb = getattr(self, "on_open_video", None)
            if cb is not None:
                cb(target, node.rel_str)
            return
        from Utils.text.files import TEXT_EXTENSIONS
        if ext in TEXT_EXTENSIONS:
            target = self._disk_path_for(node)
            if target is None or not target.is_file():
                return
            cb = getattr(self, "on_open_text", None)
            if cb is not None:
                cb(target, node.rel_str)

    def _disk_path_for(self, node: _Node) -> Path | None:
        """Resolve a tree node to its real on-disk path under the mod folder."""
        if self.game is None or self._mod_name is None:
            return None
        rel = node.rel_str if node.rel_str is not None else node.path
        return self._safe_disk_child(rel)

    def _safe_disk_child(self, rel: str) -> Path | None:
        root = self._mod_root_dir()
        relative = Path(rel.replace("\\", "/"))
        if (root is None or not relative.parts or relative.is_absolute()
                or any(part in ("", ".", "..") for part in relative.parts)):
            return None
        candidate = root.joinpath(*relative.parts)
        try:
            candidate.parent.resolve(strict=False).relative_to(
                root.resolve(strict=False))
        except (OSError, RuntimeError, ValueError):
            return None
        return candidate

    def _toggle_disable(self, node: _Node):
        if node.synthetic:
            return
        if node.is_dir:
            leaves = self._model.leaves(node)
            if not leaves:
                return
            all_on = all(l.checked for l in leaves)
            self._model.set_disabled_subtree(node, not all_on)
        else:
            self._model.set_disabled(node, not node.checked)
        self._save_exclusions()

    def _toggle_root(self, node: _Node):
        if node.synthetic or node.meta:
            return
        from Utils.filegraph.constants import ROOT_FOLDER_NAME
        if self._mod_name == ROOT_FOLDER_NAME:
            return   # [Root_Folder] already deploys to the game root
        if node.is_dir:
            leaves = self._model.leaves(node)
            if not leaves:
                return
            all_on = all(l.root_tag for l in leaves)
            self._model.set_root_subtree(node, not all_on)
        else:
            self._model.set_root_tag(node, not node.root_tag)
        self._save_root_tags()

    def _save_root_tags(self):
        if self.profile_dir is None or self._mod_name is None:
            return
        leaves = self._model.leaves(self._model._root)
        visible = {l.raw_key for l in leaves if l.raw_key is not None}
        tagged = {l.raw_key for l in leaves
                  if l.raw_key is not None and l.root_tag}
        mflogic.save_root_tags(self.profile_dir, self._mod_name, visible, tagged)
        self.changed.emit()

    def has_changes(self) -> bool:
        """True when the shown mod has any saved Mod Files edits (gates Reset)."""
        return mflogic.mod_has_changes(self.profile_dir, self._mod_name)

    def reset_mod(self) -> bool:
        """Clear every Mod Files edit for the shown mod and rebuild the tree."""
        if not mflogic.reset_mod_state(self.profile_dir, self._mod_name):
            return False
        self._stripped = set()
        self._repopulate()
        self.changed.emit()
        return True

    def _toggle_top_level(self, node: _Node):
        if self.profile_dir is None or self._mod_name is None or not node.path:
            return
        if node.is_dir and not self._model.leaves(node):
            return
        self._stripped = mflogic.toggle_top_level(node.path, self._stripped)
        # Case hints from every node path + its ancestors.
        hints: dict[str, str] = {}
        for n in self._model._by_path.values():
            if n.path:
                hints.setdefault(n.path.lower(), n.path)
                for anc in mflogic.ancestor_paths(n.path):
                    hints.setdefault(anc.lower(), anc)
        mflogic.save_strip_prefixes(self.profile_dir, self._mod_name,
                                    self._stripped, hints)
        self._repopulate()
        self.changed.emit()

    def _save_exclusions(self):
        if self.profile_dir is None or self._mod_name is None:
            return
        leaves = self._model.leaves(self._model._root)
        visible = {l.raw_key for l in leaves if l.raw_key is not None}
        excluded = {l.raw_key for l in leaves
                    if l.raw_key is not None and not l.checked}
        mflogic.save_exclusions(self.profile_dir, self._mod_name, visible, excluded)
        self.changed.emit()

    # -- right-click --------------------------------------------------------
    def _on_context_menu(self, pos):
        index = self._tree.indexAt(pos)
        node = self._model.node(index) if index.isValid() else None
        if node is self._model._root:
            node = None
        if node is not None:
            self._tree.setCurrentIndex(index.siblingAtColumn(COL_NAME))
        if self._mod_name is None:
            return

        menu = QMenu(self._tree)
        real_node = node is not None and not node.synthetic
        editable_node = real_node and not node.meta
        if real_node:
            target = self._disk_path_for(node)
            action = menu.addAction(self.tr("Open"))
            action.setEnabled(target is not None and self._entry_exists(target))
            action.triggered.connect(
                lambda _checked=False, n=node: self._open_disk_entry(n))
            if editable_node:
                action = menu.addAction(self.tr("Rename…"))
                action.triggered.connect(
                    lambda _checked=False, n=node: self._prompt_rename(n))
                action = menu.addAction(self.tr("Delete…"))
                action.triggered.connect(
                    lambda _checked=False, n=node: self._confirm_delete(n))
            menu.addSeparator()

        parent_rel = self._creation_parent(node)
        action = menu.addAction(self.tr("Create new folder…"))
        action.setEnabled(parent_rel is not None)
        action.triggered.connect(
            lambda _checked=False, p=parent_rel: self._prompt_create(p, True))
        action = menu.addAction(self.tr("Create new file…"))
        action.setEnabled(parent_rel is not None)
        action.triggered.connect(
            lambda _checked=False, p=parent_rel: self._prompt_create(p, False))

        if editable_node:
            menu.addSeparator()
            action = menu.addAction(
                self.tr("Unset Top Level") if node.top_level
                else self.tr("Set Top Level"))
            has_files = bool(self._model.leaves(node)) if node.is_dir else True
            action.setEnabled(bool(
                has_files and node.path and mflogic.parent_path(node.path)))
            action.triggered.connect(
                lambda _checked=False, n=node: self._toggle_top_level(n))

            from Utils.filegraph.constants import ROOT_FOLDER_NAME
            root_available = (self._mod_name != ROOT_FOLDER_NAME
                              and not self._tree.isColumnHidden(COL_ROOT))
            root_on = self._node_root_state(node)
            action = menu.addAction(
                self.tr("Unset as Root") if root_on else self.tr("Set as Root"))
            action.setEnabled(root_available and has_files)
            action.triggered.connect(
                lambda _checked=False, n=node: self._toggle_root(n))

            enabled = self._node_enabled_state(node)
            action = menu.addAction(self.tr("Disable") if enabled
                                    else self.tr("Enable"))
            action.setEnabled(has_files)
            action.triggered.connect(
                lambda _checked=False, n=node: self._toggle_disable(n))

        menu.exec(self._tree.viewport().mapToGlobal(pos))

    @staticmethod
    def _entry_exists(path: Path) -> bool:
        return path.exists() or path.is_symlink()

    def _creation_parent(self, node: _Node | None) -> str | None:
        if node is None:
            return "" if self._mod_root_dir() is not None else None
        if node.synthetic:
            return None
        rel = node.rel_str if node.rel_str is not None else node.path
        return rel if node.is_dir else mflogic.parent_path(rel)

    def _node_enabled_state(self, node: _Node) -> bool:
        leaves = self._model.leaves(node) if node.is_dir else [node]
        return bool(leaves) and all(leaf.checked for leaf in leaves)

    def _node_root_state(self, node: _Node) -> bool:
        leaves = self._model.leaves(node) if node.is_dir else [node]
        return bool(leaves) and all(leaf.root_tag for leaf in leaves)

    def _open_disk_entry(self, node: _Node):
        target = self._disk_path_for(node)
        if target is None or not self._entry_exists(target):
            self._show_file_error(
                self.tr("Open"), self.tr("The selected item no longer exists."))
            return
        callback = getattr(self, "on_open_path", None)
        if callback is not None:
            callback(target)
            return
        try:
            from Utils.environment.xdg import xdg_open
            xdg_open(target)
        except Exception as exc:
            self._show_file_error(self.tr("Open"), str(exc))

    def _prompt_rename(self, node: _Node):
        from gui_qt.text_input_overlay import TextInputOverlay
        TextInputOverlay.show_over(
            self, self.tr("Rename"), self.tr("New name:"),
            lambda name, n=node: self._rename_entry(n, name),
            initial=node.name, ok_label=self.tr("Rename"))

    def _rename_entry(self, node: _Node, value: str | None):
        name = self._validated_name(value, node.is_dir)
        if name is None:
            return
        source = self._disk_path_for(node)
        rel = node.rel_str if node.rel_str is not None else node.path
        parent_rel = mflogic.parent_path(rel)
        new_rel = f"{parent_rel}/{name}" if parent_rel else name
        target = self._safe_disk_child(new_rel)
        if source is None or target is None or not self._entry_exists(source):
            self._show_file_error(
                self.tr("Rename"), self.tr("The selected item no longer exists."))
            return
        if source == target:
            return
        if self._entry_exists(target):
            self._show_file_error(
                self.tr("Rename"),
                self.tr("A file or folder with that name already exists."))
            return
        try:
            source.rename(target)
        except OSError as exc:
            self._show_file_error(self.tr("Rename"), str(exc))
            return
        state_error = None
        try:
            mflogic.rename_path_state(
                self.profile_dir, self._mod_name, rel, new_rel, node.is_dir)
        except OSError as exc:
            state_error = exc
        self._disk_contents_changed()
        if state_error is not None:
            self._show_file_error(
                self.tr("Rename"),
                self.tr("The item was renamed, but its saved Mod Files settings "
                        "could not be updated: {0}").format(state_error))

    def _confirm_delete(self, node: _Node):
        from gui_qt.confirm_overlay import ConfirmOverlay

        def confirmed(ok):
            if ok:
                self._delete_entry(node)

        ConfirmOverlay.show_over(
            self, self.tr("Delete"),
            self.tr("Permanently delete '{0}' from this mod?\n\n"
                    "This cannot be undone.").format(node.name),
            confirmed, confirm_label=self.tr("Delete"), danger=True)

    def _delete_entry(self, node: _Node):
        target = self._disk_path_for(node)
        rel = node.rel_str if node.rel_str is not None else node.path
        if target is None or not self._entry_exists(target):
            self._show_file_error(
                self.tr("Delete"), self.tr("The selected item no longer exists."))
            return
        try:
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink()
        except OSError as exc:
            self._disk_contents_changed()
            self._show_file_error(self.tr("Delete"), str(exc))
            return
        state_error = None
        try:
            mflogic.remove_path_state(
                self.profile_dir, self._mod_name, rel, node.is_dir)
        except OSError as exc:
            state_error = exc
        self._disk_contents_changed()
        if state_error is not None:
            self._show_file_error(
                self.tr("Delete"),
                self.tr("The item was deleted, but its saved Mod Files settings "
                        "could not be updated: {0}").format(state_error))

    def _prompt_create(self, parent_rel: str | None, is_dir: bool):
        if parent_rel is None:
            return
        from gui_qt.text_input_overlay import TextInputOverlay
        TextInputOverlay.show_over(
            self,
            self.tr("Create new folder") if is_dir else self.tr("Create new file"),
            self.tr("Name:"),
            lambda name, p=parent_rel, d=is_dir: self._create_entry(p, name, d),
            ok_label=self.tr("Create"))

    def _validated_name(self, value: str | None, is_dir: bool) -> str | None:
        if value is None:
            return None
        name = value.strip()
        if not name or name in (".", "..") or "/" in name or "\\" in name \
                or "\x00" in name:
            self._show_file_error(
                self.tr("Invalid name"),
                self.tr("Enter one file or folder name without path separators."))
            return None
        lower = name.lower()
        excluded_dirs = {
            str(item).lower()
            for item in (getattr(self.game, "filemap_exclude_dirs", ()) or ())
        }
        hidden = ((is_dir and (lower in excluded_dirs
                               or name.startswith("prefix_")
                               or name == ".mm_bundle"))
                  or (not is_dir and (name in {"meta.ini", ".DS_Store"}
                                      or name.startswith("._"))))
        if hidden:
            self._show_file_error(
                self.tr("Invalid name"),
                self.tr("That name is reserved and would be hidden from Mod Files."))
            return None
        return name

    def _create_entry(self, parent_rel: str, value: str | None, is_dir: bool):
        name = self._validated_name(value, is_dir)
        if name is None:
            return
        rel = f"{parent_rel}/{name}" if parent_rel else name
        target = self._safe_disk_child(rel)
        parent = self._mod_root_dir() if not parent_rel \
            else self._safe_disk_child(parent_rel)
        title = self.tr("Create new folder") if is_dir else self.tr("Create new file")
        if target is None or parent is None:
            self._show_file_error(title, self.tr("The destination is not safe."))
            return
        if self._entry_exists(target):
            self._show_file_error(
                title, self.tr("A file or folder with that name already exists."))
            return
        try:
            parent.mkdir(parents=True, exist_ok=True)
            if is_dir:
                target.mkdir()
            else:
                target.touch(exist_ok=False)
        except OSError as exc:
            self._show_file_error(title, str(exc))
            return
        self._disk_contents_changed()

    def _disk_contents_changed(self):
        self._audio_controls.clear_audio()
        self._stripped = mflogic.read_strip_prefixes(
            self.profile_dir, self._mod_name)
        self._snapshot = None
        self._folders_cache = None
        self._invalidate_files()
        self._request_repopulate()
        self.changed.emit()

    def _show_file_error(self, title: str, message: str):
        from gui_qt.confirm_overlay import ConfirmOverlay
        ConfirmOverlay.show_message(self, title, message)

    def has_mod(self) -> bool:
        """True when a real mod is shown (gates Pack/Unpack + Reset)."""
        return self._mod_name is not None
