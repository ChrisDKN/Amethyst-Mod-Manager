"""Modlist view - QTreeView + ModListModel + ModRowDelegate.

Internal-move drag-reorder (model.beginMoveRows preserves selection/scroll);
TkStyleHeader owns column resizing; column state persists via column_state.
"""

from __future__ import annotations

# Crash-proof diagnostic prints (Flatpak stdout can raise BrokenPipeError and
# kill worker threads). See Utils.app_log.safe_print.
from Utils.app_log import safe_print as print  # noqa: A004

from PySide6.QtCore import Qt, QTimer, QRect, QPoint, QCoreApplication, QEvent
from PySide6.QtGui import QPainter, QColor, QPen, QAction
from PySide6.QtWidgets import (
    QTreeView, QAbstractItemView, QToolButton, QMenu,
    QStyleOptionViewItem, QToolTip,
)

from gui_qt.modlist_model import (
    ModListModel, COLUMNS, COL_NAME, COL_CATEGORY, COL_PRIORITY, COL_FLAGS,
    COL_CONFLICTS, COL_INSTALLED, COL_VERSION, COL_AUTHOR, COL_SIZE,
    HighlightRole,
)
from gui_qt.modlist_delegate import ModRowDelegate, SEP_H
from gui_qt import column_state
from gui_qt.modlist_header import TkStyleHeader
from gui_qt.theme_qt import bind_theme, _c


class _StayOpenMenu(QMenu):
    """A QMenu that stays open when a checkable action is toggled, so the user
    can flip several column/filter options without re-opening it each time.
    Non-checkable actions (and submenu navigation) behave normally."""

    def mouseReleaseEvent(self, event):
        act = self.activeAction()
        if act is not None and act.isEnabled() and act.isCheckable():
            act.trigger()          # flips the check + fires toggled
            return                 # ...but DON'T let the menu close
        super().mouseReleaseEvent(event)


# Per-column default width + minimum (design px), mirroring the Tk app's
# _layout_columns data_defaults / data_mins. Name auto-fills the leftover.
COL_DEFAULTS = {
    COL_CATEGORY: 120, COL_FLAGS: 70, COL_CONFLICTS: 95, COL_INSTALLED: 100,
    COL_VERSION: 90, COL_AUTHOR: 110, COL_PRIORITY: 75, COL_SIZE: 85,
}
COL_MINS = {
    COL_NAME: 120, COL_CATEGORY: 90, COL_FLAGS: 60, COL_CONFLICTS: 90,
    COL_INSTALLED: 90, COL_VERSION: 80, COL_AUTHOR: 80, COL_PRIORITY: 70,
    COL_SIZE: 70,
}
NAME_MIN = COL_MINS[COL_NAME]

# MIME type for archive paths dragged from the Downloads tab (newline-joined,
# utf-8). Dropping them on the modlist installs at the drop position.
ARCHIVE_DROP_MIME = "application/x-amethyst-archive-paths"

# Columns shown by default on a fresh INI (no persisted state). Tk parity:
# Category, Installed, Size are hidden until the user enables them; Author
# (Nexus uploader) is likewise opt-in.
_FIRST_RUN_HIDDEN = {COL_CATEGORY, COL_INSTALLED, COL_AUTHOR, COL_SIZE}

# Header column → sort key (Tk _DATA_COL_SORT_KEYS; keys persisted by name via
# column_state's sort_col, which stores the COLUMNS display name).
_COL_TO_SORTKEY = {
    COL_NAME: "name", COL_CATEGORY: "category", COL_FLAGS: "flags",
    COL_CONFLICTS: "conflicts", COL_INSTALLED: "installed",
    COL_VERSION: "version", COL_AUTHOR: "author", COL_PRIORITY: "priority",
    COL_SIZE: "size",
}


class ModListView(QTreeView):
    def __init__(self, model: ModListModel, parent=None):
        super().__init__(parent)
        self.setModel(model)
        self.setItemDelegate(ModRowDelegate(self))

        self.setRootIsDecorated(False)        # flat list, not a tree
        self.setUniformRowHeights(False)      # separators are taller
        self.setAlternatingRowColors(False)   # delegate paints zebra itself
        self.setMouseTracking(True)
        self.setExpandsOnDoubleClick(False)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)

        # Custom drag-reorder (NOT Qt InternalMove): we drive the reorder by
        # hand so separators (spanned rows) drag correctly and autoscroll near
        # the edges is fast/continuous like the Tk app. See _press/_move/_release.
        self.setDragDropMode(QAbstractItemView.NoDragDrop)
        self.setDragEnabled(False)
        # Qt-native drops stay on for EXTERNAL drags only (archives dragged
        # from the Downloads tab → install at the drop position). The internal
        # reorder never uses Qt DnD, so the two cannot collide.
        self.setAcceptDrops(True)
        self.on_archives_dropped = None   # callback(paths: list[str], slot: int)
        self._extern_drop = False         # an archive drag is over the view

        # Drag state.
        self._drag_rows: list[int] = []   # source rows being carried (block)
        self._drag_active = False
        self._press_row = -1
        self._press_pos = None
        # Shift-click range locking: remember the last separator row whose lock
        # box was clicked and whether that click locked (True) or unlocked it,
        # so a following shift-click applies the same action across the range.
        self._lock_anchor_row = -1
        self._lock_range_locking = True
        self._drop_slot = -1              # insertion row for the drop indicator
        self._DRAG_THRESHOLD = 6          # px before a press becomes a drag

        # Continuous autoscroll while dragging near an edge (Tk cadence).
        self._scroll_zone = 40            # px from edge that triggers scroll
        self._scroll_timer = QTimer(self)
        self._scroll_timer.setInterval(16)   # ~60fps, smooth + fast
        self._scroll_timer.timeout.connect(self._autoscroll_tick)
        self._last_mouse_y = 0

        # Right-click context menu.
        self.staging_dir = None   # set by the window for Open-folder
        self.game = None          # set by the window (for full mod removal)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_context_menu)

        # Separator state persistence (profile dir set by the window on reload).
        self.profile_dir = None
        # Rows hidden by the filter side panel / search box (unioned with the
        # collapse hiding). _searching = a non-empty query is active (collapse
        # is then bypassed so matches inside collapsed separators show).
        self._filter_hidden: set[int] = set()
        self._search_hidden: set[int] = set()
        self._searching: bool = False
        # Last hidden-row set actually applied via setRowHidden - lets
        # apply_collapse touch only the delta. Row indices go stale on any
        # structural change, so drop the cache there.
        self._applied_hidden: set[int] | None = None

        def _drop_applied(*_a):
            self._applied_hidden = None
        for sig in (model.modelReset, model.rowsInserted, model.rowsRemoved,
                    model.rowsMoved, model.layoutChanged):
            sig.connect(_drop_applied)
        # A sort rebuild reorders rows in place (layoutChanged) - separator
        # spanning + collapse hiding are row-indexed, so re-apply both.
        # Connected AFTER _drop_applied so the hidden-set cache is clear first.
        model.layoutChanged.connect(self._on_model_layout_changed)
        model.modelReset.connect(self._on_model_layout_changed)
        # A fast-path insert (add_separator/insert_mod) emits rowsInserted, not
        # layoutChanged - re-apply spanning so a new separator's lock box jumps
        # to the far right immediately instead of only after the next move.
        model.rowsInserted.connect(self._on_model_layout_changed)
        self.doubleClicked.connect(self._on_double_click)

        self._restoring = True
        self._configure_header()
        self._restore_column_state()
        self._restoring = False

        # Persist on user changes (debounced to coalesce drag-resize bursts).
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(400)
        self._save_timer.timeout.connect(self._save_column_state)
        h = self.header()
        # sectionResized is handled by _on_section_resized (which saves);
        # only moves + sort-indicator need a direct save hook here.
        h.sectionMoved.connect(self._on_section_moved)
        h.sortIndicatorChanged.connect(lambda *a: self._schedule_save())

        # Sticky separator header: the separator governing the topmost visible
        # rows stays pinned to the viewport top while its group scrolls under
        # it. Pixel-scrolling blits the viewport, which would smear the pinned
        # band - repaint the top strip on every scroll step.
        self._sticky_press: int | None = None
        sb = self.verticalScrollBar()
        self._last_vscroll = sb.value()
        sb.valueChanged.connect(self._on_vscroll)

        # Marker strip: a coloured-tick gutter beside the scrollbar showing
        # highlighted/conflicting rows (Tk parity). Shared with the plugins panel.
        from gui_qt.marker_strip import install_marker_strip
        install_marker_strip(self, HighlightRole)
        self._reposition_marker_strip()
        bind_theme(self, roles={"TEXT_MAIN"})

    def refresh_theme(self, palette: dict) -> None:
        btn = getattr(self, "_col_menu_btn", None)
        if btn is not None:
            from gui_qt.icons import icon
            btn.setIcon(icon("eye1_white.png", 16,
                             color=_c(palette, "TEXT_MAIN")))
        self.viewport().update()
        self.header().viewport().update()

    def _reposition_marker_strip(self):
        from gui_qt.marker_strip import reposition_marker_strip
        reposition_marker_strip(self)

    def _on_model_layout_changed(self, *_a):
        """Row order changed in place (sort applied/cleared/re-derived) - the
        spanning + hidden-row state is row-indexed and must be re-applied."""
        self._apply_separator_spanning()
        self.apply_collapse()

    # ---- column-sort header clicks -----------------------------------------
    def _on_header_sort_clicked(self, logical: int):
        """Tk header-click cycle: Priority = 2-click toggle (reverse mode ↔
        off); other columns = ascending → descending → clear."""
        key = _COL_TO_SORTKEY.get(logical)
        if key is None:
            return
        m = self.model()
        cur, asc = m.sort_state()
        if key == "priority":
            new = (None, True) if cur == "priority" else ("priority", True)
        elif cur == key:
            new = (key, False) if asc else (None, True)
        else:
            new = (key, True)
        self._apply_sort(logical, *new)

    def _apply_sort(self, logical: int, key: str | None, ascending: bool):
        m = self.model()
        m.set_sort(key, ascending)
        h = self.header()
        if key is None:
            h.setSortIndicator(-1, Qt.AscendingOrder)
        else:
            h.setSortIndicator(logical, Qt.AscendingOrder if ascending
                               else Qt.DescendingOrder)
        h.viewport().update()   # repaint the custom sort triangles
        self._schedule_save()

    def sort_triangle_spec(self, logical: int):
        """TkStyleHeader hook: (active, ascending) for the sort triangle on
        *logical*, or None for a non-sortable section. Inactive columns show a
        dim ascending hint; the active column shows the real direction."""
        key = _COL_TO_SORTKEY.get(logical)
        if key is None:
            return None
        cur, asc = self.model().sort_state()
        if cur == key:
            return (True, asc)
        return (False, True)

    def _configure_header(self):
        # Custom Tk-style header: owns all resizing (boundary drag moves the
        # line between two columns, total constant, no overflow). All sections
        # Fixed so Qt never auto-resizes.
        h = TkStyleHeader(self, COL_MINS, COL_DEFAULTS)
        self.setHeader(h)
        # QTreeView.setHeader() re-configures the header and resets clickable
        # to follow setSortingEnabled (off - we drive the sort by hand), so
        # re-enable it AFTER installing or sectionClicked never fires.
        h.setSectionsClickable(True)
        h.setMinimumSectionSize(min(COL_MINS.values()))
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # Column sorting is driven by hand (NOT setSortingEnabled - Qt's model
        # sort can't express the Tk semantics: separators anchored, mods
        # sorted within their group, reverse-priority layout). Header clicks
        # cycle the sort; the model derives the display order. The native
        # indicator stays hidden - TkStyleHeader paints a triangle on EVERY
        # sortable column (accent-blue on the active one) via
        # sort_triangle_spec below; setSortIndicator still tracks the state
        # for persistence.
        h.setSortIndicatorShown(False)
        h.setSortIndicator(-1, Qt.AscendingOrder)
        h.sectionClicked.connect(self._on_header_sort_clicked)
        for col, w in COL_DEFAULTS.items():
            self.setColumnWidth(col, w)
        self._fitting = False
        h.sectionResized.connect(lambda *a: self._schedule_save())
        self._build_column_menu_button(h)

    # ---- column show/hide menu (eye button, aligned over the checkboxes) --
    def _build_column_menu_button(self, header):
        """A small eye button pinned to the LEFT of the Mod Name column header,
        centred over the row checkbox column below; opens a checkable menu to
        show/hide each toggleable column (Name always stays)."""
        from gui_qt.theme_qt import active_palette, _c
        from gui_qt.icons import icon
        from PySide6.QtCore import QSize
        btn = QToolButton(header)
        # Tint the eye glyph to the theme foreground so it reads in both light
        # and dark modes (the white PNG is invisible on the light header).
        btn.setIcon(icon("eye1_white.png", 16, color=_c(active_palette(), "TEXT_MAIN")))
        btn.setIconSize(QSize(16, 16))
        btn.setCursor(Qt.ArrowCursor)
        btn.setFocusPolicy(Qt.NoFocus)
        btn.setAutoRaise(True)
        btn.setToolTip(self.tr("Show / Hide columns"))
        # Opaque header-coloured background so it sits cleanly over the Mod Name
        # header text; hover/press come from the global QToolButton QSS.
        bg = _c(active_palette(), "BG_HEADER")
        btn.setStyleSheet(
            f"QToolButton {{ background: {bg}; border: none; padding: 0px; }}")
        btn.clicked.connect(self._show_column_menu)
        self._col_menu_btn = btn
        # Callback the window sets so enabling Size can trigger a size scan.
        self.on_sizes_requested = None
        # Hooks the window sets for the quick-filter menu items: on_quick_filter
        # (key, 0|1) applies the filter; quick_filter_state(key)->int reads the
        # current tri-state so the menu shows the right check marks.
        self.on_quick_filter = None
        self.quick_filter_state = None
        # filters_active() -> bool and on_clear_filters() back the menu's
        # "Clear all filters" entry (both wired by the window).
        self.filters_active = None
        self.on_clear_filters = None
        self._position_column_menu_button()
        btn.show()

    _COL_BTN_W = 26

    def _position_column_menu_button(self):
        btn = getattr(self, "_col_menu_btn", None)
        if btn is None:
            return
        from gui_qt.modlist_delegate import CHECK_BOX
        h = self.header()
        # Centre the button on the row checkbox column below it: the delegate
        # draws each checkbox at (col_left + 10, …) with width CHECK_BOX.
        col_left = h.sectionViewportPosition(COL_NAME)
        cb_center = col_left + 10 + CHECK_BOX // 2
        x = cb_center - self._COL_BTN_W // 2
        btn.setGeometry(max(0, x), 0, self._COL_BTN_W, h.height())
        btn.raise_()

    def _add_quick_filter_action(self, menu, key: str, label: str):
        """Add one checkable quick-filter action to `menu`. Checked = the
        filter is in include-mode (state 1); toggling drives the shared state."""
        get = getattr(self, "quick_filter_state", None)
        a = QAction(label, menu)
        a.setCheckable(True)
        a.setChecked(callable(get) and get(key) == 1)
        a.toggled.connect(lambda checked, k=key: self._on_quick_filter(k, checked))
        menu.addAction(a)

    def _show_column_menu(self):
        menu = _StayOpenMenu(self)
        for col, name in enumerate(COLUMNS):
            if col == COL_NAME:
                continue   # Name is always shown
            # Same translated label as the header (registered under ModListModel).
            a = QAction(QCoreApplication.translate("ModListModel", name), menu)
            a.setCheckable(True)
            a.setChecked(not self.isColumnHidden(col))
            a.toggled.connect(lambda checked, c=col: self._set_column_visible(c, checked))
            menu.addAction(a)
        # Quick modlist filters - a faster way to apply the "By status" filters
        # from the Filters panel. These drive the same filter state, so the
        # panel checkboxes stay in sync (the window wires on_quick_filter).
        menu.addSeparator()
        for key, label in (
            ("filter_show_enabled", self.tr("Enabled")),
            ("filter_show_disabled", self.tr("Disabled")),
            ("filter_hide_separators", self.tr("Hide separators")),
            # Already translated under FilterSidePanel (it's a STATUS_FILTERS
            # label) - reuse that entry rather than minting a ModListView copy.
            ("filter_has_updates",
             QCoreApplication.translate("FilterSidePanel", "Mods with updates")),
        ):
            self._add_quick_filter_action(menu, key, label)
        # The remaining "By status" filters live in a submenu so the top level
        # stays short. Same include-mode semantics as the quick filters above.
        from gui_qt.modlist_filter import STATUS_FILTERS
        _QUICK = {"filter_show_enabled", "filter_show_disabled",
                  "filter_hide_separators", "filter_has_updates"}
        more = _StayOpenMenu(self.tr("More status filters"), menu)
        for key, label in STATUS_FILTERS:
            if key in _QUICK:
                continue
            # STATUS_FILTERS labels are registered for translation under the
            # FilterSidePanel context (see filter_panel._TR_MARKERS).
            self._add_quick_filter_action(
                more, key, QCoreApplication.translate("FilterSidePanel", label))
        menu.addMenu(more)
        # Same escape hatch the Filters panel header offers - reachable without
        # opening the panel. Greyed while nothing is filtered.
        clear = QAction(self.tr("Clear all filters"), menu)
        clear.setEnabled(callable(self.filters_active) and self.filters_active())
        clear.triggered.connect(self._on_clear_filters)
        menu.addAction(clear)
        btn = self._col_menu_btn
        menu.exec(btn.mapToGlobal(btn.rect().bottomLeft()))

    def _on_clear_filters(self):
        cb = getattr(self, "on_clear_filters", None)
        if callable(cb):
            cb()

    def _on_quick_filter(self, key: str, on: bool):
        # State 1 = include-mode (show only matching); 0 = off. Hide-separators
        # is likewise 1/0. The window hook applies it to the shared filter state.
        cb = getattr(self, "on_quick_filter", None)
        if callable(cb):
            cb(key, 1 if on else 0)

    def _set_column_visible(self, col: int, visible: bool):
        self.setColumnHidden(col, not visible)
        if visible:
            # Qt collapses a hidden section's width to 0; restore a sensible
            # width so the re-shown column is actually visible (not a 0-px
            # sliver that only appears after the next manual resize).
            if self.columnWidth(col) <= 0:
                self.header().resizeSection(col, COL_DEFAULTS.get(col, 90))
            # If Size was just enabled and we have no sizes yet, ask for a scan.
            if (col == COL_SIZE and not self.model()._sizes
                    and callable(getattr(self, "on_sizes_requested", None))):
                self.on_sizes_requested()
        self._fit_name_to_width()   # Name re-absorbs/releases the freed width
        self.viewport().update()
        self._schedule_save()

    def _on_section_moved(self, logical, old_visual, new_visual):
        """Persist column order after a drag-reorder, but keep Mod Name pinned
        as the first column (it's the stretch column + hosts the menu button)."""
        if self._restoring or getattr(self, "_pinning_name", False):
            return
        h = self.header()
        if h.visualIndex(COL_NAME) != 0:
            # A move displaced Name from position 0 - snap it back.
            self._pinning_name = True
            h.moveSection(h.visualIndex(COL_NAME), 0)
            self._pinning_name = False
        self._position_column_menu_button()
        self.viewport().update()
        self._schedule_save()

    # ---- separator collapse/expand ---------------------------------------
    def load_separator_state(self):
        """Read collapsed/lock state for the active profile into the model and
        apply row hiding. Called by the window after a modlist reload."""
        collapsed, locks, colors, deploy_paths = set(), {}, {}, {}
        if self.profile_dir is not None:
            try:
                from Utils.profile_state import (
                    read_collapsed_seps, read_separator_locks,
                    read_separator_colors, read_separator_deploy_paths)
                collapsed = read_collapsed_seps(self.profile_dir)
                locks = read_separator_locks(self.profile_dir)
                colors = read_separator_colors(self.profile_dir)
                deploy_paths = read_separator_deploy_paths(self.profile_dir)
            except Exception:
                pass
        self.model().set_separator_state(collapsed, locks, colors, deploy_paths)
        self._apply_separator_spanning()
        self.apply_collapse()

    def _apply_separator_spanning(self):
        """Separator rows span all columns so the band + centred name + the
        right-side lock box use the full row width."""
        m = self.model()
        for r in range(m.rowCount()):
            self.setFirstColumnSpanned(r, self.rootIndex(),
                                       m.entry(r).is_separator)

    def apply_collapse(self):
        """Hide rows under a collapsed separator, the filter panel, OR the search
        box. When a search is active it OVERRIDES collapse (Tk parity - a match
        inside a collapsed separator is still revealed); the filter still applies.
        """
        flt = self._filter_hidden
        srch = self._search_hidden
        if self._searching:
            # Search drives visibility; collapse is ignored so matches surface.
            hidden = srch | flt
        else:
            hidden = self.model().hidden_rows() | flt
        # Only touch rows whose visibility actually changes - setRowHidden is
        # per-row layout work, and this runs per search keystroke.
        prev = getattr(self, "_applied_hidden", None)
        root = self.rootIndex()
        self.setUpdatesEnabled(False)
        try:
            if prev is None:
                for r in range(self.model().rowCount()):
                    self.setRowHidden(r, root, r in hidden)
            else:
                for r in prev - hidden:
                    self.setRowHidden(r, root, False)
                for r in hidden - prev:
                    self.setRowHidden(r, root, True)
        finally:
            self.setUpdatesEnabled(True)
        self._applied_hidden = hidden

    def set_filter_hidden(self, rows: set[int]) -> None:
        """Set the rows the filter panel wants hidden, then reapply visibility.
        Empty set clears the filter. Repaints the marker strip too."""
        self._filter_hidden = set(rows or ())
        self.apply_collapse()
        sb = self.verticalScrollBar()
        if sb is not None:
            sb.update()

    def set_search_hidden(self, rows: set[int], *, active: bool = None) -> None:
        """Set the rows the search box wants hidden, then reapply visibility.
        `active` marks whether a query is in effect (defaults to: any rows
        hidden) so collapse is bypassed only while searching."""
        self._search_hidden = set(rows or ())
        self._searching = bool(self._search_hidden) if active is None else active
        self.apply_collapse()
        sb = self.verticalScrollBar()
        if sb is not None:
            sb.update()

    def _on_double_click(self, index):
        if not index.isValid():
            return
        e = self.model().entry(index.row())
        from gui_qt.modlist_model import _PINNED_NAMES
        # A real (user) separator toggles collapse; the synthetic pinned
        # Overwrite / Root_Folder separators open their folder like a mod.
        if e.is_separator and e.name not in _PINNED_NAMES:
            self._toggle_collapse_row(index.row())
            return
        # Ignore double-clicks that land on the checkbox (the delegate toggles
        # enable there on single click; a double there is not an open request).
        if index.column() == COL_NAME:
            rect = self.visualRect(index)
            box = QRect(rect.left() + 6, rect.top(), 26, rect.height())
            pos = self.mapFromGlobal(self.cursor().pos())
            if box.contains(pos):
                return
        folder = self._resolve_entry_folder(index.row())
        if folder is not None:
            try:
                from Utils.xdg import xdg_open
                xdg_open(str(folder))
            except Exception:
                pass

    def _resolve_entry_folder(self, row: int):
        """On-disk folder for the entry at *row* (Path), or None for real
        separators / when unresolvable. Mirrors Tk _resolve_entry_folder:
        normal mods live under staging; the synthetic Overwrite / Root_Folder
        separators resolve to the game's effective paths."""
        from gui_qt.modlist_model import OVERWRITE_NAME, ROOT_FOLDER_NAME
        m = self.model()
        if not (0 <= row < m.rowCount()):
            return None
        e = m.entry(row)
        staging = getattr(self, "staging_dir", None)
        if not e.is_separator:
            return (staging / e.name) if staging is not None else None
        if e.name == OVERWRITE_NAME:
            game = getattr(self, "game", None)
            if game is not None:
                return game.get_effective_overwrite_path()
            return (staging.parent / "overwrite") if staging is not None else None
        if e.name == ROOT_FOLDER_NAME:
            game = getattr(self, "game", None)
            if game is not None:
                return game.get_effective_root_folder_path()
            return (staging.parent / "Root_Folder") if staging is not None else None
        return None

    def _toggle_collapse_row(self, row):
        self.model().toggle_collapse(row)
        self.apply_collapse()
        self._save_separator_state()
        self.viewport().update()

    def _toggle_lock_row(self, row):
        self.model().toggle_sep_lock(row)
        self._save_separator_state()
        self.viewport().update()

    def _lock_box_click(self, row: int, shift: bool):
        """Handle a click on a separator's lock box. Plain click toggles the one
        separator and records it as the range anchor; shift-click applies the
        anchor's last action (lock/unlock) to every separator in between."""
        m = self.model()
        if shift and 0 <= self._lock_anchor_row < m.rowCount():
            m.set_sep_lock_range(self._lock_anchor_row, row,
                                 self._lock_range_locking)
            # Leave the anchor put so the range can be re-extended.
        else:
            e = m.entry(row)
            was_locked = e is not None and m.is_sep_locked(e.display_name)
            m.toggle_sep_lock(row)
            self._lock_anchor_row = row
            self._lock_range_locking = not was_locked
        self._save_separator_state()
        self.viewport().update()

    def set_all_collapsed(self, collapsed: bool):
        """Collapse or expand every separator (Expand all / Collapse all)."""
        self.model().set_all_collapsed(collapsed)
        self.apply_collapse()
        self._save_separator_state()
        self.viewport().update()
        sb = self.verticalScrollBar()
        if sb is not None:
            sb.update()

    def _save_separator_state(self):
        if self.profile_dir is None:
            return
        try:
            from Utils.profile_state import (
                write_collapsed_seps, write_separator_locks)
            m = self.model()
            write_collapsed_seps(self.profile_dir, m._collapsed)
            write_separator_locks(self.profile_dir, m._sep_locks)
        except Exception as exc:
            print(f"[gui_qt] separator state save failed: {exc}", flush=True)

    # ---- sticky separator header -------------------------------------------
    def _sticky_sep_info(self) -> tuple[int, QRect] | None:
        """(row, band_rect) for the separator band pinned to the viewport top,
        or None when no band should show. The band mirrors the separator that
        governs the topmost visible rows, so the group the top mods belong to
        is always identifiable. The next separator scrolling into the top
        SEP_H px pushes the band up and out (standard sticky-header handoff).
        """
        m = self.model()
        n = m.rowCount()
        if n == 0:
            return None
        top_idx = self.indexAt(QPoint(2, 0))
        if not top_idx.isValid():
            return None
        top = top_idx.row()
        from gui_qt.modlist_model import _PINNED_NAMES

        def _real_sep(r: int) -> bool:
            e = m.entry(r)
            return e.is_separator and e.name not in _PINNED_NAMES

        sep = next((r for r in range(top, -1, -1) if _real_sep(r)), None)
        if sep is None:
            return None
        # A separator hidden by the filter panel ("Hide separators", or a
        # filter that dropped its whole block) must not pin a band - it would
        # paint over the first visible mod row.
        if self.isRowHidden(sep, self.rootIndex()):
            return None
        # visualRect() of an off-screen row can be empty, so don't measure the
        # separator directly: it needs a pinned stand-in iff it sits ABOVE the
        # top visible row, or IS the top row but partially scrolled off.
        if sep == top and self.visualRect(m.index(top, 0)).top() >= 0:
            return None
        y = 0
        root = self.rootIndex()
        for r in range(top, n):
            if r == sep or self.isRowHidden(r, root):
                continue
            rect = self.visualRect(m.index(r, 0))
            if rect.top() >= SEP_H:
                break
            if _real_sep(r):
                y = min(0, rect.top() - SEP_H)
                break
        return sep, QRect(0, y, self.viewport().width(), SEP_H)

    def _paint_sticky_separator(self):
        info = self._sticky_sep_info()
        if info is None:
            return
        row, band = info
        # Reuse the delegate's separator painting (band colour, custom colour,
        # highlight tint, arrow, label + count, lock box) on the pinned rect.
        opt = QStyleOptionViewItem()
        opt.rect = band
        p = QPainter(self.viewport())
        self.itemDelegate().paint(p, opt, self.model().index(row, COL_NAME))
        # Bottom edge so the band reads as floating over the scrolling rows.
        p.setPen(QPen(self.itemDelegate().c_border, 1))
        p.drawLine(band.left(), band.bottom(), band.right(), band.bottom())
        p.end()

    def _on_vscroll(self, value: int):
        # Repaint the band area plus however far the blit shifted the old
        # pixels, so the previous band never smears down the viewport.
        delta = abs(value - self._last_vscroll)
        self._last_vscroll = value
        self.viewport().update(0, 0, self.viewport().width(),
                               SEP_H + delta + 1)

    def _sticky_click(self, row: int, band: QRect, pos: QPoint, shift: bool = False):
        """A click on the pinned band acts like a click on the real separator
        row: the lock box toggles the lock, anywhere else toggles collapse
        (then scrolls the separator into view so the result is visible)."""
        delegate = self.itemDelegate()
        lock = getattr(delegate, "_lock_rect", None)
        if lock is not None and lock(band).contains(pos):
            self._lock_box_click(row, shift)
            return
        self._toggle_collapse_row(row)
        self.scrollTo(self.model().index(row, 0),
                      QAbstractItemView.PositionAtTop)

    # ---- fill width: Name absorbs leftover on window resize ---------------
    def showEvent(self, event):
        super().showEvent(event)
        self._fit_name_to_width()
        self._position_column_menu_button()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_name_to_width()
        self._position_column_menu_button()
        if hasattr(self, "_marker_strip"):
            self._reposition_marker_strip()

    def _fit_name_to_width(self):
        """Keep the table exactly filling the viewport on window resize.

        Growing window: Name absorbs the extra. Shrinking window: Name gives
        back down to its minimum, then the data columns cascade down to their
        own minimums (so columns never get cut off / overflow the panel)."""
        vp = self.viewport().width()
        if vp <= 0:
            return
        h = self.header()
        others = sum(self.columnWidth(c) for c in range(len(COLUMNS))
                     if c != COL_NAME and not self.isColumnHidden(c))
        target_name = vp - others

        if target_name >= NAME_MIN:
            if target_name != self.columnWidth(COL_NAME):
                h.resizeSection(COL_NAME, target_name)
            return

        # Not enough room even at Name's minimum: pin Name to min, then shrink
        # the data columns (right-to-left) toward their minimums to fit.
        h.resizeSection(COL_NAME, NAME_MIN)
        deficit = (NAME_MIN + others) - vp
        for c in reversed([c for c in range(len(COLUMNS))
                           if c != COL_NAME and not self.isColumnHidden(c)]):
            if deficit <= 0:
                break
            room = self.columnWidth(c) - COL_MINS.get(c, 60)
            if room <= 0:
                continue
            take = min(room, deficit)
            h.resizeSection(c, self.columnWidth(c) - take)
            deficit -= take

    # ---- cross-panel highlights ------------------------------------------
    def set_conflict_maps(self, overrides, overridden_by,
                          bsa_overrides, bsa_overridden_by):
        """Store the loose + BSA override maps so a mod selection can resolve
        which mods it beats (higher/green) and which beat it (lower/red)."""
        self._overrides = {k: set(v) for k, v in (overrides or {}).items()}
        self._overridden_by = {k: set(v) for k, v in (overridden_by or {}).items()}
        self._bsa_overrides = {k: set(v) for k, v in (bsa_overrides or {}).items()}
        self._bsa_overridden_by = {k: set(v)
                                   for k, v in (bsa_overridden_by or {}).items()}
        # Re-apply any active highlight against the fresh maps.
        self._refresh_self_highlights()

    def selectAll(self) -> None:
        """Ctrl+A → select every *visible*, non-separator mod row. Qt's default
        selects the whole model (hidden rows + separators too), which is wrong
        while a filter / hide-separators is active: separators that aren't shown
        must not become part of the selection."""
        m = self.model()
        sm = self.selectionModel()
        if m is None or sm is None:
            return
        root = self.rootIndex()
        from PySide6.QtCore import QItemSelection, QItemSelectionModel
        sel = QItemSelection()
        for r in range(m.rowCount()):
            if self.isRowHidden(r, root):
                continue
            if m.entry(r).is_separator:
                continue
            idx = m.index(r, 0)
            sel.select(idx, idx)
        sm.select(sel, QItemSelectionModel.ClearAndSelect
                  | QItemSelectionModel.Rows)

    def selected_mod_names(self) -> set[str]:
        """Names of the selected mods. A selected separator contributes all the
        mods in its block (Tk parity)."""
        m = self.model()
        names: set[str] = set()
        from gui_qt.modlist_model import _PINNED_NAMES
        for idx in self.selectionModel().selectedRows():
            e = m.entry(idx.row())
            if e.is_separator:
                if e.name in _PINNED_NAMES:
                    continue
                for r in m.sep_block_rows(idx.row()):
                    names.add(m.entry(r).name)
            else:
                names.add(e.name)
        return names

    def conflict_partners(self, names: set[str]) -> tuple[set[str], set[str]]:
        """For a set of mod names, return (higher, lower): the mods they beat
        (loose+BSA) and the mods that beat them, excluding the selection."""
        ov = getattr(self, "_overrides", {})
        ob = getattr(self, "_overridden_by", {})
        bov = getattr(self, "_bsa_overrides", {})
        bob = getattr(self, "_bsa_overridden_by", {})
        higher: set[str] = set()
        lower: set[str] = set()
        for n in names:
            higher |= ov.get(n, set()) | bov.get(n, set())
            lower |= ob.get(n, set()) | bob.get(n, set())
        higher -= names
        lower -= names
        return higher, lower

    def bsa_conflict_partners(self, names: set[str]) -> tuple[set[str], set[str]]:
        """Like conflict_partners but BSA-only - used to colour plugins (Tk only
        tints plugins for BSA conflicts, never loose-file ones)."""
        bov = getattr(self, "_bsa_overrides", {})
        bob = getattr(self, "_bsa_overridden_by", {})
        higher: set[str] = set()
        lower: set[str] = set()
        for n in names:
            higher |= bov.get(n, set())
            lower |= bob.get(n, set())
        higher -= names
        lower -= names
        return higher, lower

    def _refresh_self_highlights(self):
        """Recompute green/red tints from the current mod selection."""
        # View Requirements mode: the purple/blue requirement tints own the
        # modlist - don't let a conflict rebuild stomp them (the window sets
        # this while that tab is open).
        if getattr(self, "suppress_conflict_highlights", False):
            return
        names = self.selected_mod_names()
        if not names:
            return
        higher, lower = self.conflict_partners(names)
        self.model().set_highlights(higher=higher, lower=lower)

    def set_highlighted_mods(self, mods: set[str] | None):
        """Highlight (orange) the mods owning a plugin selected in the Plugins
        panel; clears the green/red conflict tint."""
        self.model().set_highlights(anchor=mods or set())
        # Scroll the first highlighted mod into view (Tk parity).
        if mods:
            m = self.model()
            for r in range(m.rowCount()):
                if not m.entry(r).is_separator and m.entry(r).name in mods:
                    self.scrollTo(m.index(r, 0),
                                  QAbstractItemView.PositionAtCenter)
                    break

    # ---- custom drag-reorder ---------------------------------------------
    def _visible_rows(self) -> list[int]:
        """Rows currently visible (not hidden under a collapsed separator)."""
        m = self.model()
        return [r for r in range(m.rowCount())
                if not self.isRowHidden(r, self.rootIndex())]

    def _drag_block_for(self, row: int) -> list[int] | None:
        """The rows to carry when a drag starts on *row*, or None if *row* is
        un-draggable (boundary separator / locked mod). A locked separator
        carries its whole block; everything else carries the selected rows (or
        just itself)."""
        m = self.model()
        e = m.entry(row)
        from gui_qt.modlist_model import _PINNED_NAMES
        if e.name in _PINNED_NAMES:
            return None
        if not e.is_separator and e.locked:
            return None
        # Only a LOCKED separator carries its whole block (Tk parity: collapse
        # affects visibility, never the drag payload). An unlocked separator -
        # collapsed or not - moves alone: it just re-marks where a group
        # begins, and membership recomputes from the drop position.
        if e.is_separator and m.is_sep_locked(e.display_name):
            return [row] + list(m.sep_block_rows(row))
        # Multi-select: carry every selected, draggable row if this row is part
        # of the selection; otherwise just this row.
        sel = sorted({i.row() for i in self.selectionModel().selectedRows()})
        if row in sel and len(sel) > 1:
            carry = [r for r in sel
                     if m.entry(r).name not in _PINNED_NAMES
                     and not (not m.entry(r).is_separator and m.entry(r).locked)]
            return carry or [row]
        return [row]

    def mousePressEvent(self, event):
        # A press on the sticky separator band must not reach the row painted
        # underneath it - consume it and remember the band's row for release.
        if not self._drag_active:
            info = self._sticky_sep_info()
            if info is not None and info[1].contains(event.position().toPoint()):
                if event.button() == Qt.LeftButton:
                    self._sticky_press = info[0]
                event.accept()
                return
        if event.button() == Qt.MiddleButton:
            idx = self.indexAt(event.position().toPoint())
            if idx.isValid():
                e = self.model().entry(idx.row())
                if not e.is_separator:
                    # Store-specific pages win over the Nexus fallback: a mod
                    # installed from Thunderstore/mod.io has no Nexus page, and
                    # a mod carrying both should open where it came from.
                    from gui_qt.modlist_menu import (
                        _is_thunderstore_mod, _modio_url, _open_on_modio,
                        _open_on_nexus, _open_on_thunderstore)
                    if _is_thunderstore_mod(self, e.name):
                        _open_on_thunderstore(self, e.name)
                    elif _modio_url(self, e.name):
                        _open_on_modio(self, e.name)
                    else:
                        _open_on_nexus(self, e.name)
            return
        if event.button() == Qt.LeftButton:
            idx = self.indexAt(event.position().toPoint())
            self._press_row = idx.row() if idx.isValid() else -1
            self._press_pos = event.position().toPoint()
            # A real (collapsible) separator is never selectable: clicking one
            # only expands/collapses it (handled by the delegate on release).
            # Skip the base press so Qt doesn't change the selection - but keep
            # _press_row/_press_pos above so a press-and-drag still reorders it.
            if idx.isValid():
                e = self.model().entry(idx.row())
                from gui_qt.modlist_model import _PINNED_NAMES
                if e.is_separator and e.name not in _PINNED_NAMES:
                    event.accept()
                    return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        # Swallow double-clicks on the sticky band (the single-click toggle
        # already ran); the base handler would act on the covered row.
        if not self._drag_active:
            info = self._sticky_sep_info()
            if info is not None and info[1].contains(event.position().toPoint()):
                event.accept()
                return
        super().mouseDoubleClickEvent(event)

    def mouseMoveEvent(self, event):
        if not (event.buttons() & Qt.LeftButton) or self._press_row < 0:
            super().mouseMoveEvent(event)
            return
        if not self._drag_active:
            if self._press_pos is None:
                return
            if (event.position().toPoint() - self._press_pos).manhattanLength() \
                    < self._DRAG_THRESHOLD:
                return
            m = self.model()
            key, _asc = m.sort_state()
            if key and not m.reverse_mode_active:
                # Tk parity: dragging under a non-priority sort clears the
                # sort first (display snaps to natural order), then the drag
                # proceeds normally. Re-anchor the press to the entry's new row
                # (selection follows via the persistent-index remap).
                pressed = m.entry(self._press_row)
                self._apply_sort(-1, None, True)
                row = next((r for r in range(m.rowCount())
                            if m.entry(r) is pressed), -1)
                if row < 0:
                    self._press_row = -1
                    return
                self._press_row = row
            block = self._drag_block_for(self._press_row)
            if block is None:
                self._press_row = -1
                return
            self._drag_active = True
            self._drag_rows = block
            self.setCursor(Qt.ClosedHandCursor)
        # Live drag: track cursor, compute drop slot, run autoscroll.
        self._last_mouse_y = event.position().toPoint().y()
        self._update_drop_slot(self._last_mouse_y)
        if not self._scroll_timer.isActive():
            self._scroll_timer.start()
        self.viewport().update()

    def mouseReleaseEvent(self, event):
        if self._drag_active:
            self._scroll_timer.stop()
            self._commit_drop()
            self._drag_active = False
            self._drag_rows = []
            self._drop_slot = -1
            self.unsetCursor()
            self.viewport().update()
            self._press_row = -1
            return
        # Release of a press that started on the sticky separator band.
        if self._sticky_press is not None:
            row, self._sticky_press = self._sticky_press, None
            info = self._sticky_sep_info()
            pos = event.position().toPoint()
            if (info is not None and info[0] == row
                    and info[1].contains(pos)):
                shift = bool(event.modifiers() & Qt.ShiftModifier)
                self._sticky_click(row, info[1], pos, shift)
            self._press_row = -1
            event.accept()
            return
        # A click (no drag) on a real separator toggles its collapse (arrow or
        # anywhere on the band) or its lock (lock box). Its press was consumed
        # in mousePressEvent so the base view never routes it to the delegate.
        if event.button() == Qt.LeftButton and self._press_row >= 0:
            row = self._press_row
            self._press_row = -1
            shift = bool(event.modifiers() & Qt.ShiftModifier)
            if self._handle_separator_click(row, event.position().toPoint(),
                                            shift):
                event.accept()
                return
        self._press_row = -1
        super().mouseReleaseEvent(event)

    # Hovering a mod's Name cell shows its preferred store summary tooltip
    # (Nexus first, then Thunderstore when Nexus has no description).
    # Width-capped (Qt doesn't word-wrap plain-text tooltips, so a long
    # description is char-wrapped to keep the tip from stretching across the
    # screen) and length-capped. Gated by the show_summary_tooltips setting.
    _TOOLTIP_WRAP_CHARS = 60
    _TOOLTIP_MAX_CHARS = 500

    def viewportEvent(self, event):
        # Handle the Name-column description tooltip ourselves; anything else
        # (flags / conflicts cell tooltips) falls through to the delegate's
        # helpEvent via the base implementation.
        if event.type() == QEvent.ToolTip and (
                self._lock_tooltip(event) or self._name_tooltip(event)):
            return True
        return super().viewportEvent(event)

    def _lock_tooltip(self, help_event) -> bool:
        """Tk parity: hovering a separator's lock box explains what it does.
        Returns True if shown."""
        idx = self.indexAt(help_event.pos())
        if not idx.isValid():
            return False
        m = self.model()
        row = idx.row()
        e = m.entry(row) if 0 <= row < m.rowCount() else None
        from gui_qt.modlist_model import _PINNED_NAMES
        if e is None or not e.is_separator or e.name in _PINNED_NAMES:
            return False
        delegate = self.itemDelegate()
        lock = getattr(delegate, "_lock_rect", None)
        if lock is None:
            return False
        # Separator rows span the full width; build a full-width row rect so the
        # lock rect (computed from its right edge) lands where the delegate drew.
        row_rect = self.visualRect(m.index(row, COL_NAME))
        row_rect.setLeft(0)
        row_rect.setWidth(self.viewport().width())
        lk = lock(row_rect)
        if not lk.contains(help_event.pos()):
            return False
        QToolTip.showText(
            help_event.globalPos(),
            self.tr("Lock Separator - Mods in this separator are attached to it"),
            self, lk)
        return True

    def _name_tooltip(self, help_event) -> bool:
        """Show the hovered mod's description tooltip. Returns True if shown."""
        try:
            from Utils.ui_config import load_show_summary_tooltips
            if not load_show_summary_tooltips():
                return False
        except Exception:
            return False
        idx = self.indexAt(help_event.pos())
        if not idx.isValid() or idx.column() != COL_NAME:
            return False
        m = self.model()
        row = idx.row()
        if not (0 <= row < m.rowCount()):
            return False
        entry = m.entry(row)
        if entry is None or entry.is_separator:
            return False
        desc = m.description(entry.name)
        if not desc:
            return False
        if len(desc) > self._TOOLTIP_MAX_CHARS:
            desc = desc[:self._TOOLTIP_MAX_CHARS].rstrip() + "…"
        import textwrap
        wrapped = "\n".join(
            textwrap.fill(line, width=self._TOOLTIP_WRAP_CHARS,
                          break_long_words=False, break_on_hyphens=False)
            for line in desc.splitlines()) or desc
        # Pass the name-cell rect so Qt hides the tip once the cursor leaves it.
        QToolTip.showText(help_event.globalPos(), wrapped, self,
                          self.visualRect(idx))
        return True

    def _handle_separator_click(self, row: int, pos, shift: bool = False) -> bool:
        """If *row* is a real (collapsible) separator, toggle its lock (when the
        release is on the lock box) or its collapse (anywhere else on the band).
        Returns True when handled."""
        m = self.model()
        if not (0 <= row < m.rowCount()):
            return False
        e = m.entry(row)
        from gui_qt.modlist_model import _PINNED_NAMES
        if not e.is_separator or e.name in _PINNED_NAMES:
            return False
        delegate = self.itemDelegate()
        lock = getattr(delegate, "_lock_rect", None)
        row_rect = self.visualRect(m.index(row, COL_NAME))
        if lock is not None and lock(row_rect).contains(pos):
            self._lock_box_click(row, shift)
        else:
            self._toggle_collapse_row(row)
        return True

    def _update_drop_slot(self, y: int):
        """Compute the model row the block would insert *before*, from cursor Y.
        Snaps to the gap nearest the cursor among visible rows."""
        m = self.model()
        n = m.rowCount()
        vis = self._visible_rows()
        if not vis:
            self._drop_slot = 0
            return
        # Resolve the drop from the ON-SCREEN rows only. Two traps:
        #  1) A row scrolled out of the viewport has an EMPTY visualRect
        #     (top==bottom==0). vis[] includes such rows (it only excludes
        #     collapse-hidden ones), so when Root Folder is scrolled below the
        #     fold, its rect.bottom()==0 made "y >= last.bottom()" true for any
        #     y - forcing the slot onto the boundary every move. That was the
        #     random flicker.
        #  2) Consecutive row rects can leave a 1px seam; requiring
        #     top() <= y < bottom() would miss a y in the seam.
        # Build the list of rows actually painted, in viewport order, then pick
        # by seam-tolerant bottom() comparison.
        vp_h = self.viewport().height()
        onscreen = []
        for r in vis:
            rect = self.visualRect(m.index(r, 0))
            if rect.height() > 0 and rect.bottom() > 0 and rect.top() < vp_h:
                onscreen.append((r, rect))
        if not onscreen:
            self._drop_slot = max(0, min(self._drop_slot, n))
            return
        first_r, first_rect = onscreen[0]
        last_r, last_rect = onscreen[-1]
        if y < first_rect.top():
            slot = first_r
        elif y >= last_rect.bottom():
            slot = last_r + 1
        else:
            slot = None
            for r, rect in onscreen:
                if y < rect.bottom():        # seam belongs to the nearer row
                    slot = r if y < rect.center().y() else r + 1
                    break
            if slot is None:                 # defensive: treat as below last
                slot = last_r + 1
        # Clamp the indicator to the valid-drop range so the blue line never
        # renders at/below the Root Folder boundary (or above Overwrite). In
        # reverse mode boundaries flip in display space and move_block_display
        # handles its own clamping, so leave the full [0, n] range there.
        lo, hi = (0, n)
        if not getattr(m, "reverse_mode_active", False):
            lo, hi = m.movable_span()
        slot = max(lo, min(slot, hi))
        # Never leave the slot on a hidden row (inside a collapsed block):
        # visualRect() of a hidden row is empty (top()==0), which would draw
        # the indicator at the viewport top instead of under the cursor. Snap
        # to the next visible row so the line and the drop agree.
        if 0 < slot < n and self.isRowHidden(slot, self.rootIndex()):
            nxt = next((r for r in vis if r >= slot), None)
            slot = nxt if nxt is not None else n
        self._drop_slot = slot

    def _commit_drop(self):
        if not self._drag_rows or self._drop_slot < 0:
            return
        dest = self._drop_slot
        src = self._drag_rows
        m = self.model()
        # Tk parity: every drag - a mod, a lone separator, or a locked
        # separator carrying its block - drops exactly at the released slot,
        # even mid-group (the host group splits there and its remaining mods
        # fall under the dragged block). Only the Overwrite/Root boundaries
        # clamp (movable_span in _update_drop_slot / move_block).
        if m.reverse_mode_active:
            # Reverse-priority drag: resolve the drop in display space with the
            # Tk inverted-mode semantics, then the model uninverts + saves.
            hidden = {r for r in range(m.rowCount())
                      if self.isRowHidden(r, self.rootIndex())}
            m.move_block_display(src, dest, hidden=hidden)
        else:
            m.move_block(src, dest)
        # After a structural change, re-apply spanning + collapse hiding.
        self._apply_separator_spanning()
        self.apply_collapse()

    # ---- external drop: archives from the Downloads tab --------------------
    def _is_archive_drag(self, event) -> bool:
        return (self.on_archives_dropped is not None
                and event.mimeData().hasFormat(ARCHIVE_DROP_MIME))

    def dragEnterEvent(self, event):
        if self._is_archive_drag(event):
            self._extern_drop = True
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if self._extern_drop:
            # Reuse the internal-reorder slot maths + blue indicator, and the
            # same edge autoscroll (Qt DnD keeps delivering moves while the
            # timer scrolls underneath the cursor).
            self._last_mouse_y = int(event.position().y())
            self._update_drop_slot(self._last_mouse_y)
            if not self._scroll_timer.isActive():
                self._scroll_timer.start()
            self.viewport().update()
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dragLeaveEvent(self, event):
        if self._extern_drop:
            self._end_extern_drop()
            return
        super().dragLeaveEvent(event)

    def dropEvent(self, event):
        if self._extern_drop:
            self._update_drop_slot(int(event.position().y()))
            slot = self._drop_slot
            raw = bytes(event.mimeData().data(ARCHIVE_DROP_MIME)).decode("utf-8")
            paths = [p for p in raw.split("\n") if p]
            self._end_extern_drop()
            event.acceptProposedAction()
            if paths and slot >= 0:
                self.on_archives_dropped(paths, slot)
            return
        super().dropEvent(event)

    def _end_extern_drop(self):
        self._extern_drop = False
        self._drop_slot = -1
        self._scroll_timer.stop()
        self.viewport().update()

    # ---- continuous autoscroll (Tk cadence: fast, proportional to depth) ---
    def _autoscroll_tick(self):
        if not (self._drag_active or self._extern_drop):
            self._scroll_timer.stop()
            return
        h = self.viewport().height()
        y = self._last_mouse_y
        zone = self._scroll_zone
        bar = self.verticalScrollBar()
        step = 0
        if y < zone:
            depth = (zone - y) / zone               # 0..1
            step = -int(2 + depth * 22)             # up to ~24 px/tick
        elif y > h - zone:
            depth = (y - (h - zone)) / zone
            step = int(2 + depth * 22)
        # Only act when the bar can actually move that direction. At the very
        # bottom (or top) setValue() clamps to a no-op, but re-running
        # _update_drop_slot + repaint every tick against a static viewport lets
        # sub-pixel mouse jitter oscillate the drop slot - the indicator
        # flickers near the Root Folder boundary. Skip the churn when pinned.
        at_edge = (step < 0 and bar.value() <= bar.minimum()) or \
                  (step > 0 and bar.value() >= bar.maximum())
        if step and not at_edge:
            bar.setValue(bar.value() + step)
            self._update_drop_slot(y)
            self.viewport().update()

    def paintEvent(self, event):
        super().paintEvent(event)
        # Sticky separator band (hidden during a drag - it would cover the
        # drop zone while autoscrolling toward the top). An external archive
        # drag (Downloads tab) paints the same indicator.
        dragging = self._drag_active or self._extern_drop
        if not dragging:
            self._paint_sticky_separator()
        if not dragging or self._drop_slot < 0:
            return
        m = self.model()
        n = m.rowCount()
        # A slot that lands ON a pinned boundary row (Root Folder / Overwrite)
        # is a valid drop (the gap between the last mod and Root Folder), but
        # anchoring the line to the boundary row's top() flickers: during
        # autoscroll the boundary rect can be measured mid-layout, so the same
        # slot paints at two heights on consecutive frames. Anchor those to the
        # previous movable row's bottom instead - one stable y for the gap.
        from gui_qt.modlist_model import _PINNED_NAMES
        on_boundary = (0 <= self._drop_slot < n
                       and m.entry(self._drop_slot).name in _PINNED_NAMES)
        # Anchor the line to visible rows only: visualRect() of a hidden row
        # (collapsed block) is empty, which would paint the line at y=0.
        if (not on_boundary and self._drop_slot < n
                and not self.isRowHidden(self._drop_slot, self.rootIndex())):
            y = self.visualRect(m.index(self._drop_slot, 0)).top()
        else:
            vis = self._visible_rows()
            prev = next((r for r in reversed(vis) if r < self._drop_slot), None)
            if prev is None:
                return
            y = self.visualRect(m.index(prev, 0)).bottom()
        from gui_qt.theme_qt import active_palette, _c
        p = QPainter(self.viewport())
        pen = QPen(QColor(_c(active_palette(), "HIGHLIGHT_DRAG")))
        pen.setWidth(2)
        p.setPen(pen)
        p.drawLine(0, y, self.viewport().width(), y)
        p.end()

    # ---- context menu -----------------------------------------------------
    def _on_context_menu(self, pos):
        from gui_qt.modlist_menu import show_context_menu
        # Over the sticky band, target the pinned separator, not the row under it.
        info = self._sticky_sep_info() if not self._drag_active else None
        if info is not None and info[1].contains(pos):
            index = self.model().index(info[0], 0)
        else:
            index = self.indexAt(pos)
        if index.isValid():
            show_context_menu(self, self.viewport().mapToGlobal(pos), index)

    # ---- column-state persistence (keyed by logical column name) ----------
    def _schedule_save(self):
        if not self._restoring:
            self._save_timer.start()

    def _save_column_state(self):
        h = self.header()
        widths = {COLUMNS[c]: self.columnWidth(c) for c in range(len(COLUMNS))}
        order = [COLUMNS[h.logicalIndex(v)] for v in range(len(COLUMNS))]
        hidden = {COLUMNS[c] for c in range(len(COLUMNS)) if self.isColumnHidden(c)}
        sc = h.sortIndicatorSection()
        sort_col = COLUMNS[sc] if 0 <= sc < len(COLUMNS) else None
        ascending = h.sortIndicatorOrder() == Qt.AscendingOrder
        column_state.save_state(widths, order, hidden, sort_col, ascending)

    def _restore_column_state(self):
        st = column_state.load_state()
        if not (st["widths"] or st["order"] or st["hidden"] or st["sort_col"]):
            # Fresh INI: apply Tk-parity first-run hidden columns (Category /
            # Installed / Size). The user's later choices persist over this.
            for col in _FIRST_RUN_HIDDEN:
                self.setColumnHidden(col, True)
            return
        name_to_col = {n: i for i, n in enumerate(COLUMNS)}
        for name, w in st["widths"].items():
            if name in name_to_col and name != "Mod Name":  # name stays stretch
                self.setColumnWidth(name_to_col[name], w)
        for name in st["hidden"]:
            if name in name_to_col:
                self.setColumnHidden(name_to_col[name], True)
        # A column added after the state was saved (absent from the persisted
        # order) keeps its first-run default - otherwise e.g. the new Author
        # column would pop up visible for every existing user.
        for col in _FIRST_RUN_HIDDEN:
            if st["order"] and COLUMNS[col] not in st["order"]:
                self.setColumnHidden(col, True)
        h = self.header()
        for visual, name in enumerate(st["order"]):
            if name in name_to_col:
                cur = h.visualIndex(name_to_col[name])
                if cur != -1 and cur != visual:
                    h.moveSection(cur, visual)
        if st["sort_col"] in name_to_col:
            col = name_to_col[st["sort_col"]]
            order = Qt.AscendingOrder if st["ascending"] else Qt.DescendingOrder
            self.header().setSortIndicator(col, order)
            # Restore the live sort. The model is empty at this point - the
            # first set_entries() re-derives the display with this sort.
            key = _COL_TO_SORTKEY.get(col)
            if key:
                self.model().set_sort(key, st["ascending"])
