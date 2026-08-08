"""Qt model for the Downloads tab - a flat table of DownloadEntry rows (archives
+ synthetic section headers, grouped by source folder). Columns:

  0 check    - selection checkbox (archives only)
  1 name     - archive filename / section label
  2 size     - human size (archives only)
  3 install  - Install / Reinstall button (painted by the delegate)

Selection is tracked by Path in `checked` so it survives rescans/filtering
(Tk parity). Installed detection (Install vs Reinstall) comes from an
InstalledIndex set on the model.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import (
    Qt, QAbstractTableModel, QModelIndex, QT_TRANSLATE_NOOP)

from Utils.downloads_core import DownloadEntry, InstalledIndex

COL_CHECK = 0
COL_NAME = 1
COL_SIZE = 2
COL_INSTALL = 3
# Translated at display time in headerData; register literals for lupdate.
COLUMNS = ["",
           QT_TRANSLATE_NOOP("DownloadsModel", "Name"),
           QT_TRANSLATE_NOOP("DownloadsModel", "Size"),
           ""]

EntryRole = Qt.UserRole + 1
InstalledRole = Qt.UserRole + 2   # bool: archive already installed


class DownloadsModel(QAbstractTableModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: list[DownloadEntry] = []
        self._installed = InstalledIndex()
        self.checked: set[Path] = set()       # selected archive paths
        # Shift-click range selection (Tk parity): the anchor row + whether the
        # anchor's action was select(True)/deselect(False).
        self._anchor_path: Path | None = None
        self._range_select = True

    # ---- population -------------------------------------------------------
    def set_rows(self, rows: list[DownloadEntry], installed: InstalledIndex):
        self.beginResetModel()
        self._rows = rows
        self._installed = installed
        # Drop checks for archives no longer present.
        present = {e.path for e in rows if not e.is_section_header and e.path}
        self.checked &= present
        if self._anchor_path is not None and self._anchor_path not in present:
            self._anchor_path = None
        self.endResetModel()

    def entry(self, row: int) -> DownloadEntry | None:
        return self._rows[row] if 0 <= row < len(self._rows) else None

    def is_installed(self, e: DownloadEntry) -> bool:
        return (not e.is_section_header and e.path is not None
                and self._installed.is_archive_installed(e.path.name))

    # ---- selection --------------------------------------------------------
    def toggle_check(self, row: int, shift: bool = False):
        """Toggle a row's checkbox. With *shift* and a live anchor, apply the
        anchor's last action (select/deselect) to the whole range between the
        anchor and this row (Tk parity); section headers in the range skipped."""
        e = self.entry(row)
        if e is None or e.is_section_header or e.path is None:
            return
        if shift and self._anchor_path is not None:
            anchor_row = self._row_of_path(self._anchor_path)
            if anchor_row is not None:
                lo, hi = sorted((anchor_row, row))
                for r in range(lo, hi + 1):
                    ee = self._rows[r]
                    if ee.is_section_header or ee.path is None:
                        continue
                    if self._range_select:
                        self.checked.add(ee.path)
                    else:
                        self.checked.discard(ee.path)
                # Leave the anchor so the range can be re-extended.
                self.dataChanged.emit(self.index(lo, COL_CHECK),
                                      self.index(hi, COL_CHECK),
                                      [Qt.CheckStateRole])
                return
        # Plain click: toggle + set the anchor + record the action.
        if e.path in self.checked:
            self.checked.discard(e.path)
            self._range_select = False
        else:
            self.checked.add(e.path)
            self._range_select = True
        self._anchor_path = e.path
        idx = self.index(row, COL_CHECK)
        self.dataChanged.emit(idx, idx, [Qt.CheckStateRole])

    def _row_of_path(self, path: Path) -> int | None:
        for r, e in enumerate(self._rows):
            if e.path == path:
                return r
        return None

    def set_section_checked(self, header_row: int, checked: bool):
        """Select/deselect every archive under a section header."""
        n = len(self._rows)
        j = header_row + 1
        first = j
        while j < n and not self._rows[j].is_section_header:
            e = self._rows[j]
            if e.path is not None:
                if checked:
                    self.checked.add(e.path)
                else:
                    self.checked.discard(e.path)
            j += 1
        if j > first:
            self.dataChanged.emit(self.index(first, COL_CHECK),
                                  self.index(j - 1, COL_CHECK),
                                  [Qt.CheckStateRole])

    def checked_paths(self) -> list[Path]:
        return [e.path for e in self._rows
                if not e.is_section_header and e.path in self.checked]

    def checked_count(self) -> int:
        return len(self.checked_paths())

    def clear_checks(self):
        if not self.checked:
            return
        self.checked.clear()
        if self._rows:
            self.dataChanged.emit(self.index(0, COL_CHECK),
                                  self.index(len(self._rows) - 1, COL_CHECK),
                                  [Qt.CheckStateRole])

    # ---- Qt interface -----------------------------------------------------
    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()):
        return len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return self.tr(COLUMNS[section]) if COLUMNS[section] else ""
        return None

    def flags(self, index):
        if not index.isValid():
            return Qt.NoItemFlags
        e = self._rows[index.row()]
        if e.is_section_header:
            return Qt.ItemIsEnabled
        return Qt.ItemIsEnabled | Qt.ItemIsSelectable

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        e = self._rows[index.row()]
        col = index.column()
        if role == EntryRole:
            return e
        if role == InstalledRole:
            return self.is_installed(e)
        if role == Qt.DisplayRole:
            if e.is_section_header:
                return e.section_name if col == COL_NAME else ""
            if col == COL_NAME:
                return e.path.name if e.path else ""
            if col == COL_SIZE:
                return e.size_str
        if role == Qt.CheckStateRole and col == COL_CHECK and not e.is_section_header:
            return (Qt.Checked if (e.path in self.checked) else Qt.Unchecked)
        return None
