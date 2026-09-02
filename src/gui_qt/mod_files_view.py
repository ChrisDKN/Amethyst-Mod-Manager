"""Qt Mod Files tab - per-mod file tree with Top Level + Root + Disable checkbox columns.

Reuses Utils.mods.files for every bit of logic (file listing, conflict cache, the
strip-prefix promotion/demotion algorithm, the exclusion save-merge) so it stays
in lockstep with the Tk tab. This module is the Qt presentation: a QTreeView +
ModFilesModel, a toolbar (Expand all / Filters), a search box, and a footer
(Pack / Unpack BSA).
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTreeView, QLabel, QAbstractItemView,
)

import Utils.mods.files as mflogic
from gui_qt.mod_files_model import (
    ModFilesModel, _Node, COL_NAME, COL_TOPLEVEL, COL_ROOT, COL_DISABLE,
)
from gui_qt.audio_preview import AUDIO_EXTS, AudioControls
from gui_qt.video_preview import VIDEO_EXTS


class ModFilesView(QWidget):
    """Self-contained Mod Files tab widget. Call show_mod(mod_name) to populate.
    Emits changed() after any edit so the host can rebuild the filemap."""

    changed = Signal()
    filetypes_changed = Signal()   # the ext-count list changed (refresh panel)
    mod_changed = Signal(object)   # the shown mod name (or None) changed

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
        self._build()

    # -- context ------------------------------------------------------------
    def configure(self, game, profile_dir):
        if game is not self.game or profile_dir != self.profile_dir:
            self._audio_controls.clear_audio()
        self.game = game
        self.profile_dir = profile_dir
        self._snapshot = None
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
        self._snapshot = snapshot
        if self._mod_name is not None:
            self._request_repopulate()

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
        hdr.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
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
            self._audio_controls.clear_audio()
        self._mod_name = mod_name
        self.mod_changed.emit(mod_name)
        if mod_name is None:
            self._label.setText(self.tr("(no mod selected)"))
            self._model.clear()
            self._ext_counts = {}
            self._needs_repopulate = False
            self.filetypes_changed.emit()
            return
        self._label.setText(mod_name)
        self._stripped = mflogic.read_strip_prefixes(self.profile_dir, mod_name)
        self._request_repopulate()

    def _request_repopulate(self):
        """Defer the (expensive: live disk scan + conflict cache) tree rebuild
        off the caller's stack. show_mod runs inside the modlist selection
        handler - rebuilding synchronously there blocks the conflict
        highlights/marker-strip repaint for seconds on a file-heavy mod. While
        this tab is hidden (e.g. the Plugins sub-tab is showing) skip the work
        entirely and do it on show instead; when visible, coalesce onto a
        0-timer so the selection handler returns and paints first."""
        if not self.isVisible():
            self._needs_repopulate = True
            return
        t = getattr(self, "_repop_timer", None)
        if t is None:
            t = QTimer(self)
            t.setSingleShot(True)
            t.setInterval(0)
            t.timeout.connect(self._repopulate)
            self._repop_timer = t
        t.start()

    def showEvent(self, event):
        super().showEvent(event)
        if getattr(self, "_needs_repopulate", False):
            self._repopulate()

    def _repopulate(self):
        """Rebuild the tree from disk/index + the current strip/exclude/filter
        state, preserving expand state by path."""
        self._needs_repopulate = False
        mod_name = self._mod_name
        if mod_name is None:
            return
        # Preserve expand state by path.
        expanded = self._expanded_paths()

        if self._snapshot is not None:
            records = self._snapshot.mod_files(mod_name)
            files = {
                record.source_rel.decode("utf-8", "surrogateescape").lower():
                record.source
                for record in records
            }
            conflict_status = {
                record.source_rel.decode("utf-8", "surrogateescape").lower():
                record.conflict_status
                for record in records
            }
        else:
            # Before initial catalog publication, show the selected mod's real
            # files without consulting any legacy index. Conflict tinting stays
            # disabled until a snapshot arrives.
            files = mflogic.load_mod_files(self.game, mod_name)
            conflict_status = {}
        # Extension counts (pre-filter) for the filter panel.
        self._ext_counts = {}
        for rel_key in files:
            ext = Path(rel_key).suffix.lower()
            self._ext_counts[ext] = self._ext_counts.get(ext, 0) + 1
        self.filetypes_changed.emit()

        # Self-heal: drop strip entries that aren't a real ancestor folder of any
        # file (legacy corruption - e.g. a path saved without its parent prefix).
        # These would otherwise show as un-removable orphan rows.
        self._prune_orphan_strips(files)
        # One-time conversion of state written in the old post-strip key space.
        # Only root tags are pruned - BSA pack needs exclusions for the loose
        # files it deletes, which no longer exist on disk.
        mflogic.migrate_root_tags_to_raw(self.profile_dir, mod_name,
                                         files, self._stripped)
        mflogic.migrate_exclusions_to_raw(self.profile_dir, mod_name,
                                          files, self._stripped)
        mflogic.prune_orphan_root_tags(self.profile_dir, mod_name, set(files))

        excluded = mflogic.read_exclusions(self.profile_dir, mod_name)
        root_tags = mflogic.read_root_tags(self.profile_dir, mod_name)
        def conflict_of(rel_key: str) -> int:
            return conflict_status.get(rel_key.lower(), 0)

        def keep(rel_key: str, rel_str: str) -> bool:
            if not self._ext_ok(rel_key):
                return False
            if (self._search_exts
                    and Path(rel_key).suffix.lower() not in self._search_exts):
                return False
            if not self._conflict_ok(conflict_of(rel_key)):
                return False
            if self._search and self._search not in rel_str.lower():
                return False
            return True

        tree_dict = mflogic.build_tree(files, keep_rel_key=keep)

        # Build _Node hierarchy from the nested dict.
        root = _Node("", "", is_dir=True)
        by_path: dict[str, _Node] = {}

        def add_nodes(parent: _Node, subtree: dict, parent_path: str):
            for folder in sorted(k for k in subtree if k != "__files__"):
                fpath = f"{parent_path}/{folder}" if parent_path else folder
                fn = _Node(folder, fpath, is_dir=True, parent=parent)
                parent.children.append(fn)
                by_path[fpath.lower()] = fn
                add_nodes(fn, subtree[folder], fpath)
            for fname, rel_key, rel_str in sorted(subtree.get("__files__", [])):
                fpath = f"{parent_path}/{fname}" if parent_path else fname
                leaf = _Node(fname, fpath, is_dir=False, parent=parent,
                             rel_str=rel_str, raw_key=rel_key)
                # Both stores key on the RAW path, so two files that collapse
                # to one post-strip key still tick independently.
                leaf.checked = rel_key not in excluded
                leaf.root_tag = rel_key in root_tags
                leaf.conflict = conflict_of(rel_key)
                parent.children.append(leaf)
                by_path[fpath.lower()] = leaf

        add_nodes(root, tree_dict, "")
        self._add_strip_placeholders(root, by_path)
        self._recompute_top_level(by_path)
        self._add_meta_ini_row(root, by_path)
        self._model.set_root(root, by_path)

        # Restore / set expand.
        search_active = bool(self._search or self._search_exts)
        if search_active:
            self._tree.expandAll()
        else:
            self._restore_expanded(expanded, top_level_open=False)

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

    def _recompute_top_level(self, by_path: dict):
        # A row's Top Level box is CHECKED only when it currently deploys at the
        # top level AND its own path isn't itself stripped. A row stripped to
        # promote a descendant shows UNCHECKED + greyed (Tk parity) - this is the
        # "selecting a different top-level deselects the others" behaviour.
        for node in by_path.values():
            if node.synthetic:
                node.top_level = False
                node.stripped = True
                continue
            is_stripped = node.path.lower() in self._stripped
            node.stripped = is_stripped
            node.top_level = (not is_stripped
                              and mflogic.is_top_level(node.path, self._stripped))

    # -- filter helpers -----------------------------------------------------
    def _ext_ok(self, rel_key: str) -> bool:
        if not self._inc_exts and not self._exc_exts:
            return True
        ext = Path(rel_key).suffix.lower()
        if ext in self._exc_exts:
            return False
        if self._inc_exts:
            return ext in self._inc_exts
        return True

    def _conflict_ok(self, conflict: int) -> bool:
        want_win = self._filter_state.get("mf_win") == 1
        want_lose = self._filter_state.get("mf_lose") == 1
        if not want_win and not want_lose:
            return True
        if conflict == 1:
            return want_win
        if conflict == -1:
            return want_lose
        return False

    # -- search (driven by the app's column-footer search box) --------------
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
        out: set[str] = set()
        m = self._model

        def walk(parent_index):
            for r in range(m.rowCount(parent_index)):
                idx = m.index(r, 0, parent_index)
                node = m.node(idx)
                if node and node.is_dir and self._tree.isExpanded(idx) and node.path:
                    out.add(node.path.lower())
                walk(idx)
        walk(self._model.index(-1, -1).parent())  # from root
        return out

    def _restore_expanded(self, paths: set[str], top_level_open: bool):
        m = self._model

        def walk(parent_index, depth):
            for r in range(m.rowCount(parent_index)):
                idx = m.index(r, 0, parent_index)
                node = m.node(idx)
                if node and node.is_dir:
                    if (node.path and node.path.lower() in paths) or \
                       (top_level_open and depth == 0):
                        self._tree.expand(idx)
                    walk(idx, depth + 1)
        walk(self._model.index(-1, -1).parent(), 0)

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
        """Resolve a file node to its real on-disk path under the mod folder."""
        if self.game is None or self._mod_name is None or node.rel_str is None:
            return None
        try:
            from Utils.filegraph.constants import OVERWRITE_NAME, ROOT_FOLDER_NAME
            if self._mod_name == OVERWRITE_NAME and hasattr(
                    self.game, "get_effective_overwrite_path"):
                base = Path(self.game.get_effective_overwrite_path())
            elif self._mod_name == ROOT_FOLDER_NAME and hasattr(
                    self.game, "get_effective_root_folder_path"):
                base = Path(self.game.get_effective_root_folder_path())
            else:
                base = Path(self.game.get_effective_mod_staging_path()) / self._mod_name
        except Exception:
            return None
        return base / node.rel_str.replace("\\", "/")

    def _toggle_disable(self, node: _Node):
        if node.synthetic:
            return
        if node.is_dir:
            leaves = self._model.leaves(node)
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

    # -- right-click (context menu stub filled in follow-up) ----------------
    # Pack / Unpack BSA are driven by the app footer buttons (app._on_pack_bsa /
    # _on_unpack_bsa), which own the progress popup + filemap rebuild.
    def _on_context_menu(self, pos):
        pass  # wired in a later step

    def has_mod(self) -> bool:
        """True when a real mod is shown (gates Pack/Unpack + Reset)."""
        return self._mod_name is not None
