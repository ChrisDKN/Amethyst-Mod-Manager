"""Qt Overrides tab (BG3) - override paks that deploy to the game's Mods
folder but never enter modsettings.lsx: paks with no meta.lsx, or whose meta
only overwrites Larian built-in module folders. Rows are checkable; unchecking
adds the pak to excluded_mod_files (same mechanism as the Mod Files tab's
Disable), which drops it from both deployment and the modsettings scan.
Built lazily like the Data tab: only (re)scans while the sub-tab is visible.
"""

from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import (
    Qt, QAbstractTableModel, QEvent, QModelIndex, QRect, Signal,
)
from PySide6.QtGui import QBrush, QFont, QPen
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTreeView, QLabel, QAbstractItemView,
    QStyledItemDelegate,
)

from gui_qt.safe_emit import safe_emit
from Utils.mods.modlist import read_modlist
from Utils.bg3.overrides import (
    OverridePakRow, STATUS_NO_META, scan_override_paks,
)
from Utils.profiles.state import (
    read_excluded_mod_files, write_excluded_mod_files,
)
from gui_qt.theme_qt import bind_theme, qc, qc_contrast

COL_PAK = 0
COL_MOD = 1
COL_STATUS = 2

CHECK_BOX = 17        # same as the modlist/plugins checkbox
FONT_PX = 13


class _OverridesDelegate(QStyledItemDelegate):
    """Paints the modlist-style checkbox + pak name (same visuals as the
    Plugins tab's name column) and plain text for the Mod/Status columns.
    Clicking the checkbox area toggles via model.toggle()."""

    def __init__(self, view, parent=None):
        super().__init__(parent or view)
        self._view = view
        bind_theme(self, roles={
            "TEXT_MAIN", "TEXT_DIM", "BORDER_FAINT", "CHECK_FILL",
            "BG_DEEP", "BG_SELECT",
        })

    def refresh_theme(self, p):
        self.c_text = qc(p, "TEXT_MAIN")
        self.c_dim = qc(p, "TEXT_DIM")
        self.c_border = qc(p, "BORDER_FAINT")
        self.c_check = qc(p, "CHECK_FILL")
        self.c_check_off = qc(p, "BG_DEEP")
        self.c_tick = qc_contrast(p, "CHECK_FILL")
        self.c_sel = qc(p, "BG_SELECT")
        self._view.viewport().update()

    def paint(self, p, opt, index):
        r = opt.rect
        model = index.model()
        row = model.row_at(index.row())
        if row is None:
            return
        if opt.state & opt.state.State_Selected:
            p.fillRect(r, self.c_sel)
        p.save()
        enabled = model.is_enabled(index.row())
        f = QFont(); f.setPixelSize(FONT_PX); p.setFont(f)
        col = index.column()
        if col == COL_PAK:
            box = QRect(r.left() + 10, r.top() + (r.height() - CHECK_BOX) // 2,
                        CHECK_BOX, CHECK_BOX)
            p.setRenderHint(p.RenderHint.Antialiasing, True)
            p.setPen(QPen(self.c_border, 1))
            p.setBrush(QBrush(self.c_check if enabled else self.c_check_off))
            p.drawRoundedRect(box, 3, 3)
            if enabled:
                p.setPen(QPen(self.c_tick, 2))
                p.drawLine(box.left() + 4, box.center().y() + 1,
                           box.center().x() - 1, box.bottom() - 4)
                p.drawLine(box.center().x() - 1, box.bottom() - 4,
                           box.right() - 3, box.top() + 4)
            p.setRenderHint(p.RenderHint.Antialiasing, False)
            tx = box.right() + 10
            p.setPen(self.c_text if enabled else self.c_dim)
            text_rect = QRect(tx, r.top(), r.right() - tx - 6, r.height())
            elided = p.fontMetrics().elidedText(
                row.rel_str, Qt.ElideRight, text_rect.width())
            p.drawText(text_rect, Qt.AlignVCenter | Qt.AlignLeft, elided)
        else:
            p.setPen(self.c_text if (enabled and col == COL_MOD) else self.c_dim)
            text = model.data(index, Qt.DisplayRole) or ""
            text_rect = r.adjusted(6, 0, -6, 0)
            elided = p.fontMetrics().elidedText(
                text, Qt.ElideRight, text_rect.width())
            p.drawText(text_rect, Qt.AlignVCenter | Qt.AlignLeft, elided)
        p.restore()

    def editorEvent(self, event, model, opt, index):
        if event.type() != QEvent.MouseButtonRelease:
            return False
        if index.column() == COL_PAK:
            # Same generous hit box as the Plugins tab checkbox.
            box = QRect(opt.rect.left() + 6, opt.rect.top(), 26,
                        opt.rect.height())
            if box.contains(event.position().toPoint()):
                model.toggle(index.row())
                return True
        return False

    def sizeHint(self, opt, index):
        s = super().sizeHint(opt, index)
        s.setHeight(max(s.height(), 22))
        return s


class _OverridesModel(QAbstractTableModel):
    """Flat checkable list of override paks. toggled(row) fires on check."""

    toggled = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: list[OverridePakRow] = []
        self._enabled: list[bool] = []

    def set_rows(self, rows: list[OverridePakRow], enabled: list[bool]):
        self.beginResetModel()
        self._rows = rows
        self._enabled = enabled
        self.endResetModel()

    def row_at(self, i: int) -> OverridePakRow | None:
        return self._rows[i] if 0 <= i < len(self._rows) else None

    def is_enabled(self, i: int) -> bool:
        return bool(self._enabled[i]) if 0 <= i < len(self._enabled) else True

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()):
        return 3

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return (self.tr("Pak"), self.tr("Mod"), self.tr("Status"))[section]
        return None

    def flags(self, index):
        # The delegate paints the checkbox + handles its clicks (no native
        # check indicator).
        return Qt.ItemIsEnabled | Qt.ItemIsSelectable

    def data(self, index, role=Qt.DisplayRole):
        r = self._rows[index.row()]
        col = index.column()
        if role == Qt.DisplayRole:
            if col == COL_PAK:
                return r.rel_str
            if col == COL_MOD:
                return r.mod_name
            if col == COL_STATUS:
                return (self.tr("No meta.lsx") if r.status == STATUS_NO_META
                        else self.tr("Override only"))
        elif role == Qt.CheckStateRole and col == COL_PAK:
            return (Qt.Checked if self._enabled[index.row()]
                    else Qt.Unchecked)
        elif role == Qt.ToolTipRole and col == COL_PAK:
            return r.rel_str
        return None

    def toggle(self, i: int):
        if not 0 <= i < len(self._enabled):
            return
        self._enabled[i] = not self._enabled[i]
        idx = self.index(i, COL_PAK)
        self.dataChanged.emit(idx, idx, [Qt.CheckStateRole])
        self.toggled.emit(i)

    def setData(self, index, value, role=Qt.EditRole):
        # Kept for programmatic/test toggling; the delegate uses toggle().
        if role != Qt.CheckStateRole or index.column() != COL_PAK:
            return False
        i = index.row()
        self._enabled[i] = (Qt.CheckState(value) == Qt.Checked)
        self.dataChanged.emit(index, index, [Qt.CheckStateRole])
        self.toggled.emit(i)
        return True


class OverridesView(QWidget):
    """The Overrides tab. configure() once, then refresh()/mark_dirty()."""

    changed = Signal()                    # an exclusion was toggled
    _rows_ready = Signal(int, object, object)  # gen, rows, excluded map

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scan_gen = 0
        self.profile_dir: Path | None = None
        self.staging_root: Path | None = None
        self.modlist_path: Path | None = None
        self._dirty = True
        self._is_visible = False
        self.on_select_mod = None   # callback(mod_name | None) - highlight
        self._build()
        self._rows_ready.connect(self._on_rows_ready)

    # -- context ------------------------------------------------------------
    def configure(self, profile_dir, staging_root, modlist_path):
        self.profile_dir = profile_dir
        self.staging_root = staging_root
        self.modlist_path = modlist_path
        self._dirty = True

    def set_visible_tab(self, visible: bool):
        """Switching TO the tab triggers a deferred rescan if dirty."""
        self._is_visible = visible
        if visible and self._dirty:
            self.refresh()

    def mark_dirty(self):
        """Mod set changed. Rescan now if visible, else defer."""
        self._dirty = True
        if self._is_visible:
            self.refresh()

    def refresh(self):
        self._dirty = False
        self._scan_gen += 1
        gen = self._scan_gen
        profile_dir = self.profile_dir
        staging = self.staging_root
        ml_path = self.modlist_path

        def worker():
            rows: list[OverridePakRow] = []
            excluded: dict[str, set[str]] = {}
            try:
                if staging is not None and ml_path is not None \
                        and ml_path.is_file():
                    names = [e.name for e in read_modlist(ml_path)
                             if e.enabled and not e.is_separator]
                    rows = scan_override_paks(staging, names)
                if profile_dir is not None:
                    excluded = {m: set(v) for m, v in
                                read_excluded_mod_files(profile_dir).items()}
            except Exception:
                pass
            safe_emit(self._rows_ready, gen, rows, excluded)

        threading.Thread(target=worker, daemon=True,
                         name="overrides-tab-scan").start()

    # -- construction -------------------------------------------------------
    def _build(self):
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        tb = QWidget()
        tb.setObjectName("HeaderBar")
        tbl = QHBoxLayout(tb)
        tbl.setContentsMargins(8, 4, 8, 4)
        self._label = QLabel(self.tr("Override paks"))
        self._label.setObjectName("HeaderCaption")
        tbl.addWidget(self._label, 1)
        v.addWidget(tb)

        self._model = _OverridesModel(self)
        self._model.toggled.connect(self._on_toggled)
        self._tree = QTreeView()
        self._tree.setModel(self._model)
        self._tree.setRootIsDecorated(False)
        self._tree.setIndentation(0)
        self._tree.setUniformRowHeights(True)
        self._tree.setAlternatingRowColors(False)
        self._tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self._tree.setItemDelegate(_OverridesDelegate(self._tree))
        # Same Tk-style column header as the modlist/plugins (centered labels,
        # boundary-drag resize, no overflow). Pak (col 0) is the fill column.
        from gui_qt.modlist_header import TkStyleHeader
        col_mins = {COL_PAK: 140, COL_MOD: 100, COL_STATUS: 90}
        col_defaults = {COL_MOD: 160, COL_STATUS: 110}
        hdr = TkStyleHeader(self._tree, col_mins, col_defaults)
        self._tree.setHeader(hdr)
        hdr.setMinimumSectionSize(min(col_mins.values()))
        self._tree.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        for col, w in col_defaults.items():
            self._tree.setColumnWidth(col, w)
        self._pak_min = col_mins[COL_PAK]
        self._tree.viewport().installEventFilter(self)
        self._tree.selectionModel().selectionChanged.connect(
            lambda *_: self._on_selection_changed())
        v.addWidget(self._tree, 1)

        self._empty = QLabel(self.tr(
            "No override paks in the enabled mods.\n\n"
            "Override paks (no meta.lsx, or only overwriting the game's own "
            "modules) deploy to the game's Mods folder but are not part of "
            "the load order."))
        self._empty.setAlignment(Qt.AlignCenter)
        self._empty.setWordWrap(True)
        self._empty.setObjectName("EmptyState")
        v.addWidget(self._empty, 1)
        self._empty.hide()

    def eventFilter(self, obj, event):
        from PySide6.QtCore import QEvent
        if obj is self._tree.viewport() and event.type() == QEvent.Resize:
            self._fit_pak_to_width()
        return super().eventFilter(obj, event)

    def _fit_pak_to_width(self):
        """Pak column absorbs the free width (Tk fill-column, like DataView)."""
        vp = self._tree.viewport().width()
        if vp <= 0:
            return
        target = vp - (self._tree.columnWidth(COL_MOD)
                       + self._tree.columnWidth(COL_STATUS))
        if target >= self._pak_min and target != self._tree.columnWidth(COL_PAK):
            self._tree.header().resizeSection(COL_PAK, target)

    # -- results ------------------------------------------------------------
    def _on_rows_ready(self, gen: int, rows: list, excluded: dict):
        if gen != self._scan_gen:
            return
        enabled = [r.rel_key not in excluded.get(r.mod_name, ())
                   for r in rows]
        self._model.set_rows(rows, enabled)
        n = len(rows)
        if n:
            self._label.setText(
                self.tr("Override paks - {0} deployed to the game's Mods "
                        "folder, not in the load order").format(n))
        else:
            self._label.setText(self.tr("Override paks"))
        self._tree.setVisible(bool(n))
        self._empty.setVisible(not n)

    def row_count(self) -> int:
        return self._model.rowCount()

    # -- selection → modlist highlight --------------------------------------
    def _on_selection_changed(self):
        """Selecting a pak row orange-highlights its owning mod in the modlist
        (same mechanism as clicking a plugin); deselecting clears it."""
        cb = self.on_select_mod
        if cb is None:
            return
        rows = self._tree.selectionModel().selectedRows()
        row = self._model.row_at(rows[0].row()) if rows else None
        cb(row.mod_name if row is not None else None)

    # -- toggle -> excluded_mod_files ---------------------------------------
    def _on_toggled(self, i: int):
        row = self._model.row_at(i)
        if row is None or self.profile_dir is None:
            return
        # Read-modify-write this mod's list so exclusions made in the Mod
        # Files tab (or for other paks) are preserved.
        all_excluded = read_excluded_mod_files(self.profile_dir)
        keys = set(all_excluded.get(row.mod_name, ()))
        if self._model.is_enabled(i):
            keys.discard(row.rel_key)
        else:
            keys.add(row.rel_key)
        if keys:
            all_excluded[row.mod_name] = sorted(keys)
        else:
            all_excluded.pop(row.mod_name, None)
        write_excluded_mod_files(self.profile_dir, all_excluded)
        self.changed.emit()
