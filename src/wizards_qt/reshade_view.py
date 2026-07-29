"""ReShade installation wizard — Qt port of wizards/reshade.py.

Offered by every game (BaseGame._base_wizard_tools). Five steps in a
plugins-panel-scoped tab:
  1. Rendering API + executable architecture.
  2. Shader packs: optional preset picker + the full checkbox grid (greyed
     when a preset is loaded — all packs download then trim to the preset).
  3. Download ReShade DLL + shaders (parallel), prune to preset.
  4. Install d3dcompiler_47 into the Proton prefix (skippable).
  5. Install: destination (game / Root_Folder / managed mod) + Wine override.

All download/shader/preset/install logic is in the neutral Utils.reshade_tools;
this file is just the Qt UI + threading. Blocking work runs on daemon threads
and marshals back via Signals (never touch widgets off-thread).
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QStackedWidget,
    QComboBox, QRadioButton, QButtonGroup, QCheckBox, QScrollArea, QFrame,
    QLineEdit, QProgressBar, QGridLayout,
)

from gui_qt.theme_qt import active_palette, _c, button_qss, ok_text, err_text
from gui_qt.safe_emit import safe_emit
from Utils.reshade_tools import (
    API_CHOICES, OPTIONAL_SHADER_PACKS, OBSOLETE_PRESET_EFFECTS,
    download_and_extract_reshade_dll, download_and_extract_shaders,
    parse_preset_effect_files, prune_shaders_to_preset, install_reshade_files,
)

if TYPE_CHECKING:
    from Games.base_game import BaseGame

_WARN = "#d9a441"


class ReShadeView(QWidget):
    """Multi-step ReShade installer."""

    # Worker → UI thread.
    _preset_picked_sig = Signal(object)          # Path | None
    _dl_status_sig = Signal(str, str)            # (text, color)
    _dl_done_sig = Signal(bool)                  # ok?
    _d3d_status_sig = Signal(str, str)
    _d3d_done_sig = Signal(bool)
    _install_status_sig = Signal(str, str)
    _install_done_sig = Signal(bool)

    def __init__(self, game: "BaseGame", log_fn=None, on_close=None, ctx=None,
                 reshade_dll: str | None = None, reshade_arch: int | None = None):
        super().__init__()
        self._game = game
        self._log = log_fn or (lambda _m: None)
        self._on_close_cb = on_close or (lambda: None)
        self._ctx = ctx

        self._reshade_dll = reshade_dll or "dxgi.dll"
        self._reshade_arch = reshade_arch or 64
        self._override_key = Path(self._reshade_dll).stem

        self._preset_path: Path | None = None
        self._preset_wanted: set[str] = set()
        self._preset_missing: set[str] = set()
        self._extracted_dll: Path | None = None
        self._extracted_shaders: Path | None = None
        self._pack_checks: list[QCheckBox] = []
        self._mod_name_edited = False
        self._installed = False   # a successful install happened
        self._installed_dest = ""  # which destination the last install used

        self._closing = False   # teardown started — drop late worker signals

        # Each connection is guarded: a daemon worker (download / d3d install /
        # file copy) can emit AFTER the user closed the tab and the widget was
        # deleteLater()'d. Dropping late signals avoids touching a deleted widget.
        def _guard(fn):
            return lambda *a: None if self._closing else fn(*a)

        for sig, slot in (
            (self._preset_picked_sig, self._on_preset_picked),
            (self._dl_status_sig, lambda t, c: self._set_lbl(self._dl_status, t, c)),
            (self._dl_done_sig, self._on_dl_done),
            (self._d3d_status_sig, lambda t, c: self._set_lbl(self._d3d_status, t, c)),
            (self._d3d_done_sig, self._on_d3d_done),
            (self._install_status_sig, lambda t, c: self._set_lbl(self._install_status, t, c)),
            (self._install_done_sig, self._on_install_done),
        ):
            sig.connect(_guard(slot))

        self.setObjectName("ReShadeView")
        self._build()

    # ---- layout scaffolding ------------------------------------------------
    def _build(self):
        p = active_palette()
        self._dim = f"color:{_c(p,'TEXT_DIM')};"
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        bar = QWidget(); bar.setObjectName("HeaderBar")
        hb = QHBoxLayout(bar); hb.setContentsMargins(12, 8, 8, 8); hb.setSpacing(8)
        title = QLabel(self.tr("Install ReShade — {0}").format(self._game.name))
        title.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-weight:600;")
        hb.addWidget(title)
        hb.addStretch(1)
        self._close_btn = QPushButton(self.tr("✕ Close"))
        self._close_btn.setCursor(Qt.PointingHandCursor)
        self._close_btn.setStyleSheet(
            button_qss("BTN_DANGER", padding="5px 12px"))
        self._close_btn.clicked.connect(self._finish)
        hb.addWidget(self._close_btn)
        v.addWidget(bar)

        self._stack = QStackedWidget()
        v.addWidget(self._stack, 1)
        self._stack.addWidget(self._build_step_api())      # 0
        self._stack.addWidget(self._build_step_shaders())  # 1
        self._stack.addWidget(self._build_step_download()) # 2
        self._stack.addWidget(self._build_step_d3d())      # 3
        self._stack.addWidget(self._build_step_install())  # 4
        # The d3d step's prompt depends on live prefix/dep state — refresh it
        # whenever it becomes visible (Tk rebuilt the step on every show).
        self._stack.currentChanged.connect(
            lambda i: self._refresh_d3d_step() if i == 3 else None)
        self._stack.setCurrentIndex(0)

    def _page(self, title: str) -> tuple[QWidget, QVBoxLayout]:
        p = active_palette()
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.setSpacing(8)
        head = QLabel(title)
        head.setAlignment(Qt.AlignHCenter)
        head.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-weight:700;")
        lay.addWidget(head)
        return w, lay

    def _status(self, lay: QVBoxLayout) -> QLabel:
        lbl = QLabel("")
        lbl.setAlignment(Qt.AlignHCenter)
        lbl.setWordWrap(True)
        lbl.setStyleSheet(self._dim)
        lay.addWidget(lbl)
        return lbl

    def _set_lbl(self, lbl: QLabel, text: str, color: str):
        lbl.setStyleSheet(f"color:{color};" if color else self._dim)
        lbl.setText(text)

    def _primary(self, text: str) -> QPushButton:
        b = QPushButton(text)
        b.setCursor(Qt.PointingHandCursor)
        b.setStyleSheet(
            button_qss("BTN_INFO"))
        return b

    # ---- step 1: API / arch -------------------------------------------------
    def _build_step_api(self) -> QWidget:
        page, lay = self._page(self.tr("Step 1: Rendering API & Architecture"))
        note = QLabel(self.tr("Choose the graphics API this game uses and its executable "
                      "architecture. If you're not sure, dxgi.dll / 64-bit works "
                      "for most modern games."))
        note.setWordWrap(True); note.setAlignment(Qt.AlignHCenter)
        note.setStyleSheet(self._dim)
        lay.addWidget(note)

        lay.addWidget(self._field_label(self.tr("Rendering API (DLL)")))
        self._api_combo = QComboBox()
        self._api_combo.addItems([lbl for lbl, _ in API_CHOICES])
        default = next((i for i, (_, dll) in enumerate(API_CHOICES)
                        if dll == self._reshade_dll), 0)
        self._api_combo.setCurrentIndex(default)
        lay.addWidget(self._api_combo)

        lay.addWidget(self._field_label(self.tr("Executable architecture")))
        arch_row = QWidget()
        ah = QHBoxLayout(arch_row); ah.setContentsMargins(0, 0, 0, 0); ah.setSpacing(20)
        self._arch_group = QButtonGroup(self)
        for val, label in ((64, self.tr("64-bit")), (32, self.tr("32-bit"))):
            rb = QRadioButton(label)
            rb.setChecked(val == self._reshade_arch)
            self._arch_group.addButton(rb, val)
            ah.addWidget(rb)
        ah.addStretch(1)
        lay.addWidget(arch_row)

        lay.addStretch(1)
        nxt = self._primary(self.tr("Next →"))
        nxt.clicked.connect(self._apply_api_choice)
        lay.addWidget(nxt, 0, Qt.AlignHCenter)
        return page

    def _field_label(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(
            f"color:{_c(active_palette(),'TEXT_MAIN')}; font-weight:600;")
        return lbl

    def _apply_api_choice(self):
        chosen = self._api_combo.currentText()
        self._reshade_dll = next(
            (dll for lbl, dll in API_CHOICES if lbl == chosen), self._reshade_dll)
        self._override_key = Path(self._reshade_dll).stem
        self._reshade_arch = self._arch_group.checkedId() or 64
        self._stack.setCurrentIndex(1)

    # ---- step 2: shaders ----------------------------------------------------
    def _build_step_shaders(self) -> QWidget:
        page, lay = self._page(self.tr("Step 2: Select Shader Packs"))
        note = QLabel(self.tr("The official ReShade shaders are always included. Select "
                      "any additional packs to download:"))
        note.setWordWrap(True); note.setAlignment(Qt.AlignHCenter)
        note.setStyleSheet(self._dim)
        lay.addWidget(note)

        # Preset box.
        p = active_palette()
        preset_box = QFrame()
        preset_box.setStyleSheet(
            f"QFrame{{background:{_c(p,'BG_PANEL')}; border-radius:6px;}}")
        pv = QVBoxLayout(preset_box); pv.setContentsMargins(12, 10, 12, 10); pv.setSpacing(4)
        pv.addWidget(self._field_label(self.tr("Install from a preset (optional)")))
        pnote = QLabel(self.tr("Pick a ReShade preset (.ini) to install only the effects "
                       "it uses. All packs are downloaded then trimmed to the preset."))
        pnote.setWordWrap(True); pnote.setStyleSheet(self._dim)
        pv.addWidget(pnote)
        prow = QWidget(); ph = QHBoxLayout(prow); ph.setContentsMargins(0, 0, 0, 0); ph.setSpacing(8)
        self._preset_label = QLabel(self.tr("No preset selected"))
        self._preset_label.setStyleSheet(self._dim)
        ph.addWidget(self._preset_label, 1)
        self._clear_preset_btn = QPushButton(self.tr("Clear"))
        self._clear_preset_btn.clicked.connect(self._clear_preset)
        self._clear_preset_btn.setVisible(False)
        ph.addWidget(self._clear_preset_btn)
        browse = QPushButton(self.tr("Browse…"))
        browse.clicked.connect(self._browse_preset)
        ph.addWidget(browse)
        pv.addWidget(prow)
        lay.addWidget(preset_box)

        # Pack checkbox grid (scrollable).
        self._packs_hint = QLabel("")
        self._packs_hint.setStyleSheet(f"color:{_WARN};")
        self._packs_hint.setVisible(False)
        lay.addWidget(self._packs_hint)

        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setFrameShape(QFrame.NoFrame)
        grid_host = QWidget()
        grid = QGridLayout(grid_host); grid.setContentsMargins(4, 4, 4, 4)
        grid.setHorizontalSpacing(16); grid.setVerticalSpacing(2)
        cols = 2
        for i, (label, _url, _sub) in enumerate(OPTIONAL_SHADER_PACKS):
            cb = QCheckBox(label)
            self._pack_checks.append(cb)
            grid.addWidget(cb, i // cols, i % cols)
        scroll.setWidget(grid_host)
        lay.addWidget(scroll, 1)

        nav = QWidget(); nh = QHBoxLayout(nav); nh.setContentsMargins(0, 0, 0, 0); nh.setSpacing(8)
        back = QPushButton(self.tr("← Back")); back.clicked.connect(lambda: self._stack.setCurrentIndex(0))
        nh.addWidget(back)
        nh.addStretch(1)
        nxt = self._primary(self.tr("Next →"))
        nxt.clicked.connect(self._start_download)
        nh.addWidget(nxt)
        lay.addWidget(nav)
        return page

    def _browse_preset(self):
        from Utils.portal_filechooser import pick_preset_file
        # Callback fires on the portal WORKER thread — marshal via Signal.
        pick_preset_file(self.tr("Select a ReShade preset (.ini)"),
                         lambda path: safe_emit(self._preset_picked_sig, path))

    def _on_preset_picked(self, path):
        if not path:
            return
        path = Path(path)
        wanted = parse_preset_effect_files(path)
        if not wanted:
            self._log("ReShade wizard: preset has no Techniques= list — ignoring "
                      "it and using the ticked packs instead.")
            self._preset_label.setText(
                self.tr("{0} (no effects found — using pack selection)").format(path.name))
            self._preset_label.setStyleSheet(f"color:{_WARN};")
            return
        self._preset_path = path
        self._preset_wanted = wanted
        self._log(f"ReShade wizard: preset '{path.name}' needs {len(wanted)} effect(s).")
        self._preset_label.setText(path.name)
        self._preset_label.setStyleSheet(f"color:{ok_text()};")
        self._clear_preset_btn.setVisible(True)
        self._apply_preset_lock(True)

    def _clear_preset(self):
        self._preset_path = None
        self._preset_wanted = set()
        self._preset_label.setText(self.tr("No preset selected"))
        self._preset_label.setStyleSheet(self._dim)
        self._clear_preset_btn.setVisible(False)
        self._apply_preset_lock(False)

    def _apply_preset_lock(self, locked: bool):
        for cb in self._pack_checks:
            cb.setEnabled(not locked)
        self._packs_hint.setVisible(locked)
        if locked:
            self._packs_hint.setText(
                self.tr("A preset is loaded — all packs will be downloaded and trimmed "
                "to it, so individual selection is disabled."))

    # ---- step 3: download ---------------------------------------------------
    def _build_step_download(self) -> QWidget:
        page, lay = self._page(self.tr("Step 3: Download ReShade"))
        self._dl_status = self._status(lay)
        self._dl_status.setText(self.tr("Fetching latest ReShade version…"))
        self._dl_bar = QProgressBar()
        self._dl_bar.setRange(0, 0)   # indeterminate
        self._dl_bar.setTextVisible(False)
        lay.addWidget(self._dl_bar)
        lay.addStretch(1)
        self._dl_next_btn = self._primary(self.tr("Next →"))
        self._dl_next_btn.setEnabled(False)
        self._dl_next_btn.clicked.connect(lambda: self._stack.setCurrentIndex(3))
        lay.addWidget(self._dl_next_btn, 0, Qt.AlignHCenter)
        return page

    def _start_download(self):
        self._stack.setCurrentIndex(2)
        self._dl_bar.setRange(0, 0)
        self._dl_bar.setVisible(True)
        self._dl_next_btn.setEnabled(False)
        self._dl_next_btn.setText(self.tr("Next →"))
        safe_emit(self._dl_status_sig,
                  self.tr("Downloading ReShade and shaders…"), "")

        arch = self._reshade_arch
        preset = self._preset_path
        wanted = set(self._preset_wanted)
        if preset is not None:
            packs = list(OPTIONAL_SHADER_PACKS)
        else:
            packs = [pk for pk, cb in zip(OPTIONAL_SHADER_PACKS, self._pack_checks)
                     if cb.isChecked()]

        def worker():
            from Utils.config_paths import get_config_dir
            try:
                tmp = get_config_dir() / "download_cache" / "reshade"
                tmp.mkdir(parents=True, exist_ok=True)

                dll_res: list = []
                sh_res: list = []
                dll_exc: list = []
                sh_exc: list = []

                def _get_dll():
                    try:
                        dll_res.append(download_and_extract_reshade_dll(tmp, arch))
                    except Exception as e:
                        dll_exc.append(e)

                def _get_shaders():
                    try:
                        sh_res.append(download_and_extract_shaders(tmp, packs))
                    except Exception as e:
                        sh_exc.append(e)

                t1 = threading.Thread(target=_get_dll, daemon=True)
                t2 = threading.Thread(target=_get_shaders, daemon=True)
                t1.start(); t2.start(); t1.join(); t2.join()
                if dll_exc:
                    raise RuntimeError(f"ReShade DLL: {dll_exc[0]}")
                if sh_exc:
                    raise RuntimeError(f"Shaders: {sh_exc[0]}")

                self._extracted_dll = dll_res[0]
                self._extracted_shaders = sh_res[0]
                self._log(f"ReShade wizard: downloaded {self._extracted_dll.name} and shaders.")

                ok_msg = self.tr("Downloaded ReShade and shaders successfully.")
                if preset is not None and wanted:
                    safe_emit(self._dl_status_sig,
                              self.tr("Trimming shaders to preset…"), "")
                    self._preset_missing = prune_shaders_to_preset(
                        self._extracted_shaders, wanted)
                    kept = len(wanted) - len(self._preset_missing)
                    self._log(f"ReShade wizard: kept {kept}/{len(wanted)} preset "
                              f"effect(s); {len(self._preset_missing)} missing.")
                    if self._preset_missing:
                        unavailable = sorted(
                            m for m in self._preset_missing
                            if m not in OBSOLETE_PRESET_EFFECTS)
                        obsolete = sorted(
                            m for m in self._preset_missing
                            if m in OBSOLETE_PRESET_EFFECTS)
                        lines = [self.tr("Installed {0} of {1} preset effects.")
                                 .format(kept, len(wanted))]
                        if unavailable:
                            lines.append(self.tr("Missing (not in any pack): {0}")
                                         .format(", ".join(unavailable)))
                        if obsolete:
                            lines.append(
                                self.tr("Skipped (renamed/removed upstream): {0}")
                                .format(", ".join(obsolete)))
                        ok_msg = "\n".join(lines)
                    else:
                        ok_msg = self.tr("Trimmed shaders to {0} preset effect(s).") \
                            .format(kept)
                safe_emit(self._dl_status_sig, ok_msg, ok_text())
                safe_emit(self._dl_done_sig, True)
            except Exception as exc:
                self._log(f"ReShade wizard: download failed: {exc}")
                safe_emit(self._dl_status_sig,
                    self.tr("Download failed:\n{0}\n\nCheck your internet connection "
                    "and try again.").format(exc), err_text())
                safe_emit(self._dl_done_sig, False)

        threading.Thread(target=worker, daemon=True, name="reshade-download").start()

    def _on_dl_done(self, ok: bool):
        self._dl_bar.setVisible(False)
        self._dl_next_btn.setEnabled(True)
        if ok:
            self._dl_next_btn.setText(self.tr("Next →"))
            self._rewire(self._dl_next_btn,
                         lambda: self._stack.setCurrentIndex(3))
        else:
            self._dl_next_btn.setText(self.tr("Retry ↺"))
            self._rewire(self._dl_next_btn, self._start_download)

    # ---- step 4: d3dcompiler ------------------------------------------------
    def _build_step_d3d(self) -> QWidget:
        page, lay = self._page(self.tr("Step 4: Install d3dcompiler_47"))
        self._d3d_info = QLabel("")
        self._d3d_info.setWordWrap(True); self._d3d_info.setAlignment(Qt.AlignHCenter)
        lay.addWidget(self._d3d_info)
        self._d3d_status = self._status(lay)
        lay.addStretch(1)
        nav = QWidget(); nh = QHBoxLayout(nav); nh.setContentsMargins(0, 0, 0, 0); nh.setSpacing(8)
        nh.addStretch(1)
        skip = QPushButton(self.tr("Skip")); skip.clicked.connect(lambda: self._enter_install())
        nh.addWidget(skip)
        self._d3d_btn = self._primary(self.tr("Install d3dcompiler_47"))
        self._d3d_btn.clicked.connect(self._install_d3d)
        nh.addWidget(self._d3d_btn)
        nh.addStretch(1)
        lay.addWidget(nav)
        page._entered = False  # type: ignore[attr-defined]
        return page

    def _refresh_d3d_step(self):
        from Utils.protontricks import D3D_DEP_KEY, is_dep_installed
        from Utils.steam_finder import game_steam_id
        steam_id = game_steam_id(self._game)
        prefix = getattr(self._game, "_prefix_path", None)
        has_prefix = bool(prefix) and Path(prefix).is_dir()
        can_install = has_prefix or bool(steam_id)
        already = has_prefix and is_dep_installed(Path(prefix), D3D_DEP_KEY)

        if already:
            self._d3d_info.setText(self.tr("d3dcompiler_47 is already installed in this "
                                   "prefix.\nYou can skip this step."))
            self._d3d_info.setStyleSheet(f"color:{ok_text()};")
            self._d3d_btn.setText(self.tr("Next →"))
            self._rewire(self._d3d_btn, self._enter_install)
        elif not can_install:
            self._d3d_info.setText(
                self.tr("No Proton prefix or Steam ID is configured for this game — "
                "d3dcompiler_47 cannot be installed automatically. Install it "
                "manually via winecfg before running the game with ReShade."))
            self._d3d_info.setStyleSheet(f"color:{_WARN};")
            self._d3d_btn.setEnabled(False)
        else:
            self._d3d_info.setText(
                self.tr("d3dcompiler_47 will be installed into the Proton prefix for this "
                "game (via protontricks if available, otherwise bundled "
                "winetricks).\n\nThis may take up to a minute."))
            self._d3d_info.setStyleSheet(self._dim)
            self._d3d_btn.setEnabled(True)
            self._d3d_btn.setText(self.tr("Install d3dcompiler_47"))
            self._rewire(self._d3d_btn, self._install_d3d)

    def _install_d3d(self):
        self._d3d_btn.setEnabled(False)
        self._d3d_btn.setText(self.tr("Installing…"))
        game = self._game

        def worker():
            from Utils.proton_tools import install_d3dcompiler_47
            ok = False
            try:
                ok = install_d3dcompiler_47(
                    game, log_fn=lambda m: safe_emit(self._d3d_status_sig, str(m), ""))
            except Exception as exc:
                safe_emit(self._d3d_status_sig,
                          self.tr("Install error: {0}").format(exc), err_text())
            safe_emit(self._d3d_done_sig, bool(ok))

        threading.Thread(target=worker, daemon=True, name="reshade-d3d").start()

    def _on_d3d_done(self, ok: bool):
        self._d3d_btn.setEnabled(True)
        if ok:
            self._set_lbl(self._d3d_status,
                          self.tr("d3dcompiler_47 installed successfully.\nClick Next to "
                          "continue."), ok_text())
            self._d3d_btn.setText(self.tr("Next →"))
            self._rewire(self._d3d_btn, self._enter_install)
        else:
            self._set_lbl(self._d3d_status,
                          self.tr("Install failed — you can Skip and install it "
                          "manually."),
                          err_text())
            self._d3d_btn.setText(self.tr("Retry"))
            self._rewire(self._d3d_btn, self._install_d3d)

    def _rewire(self, btn: QPushButton, slot):
        try:
            btn.clicked.disconnect()
        except Exception:
            pass
        btn.clicked.connect(slot)

    # ---- step 5: install ----------------------------------------------------
    def _build_step_install(self) -> QWidget:
        page, lay = self._page(self.tr("Step 5: Install ReShade"))
        self._install_info = QLabel("")
        self._install_info.setWordWrap(True); self._install_info.setAlignment(Qt.AlignHCenter)
        self._install_info.setStyleSheet(self._dim)
        lay.addWidget(self._install_info)

        p = active_palette()
        box = QFrame()
        box.setStyleSheet(f"QFrame{{background:{_c(p,'BG_PANEL')}; border-radius:6px;}}")
        bv = QVBoxLayout(box); bv.setContentsMargins(12, 10, 12, 10); bv.setSpacing(4)
        bv.addWidget(self._field_label(self.tr("Install destination")))
        self._dest_group = QButtonGroup(self)
        for val, label in (("game", self.tr("Game folder")),
                           ("root_folder", self.tr("Root_Folder (staging)")),
                           ("mod", self.tr("As a managed mod (root-flagged)"))):
            rb = QRadioButton(label)
            rb.setProperty("dest", val)
            if val == "game":
                rb.setChecked(True)
            self._dest_group.addButton(rb)
            rb.toggled.connect(self._sync_mod_name_state)
            bv.addWidget(rb)

        mod_row = QWidget(); mh = QHBoxLayout(mod_row); mh.setContentsMargins(0, 4, 0, 0); mh.setSpacing(8)
        self._mod_name_lbl = QLabel(self.tr("Mod name"))
        self._mod_name_lbl.setStyleSheet(self._dim)
        mh.addWidget(self._mod_name_lbl)
        self._mod_name_edit = QLineEdit("ReShade")
        self._mod_name_edit.textEdited.connect(lambda _t: setattr(self, "_mod_name_edited", True))
        mh.addWidget(self._mod_name_edit, 1)
        bv.addWidget(mod_row)
        lay.addWidget(box)

        self._install_status = self._status(lay)
        lay.addStretch(1)

        nav = QWidget(); nh = QHBoxLayout(nav); nh.setContentsMargins(0, 0, 0, 0); nh.setSpacing(8)
        nh.addStretch(1)
        self._install_btn = self._primary(self.tr("Install"))
        self._install_btn.clicked.connect(self._do_install)
        nh.addWidget(self._install_btn)
        self._done_btn = QPushButton(self.tr("Done"))
        self._done_btn.setEnabled(False)
        self._done_btn.setCursor(Qt.PointingHandCursor)
        self._done_btn.setStyleSheet(
            button_qss("BTN_SUCCESS"))
        self._done_btn.clicked.connect(self._finish)
        nh.addWidget(self._done_btn)
        lay.addWidget(nav)
        return page

    def _enter_install(self):
        # Populate the fields that depend on earlier steps, then show it.
        self._install_info.setText(
            self.tr('ReShade will be installed as  {0}\nand the Wine DLL override  {1}=native,builtin\nwill be written to the Proton prefix.').format(self._reshade_dll, self._override_key))
        # Default the mod name to "<preset> - ReShade" when a preset is loaded
        # and the field is still the untouched "ReShade".
        if (self._preset_path is not None and not self._mod_name_edited
                and self._mod_name_edit.text().strip() in ("", "ReShade")):
            label = self._preset_path.stem.replace("_", " ").strip()
            if label:
                self._mod_name_edit.setText(self.tr("{0} - ReShade").format(label))
        self._sync_mod_name_state()
        self._stack.setCurrentIndex(4)

    def _selected_dest(self) -> str:
        btn = self._dest_group.checkedButton()
        return btn.property("dest") if btn is not None else "game"

    def _sync_mod_name_state(self, *_):
        is_mod = self._selected_dest() == "mod"
        self._mod_name_lbl.setEnabled(is_mod)
        self._mod_name_edit.setEnabled(is_mod)

    def _do_install(self):
        self._install_btn.setEnabled(False)
        self._install_btn.setText(self.tr("Installing…"))
        game = self._game
        dest = self._selected_dest()
        self._installed_dest = dest
        mod_name = self._mod_name_edit.text()
        args = dict(
            reshade_dll=self._reshade_dll, override_key=self._override_key,
            dest=dest, mod_name=mod_name,
            extracted_dll=self._extracted_dll,
            extracted_shaders=self._extracted_shaders,
            preset_path=self._preset_path)

        def worker():
            try:
                # NB: install_reshade_files does NO UI refresh — we reload the
                # modlist below on the GUI thread (in _on_install_done).
                # Touching widgets from this worker spawns a stray window.
                msg = install_reshade_files(
                    game, log_fn=lambda m: self._log(str(m)), **args)
                safe_emit(self._install_status_sig, msg, ok_text())
                safe_emit(self._install_done_sig, True)
            except Exception as exc:
                self._log(f"ReShade wizard error: {exc}")
                safe_emit(self._install_status_sig,
                          self.tr("Error: {0}").format(exc), err_text())
                safe_emit(self._install_done_sig, False)

        threading.Thread(target=worker, daemon=True, name="reshade-install").start()

    def _on_install_done(self, ok: bool):
        if ok:
            self._installed = True
            self._install_btn.setEnabled(False)
            self._install_btn.setText(self.tr("Install"))
            self._done_btn.setEnabled(True)
            # A managed-mod install changed modlist.txt — reload it now, on the
            # GUI thread, so the new mod shows without waiting for Done.
            if self._installed_dest == "mod":
                refresh = getattr(self._ctx, "refresh_modlist", None)
                if refresh is not None:
                    refresh()
        else:
            self._install_btn.setEnabled(True)
            self._install_btn.setText(self.tr("Retry"))

    # ---- shared -------------------------------------------------------------
    def _finish(self):
        # A managed-mod install already refreshed the modlist on the GUI thread
        # in _on_install_done; game/Root_Folder installs don't touch modlist.txt.
        if self._closing:
            return
        self._closing = True
        self._on_close_cb()
