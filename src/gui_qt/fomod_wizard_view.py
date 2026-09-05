"""FOMOD installer wizard - Qt port of gui/fomod_dialog.py.

Opens as a (detachable) tab when an archive with a FOMOD ModuleConfig is being
installed. Walks the visible install steps; each step's groups render as radio
buttons (SelectExactlyOne/SelectAtMostOne) or checkboxes (SelectAtLeastOne/
SelectAny/SelectAll). A left panel shows the hovered/selected option's image +
description. Back/Next/Finish drive the step flow; flag state + step visibility
re-evaluate on each transition via the neutral Utils.fomod_installer backend.

On Finish it calls on_finish(selections) with {step_idx_str: {group: [plugins]}}
which finish_install() feeds to resolve_files(). on_cancel() aborts the install.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QFrame, QRadioButton, QCheckBox, QButtonGroup, QSplitter, QSizePolicy,
)

from gui_qt.theme_qt import active_palette, _c
from Utils.fomod.installer import (
    get_visible_steps, get_default_selections, update_flags,
    validate_selections, resolve_plugin_type, plugin_dep_unmet, plugin_dep_met,
)
from Utils.fomod.parser import resolve_path_ci


class FomodWizardView(QWidget):
    def __init__(self, config, mod_base: Path, mod_name: str,
                 on_finish, on_cancel, parent=None, *,
                 saved_selections=None, selections_path=None,
                 installed_files=None, active_files=None, loose_files=None):
        super().__init__(parent)
        self._config = config
        self._base = Path(mod_base)
        self._mod_name = mod_name
        self._on_finish = on_finish
        self._on_cancel = on_cancel
        self._p = active_palette()

        # Per-config-step selections: {step_idx_str: {group_name: [plugin_name]}}.
        self._all_selections: dict[str, dict] = {}
        self._flag_state: dict = {}
        # Context sets FOMOD conditions evaluate against (plugins.txt /
        # loadorder.txt / filemap - see mod_install._collection_plugin_context).
        self._installed: set = installed_files or set()
        self._active: set = active_files or set()
        self._loose: set = loose_files or set()
        # Selections saved by the previous install of this mod (global config
        # JSON): pre-checked (merged with defaults) + highlighted in green so
        # the user can spot prior choices (Tk parity). Accepts both index-keyed
        # (new) and step-name-keyed (old on-disk JSON) formats.
        self._saved_selections: dict = saved_selections or {}
        self._selections_path = selections_path   # Path|None - Reset button
        self._visible_steps = []
        self._cur = 0
        # Live widget state for the current step: group_name → controls info.
        self._group_state: dict = {}

        self._build()
        self._refresh_visible_steps()
        self._load_step(0)

    # ---- build ------------------------------------------------------------
    def _c(self, k):
        return _c(self._p, k)

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        header = QWidget(); header.setObjectName("HeaderBar")
        hb = QHBoxLayout(header); hb.setContentsMargins(12, 8, 12, 8)
        title = QLabel(self._config.name or self._mod_name)
        title.setStyleSheet("font-size:15px; font-weight:600;")
        hb.addWidget(title)
        hb.addStretch(1)
        self._step_name = QLabel("")
        hb.addWidget(self._step_name)
        hb.addStretch(1)
        self._step_lbl = QLabel("")
        self._step_lbl.setStyleSheet(f"color:{self._c('TEXT_DIM')};")
        hb.addWidget(self._step_lbl)
        outer.addWidget(header)

        # Body: a resizable splitter - left = image+description, right = option
        # groups. Image side is larger by default; the user can drag the divider.
        body = QSplitter(Qt.Horizontal)
        body.setObjectName("FomodSplit")
        body.setChildrenCollapsible(False)
        body.setHandleWidth(6)

        left = QWidget(); left.setObjectName("FormBody")
        lv = QVBoxLayout(left); lv.setContentsMargins(14, 14, 14, 14); lv.setSpacing(10)
        self._image = QLabel()
        self._image.setAlignment(Qt.AlignCenter)
        self._image.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self._image.setStyleSheet(f"background:{self._c('BG_DEEP')}; border-radius:6px;")
        # Click the image → open it full-size in a new tab.
        self._image.mousePressEvent = lambda _e: self._open_lightbox()
        # Image takes ~60% of the left panel's height, description ~40%.
        lv.addWidget(self._image, 6)
        self._desc = QLabel("")
        self._desc.setWordWrap(True)
        self._desc.setAlignment(Qt.AlignTop)
        self._desc.setStyleSheet(f"color:{self._c('TEXT_MAIN')};")
        self._desc_scroll = QScrollArea()
        self._desc_scroll.setWidgetResizable(True)
        self._desc_scroll.setFrameShape(QFrame.NoFrame)
        self._desc_scroll.setWidget(self._desc)
        lv.addWidget(self._desc_scroll, 4)
        body.addWidget(left)

        self._opts_scroll = QScrollArea(); self._opts_scroll.setWidgetResizable(True)
        self._opts_scroll.setFrameShape(QFrame.NoFrame)
        self._opts_host = QWidget()
        self._opts_layout = QVBoxLayout(self._opts_host)
        self._opts_layout.setContentsMargins(18, 14, 18, 14)
        self._opts_layout.setSpacing(14)
        self._opts_layout.setAlignment(Qt.AlignTop)
        self._opts_scroll.setWidget(self._opts_host)
        body.addWidget(self._opts_scroll)
        # Selections side bigger by default (~40/60); both panes resizable.
        body.setStretchFactor(0, 4)
        body.setStretchFactor(1, 6)
        body.setSizes([440, 680])
        outer.addWidget(body, 1)

        # Button bar.
        bar = QWidget(); bar.setObjectName("BottomBar")
        bb = QHBoxLayout(bar); bb.setContentsMargins(12, 8, 12, 8)
        reset_btn = QPushButton(self.tr("Reset Selections")); reset_btn.setObjectName("FormButton")
        reset_btn.setCursor(Qt.PointingHandCursor)
        reset_btn.setToolTip(self.tr("Forget the saved selections for this mod and "
                             "restart the wizard with its defaults"))
        reset_btn.clicked.connect(self._on_reset)
        bb.addWidget(reset_btn)
        self._err = QLabel("")
        self._err.setStyleSheet(f"color:{self._c('TEXT_ERR')};")
        bb.addWidget(self._err)
        bb.addStretch(1)
        self._back_btn = QPushButton(self.tr("Back")); self._back_btn.setObjectName("FormButton")
        self._back_btn.setCursor(Qt.PointingHandCursor)
        self._back_btn.clicked.connect(self._on_back)
        bb.addWidget(self._back_btn)
        self._next_btn = QPushButton(self.tr("Next")); self._next_btn.setObjectName("PrimaryButton")
        self._next_btn.setCursor(Qt.PointingHandCursor)
        self._next_btn.clicked.connect(self._on_next)
        bb.addWidget(self._next_btn)
        cancel = QPushButton(self.tr("Cancel")); cancel.setObjectName("FormButton")
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.clicked.connect(lambda: self._on_cancel())
        bb.addWidget(cancel)
        outer.addWidget(bar)

    # ---- step flow --------------------------------------------------------
    def _config_step_idx(self, step) -> int:
        for i, s in enumerate(self._config.steps):
            if s is step:
                return i
        return 0

    def _refresh_visible_steps(self):
        try:
            self._visible_steps = get_visible_steps(
                self._config, self._flag_state, self._installed,
                self._active, self._loose)
        except Exception:
            self._visible_steps = list(self._config.steps)
        if not self._visible_steps:
            self._visible_steps = list(self._config.steps)

    def _plugin_type(self, plugin) -> str:
        try:
            return resolve_plugin_type(plugin, self._flag_state, self._installed,
                                       self._active, self._loose)
        except Exception:
            return "Optional"

    def _plugin_type_by_name(self, group, name: str) -> str:
        for p in group.plugins:
            if p.name == name:
                return self._plugin_type(p)
        return "Optional"

    def _plugin_dep_unmet_by_name(self, group, name: str) -> bool:
        for p in group.plugins:
            if p.name == name:
                return plugin_dep_unmet(p, self._active, self._installed,
                                        self._loose)
        return False

    def _is_rerun(self) -> bool:
        """True when this install has prior saved selections - i.e. the user has
        run this FOMOD before. 'Newly available' highlighting only applies then
        (on a fresh install every option is 'new', so it would be noise)."""
        return bool(self._saved_selections)

    def _newly_available(self, plugin, previously_saved) -> bool:
        """True on a RERUN when this option is gated on a plugin, that gate is now
        MET, and the option was NOT selected last time - so it's newly selectable
        and worth flagging in blue."""
        return (self._is_rerun()
                and plugin.name not in previously_saved
                and plugin_dep_met(plugin, self._active, self._installed,
                                   self._loose))

    def _load_step(self, idx: int):
        self._cur = max(0, min(idx, len(self._visible_steps) - 1))
        step = self._visible_steps[self._cur]
        step_key = str(self._config_step_idx(step))
        # Clear option widgets.
        while self._opts_layout.count():
            item = self._opts_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._group_state = {}

        # Restore or compute default selections for this step.
        # Priority: current session > saved from previous install > computed
        # defaults. Saved selections are merged with auto-detected defaults so
        # that newly installed mods still get their compatibility patches
        # auto-selected (Tk parity - gui/fomod_dialog._load_step).
        existing = self._all_selections.get(step_key)
        if existing is None:
            try:
                defaults = get_default_selections(
                    step, self._flag_state, self._installed,
                    self._active, self._loose)
            except Exception:
                defaults = {}
            saved = (self._saved_selections.get(step_key)
                     or self._saved_selections.get(step.name))
            if saved is not None:
                existing = {}
                group_map = {g.name: g for g in step.groups}
                for group_name, default_plugins in defaults.items():
                    saved_plugins = saved.get(group_name, [])
                    group = group_map.get(group_name)
                    if group and saved_plugins:
                        # Drop any saved plugin that's now NotUsable OR whose
                        # plugin fileDependency is no longer met (its required mod
                        # was removed/disabled since last install) - don't restore
                        # a stale choice whose reqs no longer hold. If that empties
                        # the group, fall back to the defaults.
                        filtered = [
                            p for p in saved_plugins
                            if self._plugin_type_by_name(group, p) != "NotUsable"
                            and not self._plugin_dep_unmet_by_name(group, p)
                        ]
                        if not filtered and saved_plugins:
                            existing[group_name] = default_plugins
                        elif group.group_type in ("SelectAtLeastOne", "SelectAny",
                                                  "SelectAll"):
                            # Multi-select: UNION the now Required/Recommended
                            # defaults into the saved choices so a patch option
                            # that only became eligible since last install (its
                            # fileDependency plugin is now present) is auto-ticked
                            # while the user's prior picks are preserved.
                            merged = list(filtered)
                            for p in default_plugins:
                                if p not in merged:
                                    merged.append(p)
                            existing[group_name] = merged
                        else:
                            # Single-select (ExactlyOne/AtMostOne): keep the saved
                            # choice; forcing a second selection is invalid.
                            existing[group_name] = filtered
                    else:
                        existing[group_name] = saved_plugins or default_plugins
            else:
                existing = defaults

        # Prior-install choices per group - highlighted in green so the user
        # can revert if they change their mind. Empty on a fresh install.
        saved_for_step = (self._saved_selections.get(step_key)
                          or self._saved_selections.get(step.name)
                          or {})

        # Show the description/image of the first SELECTED option (so a restored
        # choice is reflected on the left), falling back to the first plugin.
        selected_plugin = None
        first_plugin = None
        for group in step.groups:
            sel_names = existing.get(group.name, [])
            self._build_group(group, sel_names,
                              set(saved_for_step.get(group.name, [])))
            if first_plugin is None and group.plugins:
                first_plugin = group.plugins[0]
            if selected_plugin is None:
                for p in group.plugins:
                    if p.name in sel_names:
                        selected_plugin = p
                        break
        self._show_plugin(selected_plugin or first_plugin)

        total = len(self._visible_steps)
        self._step_name.setText(step.name.strip())
        self._step_lbl.setText(self.tr("Step {0} of {1}").format(self._cur + 1, total))
        self._back_btn.setEnabled(self._cur > 0)
        self._next_btn.setText(self.tr("Finish") if self._cur >= total - 1 else self.tr("Next"))
        self._err.setText("")
        self._opts_scroll.verticalScrollBar().setValue(0)
        self._desc_scroll.verticalScrollBar().setValue(0)

    def _build_group(self, group, selected_names, previously_saved=frozenset()):
        gtype = group.group_type
        box = QFrame(); box.setObjectName("FomodGroup")
        box.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        bl = QVBoxLayout(box); bl.setContentsMargins(16, 14, 16, 14); bl.setSpacing(10)
        # display_name = author's original when the parser de-duplicated a
        # same-named sibling group (dict keys use the unique `name`).
        gl = QLabel(group.display_name or group.name)
        gl.setObjectName("FomodGroupTitle")
        bl.addWidget(gl)

        def _style(control, locked: bool, plugin, dep_unmet: bool = False,
                   newly_available: bool = False):
            # Tk parity: Required/NotUsable options are locked + dimmed; a
            # choice saved by the previous install shows in green so the user
            # can revert if they change their mind. On a RERUN, an option that is
            # NOW available (its fileDependency plugin appeared since last install)
            # but wasn't picked before shows in BLUE so the user can spot what's
            # newly selectable - the whole reason the rerun-FOMOD flag fired. An
            # option whose fileDependency still isn't met is dimmed as a HINT
            # (still selectable). Precedence: locked > saved(green) > new(blue) >
            # unmet(dim).
            if locked:
                control.setEnabled(False)
                control.setStyleSheet(f"color:{self._c('TEXT_DIM')};")
            elif plugin.name in previously_saved:
                control.setStyleSheet(f"color:{self._c('TEXT_OK')};")
            elif newly_available:
                control.setStyleSheet(
                    f"color:{self._c('ACCENT')}; font-weight:600;")
                control.setToolTip(self.tr(
                    "Newly available - this option's required plugin is now "
                    "installed since your last run of this installer."))
            elif dep_unmet:
                control.setStyleSheet(f"color:{self._c('TEXT_DIM')};")
                control.setToolTip(self.tr(
                    "This option's required plugin isn't enabled - enable it "
                    "first, or select this only if you plan to add it."))

        controls = []
        if gtype in ("SelectExactlyOne", "SelectAtMostOne"):
            bg = QButtonGroup(box)
            # SelectExactlyOne: exclusive (one always stays picked). SelectAtMostOne:
            # NON-exclusive so the user can click the checked option again to clear
            # it (zero is valid). But a non-exclusive group won't auto-uncheck
            # siblings, so a manual handler enforces "at most one" when a new one is
            # ticked - otherwise two radios could show checked at once (only the
            # first would actually install, misleading the user).
            bg.setExclusive(gtype == "SelectExactlyOne")
            for plugin in group.plugins:
                ptype = self._plugin_type(plugin)
                rb = QRadioButton(plugin.name)
                rb.setChecked(plugin.name in selected_names)
                _style(rb, ptype in ("Required", "NotUsable"), plugin,
                       dep_unmet=plugin_dep_unmet(plugin, self._active,
                                                  self._installed, self._loose),
                       newly_available=self._newly_available(plugin,
                                                             previously_saved))
                self._hook_hover(rb, plugin)
                bg.addButton(rb)
                bl.addWidget(rb)
                controls.append((plugin, rb))
            if gtype == "SelectAtMostOne":
                self._wire_at_most_one(controls)
            self._group_state[group.name] = ("radio", gtype, controls)
        else:   # SelectAtLeastOne / SelectAny / SelectAll
            for plugin in group.plugins:
                ptype = self._plugin_type(plugin)
                cb = QCheckBox(plugin.name)
                if gtype == "SelectAll" or ptype == "Required":
                    cb.setChecked(True)
                elif ptype == "NotUsable":
                    cb.setChecked(False)
                else:
                    cb.setChecked(plugin.name in selected_names)
                locked = (gtype == "SelectAll"
                          or ptype in ("Required", "NotUsable"))
                _style(cb, locked, plugin,
                       dep_unmet=plugin_dep_unmet(plugin, self._active,
                                                  self._installed, self._loose),
                       newly_available=self._newly_available(plugin,
                                                             previously_saved))
                self._hook_hover(cb, plugin)
                bl.addWidget(cb)
                controls.append((plugin, cb))
            self._group_state[group.name] = ("check", gtype, controls)
        self._opts_layout.addWidget(box)

    def _wire_at_most_one(self, controls):
        """Enforce 'at most one' on a non-exclusive radio group: when one radio is
        toggled ON, clear the others. Toggling it OFF (clicking the checked one)
        leaves the group empty - which SelectAtMostOne permits."""
        radios = [w for _p, w in controls]

        def _make(this):
            def _on_toggled(checked):
                if not checked:
                    return
                for other in radios:
                    if other is not this and other.isChecked():
                        other.blockSignals(True)
                        other.setChecked(False)
                        other.blockSignals(False)
            return _on_toggled

        for rb in radios:
            rb.toggled.connect(_make(rb))

    def _hook_hover(self, control, plugin):
        """Show the option's image+description when the cursor HOVERS the control
        (Tk parity). Tracked via an event filter catching QEvent.Enter."""
        control._fomod_plugin = plugin
        control.installEventFilter(self)

    def eventFilter(self, obj, event):
        from PySide6.QtCore import QEvent
        if event.type() == QEvent.Enter and getattr(obj, "_fomod_plugin", None) is not None:
            self._show_plugin(obj._fomod_plugin)
        return super().eventFilter(obj, event)

    # ---- left panel -------------------------------------------------------
    def _show_plugin(self, plugin):
        self._desc.setText(plugin.description or "")
        img_rel = getattr(plugin, "image_path", "") or ""
        self._cur_pixmap = None
        self._cur_image_path = None
        if img_rel:
            # Case-insensitive walk: Windows-authored XML paths (Images\x.png)
            # often mismatch the extracted tree's casing on Linux (Tk parity).
            resolved = resolve_path_ci(str(self._base), img_rel)
            if resolved:
                pm = QPixmap(resolved)
                if not pm.isNull():
                    self._cur_pixmap = pm
                    self._cur_image_path = Path(resolved)
        self._rescale_image()

    def _rescale_image(self):
        """Stretch the current image to fill the image label (keeping aspect)."""
        if getattr(self, "_cur_pixmap", None) is not None and not self._cur_pixmap.isNull():
            size = self._image.size()
            self._image.setStyleSheet(
                f"background:{self._c('BG_DEEP')}; border-radius:6px;")
            self._image.setCursor(Qt.PointingHandCursor)
            self._image.setToolTip(self.tr("Click to view full size"))
            self._image.setPixmap(self._cur_pixmap.scaled(
                size.width() - 4, size.height() - 4,
                Qt.KeepAspectRatio, Qt.SmoothTransformation))
        else:
            self._image.clear()
            self._image.setText(self.tr("No image"))
            self._image.setCursor(Qt.ArrowCursor)
            self._image.setToolTip("")
            self._image.setStyleSheet(
                f"background:{self._c('BG_DEEP')}; border-radius:6px; "
                f"color:{self._c('TEXT_FAINT')};")

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._rescale_image()

    def _open_lightbox(self):
        """Open the current image full-size in a new tab (via the host window)."""
        if getattr(self, "_cur_image_path", None) is None:
            return
        # Walk up to the MainWindow that owns the tab widget.
        win = self.window()
        tabs = getattr(win, "_tabs", None)
        if tabs is None:
            return
        from gui_qt.image_view import ImageView
        view = ImageView(self._cur_image_path)
        tabs.open_tab(view, self.tr("Image"), key=f"img:{self._cur_image_path}")

    # ---- selection read ---------------------------------------------------
    def _read_current_selections(self) -> dict:
        result = {}
        for group_name, (_kind, gtype, controls) in self._group_state.items():
            if gtype in ("SelectExactlyOne", "SelectAtMostOne"):
                chosen = [p.name for p, w in controls if w.isChecked()]
                result[group_name] = chosen[:1]
            elif gtype == "SelectAll":
                result[group_name] = [p.name for p, _w in controls]
            else:
                result[group_name] = [p.name for p, w in controls if w.isChecked()]
        return result

    def _save_current(self):
        step = self._visible_steps[self._cur]
        self._all_selections[str(self._config_step_idx(step))] = \
            self._read_current_selections()

    def _rebuild_flags(self):
        flag_state = {}
        for i, step in enumerate(self._config.steps):
            sels = self._all_selections.get(str(i))
            if sels is None:
                continue
            try:
                flag_state = update_flags(step, sels, flag_state)
            except Exception:
                pass
        self._flag_state = flag_state

    # ---- buttons ----------------------------------------------------------
    def _on_reset(self):
        """Delete the saved-selections file and restart the wizard from step 0
        with the FOMOD's own defaults (Tk parity)."""
        if self._selections_path is not None:
            try:
                import os
                os.remove(self._selections_path)
            except OSError:
                pass
        self._saved_selections = {}
        self._all_selections = {}
        self._flag_state = {}
        self._err.setText("")
        self._refresh_visible_steps()
        self._load_step(0)

    def _on_back(self):
        if self._cur <= 0:
            return
        self._save_current()
        self._cur -= 1
        self._load_step(self._cur)

    def _on_next(self):
        self._save_current()
        step = self._visible_steps[self._cur]
        sels = self._all_selections.get(str(self._config_step_idx(step)), {})
        try:
            errors = validate_selections(step, sels, self._flag_state,
                                         self._installed, self._active,
                                         self._loose)
        except Exception:
            errors = []
        if errors:
            self._err.setText(errors[0])
            return
        self._err.setText("")
        self._rebuild_flags()
        self._refresh_visible_steps()

        # Find where the current step now sits in the (possibly changed) list.
        try:
            new_idx = self._visible_steps.index(step)
        except ValueError:
            new_idx = self._cur
        if new_idx >= len(self._visible_steps) - 1:
            # Last visible step → finish.
            self._on_finish(self._all_selections)
            return
        self._load_step(new_idx + 1)
