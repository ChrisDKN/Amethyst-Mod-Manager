"""Show Conflicts - a full (detachable) tab listing a mod's file conflicts.
Qt port of the Tk `gui/dialogs.py:OverwritesPanel`, styled for Qt.

Three panes: files this mod OVERRIDES (green, path | mods beaten), files this mod
is OVERRIDDEN BY (red, path | winning mod), and files with NO CONFLICT (blue).
BSA/BA2 archive rows (``archive.bsa : inner/path``) are tinted cyan. The file-level
data is computed on a worker thread via the neutral Utils.ui.conflicts.
"""

from __future__ import annotations


from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame,
    QSplitter, QTreeWidget, QTreeWidgetItem, QHeaderView,
)

from gui_qt.theme_qt import active_palette, bind_theme, _c, close_button
from gui_qt.worker import run_in_worker
from Utils.ui.conflicts import BSA_ROW_RE

_BSA_ROLE = Qt.UserRole + 20
_INFO_ROLE = Qt.UserRole + 21


class ShowConflictsView(QWidget):
    """Full-tab conflict detail for one mod."""

    # (win, lose, no_conflict, bsa_win_paths) from the compute worker → UI.
    _ready = Signal(object, object, object, object)

    def __init__(self, mod_name, ctx, on_close=None, log_fn=None):
        super().__init__()
        self._mod_name = mod_name
        self._ctx = ctx
        self._on_close = on_close or (lambda: None)
        self._log = log_fn or (lambda _m: None)
        self.setObjectName("ShowConflictsView")
        self._ready.connect(self._on_ready)
        self._build()
        bind_theme(self, roles={"TEXT_DIM", "TONE_CYAN"})
        self._start()

    def refresh_theme(self, p: dict) -> None:
        for tree in (getattr(self, "_over_tree", None),
                     getattr(self, "_under_tree", None),
                     getattr(self, "_none_tree", None)):
            if tree is None:
                continue
            for row in range(tree.topLevelItemCount()):
                item = tree.topLevelItem(row)
                if item.data(0, _BSA_ROLE):
                    item.setForeground(0, QColor(_c(p, "TONE_CYAN")))
                elif item.data(0, _INFO_ROLE):
                    item.setForeground(0, QColor(_c(p, "TEXT_DIM")))
            tree.viewport().update()

    # ---- layout -----------------------------------------------------------
    def _build(self):
        p = active_palette()
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        # Title bar.
        bar = QWidget(); bar.setObjectName("HeaderBar")
        hb = QHBoxLayout(bar); hb.setContentsMargins(12, 8, 8, 8)
        title = QLabel(self.tr("Conflicts: {0}").format(self._mod_name))
        title.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-weight:600; font-size:15px;")
        hb.addWidget(title)
        hb.addStretch(1)
        close = close_button(pal=p)
        close.clicked.connect(lambda: self._on_close())
        hb.addWidget(close)
        v.addWidget(bar)

        # Body: left column (2 stacked panes) | right column (1 pane).
        self._over_pane, self._over_tree = self._make_pane(
            p, self.tr("Files overriding others"), "TONE_GREEN",
            [self.tr("File path"), self.tr("Mod(s) beaten")])
        self._under_pane, self._under_tree = self._make_pane(
            p, self.tr("Files overridden by others"), "TONE_RED",
            [self.tr("File path"), self.tr("Winning mod")])
        self._none_pane, self._none_tree = self._make_pane(
            p, self.tr("Files with no conflicts"), "TONE_BLUE", [self.tr("File path")])

        left = QSplitter(Qt.Vertical)
        left.addWidget(self._over_pane)
        left.addWidget(self._under_pane)
        left.setStretchFactor(0, 1)
        left.setStretchFactor(1, 1)

        body = QSplitter(Qt.Horizontal)
        body.addWidget(left)
        body.addWidget(self._none_pane)
        body.setStretchFactor(0, 3)   # left column wider (2 panes, 2 columns each)
        body.setStretchFactor(1, 2)
        v.addWidget(body, 1)

        # Loading/status footer line.
        self._status = QLabel(self.tr("Computing conflicts…"))
        self._status.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; padding:6px 12px;")
        v.addWidget(self._status)

    def _make_pane(self, p, title, tone, columns):
        """A pane = a coloured header label over a QTreeWidget. Returns
        (container_widget, tree). The header text gets a count appended later."""
        pane = QFrame()
        pane.setObjectName("ConflictPane")
        pane.setStyleSheet(
            f"#ConflictPane {{ background:{_c(p,'BG_DEEP')};"
            f" border:1px solid {_c(p,'BORDER')}; }}")
        pv = QVBoxLayout(pane); pv.setContentsMargins(0, 0, 0, 0); pv.setSpacing(0)
        hdr = QLabel(title)
        hdr.setStyleSheet(
            f"color:{_c(p, tone)}; font-weight:700; font-size:12px;"
            f" background:{_c(p,'BG_PANEL')}; padding:6px 8px;")
        pv.addWidget(hdr)
        tree = QTreeWidget()
        tree.setColumnCount(len(columns))
        tree.setHeaderLabels(columns)
        tree.setRootIsDecorated(False)
        tree.setAlternatingRowColors(True)
        tree.setUniformRowHeights(True)
        tree.setStyleSheet("QTreeWidget { font-size:12px; } "
                           "QTreeWidget::item { padding:1px 2px; }")
        hv = tree.header()
        hv.setStretchLastSection(False)
        # File path takes the pane; the "other mod" column only what it needs.
        hv.setSectionResizeMode(0, QHeaderView.Stretch)
        for col in range(1, len(columns)):
            hv.setSectionResizeMode(col, QHeaderView.Interactive)
        pv.addWidget(tree, 1)
        pane._header = hdr
        pane._title = title
        return pane, tree

    # ---- fetch ------------------------------------------------------------
    def _start(self):
        ctx = dict(self._ctx)
        mod = self._mod_name

        def compute():
            from Utils.ui.conflicts import compute_mod_conflicts
            return compute_mod_conflicts(mod, **ctx)

        run_in_worker(compute, self._ready, name="show-conflicts",
                      unpack=True, error_result=(None, None, None, None))

    def _on_ready(self, win, lose, none, bsa_win_paths):
        if win is None and lose is None and none is None:
            self._status.setText(self.tr("Could not compute conflicts - see the log."))
            return
        # bsa_paths adds the cyan tint to loose rows that beat archives only;
        # this mod's own `archive.bsa : path` rows match BSA_ROW_RE instead.
        self._fill_two(self._over_pane, self._over_tree, win,
                       bsa_paths=bsa_win_paths or set())
        self._fill_two(self._under_pane, self._under_tree, lose)
        self._fill_one(self._none_pane, self._none_tree, none)
        self._status.setVisible(False)

    # ---- populate helpers -------------------------------------------------
    def _size_extra_columns(self, tree):
        """Shrink the non-path columns to their contents so the path column keeps
        the rest of the pane; column 0 stays stretched."""
        hv = tree.header()
        for col in range(1, tree.columnCount()):
            tree.resizeColumnToContents(col)
            hv.resizeSection(col, min(max(hv.sectionSize(col) + 12, 90), 320))

    def _fill_two(self, pane, tree, rows, bsa_paths=frozenset()):
        rows = sorted(rows or [], key=lambda r: r[0].lower())
        tree.clear()
        pane._header.setText(self.tr("{0}  ({1})").format(pane._title, len(rows)))
        if not rows:
            it = QTreeWidgetItem(["(none)", ""])
            it.setForeground(0, QColor(_c(active_palette(), "TEXT_DIM")))
            it.setData(0, _INFO_ROLE, True)
            tree.addTopLevelItem(it)
            self._size_extra_columns(tree)
            return
        for path, other in rows:
            it = QTreeWidgetItem([path, other])
            if BSA_ROW_RE.match(path) or path in bsa_paths:
                it.setForeground(0, QColor(_c(active_palette(), "TONE_CYAN")))
                it.setData(0, _BSA_ROLE, True)
            tree.addTopLevelItem(it)
        self._size_extra_columns(tree)

    def _fill_one(self, pane, tree, rows):
        rows = sorted(rows or [], key=lambda s: s.lower())
        tree.clear()
        pane._header.setText(self.tr("{0}  ({1})").format(pane._title, len(rows)))
        if not rows:
            it = QTreeWidgetItem(["(none)"])
            it.setForeground(0, QColor(_c(active_palette(), "TEXT_DIM")))
            it.setData(0, _INFO_ROLE, True)
            tree.addTopLevelItem(it)
            return
        for path in rows:
            it = QTreeWidgetItem([path])
            if BSA_ROW_RE.match(path):
                it.setForeground(0, QColor(_c(active_palette(), "TONE_CYAN")))
                it.setData(0, _BSA_ROLE, True)
            tree.addTopLevelItem(it)
