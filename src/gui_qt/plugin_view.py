"""Plugin-tab view + delegate (Plugins tab, v1).

A QTreeView over PluginModel with a delegate that paints: enable checkbox, name
(dimmed when disabled), the ESL 'L' cyan badge + master indicator in the Flags
column, the lock column, and the load-order index. Single-click the checkbox to
toggle (persists to plugins.txt).
"""

from __future__ import annotations

import textwrap

from PySide6.QtCore import (
    Qt, QRect, QSize, QEvent, QTimer, QCoreApplication, QT_TRANSLATE_NOOP)
from PySide6.QtGui import QColor, QFont, QPen, QBrush, QPainter, QAction
from PySide6.QtWidgets import (
    QTreeView, QStyledItemDelegate, QStyle, QAbstractItemView,
    QToolTip, QToolButton,
)

from gui_qt import column_state

from gui_qt.theme_qt import active_palette, _c, qc, qc_contrast
from gui_qt.icons import icon
from gui_qt.modlist_header import TkStyleHeader
from gui_qt.plugin_model import (
    PluginModel, RowRole, PFlagsRole, PHighlightRole,
    COL_NAME, COL_FLAGS, COL_LOCK, COL_PRIORITY, COL_GAME_INDEX, COLUMNS,
)
from gui_qt.plugin_state import (
    PF_MISSING, PF_LATE, PF_VMM, PF_ESL, PF_LOOT, PF_DIRTY, PF_TAGS,
    PF_USERLIST, PF_UL_CYCLE, format_loot_tooltip,
)

_FLAG_SZ = 18
_FLAG_GAP = 4

# Header line for each master-check flag's bulleted tooltip (Tk parity).
# Wrapped in self.tr() at show time (see _flag_tip); registered for lupdate.
_MASTER_TIP_HEADERS = {
    PF_MISSING: QT_TRANSLATE_NOOP("PluginDelegate", "Missing masters:"),
    PF_LATE: QT_TRANSLATE_NOOP("PluginDelegate", "Masters loaded after this plugin:"),
    PF_VMM: QT_TRANSLATE_NOOP("PluginDelegate", "Version mismatched masters:"),
}

# Flag bit → icon filename, painted left→right (order matches the Tk app:
# missing, late, vmm, userlist dot, esl, loot, dirty, tags). The userlist dot
# and the ESL cyan "L" badge are drawn specially, not as icons.
_PLUGIN_FLAG_ICONS_PRE = [
    (PF_MISSING, "warning2.png"),
    (PF_LATE, "warning.png"),
    (PF_VMM, "info.png"),
]
_PLUGIN_FLAG_ICONS_POST = [
    (PF_LOOT, "Loot_info.png"),
    (PF_DIRTY, "brush.png"),
    (PF_TAGS, "tag.png"),
]

# Bash-tag flag hidden by default (mostly clutter); flip to True to show it.
SHOW_TAG_FLAG = False

ROW_H = 33
CHECK_BOX = 17
FONT_PX = 14
LOCK_SZ = 17

# Per-column min/default widths; Plugin Name (col 0) auto-fills like the modlist.
COL_DEFAULTS = {COL_FLAGS: 80, COL_LOCK: 40, COL_PRIORITY: 44, COL_GAME_INDEX: 60}
COL_MINS = {COL_NAME: 120, COL_FLAGS: 60, COL_LOCK: 36,
            COL_PRIORITY: 40, COL_GAME_INDEX: 50}
NAME_MIN = COL_MINS[COL_NAME]

# Columns hidden on a fresh INI (no saved state): only the game-index column;
# everything else shows by default.
_FIRST_RUN_HIDDEN = {COL_GAME_INDEX}


class PluginDelegate(QStyledItemDelegate):
    def __init__(self, parent=None):
        super().__init__(parent)
        p = active_palette()
        self.c_row = qc(p, "BG_ROW")
        self.c_row_alt = qc(p, "BG_ROW_ALT")
        self.c_sel = qc(p, "BG_SELECT")
        self.c_hover = qc(p, "BG_ROW_HOVER")
        self.c_text = qc(p, "TEXT_MAIN")
        self.c_text_dim = qc(p, "TEXT_DIM")
        self.c_text_on_sel = qc(p, "TEXT_ON_ACCENT")
        # Plugins whose userlist rules form a broken cycle get red name text.
        self.c_text_cycle = qc(p, "TEXT_ERR")
        self.c_tick = qc_contrast(p, "CHECK_FILL")   # tick reads on the checkbox fill
        self.c_border = qc(p, "BORDER")
        self.c_check = qc(p, "CHECK_FILL")   # checkbox fill when enabled
        self.c_check_off = qc(p, "BG_DEEP")
        self.c_esl = qc(p, "TONE_BLUE_SOFT")
        self.c_master = qc(p, "TEXT_WARN")
        # Userlist dot (Tk parity: TEXT_WHITE fill, STATUS_BADGE_RED when the
        # plugin's userlist rules form a cycle).
        self.c_ul_dot = qc(p, "TEXT_WHITE")
        self.c_ul_dot_cycle = qc(p, "STATUS_BADGE_RED")
        # Cross-panel highlight tints (exact Tk conflict colours).
        self.c_hl_higher = qc(p, "FILE_WIN")
        self.c_hl_lower = qc(p, "FILE_LOSE")
        self.c_hl_anchor = qc(p, "FILE_ANCHOR")
        # Masters of the selected plugin get their own green row tint (Tk
        # BG_GREEN_ROW), distinct from the conflict-higher green.
        self.c_hl_master = qc(p, "BG_GREEN_ROW")

    def sizeHint(self, opt, index):
        return QSize(opt.rect.width(), ROW_H)

    def paint(self, p, opt, index):
        r = opt.rect
        row = index.data(RowRole)
        p.save()
        p.setRenderHint(p.RenderHint.Antialiasing, False)

        selected = bool(opt.state & QStyle.State_Selected)
        hl = index.data(PHighlightRole) or 0
        highlighted = False
        if selected:
            p.fillRect(r, self.c_sel)
        elif hl == 3:
            p.fillRect(r, self.c_hl_master); highlighted = True
        elif hl == 2:
            p.fillRect(r, self.c_hl_anchor); highlighted = True
        elif hl == 1:
            p.fillRect(r, self.c_hl_higher); highlighted = True
        elif hl == -1:
            p.fillRect(r, self.c_hl_lower); highlighted = True
        elif opt.state & QStyle.State_MouseOver:
            p.fillRect(r, self.c_hover)
        else:
            p.fillRect(r, self.c_row_alt if index.row() % 2 else self.c_row)

        enabled = bool(row and row.enabled)
        vanilla = bool(row and row.vanilla)
        # Vanilla plugins are greyed (dim) regardless of enabled state.
        text_color = self.c_text_on_sel if (selected or highlighted) else (
            self.c_text_dim if (vanilla or not enabled) else self.c_text)
        # A broken userlist cycle overrides the name colour with error-red, so
        # the plugin reads as a problem even when not selected/highlighted.
        if not (selected or highlighted) and (
                (index.data(PFlagsRole) or 0) & PF_UL_CYCLE):
            text_color = self.c_text_cycle
        col = index.column()

        if col == COL_NAME:
            self._paint_name(p, r, row, enabled, vanilla, text_color)
        elif col == COL_FLAGS:
            self._paint_flags(p, r, index.data(PFlagsRole) or 0)
        elif col == COL_LOCK:
            self._paint_lock(p, r, index.model().is_locked(index.row()))
        elif col in (COL_PRIORITY, COL_GAME_INDEX):
            p.setPen(text_color)
            _f = QFont(); _f.setPixelSize(FONT_PX); p.setFont(_f)
            p.drawText(r, Qt.AlignVCenter | Qt.AlignHCenter,
                       index.data(Qt.DisplayRole) or "")
        p.restore()

    def _lock_rect(self, r):
        return QRect(r.center().x() - LOCK_SZ // 2,
                     r.top() + (r.height() - LOCK_SZ) // 2, LOCK_SZ, LOCK_SZ)

    def _paint_lock(self, p, r, locked):
        lk = self._lock_rect(r)
        p.setRenderHint(p.RenderHint.Antialiasing, True)
        p.setPen(QPen(self.c_border, 1))
        p.setBrush(QBrush(self.c_check_off))
        p.drawRoundedRect(lk, 3, 3)
        if locked:
            ic = icon("lock.png", LOCK_SZ - 2)
            if not ic.isNull():
                ic.paint(p, lk.adjusted(1, 1, -1, -1))
        p.setRenderHint(p.RenderHint.Antialiasing, False)

    def _paint_name(self, p, r, row, enabled, vanilla, text_color):
        box = QRect(r.left() + 10, r.top() + (r.height() - CHECK_BOX) // 2,
                    CHECK_BOX, CHECK_BOX)
        p.setRenderHint(p.RenderHint.Antialiasing, True)
        p.setPen(QPen(self.c_border, 1))
        # Vanilla: always-on but dimmed (greyed fill + grey tick) to read as
        # locked/non-interactive. Otherwise green when enabled, hollow when not.
        fill = (self.c_check_off if vanilla else
                (self.c_check if enabled else self.c_check_off))
        p.setBrush(QBrush(fill))
        p.drawRoundedRect(box, 3, 3)
        if enabled:
            p.setPen(QPen(self.c_text_dim if vanilla else self.c_tick, 2))
            p.drawLine(box.left() + 4, box.center().y() + 1,
                       box.center().x() - 1, box.bottom() - 4)
            p.drawLine(box.center().x() - 1, box.bottom() - 4,
                       box.right() - 3, box.top() + 4)
        p.setRenderHint(p.RenderHint.Antialiasing, False)

        tx = box.right() + 10
        p.setPen(text_color)
        _f = QFont(); _f.setPixelSize(FONT_PX); p.setFont(_f)
        name_rect = QRect(tx, r.top(), r.right() - tx - 6, r.height())
        elided = p.fontMetrics().elidedText(row.name if row else "",
                                            Qt.ElideRight, name_rect.width())
        p.drawText(name_rect, Qt.AlignVCenter | Qt.AlignLeft, elided)

    @staticmethod
    def _flag_items(bits):
        """Ordered flag glyphs in the Tk draw order — (kind, bit, icon_name):
        warning icons, the userlist dot, the ESL 'L' badge, then LOOT/dirty/tags.
        Shared by _paint_flags and _hit_flag_bit so hover and hit-test agree."""
        items = []
        for bit, name in _PLUGIN_FLAG_ICONS_PRE:
            if bits & bit:
                items.append(("icon", bit, name))
        if bits & PF_USERLIST:
            items.append(("uldot", PF_USERLIST, None))
        if bits & PF_ESL:
            items.append(("esl", PF_ESL, None))
        for bit, name in _PLUGIN_FLAG_ICONS_POST:
            if bit == PF_TAGS and not SHOW_TAG_FLAG:
                continue
            if bits & bit:
                items.append(("icon", bit, name))
        return items

    def _paint_flags(self, p, r, bits):
        # (There is no master indicator — Tk doesn't show one; masters are
        # implied by extension.)
        items = self._flag_items(bits)
        if not items:
            return
        sz = _FLAG_SZ
        total = len(items) * sz + (len(items) - 1) * _FLAG_GAP
        x = r.left() + max(4, (r.width() - total) // 2)
        cy = r.center().y()
        for kind, _bit, name in items:
            cell = QRect(x, cy - sz // 2, sz, sz)
            if kind == "esl":
                f = QFont(); f.setBold(True); f.setPixelSize(13); p.setFont(f)
                p.setPen(self.c_esl)
                p.drawText(cell, Qt.AlignCenter, "L")
            elif kind == "uldot":
                # Small filled circle: white = managed in userlist.yaml,
                # red = its rules currently form a broken cycle (Tk parity).
                dot_r = 4
                p.setRenderHint(p.RenderHint.Antialiasing, True)
                p.setPen(Qt.NoPen)
                p.setBrush(QBrush(self.c_ul_dot_cycle if (bits & PF_UL_CYCLE)
                                  else self.c_ul_dot))
                p.drawEllipse(cell.center(), dot_r, dot_r)
                p.setRenderHint(p.RenderHint.Antialiasing, False)
            else:
                ic = icon(name, sz)
                if not ic.isNull():
                    ic.paint(p, cell)
            x += sz + _FLAG_GAP

    def _hit_flag_bit(self, pos, r, bits):
        """Which PF_* bit's glyph (if any) is under *pos* within the Flags cell
        rect *r*. Recomputes the same centred geometry as _paint_flags so the
        hover lands on the glyph the user sees."""
        items = self._flag_items(bits)
        if not items:
            return 0
        sz = _FLAG_SZ
        total = len(items) * sz + (len(items) - 1) * _FLAG_GAP
        x = r.left() + max(4, (r.width() - total) // 2)
        y = r.center().y() - sz // 2
        for _kind, bit, _name in items:
            if QRect(x, y, sz, sz).contains(pos):
                return bit
            x += sz + _FLAG_GAP
        return 0

    def _flag_tip(self, hit, index):
        """Tooltip text for the hovered flag bit *hit* (Tk parity). Master-check
        and LOOT flags render the captured per-plugin detail; ESL/userlist use
        fixed strings. Returns None when there's nothing to show."""
        if hit == PF_ESL:
            return "This plugin is marked as Light (ESL)"

        row = index.data(RowRole)
        if hit == PF_USERLIST:
            bits = index.data(PFlagsRole) or 0
            if bits & PF_UL_CYCLE:
                msg = ("This plugin has a broken cycle, "
                       "Right click > Show cycle for info")
            else:
                msg = "This plugin is managed by userlist.yaml"
            model = index.model()
            grp = None
            if row is not None and hasattr(model, "userlist_group"):
                grp = model.userlist_group(row.name)
            if grp:
                msg += f"\nGroup: {grp}"
            return msg

        if hit in _MASTER_TIP_HEADERS:
            names = None
            if row is not None:
                names = {PF_MISSING: row.missing_masters,
                         PF_LATE: row.late_masters,
                         PF_VMM: row.vmm_masters}.get(hit)
            header = self.tr(_MASTER_TIP_HEADERS[hit])
            if names:
                body = "\n".join(f"  - {n}" for n in names)
                return f"{header}\n{body}"
            return header

        if hit in (PF_LOOT, PF_DIRTY, PF_TAGS):
            if row is None or not row.loot_info:
                return None
            model = index.model()
            enabled_lower = (model.enabled_lower()
                             if hasattr(model, "enabled_lower") else set())
            return format_loot_tooltip(row.loot_info, enabled_lower) or None
        return None

    @staticmethod
    def _wrap_tip(text, width=100):
        """Cap tooltip line length: Qt doesn't word-wrap plain-text tooltips, so
        a long LOOT message stretches the tip across the screen. Wrap each line
        to *width* chars, indenting continuations past the bullet/leading
        whitespace so the section structure stays readable."""
        out = []
        for line in text.split("\n"):
            if len(line) <= width:
                out.append(line)
                continue
            lead = line[:len(line) - len(line.lstrip())]
            cont = lead + ("  " if line.lstrip().startswith(("-", "[")) else "")
            out.append(textwrap.fill(line, width=width, subsequent_indent=cont,
                                     break_long_words=False, break_on_hyphens=False))
        return "\n".join(out)

    def helpEvent(self, event, view, opt, index):
        """Show the per-flag tooltip when hovering a flag glyph (Tk parity)."""
        try:
            if (event.type() == QEvent.ToolTip
                    and index.isValid() and index.column() == COL_FLAGS):
                bits = index.data(PFlagsRole) or 0
                if bits:
                    hit = self._hit_flag_bit(event.pos(), opt.rect, bits)
                    tip = self._flag_tip(hit, index)
                    if tip:
                        # Pass the flags-cell rect so Qt hides the tooltip as soon
                        # as the cursor leaves the cell.
                        QToolTip.showText(event.globalPos(), self._wrap_tip(tip),
                                          view, opt.rect)
                        return True
                QToolTip.hideText()
        except Exception:
            pass
        return super().helpEvent(event, view, opt, index)

    def editorEvent(self, event, model, opt, index):
        if event.type() != QEvent.MouseButtonRelease:
            return False
        pos = event.position().toPoint()
        if index.column() == COL_NAME:
            box = QRect(opt.rect.left() + 6, opt.rect.top(), 26, opt.rect.height())
            if box.contains(pos):
                model.toggle(index.row())
                return True
        elif index.column() == COL_LOCK:
            if self._lock_rect(opt.rect).contains(pos):
                model.toggle_lock(index.row())
                return True
        return False


class PluginView(QTreeView):
    def __init__(self, model: PluginModel, parent=None):
        super().__init__(parent)
        self.setModel(model)
        self.setItemDelegate(PluginDelegate(self))
        self.setRootIsDecorated(False)
        self.setUniformRowHeights(True)
        self.setAlternatingRowColors(False)
        self.setMouseTracking(True)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)

        self._plugin_owner: dict = {}
        self._search_hidden: set[int] = set()
        self._filter_hidden: set[int] = set()
        # Delta cache for _apply_hidden — row indices go stale on structural
        # changes, so drop it there (same scheme as the modlist view).
        self._applied_hidden: set[int] | None = None

        def _drop_applied(*_a):
            self._applied_hidden = None
        for sig in (model.modelReset, model.rowsInserted, model.rowsRemoved,
                    model.rowsMoved, model.layoutChanged):
            sig.connect(_drop_applied)
        # Custom drag-reorder (vanilla pinned at top, locked rows immovable).
        self._drag_rows: list[int] = []
        self._drag_active = False
        self._press_row = -1
        self._press_pos = None
        self._drop_slot = -1
        self._DRAG_THRESHOLD = 6
        self._scroll_zone = 40
        self._scroll_timer = QTimer(self)
        self._scroll_timer.setInterval(16)
        self._scroll_timer.timeout.connect(self._autoscroll_tick)
        self._last_mouse_y = 0

        # Same Tk-style column resize as the modlist (boundary drag, fill-width,
        # no overflow). Plugin Name (col 0) is the fill column.
        h = TkStyleHeader(self, COL_MINS, COL_DEFAULTS)
        self.setHeader(h)
        h.setSectionsClickable(True)
        h.setMinimumSectionSize(min(COL_MINS.values()))
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        for col, w in COL_DEFAULTS.items():
            self.setColumnWidth(col, w)

        # Column show/hide menu + width/order/hidden persistence, mirroring the
        # modlist (saved to a separate [qt_columns_plugins] INI section).
        self._restoring = False
        self._pinning_name = False
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(400)
        self._save_timer.timeout.connect(self._save_column_state)
        h.sectionResized.connect(lambda *a: self._schedule_save())
        h.sectionMoved.connect(self._on_section_moved)
        self._build_column_menu_button(h)
        self._restore_column_state()

        from gui_qt.marker_strip import install_marker_strip, MarkerScrollBar
        install_marker_strip(self, PHighlightRole, code_map={
            -1: MarkerScrollBar._C_LOWER,    # loses conflict (red)
            1: MarkerScrollBar._C_HIGHER,    # wins conflict (green)
            2: MarkerScrollBar._C_ANCHOR,    # selected mod's plugins (orange)
            3: MarkerScrollBar._C_MASTER,    # master of selected plugin (green)
        })
        self._reposition_marker_strip()

        # Right-click context menu (mirrors modlist_view).
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_context_menu)

    def _on_context_menu(self, pos):
        index = self.indexAt(pos)
        if not index.isValid():
            return
        selected = {i.row() for i in self.selectionModel().selectedRows()}
        if index.row() not in selected:
            self.setCurrentIndex(index)   # right-click outside selection → select it
        from gui_qt.plugin_menu import show_context_menu
        show_context_menu(self, self.viewport().mapToGlobal(pos), index)

    def _reposition_marker_strip(self):
        from gui_qt.marker_strip import reposition_marker_strip
        reposition_marker_strip(self)

    # ---- column show/hide menu (eye button, over the checkboxes) ----------
    def _build_column_menu_button(self, header):
        """Eye button pinned to the LEFT of the Plugin Name header, centred over
        the row checkbox column below; opens a checkable show/hide menu (Name and
        the icon-only Lock column always stay)."""
        btn = QToolButton(header)
        # Tint the eye glyph to the theme foreground so it reads in both modes.
        btn.setIcon(icon("eye1_white.png", 16, color=_c(active_palette(), "TEXT_MAIN")))
        btn.setIconSize(QSize(16, 16))
        btn.setCursor(Qt.ArrowCursor)
        btn.setFocusPolicy(Qt.NoFocus)
        btn.setAutoRaise(True)
        btn.setToolTip(self.tr("Show / Hide columns"))
        bg = _c(active_palette(), "BG_HEADER")
        btn.setStyleSheet(
            f"QToolButton {{ background: {bg}; border: none; padding: 0px; }}")
        btn.clicked.connect(self._show_column_menu)
        self._col_menu_btn = btn
        # Hooks the window sets for the "Filters" submenu in the column menu:
        # on_quick_filter(key, 0|1) applies a plugin status filter;
        # quick_filter_state(key)->int reads its current tri-state for the
        # check marks. (Mirrors the modlist column-menu quick filters.)
        self.on_quick_filter = None
        self.quick_filter_state = None
        self._position_column_menu_button()
        btn.show()

    _COL_BTN_W = 26

    def _position_column_menu_button(self):
        btn = getattr(self, "_col_menu_btn", None)
        if btn is None:
            return
        h = self.header()
        # Centre on the row checkbox below it: the delegate draws the checkbox at
        # (col_left + 10, …) with width CHECK_BOX (see _paint_name).
        col_left = h.sectionViewportPosition(COL_NAME)
        cb_center = col_left + 10 + CHECK_BOX // 2
        x = cb_center - self._COL_BTN_W // 2
        btn.setGeometry(max(0, x), 0, self._COL_BTN_W, h.height())
        btn.raise_()

    def _show_column_menu(self):
        from gui_qt.modlist_view import _StayOpenMenu
        menu = _StayOpenMenu(self)
        for col, name in enumerate(COLUMNS):
            if col in (COL_NAME, COL_LOCK):
                continue   # Name always shown; Lock is icon-only (no label)
            a = QAction(QCoreApplication.translate("PluginModel", name), menu)
            a.setCheckable(True)
            a.setChecked(not self.isColumnHidden(col))
            a.toggled.connect(lambda checked, c=col: self._set_column_visible(c, checked))
            menu.addAction(a)
        # A "Filters" submenu giving quick access to the plugin "By status"
        # filters from the Filters panel. Same include-mode (state 1) semantics;
        # the window wires on_quick_filter so the panel stays in sync.
        from gui_qt.modlist_filter import PLUGIN_STATUS_FILTERS
        menu.addSeparator()
        get = getattr(self, "quick_filter_state", None)
        filters = _StayOpenMenu(self.tr("Filters"), menu)
        for key, label in PLUGIN_STATUS_FILTERS:
            # Labels are registered for translation under FilterSidePanel.
            a = QAction(QCoreApplication.translate("FilterSidePanel", label), filters)
            a.setCheckable(True)
            a.setChecked(callable(get) and get(key) == 1)
            a.toggled.connect(lambda checked, k=key: self._on_quick_filter(k, checked))
            filters.addAction(a)
        menu.addMenu(filters)
        btn = self._col_menu_btn
        menu.exec(btn.mapToGlobal(btn.rect().bottomLeft()))

    def _on_quick_filter(self, key: str, on: bool):
        cb = getattr(self, "on_quick_filter", None)
        if callable(cb):
            cb(key, 1 if on else 0)

    def _set_column_visible(self, col: int, visible: bool):
        self.setColumnHidden(col, not visible)
        if visible and self.columnWidth(col) <= 0:
            # Qt collapses a hidden section to 0; restore a sensible width so the
            # re-shown column is actually visible.
            self.header().resizeSection(col, COL_DEFAULTS.get(col, 60))
        self._fit_name_to_width()   # Name re-absorbs/releases the freed width
        self.viewport().update()
        self._schedule_save()

    def _on_section_moved(self, logical, old_visual, new_visual):
        """Persist column order after a drag-reorder, keeping Plugin Name pinned
        as the first (stretch + menu-button) column."""
        if self._restoring or self._pinning_name:
            return
        h = self.header()
        if h.visualIndex(COL_NAME) != 0:
            self._pinning_name = True
            h.moveSection(h.visualIndex(COL_NAME), 0)
            self._pinning_name = False
        self._position_column_menu_button()
        self.viewport().update()
        self._schedule_save()

    # ---- column-state persistence (keyed by logical column name) ----------
    def _schedule_save(self):
        if not self._restoring:
            self._save_timer.start()

    def _save_column_state(self):
        h = self.header()
        widths = {COLUMNS[c]: self.columnWidth(c) for c in range(len(COLUMNS))}
        order = [COLUMNS[h.logicalIndex(v)] for v in range(len(COLUMNS))]
        hidden = {COLUMNS[c] for c in range(len(COLUMNS)) if self.isColumnHidden(c)}
        # Lock's label is "" — skip it from name-keyed persistence (would collide
        # with an empty order token); its visibility never changes anyway.
        widths.pop("", None)
        hidden.discard("")
        order = [n for n in order if n]
        column_state.save_state(widths, order, hidden, None, True,
                                section="qt_columns_plugins")

    def _restore_column_state(self):
        st = column_state.load_state(section="qt_columns_plugins", columns=COLUMNS)
        self._restoring = True
        try:
            if not (st["widths"] or st["order"] or st["hidden"]):
                # Fresh INI: hide only the game-index column; show the rest.
                for col in _FIRST_RUN_HIDDEN:
                    self.setColumnHidden(col, True)
                return
            name_to_col = {n: i for i, n in enumerate(COLUMNS) if n}
            for name, w in st["widths"].items():
                if name in name_to_col and name != "Plugin Name":  # Name stays stretch
                    self.setColumnWidth(name_to_col[name], w)
            for name in st["hidden"]:
                if name in name_to_col:
                    self.setColumnHidden(name_to_col[name], True)
            h = self.header()
            for visual, name in enumerate(st["order"]):
                if name in name_to_col:
                    cur = h.visualIndex(name_to_col[name])
                    if cur != -1 and cur != visual:
                        h.moveSection(cur, visual)
        finally:
            self._restoring = False

    # ---- search + filter row hiding --------------------------------------
    def _apply_hidden(self) -> None:
        """Hide the UNION of search-hidden and filter-hidden rows so the search
        box and the Filters panel compose instead of clobbering each other.
        Only the delta against the last applied set is touched (setRowHidden is
        per-row layout work and this runs per search keystroke)."""
        hidden = self._search_hidden | self._filter_hidden
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
        sb = self.verticalScrollBar()
        if sb is not None:
            sb.update()

    def set_search_hidden(self, rows: set[int]) -> None:
        """Hide the given rows (search box). Empty set shows everything."""
        self._search_hidden = set(rows or ())
        self._apply_hidden()

    def set_filter_hidden(self, rows: set[int]) -> None:
        """Hide the given rows (Filters panel). Empty set clears the filter."""
        self._filter_hidden = set(rows or ())
        self._apply_hidden()

    # ---- cross-panel highlights ------------------------------------------
    def set_plugin_owner(self, owner: dict):
        """owner maps plugin filename (lower) → owning mod name."""
        self._plugin_owner = dict(owner or {})

    def selected_owner_mods(self, owner: dict) -> set:
        """The mods that own the currently-selected plugins."""
        m = self.model()
        mods: set = set()
        for idx in self.selectionModel().selectedRows():
            r = m.row(idx.row())
            mod = (owner or {}).get(r.name.lower())
            if mod:
                mods.add(mod)
        return mods

    # ---- persistent marker-strip overlays (Tk parity) --------------------
    def refresh_missing_marker(self) -> None:
        """Repaint the persistent red marker-strip ticks for every plugin that
        has missing masters (PF_MISSING flag). Selection-independent — mirrors
        the Tk marker strip's top-priority 'missing masters' band. Call after
        the model's rows change (reload)."""
        sb = getattr(self, "_marker_strip", None)
        if sb is None:
            return
        m = self.model()
        rows = {i for i in range(m.rowCount())
                if (m.row(i).flags & PF_MISSING)}
        sb.set_persistent_rows(missing=rows)

    def refresh_cycle_marker(self) -> None:
        """Repaint the persistent red marker-strip ticks for every plugin whose
        userlist rules form a broken cycle (PF_UL_CYCLE flag). Selection-
        independent, mirrors refresh_missing_marker. Call after the model's rows
        change (reload)."""
        sb = getattr(self, "_marker_strip", None)
        if sb is None:
            return
        m = self.model()
        rows = {i for i in range(m.rowCount())
                if (m.row(i).flags & PF_UL_CYCLE)}
        sb.set_persistent_rows(cycle=rows)

    def set_master_highlight(self, master_names_lower: set) -> None:
        """Green-highlight the rows (and marker-strip ticks) whose plugin is a
        master of the currently-selected plugin (Tk parity). Pass an empty set
        to clear. Uses highlight code 3 (BG_GREEN_ROW tint), which the delegate
        prioritises over the cross-panel conflict/anchor tints."""
        sb = getattr(self, "_marker_strip", None)
        wanted = {n.lower() for n in (master_names_lower or ())}
        m = self.model()
        rows = {i for i in range(m.rowCount())
                if m.row(i).name.lower() in wanted}
        if sb is not None:
            sb.set_persistent_rows(master=rows)
        # Tint the master rows green in the list body too (Tk BG_GREEN_ROW).
        m.set_highlights({n: 3 for n in wanted})

    def set_highlight_from_mods(self, mod_names: set, bsa_higher: set,
                                bsa_lower: set, owner: dict,
                                bsa_index_path=None):
        """Highlight plugins from a modlist selection (Tk parity):
          - orange (anchor): plugins of the selected mod(s) — unconditional.
          - green/red: plugins of mods in a *BSA* conflict with the selection,
            and ONLY plugins that actually own a BSA. Loose-file conflicts do
            NOT colour plugins (a standalone plugin loads no archive contents).
        owner maps plugin filename(lower) → mod name."""
        # Invert owner → mod → [plugin names(lower)].
        mod_to_plugins: dict[str, list[str]] = {}
        for plugin, mod in (owner or {}).items():
            mod_to_plugins.setdefault(mod, []).append(plugin)

        bsa_filter = self._bsa_owning_plugins(
            (bsa_higher or set()) | (bsa_lower or set()),
            mod_to_plugins, bsa_index_path)

        hl: dict[str, int] = {}
        for mod in (bsa_lower or set()):
            for pl in mod_to_plugins.get(mod, []):
                if pl in bsa_filter:
                    hl[pl] = -1
        for mod in (bsa_higher or set()):
            for pl in mod_to_plugins.get(mod, []):
                if pl in bsa_filter:
                    hl[pl] = 1
        for mod in (mod_names or set()):
            for pl in mod_to_plugins.get(mod, []):
                hl[pl] = 2   # anchor wins over conflict tint
        self.model().set_highlights(hl)
        self.viewport().update()

    def _bsa_owning_plugins(self, mods: set, mod_to_plugins: dict,
                            bsa_index_path) -> set:
        """{plugin filename(lower)} for plugins in *mods* that own a BSA via
        basename match — reuses the backend's _bsa_owning_plugin (Tk parity)."""
        if not mods or bsa_index_path is None:
            return set()
        try:
            from Utils.bsa_filemap import read_bsa_index, _bsa_owning_plugin
        except Exception:
            return set()
        idx = read_bsa_index(bsa_index_path) or {}
        result: set = set()
        for mod in mods:
            archives = idx.get(mod)
            if not archives:
                continue
            plugins = mod_to_plugins.get(mod, [])
            stems = {p.rsplit(".", 1)[0].lower(): p for p in plugins}
            if not stems:
                continue
            for bsa_name, _mt, _paths in archives:
                bsa_stem = bsa_name.rsplit(".", 1)[0]
                owning = _bsa_owning_plugin(bsa_stem, set(stems.keys()))
                if owning is not None and owning in stems:
                    result.add(stems[owning])
        return result

    # ---- custom drag-reorder ---------------------------------------------
    def _drag_block_for(self, row: int) -> list[int] | None:
        m = self.model()
        if not m.is_movable(row):
            return None
        sel = sorted({i.row() for i in self.selectionModel().selectedRows()})
        if row in sel and len(sel) > 1:
            carry = [r for r in sel if m.is_movable(r)]
            # Contiguous only (model.move_rows requires it).
            if carry and carry[-1] - carry[0] == len(carry) - 1:
                return carry
        return [row]

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            idx = self.indexAt(event.position().toPoint())
            self._press_row = idx.row() if idx.isValid() else -1
            self._press_pos = event.position().toPoint()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if not (event.buttons() & Qt.LeftButton) or self._press_row < 0:
            super().mouseMoveEvent(event)
            return
        if not self._drag_active:
            if self._press_pos is None or (
                    event.position().toPoint() - self._press_pos
            ).manhattanLength() < self._DRAG_THRESHOLD:
                return
            block = self._drag_block_for(self._press_row)
            if block is None:
                self._press_row = -1
                return
            self._drag_active = True
            self._drag_rows = block
            self.setCursor(Qt.ClosedHandCursor)
        self._last_mouse_y = event.position().toPoint().y()
        self._update_drop_slot(self._last_mouse_y)
        if not self._scroll_timer.isActive():
            self._scroll_timer.start()
        self.viewport().update()

    def mouseReleaseEvent(self, event):
        if self._drag_active:
            self._scroll_timer.stop()
            if self._drag_rows and self._drop_slot >= 0:
                self.model().move_rows(self._drag_rows, self._drop_slot)
            self._drag_active = False
            self._drag_rows = []
            self._drop_slot = -1
            self.unsetCursor()
            self.viewport().update()
            self._press_row = -1
            return
        self._press_row = -1
        super().mouseReleaseEvent(event)

    def _visible_rows(self) -> list[int]:
        """Rows not hidden by the search box / filter panel."""
        m = self.model()
        return [r for r in range(m.rowCount())
                if not self.isRowHidden(r, self.rootIndex())]

    def _update_drop_slot(self, y: int):
        m = self.model()
        n = m.rowCount()
        vis = self._visible_rows()
        if not vis:
            self._drop_slot = 0
            return
        slot = None
        for r in vis:
            rect = self.visualRect(m.index(r, 0))
            if rect.top() <= y < rect.bottom():
                slot = r if y < rect.center().y() else r + 1
                break
        if slot is None:
            first = self.visualRect(m.index(vis[0], 0))
            slot = vis[0] if y < first.top() else vis[-1] + 1
        slot = max(0, min(slot, n))
        # Never leave the slot on a hidden (filtered-out) row: visualRect()
        # of a hidden row is empty (top()==0), which would draw the indicator
        # at the viewport top instead of under the cursor. Snap to the next
        # visible row so the line and the drop agree.
        if 0 < slot < n and self.isRowHidden(slot, self.rootIndex()):
            nxt = next((r for r in vis if r >= slot), None)
            slot = nxt if nxt is not None else n
        self._drop_slot = slot

    def _autoscroll_tick(self):
        if not self._drag_active:
            self._scroll_timer.stop()
            return
        h = self.viewport().height()
        y = self._last_mouse_y
        zone = self._scroll_zone
        bar = self.verticalScrollBar()
        step = 0
        if y < zone:
            step = -int(2 + (zone - y) / zone * 22)
        elif y > h - zone:
            step = int(2 + (y - (h - zone)) / zone * 22)
        if step:
            bar.setValue(bar.value() + step)
            self._update_drop_slot(y)
            self.viewport().update()

    def paintEvent(self, event):
        super().paintEvent(event)
        if not self._drag_active or self._drop_slot < 0:
            return
        m = self.model()
        n = m.rowCount()
        # Anchor the line to visible rows only: visualRect() of a hidden row
        # (filtered out) is empty, which would paint the line at y=0.
        if self._drop_slot < n and not self.isRowHidden(self._drop_slot,
                                                        self.rootIndex()):
            y = self.visualRect(m.index(self._drop_slot, 0)).top()
        else:
            vis = self._visible_rows()
            prev = next((r for r in reversed(vis) if r < self._drop_slot), None)
            if prev is None:
                return
            y = self.visualRect(m.index(prev, 0)).bottom()
        p = QPainter(self.viewport())
        pen = QPen(QColor(_c(active_palette(), "HIGHLIGHT_DRAG")))
        pen.setWidth(2); p.setPen(pen)
        p.drawLine(0, y, self.viewport().width(), y)
        p.end()

    def showEvent(self, event):
        super().showEvent(event)
        self._fit_name_to_width()
        self._reposition_marker_strip()
        self._position_column_menu_button()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_name_to_width()
        if hasattr(self, "_marker_strip"):
            self._reposition_marker_strip()
        self._position_column_menu_button()

    def _fit_name_to_width(self):
        vp = self.viewport().width()
        if vp <= 0:
            return
        from gui_qt.plugin_model import COLUMNS
        others = sum(self.columnWidth(c) for c in range(len(COLUMNS))
                     if c != COL_NAME and not self.isColumnHidden(c))
        target = vp - others
        h = self.header()
        if target >= NAME_MIN:
            if target != self.columnWidth(COL_NAME):
                h.resizeSection(COL_NAME, target)
            return
        h.resizeSection(COL_NAME, NAME_MIN)
        deficit = (NAME_MIN + others) - vp
        for c in reversed([c for c in range(len(COLUMNS))
                           if c != COL_NAME and not self.isColumnHidden(c)]):
            if deficit <= 0:
                break
            room = self.columnWidth(c) - COL_MINS.get(c, 40)
            if room <= 0:
                continue
            take = min(room, deficit)
            h.resizeSection(c, self.columnWidth(c) - take)
            deficit -= take
