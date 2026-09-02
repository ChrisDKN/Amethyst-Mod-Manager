"""Saves -the plugins-panel sub-tab listing the game's save folders.

Locations come from the Ludusavi manifest (Utils.saves.paths); a Bethesda
profile-saves folder is listed alongside them. Read-only.

Like the other sub-tabs it builds lazily and scans on a daemon thread (save
folders live in Wine prefixes on slow storage). Folders list their contents
the first time they are opened, not up front. Selecting a Bethesda save fills
the details pane (gui_qt.save_preview) from a worker, with a generation
counter dropping results whose row is no longer selected. Search and Filters
hide rows in place, so an expanded folder keeps its listing.

Read-only apart from the right-click menu (delete a save, copy/move one into a
profile saves folder) and the footer's Export/Import.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QTreeWidget, QTreeWidgetItem,
    QAbstractItemView, QSizePolicy, QSplitter,
)

from gui_qt.icons import icon
from gui_qt.theme_qt import active_palette, bind_theme, _c
from gui_qt.worker import run_in_worker
from gui_qt.i18n import profile_display
from Utils.wine.manager import fmt_size, get_dir_size
from Utils.saves.paths import matches_patterns, save_paths_for_game
from Utils.environment.xdg import xdg_open

# Entries listed per folder. A folder with more than this many children is
# truncated -some games keep thousands of autosaves and the tree would stall
# the UI thread building rows nobody scrolls to.
_MAX_ENTRIES = 500

_DATE_FMT = "%d %b %Y  %H:%M"

# Starting height of the save-details pane, in pixels. Enough for the
# screenshot plus a handful of metadata rows without swallowing the list.
_PREVIEW_H = 230

# Expand-arrow size -same as the Data / Mod Files / Text Files delegates.
_ARROW_SZ = 20

_COL_NAME, _COL_TYPE, _COL_SIZE, _COL_DATE = 0, 1, 2, 3

# Row groups, pinned above one another whatever the sort column is: locations,
# then folders, then files, with notice rows ("(empty)", "…") always last.
_GRP_LOCATION, _GRP_DIR, _GRP_FILE, _GRP_INFO = 0, 1, 2, 3

# Payload roles on tree items.
_PATH_ROLE = Qt.UserRole + 1     # the path a row points at
_DIR_ROLE = Qt.UserRole + 2      # True on a folder row (expandable)
_LOADED_ROLE = Qt.UserRole + 3   # True once a folder's children are listed
_INFO_ROLE = Qt.UserRole + 4     # True on a notice row ("(empty)", "…")


_spacer = None


def _spacer_icon():
    """Transparent icon the size of the expand arrow, to align file rows."""
    global _spacer
    if _spacer is None:
        from PySide6.QtGui import QIcon, QPixmap
        pixmap = QPixmap(_ARROW_SZ, _ARROW_SZ)
        pixmap.fill(Qt.transparent)
        _spacer = QIcon(pixmap)
    return _spacer


def _ext_of(name: str) -> str:
    """Filter key for a file name: its lowercased extension, or "(none)"."""
    return Path(name).suffix.lower() or "(none)"


class _SaveItem(QTreeWidgetItem):
    """A row that sorts on its real values -bytes and mtime, not the formatted
    text, and file type by extension.

    Built detached and added in bulk: with sorting enabled Qt places an item the
    moment it gets a parent, so a row parented before its text is set would sort
    as an empty row."""

    def __init__(self, group: int, name: str = "", ext: str = "",
                 size: int = 0, mtime: float = 0.0, order: int = 0):
        super().__init__()
        self._group = group
        self._sort_name = name.casefold()
        self._ext = ext
        self._size = size
        self._mtime = mtime
        self._order = order

    def _key(self, column: int):
        if column == _COL_TYPE:
            # Extensionless files group at the END -"(none)" would otherwise
            # sort ahead of every real extension on the punctuation.
            return (self._ext if self._ext != "(none)" else "￿",
                    self._sort_name)
        if column == _COL_SIZE:
            return (self._size, self._sort_name)
        if column == _COL_DATE:
            return (self._mtime, self._sort_name)
        return (self._sort_name,)

    def __lt__(self, other):
        tree = self.treeWidget()
        if tree is None or not isinstance(other, _SaveItem):
            return super().__lt__(other)
        # Qt sorts descending by swapping the operands, so anything held in a
        # fixed position has to flip with the direction to stay put.
        desc = tree.header().sortIndicatorOrder() == Qt.DescendingOrder
        if self._group != other._group:
            return self._group > other._group if desc \
                else self._group < other._group
        if self._group == _GRP_LOCATION:
            # Location rows are the resolver's own order (profile saves first),
            # not data to sort -the sort applies to the saves inside them.
            return self._order > other._order if desc else self._order < other._order
        return self._key(tree.sortColumn()) < other._key(tree.sortColumn())


def _list_dir(path, log=None, patterns=()) -> tuple[list, bool]:
    """One folder's ``(name, is_dir, size, mtime)`` entries, newest first (so a
    truncated listing keeps the saves that matter), plus whether it truncated.

    *patterns* (a location's name globs, e.g. ``*.sav``) filters this folder to
    the entries the manifest calls saves; folders opened underneath it are
    listed whole."""
    try:
        children = list(os.scandir(path))
    except OSError as exc:
        if log:
            log(f"[saves] cannot read {path}: {exc}")
        return [], False

    listing = []
    for entry in children:
        if not matches_patterns(entry.name, patterns):
            continue
        try:
            listing.append((entry.name, entry.is_dir(follow_symlinks=False),
                            entry.stat(follow_symlinks=False).st_mtime, entry.path))
        except OSError:
            continue
    listing.sort(key=lambda e: e[2], reverse=True)
    truncated = len(listing) > _MAX_ENTRIES

    entries = []
    for name, is_dir, mtime, full in listing[:_MAX_ENTRIES]:
        try:
            size = get_dir_size(Path(full)) if is_dir else os.stat(full).st_size
        except OSError:
            size = 0
        entries.append((name, is_dir, size, mtime))
    return entries, truncated


class _ElidedPathLabel(QLabel):
    """Middle-elides an overlong path rather than widening the panel."""

    def __init__(self):
        super().__init__()
        self._full = ""
        sp = self.sizePolicy()
        sp.setHorizontalPolicy(QSizePolicy.Ignored)
        self.setSizePolicy(sp)

    def setText(self, text):  # noqa: N802 (Qt override)
        self._full = text
        self.setToolTip(text)
        self._apply_elide()

    def _apply_elide(self):
        fm = self.fontMetrics()
        width = max(50, self.width() - 20)
        super().setText(fm.elidedText(self._full, Qt.ElideMiddle, width))

    def resizeEvent(self, event):  # noqa: N802 (Qt override)
        super().resizeEvent(event)
        self._apply_elide()


class SavesView(QWidget):
    """Sub-tab body listing the game's save folders and their contents."""

    scan_done = Signal(object)
    #: One expanded folder's listing, from the worker that read it.
    dir_scan_done = Signal(object)
    #: Footer summary text ("2 location(s) · 41 entries · 1.2 GB").
    status_changed = Signal(str)
    #: True when a row is selected, so the footer's Open folder button syncs.
    selection_changed = Signal(bool)
    #: False while an export/import runs, so the footer disables its buttons.
    busy_changed = Signal(bool)
    #: (ok, message) from an export/import worker → UI thread.
    transfer_done = Signal(bool, str)
    #: The pickers' callbacks fire on a portal WORKER thread; marshal the
    #: chosen path to the GUI thread before touching any widget.
    export_path_picked = Signal(object)
    import_path_picked = Signal(object)
    #: Transient one-line notice for the footer (progress / result).
    notify = Signal(str, str)
    #: A parsed save header for the details pane, from the parse worker.
    preview_done = Signal(object)
    #: The listed file types changed, so the Filters panel can restock its list.
    filetypes_changed = Signal()

    def __init__(self, log_fn=None):
        super().__init__()
        self._log = log_fn or (lambda _m: None)
        self._game = None
        self._profile_name = ""
        self._dirty = True
        self._is_visible = False
        self._scanning = False
        self._busy = False
        self._last_pct = -1
        # Last scan result, so export/import know which folder a row belongs to.
        self._scanned: list[dict] = []
        # Folder rows waiting on a listing: token → item. _tree_gen bumps on
        # every rebuild so a listing landing after its rows died is dropped.
        self._pending: dict[int, object] = {}
        self._next_token = 0
        self._tree_gen = 0
        # Details pane: generation counter so a slow parse can't overwrite a
        # newer selection; height persists for the session only.
        self._preview_gen = 0
        self._preview_height = _PREVIEW_H
        # Rows hide in place rather than rebuild, so an expanded folder keeps
        # its listing across a search.
        self._search = ""
        self._search_exts: frozenset = frozenset()
        self._inc_exts: set = set()
        self._exc_exts: set = set()
        self._ext_counts: dict[str, int] = {}
        self._search_timer = None
        # Unfiltered footer summary, restored when the filter is cleared.
        self._status_base = ""

        self.setObjectName("SavesView")
        self._build()
        self.scan_done.connect(self._on_scan_done)
        self.dir_scan_done.connect(self._on_dir_scan_done)
        self.preview_done.connect(self._on_preview_done)
        self.export_path_picked.connect(self._on_export_path_picked)
        self.import_path_picked.connect(self._on_import_path_picked)
        self.transfer_done.connect(self._on_transfer_done)
        bind_theme(self, roles={"TEXT_DIM", "ACCENT", "DROPDOWN_ARROW"})
        # Warm the manifest off-thread -app.py's _saves_supported() would
        # otherwise pay the ~70 ms first load on the UI thread mid game-switch.
        from Utils.saves.ludusavi import data_info
        run_in_worker(data_info, None, name="ludusavi-warm")

    def refresh_theme(self, p: dict) -> None:
        """Recolour existing item brushes/icons without rebuilding the tree."""
        tree = getattr(self, "_tree", None)
        if tree is None:
            return

        def walk(item):
            if item.data(0, _INFO_ROLE):
                item.setForeground(0, self._brush(_c(p, "TEXT_DIM")))
            elif isinstance(item, _SaveItem) and item._group == _GRP_LOCATION:
                item.setForeground(0, self._brush(_c(p, "ACCENT")))
            if item.data(0, _DIR_ROLE) or item.childCount():
                self._sync_arrow(item)
            for i in range(item.childCount()):
                walk(item.child(i))

        for i in range(tree.topLevelItemCount()):
            walk(tree.topLevelItem(i))
        tree.viewport().update()

    # ---- lazy-build plumbing (mirrors the other sub-tabs) -----------------
    def configure(self, game, profile_name: str = ""):
        """Point the tab at a game/profile; contents rebuild when next shown."""
        self._game = game
        self._profile_name = profile_name or ""
        self.mark_dirty()

    def set_visible_tab(self, visible: bool):
        was_visible = self._is_visible
        self._is_visible = visible
        if visible and self._dirty:
            self.refresh()
        # The details pane is wide (screenshot + plugin list). Left mounted
        # while this tab is hidden it still pins the whole panel's minimum
        # width -a QStackedWidget sizes to its largest page, showing or not.
        # So drop it on the way out and re-parse on the way back in.
        if not visible and was_visible:
            self._preview_gen += 1      # kill any parse still in flight
            self._hide_preview()
        elif visible and not was_visible:
            self._preview_for(self.selected_path())

    def mark_dirty(self):
        self._dirty = True
        if self._is_visible:
            self.refresh()

    # ---- layout -----------------------------------------------------------
    def _build(self):
        p = active_palette()
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        # Header line: the resolved save path.
        self._info = _ElidedPathLabel()
        self._info.setStyleSheet(
            f"color:{_c(p,'TEXT_MAIN')}; padding:6px 8px 4px 8px;")
        v.addWidget(self._info)

        self._tree = QTreeWidget()
        self._tree.setColumnCount(4)
        self._tree.setHeaderLabels([self.tr("Name"), self.tr("File type"),
                                    self.tr("Size"), self.tr("Modified")])
        self._tree.setRootIsDecorated(False)   # we draw our own arrow
        self._tree.setIndentation(_ARROW_SZ + 4)
        self._tree.setUniformRowHeights(True)
        # Elide the middle so drive and save folder both stay readable.
        self._tree.setTextElideMode(Qt.ElideMiddle)
        self._tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self._tree.setAlternatingRowColors(True)
        self._tree.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # TkStyleHeader gives the modlist's drag-resize (one side grows, the
        # other shrinks, total constant). Size/Modified size to their content,
        # Name takes the rest -no dead space trailing the date.
        from gui_qt.modlist_header import TkStyleHeader
        fm = self._tree.fontMetrics()
        date_w = fm.horizontalAdvance(time.strftime(_DATE_FMT, time.localtime())) + 24
        size_w = fm.horizontalAdvance("1000.0 MB") + 24
        type_w = fm.horizontalAdvance(self.tr("Folder") + "MMM") + 24
        col_mins = {_COL_NAME: 140, _COL_TYPE: 60, _COL_SIZE: 60, _COL_DATE: 90}
        col_defaults = {_COL_TYPE: type_w, _COL_SIZE: size_w, _COL_DATE: date_w}
        hdr = TkStyleHeader(self._tree, col_mins, col_defaults)
        self._tree.setHeader(hdr)
        hdr.setMinimumSectionSize(min(col_mins.values()))
        hdr.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        for col, wdt in col_defaults.items():
            self._tree.setColumnWidth(col, wdt)
        self._name_min = col_mins[_COL_NAME]
        # Sorting: file type first, so saves of a kind sit together (a Bethesda
        # folder interleaves .ess/.skse/.bak per save otherwise). Click a header
        # to re-sort; Qt drives it, sizes and dates compare as numbers via
        # _SaveItem. Enabled AFTER setHeader() -that call resets the header's
        # clickable flag to follow the sorting state.
        self._tree.setSortingEnabled(True)
        self._tree.sortByColumn(_COL_TYPE, Qt.AscendingOrder)
        hdr.setSortIndicatorShown(True)
        self._tree.viewport().installEventFilter(self)
        self._tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._on_context_menu)
        self._tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        self._tree.itemSelectionChanged.connect(self._on_selection_changed)
        # Click anywhere on a row to expand/collapse, like a modlist separator
        # -but the row stays selectable, since Export/Import key off it.
        self._tree.itemClicked.connect(self._on_item_clicked)
        # Qt's branch indicator doesn't match the other tabs -carry their
        # arrow.png/right.png as the row icon and swap it on toggle.
        self._tree.itemExpanded.connect(self._on_item_expanded)
        self._tree.itemCollapsed.connect(self._sync_arrow)
        # 33px rows to match plugin_view.ROW_H. setRootIsDecorated(False) only
        # drops the TOP level's marker, so blank the branch column outright or a
        # nested folder draws Qt's triangle beside our arrow.
        self._tree.setStyleSheet(
            f"QTreeWidget {{ background:{_c(p,'BG_DEEP')}; border:none; }}"
            "QTreeWidget::item { height: 33px; }"
            "QTreeWidget::branch { image: none; border-image: none; }")

        # Details pane stays hidden until a save is selected -on an 800px
        # Deck screen an empty one is pure loss.
        from gui_qt.save_preview import SavePreviewPane
        self._preview = SavePreviewPane()
        self._preview.setVisible(False)
        self._split = QSplitter(Qt.Vertical)
        self._split.setChildrenCollapsible(False)
        self._split.setHandleWidth(4)
        self._split.addWidget(self._tree)
        self._split.addWidget(self._preview)
        self._split.setStretchFactor(0, 1)
        self._split.setStretchFactor(1, 0)
        self._split.splitterMoved.connect(self._remember_preview_height)
        v.addWidget(self._split, 1)

    def eventFilter(self, obj, event):  # noqa: N802 (Qt override)
        from PySide6.QtCore import QEvent
        if obj is self._tree.viewport() and event.type() == QEvent.Resize:
            self._fit_name_to_width()
        return super().eventFilter(obj, event)

    def _fit_name_to_width(self):
        """Name takes the width Size + Modified leave (as the Data tab does);
        otherwise the last column stretches and trails a gap after the date."""
        vp = self._tree.viewport().width()
        if vp <= 0:
            return
        target = (vp - self._tree.columnWidth(_COL_TYPE)
                  - self._tree.columnWidth(_COL_SIZE)
                  - self._tree.columnWidth(_COL_DATE))
        if target >= self._name_min and target != self._tree.columnWidth(_COL_NAME):
            self._tree.header().resizeSection(_COL_NAME, target)

    # ---- scanning ---------------------------------------------------------
    def refresh(self):
        """Re-resolve the save locations and rescan them on a worker thread."""
        if self._scanning:
            return
        self._dirty = False
        # Drop the old result: until the rescan lands, Export/Import would
        # still resolve to the previous game's folder.
        self._scanned = []
        if self._game is None or not getattr(self._game, "is_configured", lambda: False)():
            self._clear_tree()
            self._info.setText(self.tr("No configured game selected."))
            self.status_changed.emit("")
            self.selection_changed.emit(False)
            return
        self._scanning = True
        self._info.setText(self.tr("Looking for save folders…"))
        self._clear_tree()
        self.status_changed.emit("")
        self.selection_changed.emit(False)
        run_in_worker(self._scan, self.scan_done, name="saves-scan", error_result=[])

    def _locations(self) -> list:
        """Manifest-resolved locations plus the profile saves folder, deduped."""
        locations = [(loc.path, loc.in_prefix, False, loc.exists, loc.patterns)
                     for loc in save_paths_for_game(self._game)]
        profile_dir = self._profile_saves_dir()
        if profile_dir is not None:
            locations.insert(0, (profile_dir, False, True, True, ()))
        out, seen = [], set()
        for path, in_prefix, is_profile, exists, patterns in locations:
            key = os.path.realpath(path)
            if key in seen:
                continue
            seen.add(key)
            out.append((path, in_prefix, is_profile, exists, patterns))
        return out

    def _profile_saves_dir(self) -> "Path | None":
        """The active profile's saves folder, for handlers that keep one."""
        if not (self._profile_name and getattr(self._game, "profile_saves", False)):
            return None
        getter = getattr(self._game, "_profile_saves_dir", None)
        if getter is None:
            return None
        try:
            path = Path(getter(self._profile_name))
        except Exception:
            return None
        return path if path.is_dir() else None

    def _scan(self) -> list:
        """Worker: list each location's top level. Subfolders wait until the
        user expands them (_on_item_expanded)."""
        result = []
        for path, in_prefix, is_profile, exists, patterns in self._locations():
            entries, truncated = _list_dir(path, self._log, patterns) if exists \
                else ([], False)
            result.append({
                "path": path,
                "in_prefix": in_prefix,
                "is_profile": is_profile,
                "exists": exists,
                "patterns": patterns,
                "entries": entries,
                "truncated": truncated,
                "total": sum(e[2] for e in entries),
            })
        return result

    # ---- rendering --------------------------------------------------------
    def _on_item_clicked(self, item, _column):
        """Toggle a folder row on a plain click; open a file row's preview."""
        if item.childCount() or item.data(0, _DIR_ROLE):
            item.setExpanded(not item.isExpanded())
            return
        self._maybe_open_file(item)

    def _maybe_open_file(self, item):
        """Single-click a file → open a panel-scoped image preview or text
        editor, the way the Mod Files tree does, via the host's callback."""
        raw = item.data(0, _PATH_ROLE)
        if not raw:
            return
        path = Path(raw)
        ext = path.suffix.lower()
        from gui_qt.image_preview import PREVIEW_EXTS
        from Utils.text.files import TEXT_EXTENSIONS
        if ext in PREVIEW_EXTS:
            cb = getattr(self, "on_open_image", None)
        elif ext in TEXT_EXTENSIONS:
            cb = getattr(self, "on_open_text", None)
        else:
            return
        if cb is None or not path.is_file():
            return
        cb(path, path.name)

    def _on_item_double_clicked(self, _item, _column):
        """Open the folder. A double-click's two clicks toggle the row twice,
        so it lands back where it started -this is purely 'open'."""
        self.open_folder()

    def _on_item_expanded(self, item):
        """Sync the arrow and, for a folder opened for the first time, list it."""
        self._sync_arrow(item)
        if not item.data(0, _DIR_ROLE) or item.data(0, _LOADED_ROLE):
            return
        path = item.data(0, _PATH_ROLE)
        if not path:
            return
        item.setData(0, _LOADED_ROLE, True)
        # Placeholder so the row doesn't snap shut while the folder is read.
        item.takeChildren()
        item.addChild(self._notice_row(self.tr("Reading…")))

        token, self._next_token = self._next_token, self._next_token + 1
        self._pending[token] = item
        gen = self._tree_gen
        run_in_worker(
            lambda: self._dir_worker(token, gen, path), self.dir_scan_done,
            name="saves-dir-scan",
            error_result={"token": token, "gen": gen, "entries": [], "truncated": False})

    def _dir_worker(self, token: int, gen: int, path: str) -> dict:
        entries, truncated = _list_dir(path, self._log)
        return {"token": token, "gen": gen, "path": path,
                "entries": entries, "truncated": truncated}

    def _on_dir_scan_done(self, payload):
        """Fill in an expanded folder's children, unless the tree moved on."""
        item = self._pending.pop(payload.get("token"), None)
        if item is None or payload.get("gen") != self._tree_gen:
            return
        item.takeChildren()
        self._add_entry_rows(item, {"path": payload.get("path", ""),
                                    "entries": payload["entries"],
                                    "truncated": payload["truncated"],
                                    "exists": True})
        self._sync_arrow(item)
        # A folder opened for the first time brings new file types with it.
        self.filetypes_changed.emit()
        self._apply_filter()

    def _sync_arrow(self, item):
        """Swap a location/folder row's arrow to match its expanded state."""
        if item.childCount() == 0 and not item.data(0, _DIR_ROLE):
            return
        expanded = item.isExpanded()
        item.setIcon(0, icon("arrow.png" if expanded else "right.png",
                             _ARROW_SZ, color=_c(active_palette(), "DROPDOWN_ARROW")))

    def _location_label(self, loc) -> str:
        """Display text for a save location: ~-shortened path + origin tag."""
        path = str(loc["path"])
        home = str(Path.home())
        if path.startswith(home + os.sep):
            path = "~" + path[len(home):]
        if loc["is_profile"]:
            return self.tr("{0}   [profile saves -{1}]").format(path, self._profile_name)
        # Say which files are the saves -otherwise a folder shared with other
        # data looks like it is hiding entries.
        if loc.get("patterns"):
            path = self.tr("{0}   ({1})").format(path, ", ".join(loc["patterns"]))
        if loc["in_prefix"]:
            return self.tr("{0}   [in prefix]").format(path)
        return path

    def _type_text(self, name: str, is_dir: bool) -> str:
        """File-type cell: "Folder", the bare extension, or "File" for none."""
        if is_dir:
            return self.tr("Folder")
        ext = _ext_of(name)
        return self.tr("File") if ext == "(none)" else ext[1:].upper()

    def _add_rows(self, parent, items):
        """Add finished rows in one go and sort them into the current order."""
        if not items:
            return
        col = self._tree.sortColumn()
        order = self._tree.header().sortIndicatorOrder()
        if parent is None:
            self._tree.addTopLevelItems(items)
            self._tree.sortItems(col if col >= 0 else _COL_TYPE, order)
        else:
            parent.addChildren(items)
            parent.sortChildren(col if col >= 0 else _COL_TYPE, order)

    def _add_entry_rows(self, parent, loc):
        """File/folder rows for one folder, under *parent* (None = top level)."""
        p = active_palette()
        arrow = icon("right.png", _ARROW_SZ, color=_c(p, "DROPDOWN_ARROW"))
        items = []
        for name, is_dir, size, mtime in loc["entries"]:
            ext = _ext_of(name)
            row = _SaveItem(_GRP_DIR if is_dir else _GRP_FILE, name,
                            "" if is_dir else ext, size, mtime)
            row.setText(_COL_NAME, name + ("/" if is_dir else ""))
            row.setText(_COL_TYPE, self._type_text(name, is_dir))
            row.setText(_COL_SIZE, fmt_size(size))
            row.setText(_COL_DATE, time.strftime(_DATE_FMT, time.localtime(mtime)))
            row.setData(0, _PATH_ROLE, str(Path(loc["path"]) / name))
            if is_dir:
                # Claim an arrow before the folder is read -children only
                # arrive once the row is opened.
                row.setData(0, _DIR_ROLE, True)
                row.setChildIndicatorPolicy(QTreeWidgetItem.ShowIndicator)
                row.setIcon(0, arrow)
            else:
                # Same-size blank, so file names line up with folder names.
                row.setIcon(0, _spacer_icon())
                self._ext_counts[ext] = self._ext_counts.get(ext, 0) + 1
            items.append(row)
        if loc["truncated"]:
            items.append(self._notice_row(
                self.tr("… only the first {0} entries are shown")
                .format(_MAX_ENTRIES)))
        if not loc["entries"]:
            if not loc.get("exists", True):
                text = self.tr("(not created yet -the game saves here)")
            elif loc.get("patterns"):
                # "(empty)" would be a lie: the folder can be full of files
                # that simply are not this game's saves.
                text = self.tr("(no {0} saves here yet)").format(
                    ", ".join(loc["patterns"]))
            else:
                text = self.tr("(empty)")
            items.append(self._notice_row(text))
        self._add_rows(parent, items)

    def _notice_row(self, text: str):
        """A dimmed, unfilterable "(empty)"/truncation row, sorted last."""
        row = _SaveItem(_GRP_INFO, text)
        row.setText(_COL_NAME, text)
        row.setForeground(0, self._brush(_c(active_palette(), "TEXT_DIM")))
        row.setIcon(0, _spacer_icon())
        row.setData(0, _INFO_ROLE, True)
        return row

    def _clear_tree(self):
        """Empty the tree and invalidate any folder listing still in flight."""
        self._tree.clear()
        self._pending.clear()
        self._tree_gen += 1
        self._ext_counts = {}
        self._status_base = ""
        # The selection went with the rows; a stale details pane is worse
        # than none.
        self._preview_gen += 1
        self._hide_preview()

    def _on_scan_done(self, locations):
        self._scanning = False
        self._scanned = list(locations or [])
        self._clear_tree()

        if not locations:
            self._info.setText(self.tr(
                "No save folders found for this game. Either it keeps its saves "
                "somewhere the Ludusavi manifest does not know about, it stores "
                "them in the cloud, or it has not been played yet."))
            self.status_changed.emit("")
            self.selection_changed.emit(False)
            self.filetypes_changed.emit()
            return

        p = active_palette()
        single = len(locations) == 1
        # One location: contents at the top level, path in the header.
        # Several: one expandable row each.

        total_files = total_bytes = 0
        for idx, loc in enumerate(locations):
            if single:
                self._add_entry_rows(None, loc)
            else:
                label = self._location_label(loc)
                top = _SaveItem(_GRP_LOCATION, label, "", loc["total"], order=idx)
                top.setText(_COL_NAME, label)
                top.setText(_COL_SIZE, fmt_size(loc["total"]))
                top.setToolTip(0, str(loc["path"]))
                top.setData(0, _PATH_ROLE, str(loc["path"]))
                top.setForeground(0, self._brush(_c(p, "ACCENT")))
                # Parented BEFORE its children: a detached item has no tree to
                # read the sort column from, so its rows would sort by text.
                self._add_rows(None, [top])
                self._add_entry_rows(top, loc)
                top.setExpanded(True)
                self._sync_arrow(top)
            total_files += len(loc["entries"])
            total_bytes += loc["total"]

        if single:
            self._info.setText(self._location_label(locations[0]))
            self._info.setToolTip(str(locations[0]["path"]))
        else:
            self._info.setText(self.tr("{0} save locations").format(len(locations)))
            self._info.setToolTip("")
        self._status_base = (
            self.tr("{0} location(s) · {1} entries · {2}")
            .format(len(locations), total_files, fmt_size(total_bytes)))
        self.selection_changed.emit(False)
        self.filetypes_changed.emit()
        # A rescan must not quietly drop a live filter.
        self._apply_filter()

    @staticmethod
    def _brush(color: str):
        from PySide6.QtGui import QBrush, QColor
        return QBrush(QColor(color))

    # ---- search / filters -------------------------------------------------
    @staticmethod
    def filter_spec() -> list[dict]:
        """Spec for the filter side panel; state comes back via
        apply_filter_state."""
        return [{"title": "By file type", "type": "dynamic", "id": "filetypes"}]

    def apply_filter_state(self, state: dict):
        self._inc_exts = set(state.get("filetypes") or ())
        self._exc_exts = set(state.get("filetypes_exclude") or ())
        self._apply_filter()

    def filetype_items(self) -> list[tuple]:
        """Current (ext, label, count) list for the filter panel's dynamic list."""
        return [(ext, self.tr("(no ext)") if ext == "(none)" else ext, n)
                for ext, n in sorted(self._ext_counts.items())]

    def _on_search(self, text: str):
        """Footer search box → needle + `!.ess`-style file-type tokens."""
        from Utils.text.search import parse_file_query
        needle, self._search_exts = parse_file_query(text)
        self._search = needle
        if self._search_timer is None:
            t = QTimer(self)
            t.setSingleShot(True)
            t.setInterval(150)
            t.timeout.connect(self._apply_filter)
            self._search_timer = t
        self._search_timer.start()

    def _filter_active(self) -> bool:
        return bool(self._search or self._search_exts
                    or self._inc_exts or self._exc_exts)

    def _iter_items(self):
        from PySide6.QtWidgets import QTreeWidgetItemIterator
        it = QTreeWidgetItemIterator(self._tree)
        while it.value():
            yield it.value()
            it += 1

    def _row_matches(self, item) -> bool:
        name = item.text(_COL_NAME)
        ext = _ext_of(name)
        if self._inc_exts and ext not in self._inc_exts:
            return False
        if ext in self._exc_exts:
            return False
        if self._search_exts and ext not in self._search_exts:
            return False
        return not self._search or self._search in name.casefold()

    def _apply_filter(self):
        """Hide the file rows the search/filter excludes."""
        active = self._filter_active()
        shown = total = 0
        for item in self._iter_items():
            if item.data(0, _INFO_ROLE):
                item.setHidden(active)
                continue
            # Folder rows always stay -one is only listed once opened, so
            # hiding it would hide saves nobody has scanned yet.
            if item.data(0, _DIR_ROLE) or item.childCount():
                item.setHidden(False)
                continue
            total += 1
            keep = not active or self._row_matches(item)
            item.setHidden(not keep)
            shown += int(keep)
        # A hidden row must not stay selected -Export/Import and the details
        # pane both key off it.
        current = self._tree.currentItem()
        if current is not None and current.isHidden():
            self._tree.setCurrentItem(None)
        if active:
            self.status_changed.emit(
                self.tr("{0} of {1} entries shown").format(shown, total))
        else:
            self.status_changed.emit(self._status_base)

    # ---- save preview -----------------------------------------------------
    def set_known_plugins(self, names):
        """The active load order, so the preview can flag missing plugins."""
        self._preview.set_known_plugins(names)

    def _on_selection_changed(self):
        path = self.selected_path()
        self.selection_changed.emit(path is not None)
        self._preview_for(path)

    def _preview_for(self, path):
        """Show the selected save's details, parsed on a worker thread."""
        # Bump first so an in-flight parse can't overwrite this selection.
        self._preview_gen += 1
        gen = self._preview_gen

        item = self._tree.currentItem()
        if path is None or (item is not None and item.data(0, _DIR_ROLE)):
            self._hide_preview()
            return
        # Extension only -the magic-byte check opens the file, and arrowing
        # down a list would then do a read per row on the UI thread. The worker
        # does the real check and hands back None if it isn't a save.
        from Utils.saves.header import SAVE_EXTS
        if path.suffix.lower() not in SAVE_EXTS:
            self._hide_preview()
            return

        self._preview.set_message(self.tr("Reading save…"))
        self._show_preview()
        target = str(path)
        run_in_worker(lambda: self._preview_worker(gen, target),
                      self.preview_done, name="save-preview",
                      error_result={"gen": gen, "header": None, "mtime": 0.0,
                                    "path": target})

    @staticmethod
    def _preview_worker(gen: int, path: str) -> dict:
        from Utils.saves.header import parse_save
        try:
            mtime = os.stat(path).st_mtime
        except OSError:
            mtime = 0.0
        # FO3/NV carry no timestamp of their own -mtime is the fallback.
        return {"gen": gen, "header": parse_save(path), "mtime": mtime,
                "path": path}

    def _on_preview_done(self, payload):
        if payload.get("gen") != self._preview_gen:
            return              # superseded by a newer selection
        # A None header means the extension lied -say so rather than
        # collapsing the pane out from under the row just clicked.
        self._preview.set_header(payload.get("header"),
                                 path=payload.get("path"),
                                 mtime=payload.get("mtime", 0.0))

    def _show_preview(self):
        if self._preview.isVisible():
            return
        self._preview.setVisible(True)
        total = max(self._split.height(), self._preview_height + 120)
        self._split.setSizes([total - self._preview_height, self._preview_height])

    def _hide_preview(self):
        self._preview.clear()
        self._preview.setVisible(False)
        # By the time the tab is switched away we are already off-screen, and a
        # hidden splitter never processes the hide's layout request -so its old,
        # wide minimum would stay cached and keep pinning the panel. refresh()
        # recomputes it now.
        self._split.refresh()

    def _remember_preview_height(self, *_args):
        """Keep a dragged pane height for the next save the user clicks."""
        if not self._preview.isVisible():
            return
        height = self._split.sizes()[1]
        if height > 0:
            self._preview_height = height

    # ---- actions ----------------------------------------------------------
    def selected_path(self) -> "Path | None":
        item = self._tree.currentItem()
        if item is None:
            return None
        raw = item.data(0, _PATH_ROLE)
        return Path(raw) if raw else None

    def can_open(self) -> bool:
        """Whether Open folder has something to open -a row or the location."""
        return self.selected_path() is not None or self.target_location() is not None

    def open_folder(self):
        """Open the selected entry's folder, or -with nothing selected -the
        save location itself."""
        path = self.selected_path() or self.target_location()
        if path is None:
            return
        target = path if path.is_dir() else path.parent
        xdg_open(target, self._log)

    # ---- right-click menu -------------------------------------------------
    def _location_roots(self) -> set:
        """Realpaths of the save locations themselves (not their contents)."""
        return {os.path.realpath(loc["path"]) for loc in self._scanned}

    def _profile_targets(self) -> list:
        """(profile, saves folder) for every profile of a game that can keep
        profile-specific saves.

        Offered whether or not the setting is currently on, here or in the
        destination profile: parking a save in a profile folder ahead of turning
        the option on is a normal thing to want. The folder is created by the
        transfer if it is not there yet. Empty only for games that cannot keep
        profile saves at all -that folder would never be read."""
        game = self._game
        if game is None or not getattr(game, "supports_profile_saves", True):
            return []
        getter = getattr(game, "_profile_saves_dir", None)
        if getter is None:
            return []
        from Utils.games.registry import _profiles_for_game
        targets = []
        for name in _profiles_for_game(getattr(game, "name", "")):
            try:
                targets.append((name, Path(getter(name))))
            except Exception:
                continue
        return targets

    def _on_context_menu(self, pos):
        item = self._tree.itemAt(pos)
        if item is None:
            return
        # Right-clicking a row acts on it, as in the plugins panel.
        if item is not self._tree.currentItem():
            self._tree.setCurrentItem(item)
        menu = self._build_context_menu(item)
        if menu is not None:
            menu.exec(self._tree.viewport().mapToGlobal(pos))

    def _menu_action(self, menu, label, slot, enabled=True):
        """Add one item. triggered() passes a `checked` bool that would
        otherwise clobber a slot's captured defaults -swallow it."""
        from PySide6.QtGui import QAction
        action = QAction(label, menu)
        action.triggered.connect(lambda _checked=False, _s=slot: _s())
        action.setEnabled(enabled)
        menu.addAction(action)
        return action

    def _build_context_menu(self, item):
        """Menu for one row, or None when the row has nothing to act on."""
        from PySide6.QtWidgets import QMenu
        raw = item.data(0, _PATH_ROLE)
        if item.data(0, _INFO_ROLE) or not raw:
            return None
        path = Path(raw)
        # A location row stands for the whole save folder: the footer's
        # Export/Import own that, and deleting it would take the folder itself.
        is_location = os.path.realpath(path) in self._location_roots()

        menu = QMenu(self._tree)
        self._menu_action(
            menu,
            self.tr("Open folder") if item.data(0, _DIR_ROLE) or is_location
            else self.tr("Open containing folder"),
            self.open_folder)

        targets = [] if is_location else self._profile_targets()
        if targets:
            menu.addSeparator()
            # PySide frees a submenu as soon as its Python wrapper goes out of
            # scope, even though the parent menu still lists the action -so the
            # menu keeps them itself.
            menu._submenus = [self._add_profile_items(menu, path, targets, False),
                              self._add_profile_items(menu, path, targets, True)]
        if not is_location:
            menu.addSeparator()
            self._menu_action(menu, self.tr("Delete…"),
                              lambda: self._confirm_delete(path),
                              enabled=not self._busy)
        return menu

    def _add_profile_items(self, menu, path, targets, move: bool):
        """Copy/Move-to-profile-saves item -a submenu once there are several
        profiles, since which one to land in is then a real question. Returns
        the submenu, for the caller to keep alive."""
        label = self.tr("Move to profile saves") if move \
            else self.tr("Copy to profile saves")
        if len(targets) == 1:
            name, folder = targets[0]
            self._menu_action(
                menu, self.tr("{0} ({1})").format(label, profile_display(name)),
                lambda: self._start_entry_transfer(path, folder, name, move),
                enabled=self._can_transfer_to(path, folder))
            return None
        from PySide6.QtWidgets import QMenu
        sub = QMenu(label, menu)
        menu.addMenu(sub)
        for name, folder in targets:
            shown = profile_display(name)
            text = self.tr("{0} (current)").format(shown) \
                if name == self._profile_name else shown
            self._menu_action(
                sub, text,
                lambda f=folder, n=name: self._start_entry_transfer(path, f, n, move),
                enabled=self._can_transfer_to(path, folder))
        return sub

    def _can_transfer_to(self, path: Path, folder: Path) -> bool:
        """False for the folder the save already sits in -with profile saves
        deployed the game's save location IS that folder, so most rows are
        already there and the item would be a no-op."""
        if self._busy:
            return False
        try:
            return os.path.realpath(path.parent) != os.path.realpath(folder)
        except OSError:
            return True

    def _start_entry_transfer(self, path: Path, dest_dir: Path, profile: str,
                              move: bool):
        """Copy/move one save into a profile saves folder, asking first when
        that would replace a save already there."""
        if self._busy:
            return
        dest = Path(dest_dir) / path.name
        if dest.exists() or dest.is_symlink():
            from gui_qt.confirm_overlay import ConfirmOverlay
            ConfirmOverlay.show_over(
                self,
                self.tr("Replace {0}?").format(path.name),
                self.tr("{0} already exists in the {1} saves folder:\n{2}\n\n"
                        "It is replaced by the one you picked.").format(
                    path.name, profile, dest_dir),
                lambda ok: self._do_entry_transfer(path, dest_dir, profile,
                                                   move, True) if ok else None,
                confirm_label=self.tr("Replace"))
            return
        self._do_entry_transfer(path, dest_dir, profile, move, False)

    def _do_entry_transfer(self, path: Path, dest_dir: Path, profile: str,
                           move: bool, overwrite: bool):
        self._set_busy(True)
        self.notify.emit(self.tr("Moving {0}…").format(path.name) if move
                         else self.tr("Copying {0}…").format(path.name), "info")
        run_in_worker(
            lambda: self._entry_transfer_worker(path, Path(dest_dir), profile,
                                                move, overwrite),
            self.transfer_done, name="saves-entry-transfer", unpack=True,
            error_result=(False, self.tr("Could not move the save.") if move
                          else self.tr("Could not copy the save.")))

    def _entry_transfer_worker(self, src: Path, dest_dir: Path, profile: str,
                               move: bool, overwrite: bool) -> tuple[bool, str]:
        from Utils.saves.transfer import SaveTransferError, transfer_save_entry
        try:
            count, size, dest = transfer_save_entry(
                src, dest_dir, move=move, overwrite=overwrite,
                progress_fn=self._progress)
        except SaveTransferError as exc:
            return False, str(exc)
        except OSError as exc:
            return False, self.tr("Transfer failed: {0}").format(exc)
        self._log(f"[saves] {'moved' if move else 'copied'} {src} → {dest} "
                  f"({count} file(s), {fmt_size(size)})")
        template = self.tr("Moved {0} ({1}) to the {2} saves folder.") if move \
            else self.tr("Copied {0} ({1}) to the {2} saves folder.")
        return True, template.format(src.name, fmt_size(size), profile)

    def _confirm_delete(self, path: Path):
        if self._busy:
            return
        if path.is_dir() and not path.is_symlink():
            body = self.tr("Delete the folder {0} and everything in it?\n{1}\n\n"
                           "This cannot be undone.").format(path.name, path)
        else:
            body = self.tr("Delete {0}?\n{1}\n\nThis cannot be undone.").format(
                path.name, path)
        from gui_qt.confirm_overlay import ConfirmOverlay
        ConfirmOverlay.show_over(
            self, self.tr("Delete save?"), body,
            lambda ok: self._do_delete(path) if ok else None,
            confirm_label=self.tr("Delete"))

    def _do_delete(self, path: Path):
        self._set_busy(True)
        self.notify.emit(self.tr("Deleting {0}…").format(path.name), "info")
        run_in_worker(lambda: self._delete_worker(path), self.transfer_done,
                      name="saves-delete", unpack=True,
                      error_result=(False, self.tr("Could not delete the save.")))

    def _delete_worker(self, path: Path) -> tuple[bool, str]:
        from Utils.saves.transfer import SaveTransferError, delete_save_entry
        try:
            count, size = delete_save_entry(path)
        except SaveTransferError as exc:
            return False, str(exc)
        except OSError as exc:
            return False, self.tr("Delete failed: {0}").format(exc)
        self._log(f"[saves] deleted {path} ({count} file(s), {fmt_size(size)})")
        return True, self.tr("Deleted {0} ({1}).").format(path.name, fmt_size(size))

    # ---- export / import --------------------------------------------------
    def _target_loc(self) -> "dict | None":
        """The location export/import act on: the only one, or the one owning
        the selected row when a game has several."""
        if not self._scanned:
            return None
        if len(self._scanned) == 1:
            return self._scanned[0]
        selected = self.selected_path()
        if selected is not None:
            for loc in self._scanned:
                root = Path(loc["path"])
                if selected == root or root in selected.parents:
                    return loc
        return None

    def target_location(self) -> "Path | None":
        """The folder export/import act on."""
        loc = self._target_loc()
        return Path(loc["path"]) if loc is not None else None

    def can_transfer(self) -> bool:
        """Whether Export/Import have an unambiguous folder to work on."""
        return not self._busy and self.target_location() is not None

    def _set_busy(self, busy: bool):
        self._busy = busy
        self.busy_changed.emit(not busy)

    def _default_export_name(self) -> str:
        game = getattr(self._game, "name", "") or "game"
        stamp = time.strftime("%Y%m%d")
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in game)
        return f"{safe}_saves_{stamp}.zip"

    def start_export(self):
        """Ask where to write the zip; packing starts once a path comes back."""
        if not self.can_transfer():
            return
        from Utils.ui.portal import pick_save_file
        from gui_qt.safe_emit import safe_emit
        pick_save_file(
            self.tr("Export saves"),
            lambda path: safe_emit(self.export_path_picked, path),
            current_name=self._default_export_name(),
            filters=[(self.tr("Zip archives (*.zip)"), ["*.zip"]),
                     (self.tr("All files"), ["*"])])

    def _on_export_path_picked(self, path):
        loc = self._target_loc()
        if not path or loc is None:
            return
        source, patterns = Path(loc["path"]), loc.get("patterns", ())
        dest = Path(path)
        if dest.suffix.lower() != ".zip":
            dest = dest.with_name(dest.name + ".zip")
        self._set_busy(True)
        self.notify.emit(self.tr("Packing saves…"), "info")
        run_in_worker(lambda: self._export_worker(source, dest, patterns),
                      self.transfer_done, name="saves-export", unpack=True,
                      error_result=(False, self.tr("Export failed.")))

    def _export_worker(self, source: Path, dest: Path, patterns=()) -> tuple[bool, str]:
        from Utils.saves.transfer import SaveTransferError, export_saves
        try:
            count, size = export_saves(source, dest, self._progress, patterns)
        except SaveTransferError as exc:
            return False, str(exc)
        except OSError as exc:
            return False, self.tr("Export failed: {0}").format(exc)
        self._log(f"[saves] exported {count} file(s), {fmt_size(size)} → {dest}")
        return True, self.tr("Exported {0} file(s) ({1}) to {2}").format(
            count, fmt_size(size), dest.name)

    def start_import(self):
        """Ask for a zip; the confirm prompt and extraction follow."""
        if not self.can_transfer():
            return
        from Utils.ui.portal import pick_file
        from gui_qt.safe_emit import safe_emit
        pick_file(
            self.tr("Import saves"),
            lambda path: safe_emit(self.import_path_picked, path),
            filters=[(self.tr("Zip archives (*.zip)"), ["*.zip"]),
                     (self.tr("All files"), ["*"])])

    def _on_import_path_picked(self, path):
        loc = self._target_loc()
        if not path or loc is None:
            return
        location, patterns = Path(loc["path"]), loc.get("patterns", ())
        src = Path(path)
        # Importing replaces the folder's contents -say so first.
        moved = self.tr("The current contents are moved aside to a \"{0}\" "
                        "folder first, so nothing is lost.").format(
            location.name + ".before-import-…")
        if patterns:
            # Only the save files move; the rest of the folder is left alone,
            # which matters when the game keeps its saves beside its own data.
            moved = self.tr("The current {0} files are moved aside to a \"{1}\" "
                            "folder first, so nothing is lost.").format(
                ", ".join(patterns), location.name + ".before-import-…")
        from gui_qt.confirm_overlay import ConfirmOverlay
        ConfirmOverlay.show_over(
            self,
            self.tr("Import saves?"),
            self.tr("Extract {0} into\n{1}\n\n{2}").format(src.name, location, moved),
            lambda ok: self._do_import(src, location, patterns) if ok else None,
            confirm_label=self.tr("Import"))

    def _do_import(self, src: Path, location: Path, patterns=()):
        self._set_busy(True)
        self.notify.emit(self.tr("Extracting saves…"), "info")
        run_in_worker(lambda: self._import_worker(src, location, patterns),
                      self.transfer_done, name="saves-import", unpack=True,
                      error_result=(False, self.tr("Import failed.")))

    def _import_worker(self, src: Path, location: Path, patterns=()) -> tuple[bool, str]:
        from Utils.saves.transfer import SaveTransferError, import_saves
        try:
            count, size, backup = import_saves(src, location, self._progress,
                                               patterns=patterns)
        except SaveTransferError as exc:
            return False, str(exc)
        except OSError as exc:
            return False, self.tr("Import failed: {0}").format(exc)
        if backup is not None:
            self._log(f"[saves] previous saves moved to {backup}")
        self._log(f"[saves] imported {count} file(s), {fmt_size(size)} → {location}")
        return True, self.tr("Imported {0} file(s) ({1}).").format(count, fmt_size(size))

    def _progress(self, done, total, _phase):
        """Worker progress → a percentage in the footer. safe_emit: the
        transfer thread outlives a teardown (app close mid-export)."""
        if total <= 0:
            return
        pct = int(done * 100 / total)
        if pct != getattr(self, "_last_pct", -1):
            self._last_pct = pct
            from gui_qt.safe_emit import safe_emit
            safe_emit(self.notify, f"{pct}%", "info")

    def _on_transfer_done(self, ok: bool, message: str):
        self._set_busy(False)
        self._last_pct = -1
        self.notify.emit(message, "success" if ok else "error")
        if ok:
            self.mark_dirty()
