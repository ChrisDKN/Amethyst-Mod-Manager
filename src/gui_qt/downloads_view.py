"""Qt Downloads tab - scans archive folders (Downloads + per-game cache + extras),
lists them grouped by source with Install/Reinstall buttons + checkboxes. Reuses
Utils.downloads_core for all scanning/filtering/installed-detection, and
Utils.download_locations for the (backward-compatible) settings. Built lazily:
only (re)scans when the sub-tab is visible.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QTreeView, QAbstractItemView,
)

import Utils.downloads_core as dc
from gui_qt.downloads_model import (
    DownloadsModel, COL_CHECK, COL_NAME, COL_SIZE, COL_INSTALL,
)


class DownloadsView(QWidget):
    """The Downloads tab. configure() once, then refresh()/mark_dirty()."""

    selection_changed = Signal()      # checked count changed (update footer)
    filetypes_changed = Signal()      # ext/location lists changed (filter panel)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.game = None
        self.game_name_getter = None      # callable -> active game name | None
        self.on_install = None            # callback(path) - per-row / selected
        self._dirty = True
        self._is_visible = False
        self._all_entries: list = []      # unfiltered scan result
        self._search = ""
        self._only_installed = 0
        self._only_not_installed = 0
        self._inc_exts: set = set()
        self._exc_exts: set = set()
        self._inc_locs: set = set()
        self._exc_locs: set = set()
        # Auto-refresh: watch the scan dirs for added/removed files. A short
        # debounce coalesces bursts (an archive lands as several FS events).
        from PySide6.QtCore import QFileSystemWatcher
        self._watcher = QFileSystemWatcher(self)
        self._watcher.directoryChanged.connect(self._on_dir_changed)
        self._watch_timer = QTimer(self)
        self._watch_timer.setSingleShot(True)
        self._watch_timer.setInterval(400)
        self._watch_timer.timeout.connect(self._rescan)
        self._build()

    # -- context ------------------------------------------------------------
    def configure(self, game, game_name_getter):
        self.game = game
        self.game_name_getter = game_name_getter
        self._dirty = True
        # Start watching immediately so files added before the tab is first
        # opened still mark it dirty (it'll rebuild on show).
        self._update_watch_dirs(self._game_name())

    def set_visible_tab(self, visible: bool):
        self._is_visible = visible
        if visible and self._dirty:
            self.refresh()

    def mark_dirty(self):
        self._dirty = True
        if self._is_visible:
            self.refresh()

    def refresh(self):
        self._dirty = False
        self._rescan()

    # -- construction -------------------------------------------------------
    def _build(self):
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        self._model = DownloadsModel(self)
        self._tree = QTreeView()
        self._tree.setModel(self._model)
        self._tree.setRootIsDecorated(False)
        self._tree.setIndentation(0)
        self._tree.setUniformRowHeights(True)
        self._tree.setAlternatingRowColors(False)
        self._tree.setSelectionMode(QAbstractItemView.NoSelection)
        self._tree.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        from gui_qt.downloads_delegate import DownloadsDelegate
        self._delegate = DownloadsDelegate(self._tree)
        self._delegate.on_install = self._on_row_install
        self._delegate.on_toggle_section = self._on_toggle_section
        self._tree.setItemDelegate(self._delegate)
        # Checkbox toggles (delegate → model) bubble up so the footer counts.
        self._model.dataChanged.connect(self._on_model_changed)

        from gui_qt.modlist_header import TkStyleHeader
        col_mins = {COL_CHECK: 34, COL_NAME: 160, COL_SIZE: 70, COL_INSTALL: 100}
        col_defaults = {COL_CHECK: 34, COL_SIZE: 90, COL_INSTALL: 100}
        hdr = TkStyleHeader(self._tree, col_mins, col_defaults)
        self._tree.setHeader(hdr)
        hdr.setMinimumSectionSize(min(col_mins.values()))
        for col, wdt in col_defaults.items():
            self._tree.setColumnWidth(col, wdt)
        self._name_min = col_mins[COL_NAME]
        self._tree.viewport().installEventFilter(self)
        v.addWidget(self._tree, 1)

    def eventFilter(self, obj, event):
        from PySide6.QtCore import QEvent
        if obj is self._tree.viewport():
            if event.type() == QEvent.Resize:
                self._fit_name_to_width()
            # Drag an archive row out (onto the modlist to install-at-position).
            # Only observe here - press/release still reach the delegate, so the
            # checkbox and Install button behave as before; a drag only starts
            # after the cursor moves past the threshold with the button held.
            elif (event.type() == QEvent.MouseButtonPress
                    and event.button() == Qt.LeftButton):
                self._drag_press_pos = event.position().toPoint()
                self._drag_press_path = self._draggable_path_at(self._drag_press_pos)
            elif (event.type() == QEvent.MouseMove
                    and getattr(self, "_drag_press_path", None) is not None
                    and event.buttons() & Qt.LeftButton):
                from PySide6.QtWidgets import QApplication
                delta = (event.position().toPoint() - self._drag_press_pos)
                if delta.manhattanLength() >= QApplication.startDragDistance():
                    self._start_archive_drag()
            elif event.type() in (QEvent.MouseButtonRelease, QEvent.Leave):
                self._drag_press_path = None
        return super().eventFilter(obj, event)

    def _draggable_path_at(self, pos):
        """The archive path under *pos* if a drag may start there: the Name or
        Size cell of a real archive row (never the checkbox / Install button,
        so press-and-slide on those still works as a click)."""
        index = self._tree.indexAt(pos)
        if not index.isValid() or index.column() not in (COL_NAME, COL_SIZE):
            return None
        e = self._model.entry(index.row())
        if e is None or e.is_section_header or e.path is None:
            return None
        return e.path

    def _start_archive_drag(self):
        """Begin a Qt drag carrying archive paths. If the pressed row is one of
        several checked rows the whole checked set is carried; otherwise just
        the pressed row."""
        path = self._drag_press_path
        self._drag_press_path = None
        checked = self._model.checked_paths()
        paths = checked if (path in checked and len(checked) > 1) else [path]
        from PySide6.QtCore import QMimeData
        from PySide6.QtGui import QDrag
        from gui_qt.modlist_view import ARCHIVE_DROP_MIME
        mime = QMimeData()
        mime.setData(ARCHIVE_DROP_MIME,
                     "\n".join(str(p) for p in paths).encode("utf-8"))
        drag = QDrag(self._tree)
        drag.setMimeData(mime)
        drag.setPixmap(self._drag_pixmap(
            path.name if len(paths) == 1
            else self.tr("{0} archives").format(len(paths))))
        # NB: over non-target areas Qt asks for its 'forbidden' no-drop cursor.
        # setDragCursor(pixmap, Qt.IgnoreAction) DOES override that at the Qt
        # level (verified: QGuiApplication.overrideCursor is our pixmap during
        # the drag), but under a Wayland session the app runs via XWayland and
        # KWin draws its OWN DnD cursor for XWayland clients - the X11 override
        # cursor can't reach it (KDE bug: XWayland drag shows the wrong cursor).
        # So there's nothing to set here; the badge is compositor-owned. The
        # setPixmap label + the modlist's blue drop line carry the feedback.
        drag.exec(Qt.CopyAction)

    def _drag_pixmap(self, text: str):
        """A small translucent label following the cursor during the drag."""
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QPixmap, QPainter, QColor
        fm = self._tree.fontMetrics()
        text = fm.elidedText(text, Qt.ElideMiddle, 260)
        w = fm.horizontalAdvance(text) + 16
        h = fm.height() + 8
        pm = QPixmap(w, h)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 170))
        p.drawRoundedRect(QRectF(0, 0, w, h), 4, 4)
        p.setPen(QColor(255, 255, 255, 230))
        p.drawText(QRectF(8, 4, w - 16, h - 8), Qt.AlignLeft | Qt.AlignVCenter, text)
        p.end()
        return pm

    def _on_model_changed(self, tl, br, roles):
        if Qt.CheckStateRole in roles:
            self.selection_changed.emit()

    def _fit_name_to_width(self):
        vp = self._tree.viewport().width()
        if vp <= 0:
            return
        others = (self._tree.columnWidth(COL_CHECK)
                  + self._tree.columnWidth(COL_SIZE)
                  + self._tree.columnWidth(COL_INSTALL))
        target = vp - others
        if target >= self._name_min and target != self._tree.columnWidth(COL_NAME):
            self._tree.header().resizeSection(COL_NAME, target)

    # -- scan / filter ------------------------------------------------------
    def _game_name(self):
        try:
            return self.game_name_getter() if self.game_name_getter else None
        except Exception:
            return None

    def _rescan(self):
        name = self._game_name()
        self._update_watch_dirs(name)
        self._all_entries = dc.scan_download_dirs(name)
        self.filetypes_changed.emit()
        self._apply()

    def _apply(self):
        installed = dc.build_installed_index(self.game)
        rows = dc.filter_entries(
            self._all_entries, installed,
            only_installed=self._only_installed,
            only_not_installed=self._only_not_installed,
            locations=frozenset(self._inc_locs) or None,
            locations_exclude=frozenset(self._exc_locs) or None,
            filetypes=frozenset(self._inc_exts) or None,
            filetypes_exclude=frozenset(self._exc_exts) or None,
            search=self._search)
        self._model.set_rows(rows, installed)
        self.selection_changed.emit()

    # -- auto-refresh (filesystem watch) ------------------------------------
    def _staging_dir(self):
        """The active game's mod staging folder (so a mod added/removed there
        flips Install↔Reinstall), or None."""
        if self.game is None:
            return None
        try:
            p = self.game.get_effective_mod_staging_path()
            return p if p and p.is_dir() else None
        except Exception:
            return None

    def _update_watch_dirs(self, game_name):
        """Point the watcher at the current scan dirs PLUS the game's staging dir
        (it may change with the game / locations settings). Only existing dirs
        can be watched. Watching staging makes Reinstall revert to Install when a
        mod is removed (in-app or externally)."""
        watched = set(self._watcher.directories())
        wanted = {str(p) for p in dc.get_scan_dirs(game_name) if p.is_dir()}
        staging = self._staging_dir()
        if staging is not None:
            wanted.add(str(staging))
        stale = watched - wanted
        if stale:
            self._watcher.removePaths(list(stale))
        add = wanted - watched
        if add:
            self._watcher.addPaths(list(add))

    def _on_dir_changed(self, _path):
        """A watched folder's contents changed. Re-scan only when the Downloads
        tab is visible (debounced); otherwise mark dirty for the next show."""
        if self._is_visible:
            self._watch_timer.start()
        else:
            self._dirty = True

    # -- filter spec / state ------------------------------------------------
    def filter_spec(self) -> list[dict]:
        return [
            {"title": "By status", "type": "checks", "items": [
                ("only_installed", "Show only installed", True),
                ("only_not_installed", "Show only not installed", True),
            ]},
            {"title": "By location", "type": "dynamic", "id": "locations"},
            {"title": "By file type", "type": "dynamic", "id": "filetypes"},
        ]

    def apply_filter_state(self, state: dict):
        self._only_installed = state.get("only_installed", 0)
        self._only_not_installed = state.get("only_not_installed", 0)
        self._inc_exts = set(state.get("filetypes") or ())
        self._exc_exts = set(state.get("filetypes_exclude") or ())
        self._inc_locs = set(state.get("locations") or ())
        self._exc_locs = set(state.get("locations_exclude") or ())
        self._apply()

    def filetype_items(self) -> list[tuple]:
        items = sorted(dc.filetype_counts(self._all_entries).items())
        return [(ext or "(none)", ext or "(no ext)", n) for ext, n in items]

    def location_items(self) -> list[tuple]:
        return dc.location_options(self._all_entries, self._game_name())

    # -- search -------------------------------------------------------------
    def _on_search(self, text: str):
        self._search = (text or "").strip().casefold()
        t = getattr(self, "_search_timer", None)
        if t is None:
            t = QTimer(self)
            t.setSingleShot(True)
            t.setInterval(150)
            t.timeout.connect(self._apply)
            self._search_timer = t
        t.start()

    # -- selection / install ------------------------------------------------
    def _on_row_install(self, path: Path):
        if self.on_install is not None:
            self.on_install([str(path)])

    def _on_toggle_section(self, header_row: int):
        # Select all if not all selected; else deselect all.
        n = self._model.rowCount()
        j = header_row + 1
        all_on = True
        any_row = False
        while j < n:
            e = self._model.entry(j)
            if e is None or e.is_section_header:
                break
            if e.path is not None:
                any_row = True
                if e.path not in self._model.checked:
                    all_on = False
            j += 1
        self._model.set_section_checked(header_row, not (any_row and all_on))
        self.selection_changed.emit()

    def checked_count(self) -> int:
        return self._model.checked_count()

    def checked_paths(self) -> list[str]:
        return [str(p) for p in self._model.checked_paths()]

    def clear_checks(self):
        self._model.clear_checks()
        self.selection_changed.emit()

    def install_selected(self):
        paths = self.checked_paths()
        if paths and self.on_install is not None:
            self.on_install(paths)
