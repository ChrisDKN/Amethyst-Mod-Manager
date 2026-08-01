"""Settings view — a panel-scoped tab that overlays the Modlist panel.

Opened from the top-bar gear button (app.py `_open_settings_tab`) via
`DetachableTabWidget.open_scoped_tab(..., self._modlist_panel_stack, key="settings")`
— the same modlist-scoped mechanism as the image preview / text editor. The
modlist content is swapped out for this widget while the rest of the UI (plugins
panel, headers, footers) stays live; closing the tab restores the modlist.

Save-on-change: every control writes straight to amethyst.ini through the
toolkit-free `Utils.ui_config` load_*/save_* helpers the moment it changes — there
is no Save/Cancel button. A few settings (Language, Theme, UI Scale) only take
effect on restart and say so inline.

A curated subset of the Tk Settings panel (gui/status_bar.py `SettingsPanel`):
User Interface (incl. Theme + UI Scale), Archives, Downloads, Extraction,
General, Paths — plus a Manage Caches action. Theme colour pickers are
intentionally omitted (Qt has no colour-override system yet).

Option descriptions are tooltips (on the row and on its accent "?" marker), not
inline labels — rows stay one line tall and nothing clips at narrow widths.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QScrollArea, QFrame,
    QLabel, QCheckBox, QComboBox, QSlider, QLineEdit, QPushButton, QGroupBox,
)

from gui_qt.theme_qt import active_palette, _c
from gui_qt.help_marker import tip_text, make_help_marker, help_mark_qss
from gui_qt.wheel_guard import no_wheel
from Utils import ui_config as uc


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
class SettingsView(QWidget):
    # Carries the cache-size scan result from a daemon worker thread to the UI
    # pick_folder's callback fires on the portal WORKER thread; marshal the
    # (edit, save_fn, path) result to the GUI thread before touching a widget.
    _folder_picked = Signal(object)

    def __init__(self, window):
        super().__init__()
        self._window = window          # main window — for _notify, threads
        self._pal = active_palette()
        self.setObjectName("SettingsView")
        self._folder_picked.connect(self._on_folder_picked)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        outer.addWidget(scroll)

        body = QWidget()
        scroll.setWidget(body)
        self._v = QVBoxLayout(body)
        self._v.setContentsMargins(16, 14, 16, 18)
        self._v.setSpacing(14)

        title = QLabel(self.tr("Settings"))
        f = title.font(); f.setPointSize(f.pointSize() + 4); f.setBold(True)
        title.setFont(f)
        self._v.addWidget(title)

        self.setStyleSheet(self._qss())

        # Collection settings — read once here; both the Downloads and the
        # Extraction sections persist through this shared dict.
        try:
            cs = uc.load_collection_settings()
        except Exception:
            cs = {}
        self._cs = {
            "max_concurrent": int(cs.get("max_concurrent", uc._DEFAULT_MAX_CONCURRENT)),
            "max_extract_workers": int(cs.get("max_extract_workers", uc._DEFAULT_MAX_EXTRACT_WORKERS)),
            "check_download_locations": bool(cs.get("check_download_locations", True)),
            "clear_archive_after_install": bool(cs.get("clear_archive_after_install", False)),
        }

        self._build_user_interface()
        self._build_archives()
        self._build_downloads()
        self._build_extraction()
        self._build_general()
        self._build_paths()
        self._v.addStretch(1)

    # ---- styling ----------------------------------------------------------
    def _qss(self) -> str:
        c = lambda k: _c(self._pal, k)
        return f"""
        QGroupBox {{
            border: 1px solid {c('BORDER')};
            border-radius: 6px;
            margin-top: 10px;
            padding: 10px 12px 12px 12px;
            background: {c('BG_PANEL')};
        }}
        QGroupBox::title {{
            subcontrol-origin: margin;
            left: 10px; padding: 0 5px;
            color: {c('TEXT_MAIN')};
            font-weight: bold;
        }}
        {help_mark_qss(self._pal)}
        QLabel#RestartNote {{ color: {c('TEXT_WARN')}; }}
        QSlider::groove:horizontal {{
            height: 4px; background: {c('BG_DEEP')}; border-radius: 2px;
        }}
        QSlider::handle:horizontal {{
            background: {c('ACCENT')}; width: 14px; margin: -6px 0;
            border-radius: 7px;
        }}
        QSlider::sub-page:horizontal {{ background: {c('ACCENT')}; border-radius: 2px; }}
        """

    # ---- section + control builders --------------------------------------
    def _section(self, title: str) -> QGridLayout:
        """Add a QGroupBox and return a QGridLayout to fill (label | control)."""
        box = QGroupBox(title)
        grid = QGridLayout(box)
        grid.setContentsMargins(8, 6, 8, 6)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(8)
        grid.setColumnStretch(0, 0)
        grid.setColumnStretch(1, 1)
        self._v.addWidget(box)
        # Track the next free row per grid via a dynamic attribute.
        grid.setProperty("_row", 0)
        return grid

    def _next_row(self, grid: QGridLayout) -> int:
        r = int(grid.property("_row") or 0)
        grid.setProperty("_row", r + 1)
        return r

    def _tip_text(self, text: str) -> str:
        return tip_text(text)

    def _help_marker(self, text: str) -> QLabel:
        return make_help_marker(text)

    def _checkbox(self, grid: QGridLayout, label: str, load_fn, save_fn,
                  help: str | None = None, on_changed=None) -> QCheckBox:
        cb = QCheckBox(label)
        try:
            cb.setChecked(bool(load_fn()))
        except Exception:
            pass

        def _toggled(v):
            self._safe_save(save_fn, v)
            if on_changed is not None:
                try:
                    on_changed(v)
                except Exception:
                    pass

        cb.toggled.connect(_toggled)
        row = self._next_row(grid)
        if help:
            cb.setToolTip(self._tip_text(help))
            wrap = QHBoxLayout()
            wrap.setContentsMargins(0, 0, 0, 0)
            wrap.addWidget(cb)
            wrap.addWidget(self._help_marker(help))
            wrap.addStretch(1)
            holder = QWidget(); holder.setLayout(wrap)
            grid.addWidget(holder, row, 0, 1, 2)
        else:
            grid.addWidget(cb, row, 0, 1, 2)
        return cb

    def _combo(self, grid: QGridLayout, label: str,
               pairs: list[tuple[str, str]], current_value: str, save_fn,
               restart_note: bool = False) -> QComboBox:
        """`pairs` = [(display, value), ...]; selecting saves the value."""
        row = self._next_row(grid)
        grid.addWidget(QLabel(label), row, 0)
        combo = QComboBox()
        values = [v for _d, v in pairs]
        for disp, _v in pairs:
            combo.addItem(disp)
        if current_value in values:
            combo.setCurrentIndex(values.index(current_value))
        combo.currentIndexChanged.connect(
            lambda i: self._safe_save(save_fn, values[i]))
        no_wheel(combo)
        grid.addWidget(combo, row, 1, Qt.AlignLeft)
        if restart_note:
            note = QLabel(self.tr("Changes take effect after restart."))
            note.setObjectName("RestartNote")
            grid.addWidget(note, self._next_row(grid), 0, 1, 2)
        return combo

    def _slider(self, grid: QGridLayout, label: str, lo: int, hi: int,
                value: int, on_change, help: str | None = None) -> QSlider:
        """Integer slider lo..hi with a live value label. `on_change(int)`."""
        row = self._next_row(grid)
        lbl = QLabel(label)
        grid.addWidget(lbl, row, 0)
        wrap = QHBoxLayout()
        sld = QSlider(Qt.Horizontal)
        sld.setMinimum(lo); sld.setMaximum(hi)
        sld.setValue(max(lo, min(hi, value)))
        sld.setFixedWidth(220)
        val_lbl = QLabel(str(sld.value()))
        val_lbl.setMinimumWidth(48)
        sld.valueChanged.connect(lambda v: val_lbl.setText(str(v)))
        sld.valueChanged.connect(lambda v: on_change(v))
        no_wheel(sld)
        wrap.addWidget(sld)
        wrap.addWidget(val_lbl)
        if help:
            tip = self._tip_text(help)
            lbl.setToolTip(tip)
            sld.setToolTip(tip)
            wrap.addWidget(self._help_marker(help))
        wrap.addStretch(1)
        holder = QWidget(); holder.setLayout(wrap)
        grid.addWidget(holder, row, 1)
        return sld, val_lbl

    def _path_row(self, grid: QGridLayout, label: str, load_fn, save_fn,
                  help: str | None = None) -> QLineEdit:
        row = self._next_row(grid)
        lbl = QLabel(label)
        grid.addWidget(lbl, row, 0)
        wrap = QHBoxLayout()
        edit = QLineEdit()
        try:
            edit.setText(load_fn() or "")
        except Exception:
            pass
        edit.editingFinished.connect(
            lambda: self._safe_save(save_fn, edit.text().strip()))
        browse = QPushButton(self.tr("Browse"))
        browse.setCursor(Qt.PointingHandCursor)
        browse.clicked.connect(lambda: self._browse_into(edit, save_fn, label))
        clear = QPushButton(self.tr("Clear"))
        clear.setCursor(Qt.PointingHandCursor)
        clear.clicked.connect(lambda: self._clear_path(edit, save_fn))
        wrap.addWidget(edit, 1)
        wrap.addWidget(browse)
        wrap.addWidget(clear)
        if help:
            tip = self._tip_text(help)
            lbl.setToolTip(tip)
            edit.setToolTip(tip)
            wrap.addWidget(self._help_marker(help))
        holder = QWidget(); holder.setLayout(wrap)
        grid.addWidget(holder, row, 1)
        return edit

    def _file_row(self, grid: QGridLayout, label: str, load_fn, save_fn,
                  filters: "list[tuple[str, list[str]]] | None" = None,
                  help: str | None = None) -> QLineEdit:
        """Like :meth:`_path_row` but the Browse button picks a single file
        (e.g. an AppImage) instead of a folder."""
        row = self._next_row(grid)
        lbl = QLabel(label)
        grid.addWidget(lbl, row, 0)
        wrap = QHBoxLayout()
        edit = QLineEdit()
        try:
            edit.setText(load_fn() or "")
        except Exception:
            pass
        edit.editingFinished.connect(
            lambda: self._safe_save(save_fn, edit.text().strip()))
        browse = QPushButton(self.tr("Browse"))
        browse.setCursor(Qt.PointingHandCursor)
        browse.clicked.connect(
            lambda: self._browse_file_into(edit, save_fn, label, filters))
        clear = QPushButton(self.tr("Clear"))
        clear.setCursor(Qt.PointingHandCursor)
        clear.clicked.connect(lambda: self._clear_path(edit, save_fn))
        wrap.addWidget(edit, 1)
        wrap.addWidget(browse)
        wrap.addWidget(clear)
        if help:
            tip = self._tip_text(help)
            lbl.setToolTip(tip)
            edit.setToolTip(tip)
            wrap.addWidget(self._help_marker(help))
        holder = QWidget(); holder.setLayout(wrap)
        grid.addWidget(holder, row, 1)
        return edit

    # ---- sections ---------------------------------------------------------
    def _build_user_interface(self):
        # Language, Theme and UI Scale are all applied once at startup (Qt reads
        # QT_SCALE_FACTOR / the palette / the translator only at launch), so each
        # change persists to amethyst.ini and offers a self-restart. They are
        # grouped together with a single shared restart note beneath them; the
        # Hide BSA conflicts toggle (which applies live) sits below that.
        g = self._section(self.tr("User Interface"))
        # Language row: combo + a "Sync language files" button that pulls the
        # latest translations from the Resources branch on demand.
        row = self._next_row(g)
        g.addWidget(QLabel(self.tr("Language")), row, 0)
        self._lang_combo = QComboBox()
        no_wheel(self._lang_combo)
        self._lang_sync_btn = QPushButton(self.tr("Sync language files"))
        self._lang_sync_btn.setCursor(Qt.PointingHandCursor)
        self._lang_sync_btn.clicked.connect(self._on_sync_languages)
        lang_wrap = QHBoxLayout()
        lang_wrap.setContentsMargins(0, 0, 0, 0)
        lang_wrap.addWidget(self._lang_combo)
        lang_wrap.addWidget(self._lang_sync_btn)
        lang_wrap.addStretch(1)
        lang_holder = QWidget(); lang_holder.setLayout(lang_wrap)
        g.addWidget(lang_holder, row, 1)
        self._populate_language_combo()

        self._build_theme(g)
        self._build_ui_scale(g)

        # Single shared restart note beneath Language / Theme / UI Scale.
        note = QLabel(self.tr("Changes take effect after restart."))
        note.setObjectName("RestartNote")
        g.addWidget(note, self._next_row(g), 0, 1, 2)

        self._checkbox(
            g, self.tr("Hide BSA conflicts"),
            uc.load_hide_bsa_conflicts, uc.save_hide_bsa_conflicts,
            help=self.tr("Hide BSA/BA2 archive conflict flags (also skips that "
                 "conflict scan for a small speed-up)."),
            on_changed=lambda _v: self._rebuild_conflicts())

        # Read live by the modlist view on each hover, so persisting the value
        # is enough — no rebuild/refresh needed.
        self._checkbox(
            g, self.tr("Show mod description tooltips"),
            uc.load_show_summary_tooltips, uc.save_show_summary_tooltips,
            help=self.tr("Show a mod's Nexus description as a tooltip when you "
                 "hover over its name in the mod list."))

    def _build_theme(self, g):
        """Theme picker (formerly its own Appearance section). Takes effect on
        restart, like Language / UI Scale — selecting a new theme persists it and
        offers a self-restart via the window's theme restart prompt. The
        "Edit / Create Theme…" button sits to the right of the dropdown (mirrors
        the Language row's combo + sync button layout)."""
        try:
            from Utils.themes import load_display_names
            themes = load_display_names() or {"dark": "Dark"}
        except Exception:
            themes = {"dark": "Dark", "light": "Light"}
        try:
            current = uc.get_appearance_mode()
        except Exception:
            current = "dark"

        row = self._next_row(g)
        g.addWidget(QLabel(self.tr("Theme")), row, 0)

        combo = QComboBox()
        no_wheel(combo)
        values = [tid for tid in themes]
        for tid, disp in themes.items():
            combo.addItem(disp, tid)
        if current in values:
            combo.setCurrentIndex(values.index(current))
        combo.currentIndexChanged.connect(
            lambda i: self._on_theme_changed(values[i]))

        edit_btn = QPushButton(self.tr("Edit / Create Theme…"))
        edit_btn.setCursor(Qt.PointingHandCursor)
        edit_btn.clicked.connect(self._open_theme_editor)

        wrap = QHBoxLayout()
        wrap.setContentsMargins(0, 0, 0, 0)
        wrap.addWidget(combo)
        wrap.addWidget(edit_btn)
        wrap.addStretch(1)
        holder = QWidget(); holder.setLayout(wrap)
        g.addWidget(holder, row, 1)

    def _on_theme_changed(self, tid: str):
        """Persist the chosen theme, then offer a restart (same pattern as UI
        scale / language) so the new palette applies on a fresh launch."""
        self._safe_save(uc.save_appearance_mode, tid)
        from gui_qt.theme_qt import invalidate_palette_cache
        invalidate_palette_cache()
        self._prompt_restart("theme")

    def _open_theme_editor(self):
        """Open the full-screen theme editor tab via the main window."""
        opener = getattr(self._window, "_open_theme_editor_tab", None)
        if callable(opener):
            opener()

    def _open_install_name_patterns(self):
        """Open the custom install-name rules editor tab via the main window."""
        opener = getattr(self._window, "_open_install_name_patterns_tab", None)
        if callable(opener):
            opener()

    def _build_ui_scale(self, g):
        """Add the UI Scale row: an Auto checkbox + a 50–200% slider.

        When Auto is ticked the slider is disabled and the saved value is the
        string 'auto' (resolved to the detected HiDPI scale at load time);
        unticking enables the slider and saves a float multiplier. Either edit
        offers a self-restart so the new QT_SCALE_FACTOR takes effect.
        """
        current = uc.get_ui_scale()          # float, already loaded at startup
        is_auto = uc.load_ui_scale_is_auto()
        pct = int(round(max(0.5, min(3.0, current)) * 100))

        # Percent slider. Persisting + the restart prompt fire only when the
        # user finishes the gesture (sliderReleased / keyboard / click), never
        # on every 1% valueChanged tick while dragging — otherwise the restart
        # overlay would reappear on every intermediate value. The value label
        # still tracks live via valueChanged. Pass a no-op change cb to _slider
        # so it doesn't wire its own per-tick persist.
        self._scale_slider, self._scale_val_lbl = self._slider(
            g, self.tr("UI Scale"), 50, 200, pct, lambda _v: None,
            help=self.tr("Make the whole interface bigger or smaller. "
               "Changes take effect after a restart."))
        self._scale_slider.setSingleStep(5)
        self._scale_slider.setPageStep(10)
        self._scale_val_lbl.setText(f"{pct}%")
        # valueChanged fires on every tick — while dragging (isSliderDown()) it
        # only updates the label; a change that lands with the handle NOT held
        # down (arrow keys, groove click, page step) commits immediately.
        # sliderReleased then commits the final value at the end of a drag.
        self._scale_slider.valueChanged.connect(self._on_scale_value_changed)
        self._scale_slider.sliderReleased.connect(self._commit_ui_scale)
        self._scale_slider.setEnabled(not is_auto)

        # Auto checkbox — sits below the slider; ticking it disables the slider.
        self._scale_auto_cb = QCheckBox(self.tr("Auto (match display)"))
        self._scale_auto_cb.setChecked(is_auto)
        self._scale_auto_cb.toggled.connect(self._on_ui_scale_auto_toggled)
        g.addWidget(self._scale_auto_cb, self._next_row(g), 0, 1, 2)

    def _on_ui_scale_auto_toggled(self, on: bool):
        self._scale_slider.setEnabled(not on)
        if on:
            # Enabling auto switches to the detected scale, which differs from
            # whatever manual value is applied — needs a restart to take effect.
            self._safe_save(uc.save_ui_scale, "auto")
            self._prompt_scale_restart()
        else:
            # Disabling auto just re-enables the slider. The slider already shows
            # the currently-applied scale, so nothing changes until the user
            # actually moves it (which commits + prompts then). Persist the
            # explicit value so a manual scale is recorded, but don't prompt.
            self._safe_save(uc.save_ui_scale, self._scale_slider.value() / 100.0)

    def _on_scale_value_changed(self, pct: int):
        """Live label update on every tick. Commit immediately only when the
        handle is NOT being dragged (keyboard/click); a drag commits on release
        via sliderReleased, so we don't prompt on every intermediate value."""
        self._scale_val_lbl.setText(f"{pct}%")
        if not self._scale_slider.isSliderDown():
            self._commit_ui_scale()

    def _commit_ui_scale(self):
        """Persist the current slider value and offer a restart. Called when the
        user finishes changing the slider (release / click / key), so the
        restart prompt appears once per gesture, not per 1% tick."""
        # Only persist when the user is driving the slider (Auto off).
        if getattr(self, "_scale_auto_cb", None) is not None \
                and self._scale_auto_cb.isChecked():
            return
        pct = self._scale_slider.value()
        self._safe_save(uc.save_ui_scale, pct / 100.0)
        self._prompt_scale_restart()

    def _prompt_scale_restart(self):
        self._prompt_restart("scale")

    def _prompt_restart(self, kind: str):
        """Offer a self-restart so a startup-only setting (UI scale / theme /
        language) applies. Reuses the window's matching restart flow, falling
        back to the UI-scale prompt (any of them just re-execs the app)."""
        win = self._window
        by_kind = {
            "scale": "_prompt_ui_scale_restart",
            "theme": "_prompt_theme_restart",
            "language": "_prompt_language_restart",
        }
        names = [by_kind.get(kind, "")]
        names += ["_prompt_ui_scale_restart", "_prompt_theme_restart",
                  "_prompt_language_restart"]
        for name in names:
            prompt = getattr(win, name, None)
            if callable(prompt):
                try:
                    prompt()
                    return
                except Exception:
                    pass

    def _populate_language_combo(self):
        """(Re)fill the Language combo from i18n.available_languages(), storing
        each locale code as item-data and preserving the current selection. Owns
        its own save wiring (the generic _combo closure can't repopulate)."""
        from gui_qt.i18n import available_languages
        combo = getattr(self, "_lang_combo", None)
        if combo is None:
            return
        combo.blockSignals(True)
        current = uc.load_language()
        # Disconnect only our own slot — a bare disconnect() would also sever
        # any other connection on the signal.
        prev_slot = getattr(self, "_lang_combo_slot", None)
        if prev_slot is not None:
            combo.currentIndexChanged.disconnect(prev_slot)
            self._lang_combo_slot = None
        combo.clear()
        sel = 0
        for i, (disp, code) in enumerate(available_languages()):
            combo.addItem(disp, userData=code)
            if code == current:
                sel = i
        combo.setCurrentIndex(sel)
        self._lang_combo_slot = (
            lambda i: self._on_language_changed(combo.itemData(i)))
        combo.currentIndexChanged.connect(self._lang_combo_slot)
        combo.blockSignals(False)

    def _on_language_changed(self, code):
        """Persist the chosen language, then offer a restart (same pattern as UI
        scale / theme) so the new translator applies on a fresh launch."""
        self._safe_save(uc.save_language, code)
        self._prompt_restart("language")

    def refresh_language_options(self):
        """Called when a background sync adds new .qm files — refresh the picker
        so newly-downloaded languages appear without reopening Settings."""
        self._populate_language_combo()

    def _on_sync_languages(self):
        """Manually pull the latest translations from the Resources branch. The
        worker fires the window's _languages_synced signal (→ refresh + toast)
        just like the automatic startup sync."""
        try:
            self._window._sync_languages_now()
        except Exception:
            pass

    def _build_archives(self):
        g = self._section(self.tr("Archives"))
        self._checkbox(
            g, self.tr("Clear archive after install"),
            uc.load_clear_archive_after_install,
            uc.save_clear_archive_after_install,
            help=self.tr("Delete a mod's downloaded archive after it is extracted. "
                 "Only applies to archives Amethyst downloaded itself — installs "
                 "from the Install Mod button or the Downloads tab keep their "
                 "archive."))
        self._checkbox(
            g, self.tr("Keep FOMOD archives"),
            uc.load_keep_fomod_archives, uc.save_keep_fomod_archives,
            help=self.tr("Mods installed via a FOMOD installer keep their archive even "
                 "when 'Clear archive after install' is on."))
        self._checkbox(
            g, self.tr("Install new mods disabled"),
            uc.load_install_mods_disabled, uc.save_install_mods_disabled,
            help=self.tr("Newly installed mods start disabled in the modlist instead "
                 "of enabled. Applies to every install path except collection "
                 "installs."))

    def _build_downloads(self):
        # Collection settings — all persisted together via save_collection_settings.
        g = self._section(self.tr("Downloads"))
        self._slider(
            g, self.tr("Max concurrent downloads"), 1, uc._MAX_CONCURRENT_CEILING,
            self._cs["max_concurrent"], self._save_max_concurrent)

        # Download speed limit — global cap shared by all download threads.
        lim_sld, lim_lbl = self._slider(
            g, self.tr("Download speed limit"), 0, 250,
            int(uc.load_download_speed_limit()), self._save_speed_limit,
            help=self.tr("Cap the combined download speed of all downloads "
               "(collections, single mods, nxm links) so they don't use the "
               "whole connection. Applies immediately, including to a running "
               "collection install."))
        def _fmt_limit(v, _lbl=lim_lbl):
            _lbl.setText(self.tr("Unlimited") if int(v) == 0 else
                         self.tr("{0} MB/s").format(int(v)))
        lim_sld.valueChanged.connect(_fmt_limit)
        _fmt_limit(lim_sld.value())

    def _build_extraction(self):
        # Extraction resource limits — apply to every install (single mods,
        # Downloads tab and collections), not just collection installs.
        g = self._section(self.tr("Extraction"))
        self._slider(
            g, self.tr("Max extractions"), 1, uc._MAX_EXTRACT_WORKERS_CEILING,
            self._cs["max_extract_workers"], self._save_max_extract,
            help=self.tr("Extractions are gated by available memory; the effective number "
               "may be lower than set."))

        import os as _os
        ext = uc.load_extraction_settings()
        thr_sld, thr_lbl = self._slider(
            g, self.tr("Extraction CPU threads"), 0, _os.cpu_count() or 8,
            int(ext.get("cpu_threads", 0)),
            lambda v: self._safe_save(uc.save_extraction_cpu_threads, v),
            help=self.tr("CPU threads each extraction may use. 'All' is fastest; a "
               "lower value keeps the system responsive while large archives "
               "extract."))
        def _fmt_threads(v, _lbl=thr_lbl):
            _lbl.setText(self.tr("All") if int(v) == 0 else str(v))
        thr_sld.valueChanged.connect(_fmt_threads)
        _fmt_threads(thr_sld.value())
        self._checkbox(
            g, self.tr("Low priority extractions"),
            lambda: bool(uc.load_extraction_settings().get("low_priority", False)),
            uc.save_extraction_low_priority,
            help=self.tr("Run extractions at low CPU and disk priority so they yield "
                 "to other applications instead of slowing them down. Extraction "
                 "speed is unaffected while the system is otherwise idle."))

        # Manage Caches action.
        row = self._next_row(g)
        g.addWidget(QLabel(self.tr("Caches")), row, 0)
        self._cache_btn = QPushButton(self.tr("Manage Caches…"))
        self._cache_btn.setCursor(Qt.PointingHandCursor)
        self._cache_btn.clicked.connect(self._on_manage_caches)
        cwrap = QHBoxLayout()
        cwrap.addWidget(self._cache_btn)
        cwrap.addStretch(1)
        holder = QWidget(); holder.setLayout(cwrap)
        g.addWidget(holder, row, 1)

    def _build_general(self):
        g = self._section(self.tr("General"))
        self._checkbox(
            g, self.tr("Normalise folder casing"),
            uc.load_normalize_folder_case, uc.save_normalize_folder_case,
            help=self.tr("Unify folder names to a single casing across mods. Disable on "
                 "case-insensitive filesystems."))
        self._checkbox(
            g, self.tr("Rename mod after install"),
            uc.load_rename_mod_after_install, uc.save_rename_mod_after_install,
            help=self.tr("Show a rename prompt after installing a mod."))
        # Custom install-name rules — a full editor (opened as its own tab)
        # rather than a single control, so it gets a button row here.
        patt_help = self.tr(
            "Add your own regex search/replace rules to clean up mod names on "
            "install — useful when a download site changes its filename format.")
        patt_btn = QPushButton(self.tr("Edit custom install-name rules…"))
        patt_btn.setCursor(Qt.PointingHandCursor)
        patt_btn.clicked.connect(self._open_install_name_patterns)
        patt_btn.setToolTip(self._tip_text(patt_help))
        wrap = QHBoxLayout()
        wrap.setContentsMargins(0, 0, 0, 0)
        wrap.addWidget(patt_btn)
        wrap.addWidget(self._help_marker(patt_help))
        wrap.addStretch(1)
        holder = QWidget(); holder.setLayout(wrap)
        g.addWidget(holder, self._next_row(g), 0, 1, 2)
        self._checkbox(
            g, self.tr("Restore on close"),
            uc.load_restore_on_close, uc.save_restore_on_close,
            help=self.tr("Restore all deployed games to vanilla when the app is closed."))
        self._checkbox(
            g, self.tr("Use pre-release versions"),
            uc.load_allow_prerelease, uc.save_allow_prerelease,
            help=self.tr("Also offer beta and release-candidate app builds when checking "
                 "for updates."),
            on_changed=self._on_prerelease_toggle)
        self._checkbox(
            g, self.tr("Notify about new versions on startup"),
            uc.load_update_notifications, uc.save_update_notifications,
            help=self.tr("Show a notification when a new version of Amethyst "
                 "is available. Turning this off only mutes the notification "
                 "— you can still update via your package manager or by "
                 "toggling the pre-release setting."))

        self._maybe_add_flatpak_enroll(g)

    def _maybe_add_flatpak_enroll(self, g):
        """Offer a one-time 'Enable automatic updates' button to flatpak users
        who installed from a bundle and aren't yet tracking our remote.

        Adding the remote hands updates to the OS (native `flatpak update`,
        GNOME Software / Discover, delta downloads). Once enrolled, the button
        is hidden. No-op outside the flatpak or when already remote-tracked.
        """
        from Utils.version_check import (
            is_flatpak, flatpak_installed_from_remote,
        )
        if not is_flatpak() or flatpak_installed_from_remote():
            return
        enroll_help = self.tr(
            "Switch this Flatpak to the Amethyst update remote so future "
            "updates arrive automatically through your package manager "
            "(GNOME Software / Discover) with smaller downloads. This "
            "reinstalls the app once from the remote and relaunches it.")
        btn = QPushButton(self.tr("Enable automatic updates…"))
        btn.setCursor(Qt.PointingHandCursor)
        btn.clicked.connect(self._on_enroll_flatpak_remote)
        btn.setToolTip(self._tip_text(enroll_help))
        wrap = QHBoxLayout()
        wrap.setContentsMargins(0, 0, 0, 0)
        wrap.addWidget(btn)
        wrap.addWidget(self._help_marker(enroll_help))
        wrap.addStretch(1)
        holder = QWidget(); holder.setLayout(wrap)
        g.addWidget(holder, self._next_row(g), 0, 1, 2)

    def _on_enroll_flatpak_remote(self):
        """Confirm, then add the remote + reinstall-from-remote (relaunches)."""
        from gui_qt.confirm_overlay import ConfirmOverlay
        from Utils.version_check import enroll_flatpak_remote
        from Utils.ui_config import load_allow_prerelease

        def _go(ok: bool):
            if not ok:
                return
            allow_pre = load_allow_prerelease()
            status = enroll_flatpak_remote(allow_prerelease=allow_pre)
            if status == "launched":
                win = self._window
                if win is not None:
                    win.close()  # the detached child relaunches from the remote
            elif status == "no-branch":
                channel = self.tr("beta") if allow_pre else self.tr("stable")
                ConfirmOverlay.show_message(
                    self._window,
                    self.tr("Channel not available"),
                    self.tr("The {0} channel isn't published on the update "
                            "remote yet. Try again after the next {0} "
                            "release (or change the pre-release setting)."
                            ).format(channel))
            else:
                ConfirmOverlay.show_message(
                    self._window,
                    self.tr("Could not reach Flatpak"),
                    self.tr("The host Flatpak service couldn't be reached. "
                            "You can add the remote manually:\n\n"
                            "flatpak remote-add --user amethyst "
                            "https://chrisdkn.github.io/Amethyst-Mod-Manager/"
                            "amethyst.flatpakrepo"))
        ConfirmOverlay.show_over(
            self._window,
            self.tr("Enable automatic updates?"),
            self.tr("Amethyst will add its update remote and reinstall itself "
                    "from it once, then relaunch. Future updates then arrive "
                    "automatically through your package manager."),
            _go,
            confirm_label=self.tr("Enable"),
            cancel_label=self.tr("Cancel"),
            danger=False,
        )

    def _build_paths(self):
        g = self._section(self.tr("Paths"))
        from Utils.config_paths import get_config_dir, get_default_staging_root
        base = get_config_dir()
        self._path_row(
            g, self.tr("Default Mod Staging Folder"),
            uc.load_default_staging_path, uc.save_default_staging_path,
            help=self.tr("When set, games added after this point stage mods here. "
                 "Blank = default ({0}).").format(
                     get_default_staging_root() / self.tr("<game name>")))
        self._path_row(
            g, self.tr("Download Cache Folder"),
            uc.load_download_cache_path, uc.save_download_cache_path,
            help=self.tr("Where downloaded mod archives are stored. "
                 "Blank = default ({0}).").format(base / 'download_cache'))
        self._path_row(
            g, self.tr("Heroic Config Location"),
            uc.load_heroic_config_path, uc.save_heroic_config_path,
            help=self.tr("Folder containing Heroic's config.json. Blank = auto-detect "
                 "(Flatpak and native locations)."))
        self._path_row(
            g, self.tr("Lutris Data Location"),
            uc.load_lutris_data_path, uc.save_lutris_data_path,
            help=self.tr("Folder containing Lutris's pga.db. Blank = auto-detect "
                 "(Flatpak and native locations)."))
        self._file_row(
            g, self.tr("Lutris AppImage"),
            uc.load_lutris_appimage_path, uc.save_lutris_appimage_path,
            filters=[("AppImage", ["*.AppImage", "*.appimage"]), ("All files", ["*"])],
            help=self.tr("Path to the Lutris AppImage, so Play can launch it "
                 "directly. Only needed for AppImage installs — leave blank for "
                 "Flatpak or native Lutris."))
        self._path_row(
            g, self.tr("Faugus Data Location"),
            uc.load_faugus_data_path, uc.save_faugus_data_path,
            help=self.tr("Folder containing Faugus Launcher's games.json. "
                 "Blank = auto-detect (Flatpak and native locations)."))
        self._file_row(
            g, self.tr("Faugus AppImage"),
            uc.load_faugus_appimage_path, uc.save_faugus_appimage_path,
            filters=[("AppImage", ["*.AppImage", "*.appimage"]), ("All files", ["*"])],
            help=self.tr("Path to the Faugus Launcher AppImage, so Play can "
                 "launch it directly. Only needed for AppImage installs — leave "
                 "blank for Flatpak or native Faugus."))
        self._path_row(
            g, self.tr("Steam libraryfolders.vdf"),
            uc.load_steam_libraries_vdf_path, uc.save_steam_libraries_vdf_path,
            help=self.tr("Path to libraryfolders.vdf (or its folder). Blank = auto-detect "
                 "(standard, Flatpak and Snap locations)."))

    # ---- collection setting handlers (all persist the whole group) --------
    def _persist_collection(self):
        self._safe_save(
            uc.save_collection_settings,
            self._cs["max_concurrent"],
            self._cs["check_download_locations"],
            self._cs["clear_archive_after_install"],
            self._cs["max_extract_workers"])

    def _save_max_concurrent(self, value: int):
        self._cs["max_concurrent"] = int(value)
        self._persist_collection()

    def _save_max_extract(self, value: int):
        self._cs["max_extract_workers"] = int(value)
        self._persist_collection()

    def _save_speed_limit(self, value: int):
        # Apply to in-flight downloads immediately, then persist.
        from Utils import bandwidth_limit
        bandwidth_limit.set_limit_mbps(float(value))
        self._safe_save(uc.save_download_speed_limit, float(value))


    # ---- path browse / clear ----------------------------------------------
    def _browse_into(self, edit: QLineEdit, save_fn, title: str):
        from Utils.portal_filechooser import pick_folder
        pick_folder(f"Select {title}",
                    lambda path: self._folder_picked.emit((edit, save_fn, path)))

    def _browse_file_into(self, edit: QLineEdit, save_fn, title: str,
                          filters=None):
        # Reuse the folder-picked signal/slot — the payload shape is identical
        # (edit, save_fn, path); pick_file just returns a file Path.
        from Utils.portal_filechooser import pick_file
        pick_file(f"Select {title}",
                  lambda path: self._folder_picked.emit((edit, save_fn, path)),
                  filters=filters)

    def _on_folder_picked(self, payload):
        edit, save_fn, path = payload
        if path:
            edit.setText(str(path))
            self._safe_save(save_fn, str(path))

    def _clear_path(self, edit: QLineEdit, save_fn):
        edit.clear()
        self._safe_save(save_fn, "")

    # ---- Manage Caches ----------------------------------------------------
    def _on_manage_caches(self):
        """Open the borderless per-game cache browser overlay (Tk parity)."""
        from gui_qt.cache_manager_overlay import CacheManagerOverlay
        active = getattr(getattr(self._window, "_gs", None), "game_name", "") or ""
        CacheManagerOverlay.show_over(
            self._window, active_game_name=active)

    def _on_prerelease_toggle(self, value: bool):
        """Re-run the app update check immediately (Tk parity).

        When *unticking* (opting out), pass force_downgrade_prompt=True so the
        user is offered a switch to the latest stable even if it's older than
        the pre-release they're currently running. When *ticking*, no force —
        the normal upgrade check already handles "is there a newer build?".
        """
        check = getattr(self._window, "_check_for_app_update", None)
        if callable(check):
            check(force_downgrade_prompt=not value, force_fresh=True)

    # ---- helpers ----------------------------------------------------------
    def _rebuild_conflicts(self):
        """Ask the window to rebuild conflicts so a setting that affects them
        (e.g. Hide BSA conflicts) applies live without a manual refresh."""
        win = self._window
        if win is not None and hasattr(win, "_rebuild_conflicts_async"):
            try:
                win._rebuild_conflicts_async()
            except Exception:
                pass

    def _safe_save(self, save_fn, *args):
        try:
            save_fn(*args)
        except Exception as exc:
            self._notify(self.tr("Failed to save setting: {0}").format(exc), "warning")

    def _notify(self, text: str, state: str = "info"):
        win = self._window
        if win is not None and hasattr(win, "_notify"):
            try:
                win._notify(text, state)
                return
            except Exception:
                pass
        print(f"[settings] {text}")
