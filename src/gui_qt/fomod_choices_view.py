"""Editable FOMOD choices tree with session drafts and reinstall support."""

from __future__ import annotations

from copy import deepcopy

from PySide6.QtCore import Qt, Signal, QRect, QEvent
from PySide6.QtGui import QFont, QPen, QBrush
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QTreeWidget, QTreeWidgetItem,
    QStyledItemDelegate, QAbstractItemView, QPushButton,
)

from gui_qt.theme_qt import (active_palette, bind_theme, _c, qc, qc_contrast,
                             close_button)
from gui_qt.icons import icon
from gui_qt.worker import run_in_worker

CHECK_BOX = 17        # same as the modlist checkbox
ARROW_SZ = 20         # same as the modlist separator arrow
INDENT = 18           # per-depth indent for the tree column
FONT_PX = 13

COL_NAME = 0
COL_DESC = 1

# Row kinds, so the delegate can paint without re-deriving structure.
_KIND_ROLE = Qt.UserRole + 30
_KIND_STEP = "step"
_KIND_GROUP = "group"
_KIND_OPTION = "option"
_KIND_INFO = "info"
_SELECTED_ROLE = Qt.UserRole + 31   # option rows: True when picked
_STATE_ROLE = Qt.UserRole + 32
_OPTION_ROLE = Qt.UserRole + 33

# FOMOD's selection types, spelled the way the wizard presents them.
_GROUP_TYPE_LABELS = {
    "SelectExactlyOne": "select one",
    "SelectAtMostOne": "select at most one",
    "SelectAtLeastOne": "select at least one",
    "SelectAny": "select any",
    "SelectAll": "all required",
}


class FomodChoicesDelegate(QStyledItemDelegate):
    """Draws the shared arrow indicator + per-depth indent and a modlist-style
    checkbox for option rows (ticked = the option the install used)."""

    def __init__(self, view, parent=None):
        super().__init__(parent or view)
        self._view = view
        bind_theme(self, roles={
            "TEXT_MAIN", "TEXT_DIM", "BORDER_FAINT", "CHECK_FILL", "BG_DEEP",
            "BG_SELECT", "DROPDOWN_ARROW", "TONE_GREEN", "ACCENT",
        })

    def refresh_theme(self, p: dict) -> None:
        self.c_text = qc(p, "TEXT_MAIN")
        self.c_dim = qc(p, "TEXT_DIM")
        self.c_sel_text = qc(p, "TONE_GREEN")
        self.c_border = qc(p, "BORDER_FAINT")
        self.c_check = qc(p, "CHECK_FILL")
        self.c_check_off = qc(p, "BG_DEEP")
        self.c_tick = qc_contrast(p, "CHECK_FILL")
        self.c_sel = qc(p, "BG_SELECT")
        self.c_arrow = _c(p, "DROPDOWN_ARROW")
        self.c_new = qc(p, "ACCENT")
        self._view.viewport().update()

    def paint(self, p, opt, index):
        r = opt.rect
        kind = index.data(_KIND_ROLE) or _KIND_INFO
        if opt.state & opt.state.State_Selected:
            p.fillRect(r, self.c_sel)

        f = QFont()
        f.setPixelSize(FONT_PX)
        # Wizard pages are the structural rows - bold, like a section head.
        f.setBold(kind == _KIND_STEP)
        p.setFont(f)

        if index.column() != COL_NAME:
            p.setPen(self.c_dim)
            text_rect = QRect(r.left() + 4, r.top(),
                              max(0, r.width() - 8), r.height())
            fm = p.fontMetrics()
            p.drawText(text_rect, Qt.AlignVCenter | Qt.AlignLeft,
                       fm.elidedText(index.data() or "", Qt.ElideRight,
                                     text_rect.width()))
            return

        depth = self._depth(index)
        x = r.left() + 4 + depth * INDENT

        # Arrow only where there is something to expand.
        if self._view.model().rowCount(index) > 0:
            a = QRect(x, r.top() + (r.height() - ARROW_SZ) // 2,
                      ARROW_SZ, ARROW_SZ)
            expanded = self._view.isExpanded(index)
            ico = icon("arrow.png" if expanded else "right.png", ARROW_SZ,
                       color=self.c_arrow)
            if not ico.isNull():
                ico.paint(p, a)
        x += ARROW_SZ + 2

        if kind == _KIND_OPTION:
            selected = bool(index.data(_SELECTED_ROLE))
            box = QRect(x, r.top() + (r.height() - CHECK_BOX) // 2,
                        CHECK_BOX, CHECK_BOX)
            self._paint_check(p, box, selected)
            x += CHECK_BOX + 6
            state = index.data(_STATE_ROLE)
            if state is None:
                color = self.c_sel_text if selected else self.c_dim
            elif state.locked:
                color = self.c_dim
            elif state.previously_selected:
                color = self.c_sel_text
            elif state.newly_available:
                color = self.c_new
            else:
                color = self.c_dim if state.missing_dependency else self.c_text
            p.setPen(color)
        elif kind == _KIND_INFO:
            p.setPen(self.c_dim)
        else:
            p.setPen(self.c_text)

        text_rect = QRect(x, r.top(), max(0, r.right() - x - 4), r.height())
        fm = p.fontMetrics()
        p.drawText(text_rect, Qt.AlignVCenter | Qt.AlignLeft,
                   fm.elidedText(index.data() or "", Qt.ElideRight,
                                 text_rect.width()))

    def _paint_check(self, p, box, on):
        p.setRenderHint(p.RenderHint.Antialiasing, True)
        p.setPen(QPen(self.c_border, 1))
        p.setBrush(QBrush(self.c_check if on else self.c_check_off))
        p.drawRoundedRect(box, 3, 3)
        if on:
            p.setPen(QPen(self.c_tick, 2))
            p.drawLine(box.left() + 4, box.center().y() + 1,
                       box.center().x() - 1, box.bottom() - 4)
            p.drawLine(box.center().x() - 1, box.bottom() - 4,
                       box.right() - 3, box.top() + 4)
        p.setRenderHint(p.RenderHint.Antialiasing, False)

    def _depth(self, index) -> int:
        d = 0
        idx = index.parent()
        while idx.isValid():
            d += 1
            idx = idx.parent()
        return d

    def sizeHint(self, opt, index):
        s = super().sizeHint(opt, index)
        s.setHeight(max(s.height(), 22))
        return s

    def editorEvent(self, event, model, option, index):
        return False


class FomodChoicesView(QWidget):
    """Full-tab editor for one mod's FOMOD choices."""

    # FomodChoices (or None) from the load worker → UI.
    _ready = Signal(object)

    def __init__(self, mod_name, profile_dir, game_name="",
                 on_close=None, log_fn=None, controller=None):
        super().__init__()
        self._mod_name = mod_name
        self._profile_dir = str(profile_dir) if profile_dir else ""
        self._game_name = game_name or ""
        self._on_close = on_close or (lambda: None)
        self._log = log_fn or (lambda _m: None)
        self._controller = controller
        self._session = None
        self._config_id = ""
        self._message = ""
        self._rows = {}
        self._steps = {}
        self._context_loading = False
        # Guards a late worker result from a mod the user already moved off of.
        self._load_token = 0
        self.setObjectName("FomodChoicesView")
        self._ready.connect(self._on_ready)
        self._build()
        if controller is not None:
            controller.changed.connect(self._controller_changed)
        bind_theme(self, roles={"TEXT_DIM", "TEXT_MAIN"})
        self._start()

    # ---- layout -----------------------------------------------------------
    def _build(self):
        p = active_palette()
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        bar = QWidget(); bar.setObjectName("HeaderBar")
        hb = QHBoxLayout(bar); hb.setContentsMargins(12, 8, 8, 8)
        self._title = QLabel(self._title_text())
        self._title.setStyleSheet(
            f"color:{_c(p,'TEXT_MAIN')}; font-weight:600; font-size:15px;")
        hb.addWidget(self._title)
        hb.addStretch(1)
        close = close_button(pal=p)
        close.clicked.connect(lambda: self._on_close())
        hb.addWidget(close)
        v.addWidget(bar)

        self._tree = QTreeWidget()
        self._tree.setColumnCount(2)
        self._tree.setHeaderLabels([self.tr("Option"), self.tr("Description")])
        # No native branch decoration / indent: the delegate draws both, so the
        # arrows match the Mod Files and Data trees.
        self._tree.setRootIsDecorated(False)
        self._tree.setIndentation(0)
        self._tree.setUniformRowHeights(True)
        self._tree.setAlternatingRowColors(False)
        self._tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self._tree.setExpandsOnDoubleClick(False)
        self._tree.installEventFilter(self)
        self._tree.setItemDelegate(FomodChoicesDelegate(self._tree))
        self._tree.expanded.connect(lambda *_: self._tree.viewport().update())
        self._tree.collapsed.connect(lambda *_: self._tree.viewport().update())

        from gui_qt.modlist_header import TkStyleHeader
        col_mins = {COL_NAME: 160, COL_DESC: 120}
        col_defaults = {COL_NAME: 420}
        hdr = TkStyleHeader(self._tree, col_mins, col_defaults)
        self._tree.setHeader(hdr)
        hdr.setMinimumSectionSize(min(col_mins.values()))
        hdr.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        for col, wdt in col_defaults.items():
            self._tree.setColumnWidth(col, wdt)
        self._desc_min = col_mins[COL_DESC]
        # TkStyleHeader owns resizing and keeps the total width constant, so
        # there is no last-section stretch: grow Description into the slack
        # ourselves (Mod Files does the same for its name column).
        self._tree.viewport().installEventFilter(self)
        v.addWidget(self._tree, 1)

        self._status = QLabel(self.tr("Reading saved choices…"))
        self._status.setWordWrap(True)
        self._status.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; padding:6px 12px;")
        v.addWidget(self._status)

        buttons = QHBoxLayout()
        buttons.setContentsMargins(12, 8, 12, 8)
        buttons.setSpacing(8)
        self._restore = QPushButton(self.tr("Restore saved choices"))
        self._reset = QPushButton(self.tr("Reset to defaults"))
        self._reinstall = QPushButton(self.tr("Reinstall with these choices"))
        self._reinstall.setObjectName("PrimaryButton")
        for button in (self._restore, self._reset):
            button.setObjectName("FormButton")
            buttons.addWidget(button)
        buttons.addStretch(1)
        buttons.addWidget(self._reinstall)
        self._restore.clicked.connect(lambda: self._reset_draft(False))
        self._reset.clicked.connect(lambda: self._reset_draft(True))
        self._reinstall.clicked.connect(self._submit)
        v.addLayout(buttons)
        self._update_buttons()

    def _title_text(self) -> str:
        return self.tr("FOMOD Choices: {0}").format(self._mod_name)

    def eventFilter(self, obj, event):
        if obj is self._tree.viewport() and event.type() == QEvent.Resize:
            self._fit_desc_to_width()
        if obj is self._tree and event.type() == QEvent.KeyPress:
            if event.key() == Qt.Key_Space:
                self._toggle(self._tree.currentItem())
                return True
        if obj is self._tree.viewport() and event.type() in (
                QEvent.MouseButtonPress, QEvent.MouseButtonDblClick):
            if event.button() == Qt.LeftButton:
                index = self._tree.indexAt(event.position().toPoint())
                if index.isValid() and index.column() == COL_NAME:
                    rect = self._tree.visualRect(index)
                    depth = self._tree.itemDelegate()._depth(index)
                    x = rect.left() + 4 + depth * INDENT
                    pos = event.position().x()
                    item = self._tree.itemFromIndex(index)
                    kind = index.data(_KIND_ROLE)
                    if kind in (_KIND_STEP, _KIND_GROUP) and x <= pos <= x + ARROW_SZ:
                        if event.type() == QEvent.MouseButtonPress:
                            item.setExpanded(not item.isExpanded())
                        return True
                    x += ARROW_SZ + 2
                    if kind == _KIND_OPTION and x <= pos <= x + CHECK_BOX:
                        if event.type() == QEvent.MouseButtonPress:
                            self._tree.setCurrentItem(item)
                            self._toggle(item)
                        return True
        return super().eventFilter(obj, event)

    def _fit_desc_to_width(self):
        """Give Description whatever the Option column leaves over."""
        vp = self._tree.viewport().width()
        if vp <= 0:
            return
        target = vp - self._tree.columnWidth(COL_NAME)
        if target >= self._desc_min and target != self._tree.columnWidth(COL_DESC):
            self._tree.header().resizeSection(COL_DESC, target)

    # ---- live retarget ----------------------------------------------------
    def current_mod_name(self) -> str:
        """The mod this tab is showing (lets the selection-follow skip a
        retarget onto the mod already displayed)."""
        return self._mod_name

    def set_mod(self, mod_name, profile_dir=None, game_name=None):
        """Re-point this tab at *mod_name* in place (selecting another FOMOD mod
        reuses the open tab rather than tearing it down and rebuilding)."""
        self._store_draft()
        if profile_dir is not None:
            self._profile_dir = str(profile_dir) if profile_dir else ""
        if game_name is not None:
            self._game_name = game_name or ""
        self._mod_name = mod_name
        self._title.setText(self._title_text())
        self._tree.clear()
        self._session = None
        self._rows = {}
        self._steps = {}
        self._message = ""
        self._update_buttons()
        self._status.setText(self.tr("Reading saved choices…"))
        self._status.setVisible(True)
        self._start()

    # ---- fetch ------------------------------------------------------------
    def _start(self):
        mod = self._mod_name
        pdir = self._profile_dir
        game = self._game_name
        self._load_token += 1
        token = self._load_token
        self._context_loading = True
        self._update_buttons()
        loader = (self._controller.loader(mod, pdir, game)
                  if self._controller is not None else None)

        def load():
            if loader is not None:
                return token, loader()
            from Utils.fomod.choices import load_choices
            return token, (load_choices(mod, pdir, game), (set(), set(), set()))

        run_in_worker(load, self._ready, name="fomod-choices",
                      error_result=(token, None))

    def _on_ready(self, payload):
        token, result = payload
        # A newer set_mod() already superseded this load.
        if token != self._load_token:
            return
        self._context_loading = False
        choices, context = result if result is not None else (None, None)
        if choices is None:
            # Clear first: a retarget must not leave the previous mod's rows up.
            self._tree.clear()
            self._message = self.tr(
                "Saved FOMOD choices are missing, unreadable, or could not be matched to this mod. "
                "Run a normal reinstall to record them again.")
            self._session = None
            self._status.setVisible(True)
            self._update_buttons()
            return
        if choices.config is not None and self._controller is not None:
            from Utils.fomod.session import FomodSession, config_identity
            identity = config_identity(choices.config)
            draft = self._controller.drafts.get(self._key())
            if draft is not None and draft[0] != identity:
                self._message = self.tr("The installer configuration changed; previous edits were discarded.")
                draft = None
            if self._session is None or identity != self._config_id:
                self._session = FomodSession(
                    choices.config, choices.selections, context,
                    draft=draft[1] if draft is not None else None,
                    restore_saved=draft[2] if draft is not None else True)
                self._config_id = identity
                self._fill_editor()
            else:
                self._session.saved = deepcopy(choices.selections)
                self._session.recompute(context)
            self._sync_editor()
            return
        self._session = None
        self._update_buttons()
        self._fill(choices)
        if choices.is_empty:
            self._status.setText(self.tr(
                "The installer recorded no selections for this mod."))
            self._status.setVisible(True)
            return
        if not choices.from_config:
            # Without the config copy only the picked options are knowable.
            self._status.setText(self.tr(
                "Installer configuration is unavailable or does not match the saved choices. "
                "Run a normal reinstall to restore it; showing recorded selections only."))
            self._status.setVisible(True)
            return
        self._status.setVisible(False)

    def _key(self):
        return self._game_name, self._profile_dir, self._mod_name

    def _store_draft(self):
        if self._controller is not None and self._session is not None:
            self._controller.drafts[self._key()] = (
                self._config_id, deepcopy(self._session.draft), self._session.restore_saved)

    def _fill_editor(self):
        self._tree.clear()
        self._rows = {}
        self._steps = {}
        for si, step in enumerate(self._session.config.steps):
            item = QTreeWidgetItem([self.tr("Step {0}: {1}").format(si + 1, step.name), ""])
            item.setData(0, _KIND_ROLE, _KIND_STEP)
            self._tree.addTopLevelItem(item)
            self._steps[si] = item
            for gi, group in enumerate(step.groups):
                parent = QTreeWidgetItem([group.display_name or group.name or self.tr("(unnamed group)"),
                                         _GROUP_TYPE_LABELS.get(group.group_type, "")])
                parent.setData(0, _KIND_ROLE, _KIND_GROUP)
                item.addChild(parent)
                for plugin in group.plugins:
                    row = QTreeWidgetItem([plugin.name, _one_line(plugin.description)])
                    row.setData(0, _KIND_ROLE, _KIND_OPTION)
                    row.setData(0, _OPTION_ROLE, (si, gi, plugin.name))
                    row.setToolTip(COL_DESC, plugin.description)
                    parent.addChild(row)
                    self._rows[si, gi, plugin.name] = row
        self._tree.expandAll()

    def _sync_editor(self):
        session = self._session
        for si, item in self._steps.items():
            item.setHidden(si not in session.visible)
        for (si, gi, name), row in self._rows.items():
            if si not in session.visible:
                continue
            group = session.config.steps[si].groups[gi]
            state = session.states[si, gi][name]
            selected = name in session.selections[str(si)][group.name]
            row.setData(0, _SELECTED_ROLE, selected)
            row.setData(0, _STATE_ROLE, state)
            row.setCheckState(0, Qt.Checked if selected else Qt.Unchecked)
            tip = ""
            if state.plugin_type == "NotUsable":
                tip = self.tr("This option is not usable with the current choices and plugins.")
            elif state.locked:
                tip = self.tr("This selection is required by the installer.")
            elif state.missing_dependency:
                tip = self.tr("This option's required plugin isn't enabled - enable it first, or select this only if you plan to add it.")
            elif state.newly_available:
                tip = self.tr("Newly available - this option's required plugin is now installed since your last run of this installer.")
            row.setToolTip(COL_NAME, tip or row.toolTip(COL_DESC))
        self._store_draft()
        self._update_buttons()
        self._tree.viewport().update()

    def _update_buttons(self):
        pending = self._controller is not None and self._controller.is_pending(self._key())
        ready = self._session is not None and not pending and not self._context_loading
        for button in (self._restore, self._reset):
            button.setEnabled(ready)
        self._reinstall.setEnabled(ready and not self._session.errors)
        if pending:
            status = self.tr("Reinstall pending…")
        elif self._session is not None and self._session.errors:
            si, error = self._session.errors[0]
            status = self.tr("Step {0}: {1}").format(si + 1, error)
        elif self._context_loading:
            status = self.tr("Checking choices…")
        else:
            status = self._message
        self._status.setText(status)
        self._status.setVisible(bool(status))

    def _toggle(self, item):
        if self._session is None or item is None or self._context_loading:
            return
        if self._controller.is_pending(self._key()):
            return
        option = item.data(0, _OPTION_ROLE)
        if option is not None:
            self._message = ""
            self._session.toggle(*option)
            self._sync_editor()

    def _reset_draft(self, defaults):
        if self._session is None:
            return
        from Utils.fomod.session import FomodSession
        saved = self._session.saved
        self._session = FomodSession(self._session.config, saved, self._session.context,
                                     restore_saved=not defaults)
        self._message = ""
        self._sync_editor()

    def _submit(self):
        if self._reinstall.isEnabled():
            self._controller.submit(self._key(), self._session, self._config_id)

    def _controller_changed(self, key, success, message):
        if key != self._key():
            return
        self._message = message
        if success:
            self._session = None
        if not self._controller.is_pending(key):
            self._start()
        else:
            self._update_buttons()

    def refresh_context(self):
        if not self._context_loading and not self._controller.is_pending(self._key()):
            self._start()
            return True
        return False

    def showEvent(self, event):
        super().showEvent(event)
        if self._controller is not None and self._session is not None:
            self.refresh_context()

    # ---- populate ---------------------------------------------------------
    def _fill(self, choices):
        tree = self._tree
        tree.clear()

        if not choices.steps:
            it = QTreeWidgetItem([self.tr("(no choices recorded)"), ""])
            it.setData(0, _KIND_ROLE, _KIND_INFO)
            tree.addTopLevelItem(it)
            return

        for num, step in enumerate(choices.steps, start=1):
            label = (self.tr("Step {0}: {1}").format(num, step.name)
                     if step.name else self.tr("Step {0}").format(num))
            step_item = QTreeWidgetItem([label, ""])
            step_item.setData(0, _KIND_ROLE, _KIND_STEP)
            tree.addTopLevelItem(step_item)

            for group in step.groups:
                gtype = _GROUP_TYPE_LABELS.get(group.group_type, "")
                gname = group.name or self.tr("(unnamed group)")
                group_item = QTreeWidgetItem([gname, gtype])
                group_item.setData(0, _KIND_ROLE, _KIND_GROUP)
                step_item.addChild(group_item)

                for opt in group.options:
                    oi = QTreeWidgetItem([opt.name, _one_line(opt.description)])
                    oi.setData(0, _KIND_ROLE, _KIND_OPTION)
                    oi.setData(0, _SELECTED_ROLE, opt.selected)
                    if opt.description:
                        oi.setToolTip(COL_DESC, opt.description)
                    group_item.addChild(oi)

        tree.expandAll()


def _one_line(text: str) -> str:
    """FOMOD descriptions are free-form XML text - flatten for the column."""
    return " ".join((text or "").split())
