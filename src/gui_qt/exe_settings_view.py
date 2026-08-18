"""Per-executable settings - a plugins-panel-scoped tab.

Qt port of the non-launcher branch of Tk's ExeConfigPanel (gui/dialogs.py):
launch arguments (+ insert game/mod path), Proton version override with the
prefix tool buttons (run exe / winetricks / open folder), Steam-style launch
options, and Remove EXE. "Run from Data folder" is gone - the dropdown no
longer scans the staging tree. Entries are manual custom exes, plus
auto-detected framework launchers (installed script extenders) for which
Remove becomes "Hide from dropdown" (they aren't in custom_exes.json).

All persistence goes through Utils.exe_launch (same files the Tk app uses).
The prefix tool workers run on daemon threads and only touch log_fn (the
app's thread-safe _append_log) - never widgets.
"""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame,
    QComboBox, QLineEdit, QPlainTextEdit, QMenu, QScrollArea, QCheckBox,
)

from gui_qt.safe_emit import safe_emit
from gui_qt.theme_qt import active_palette, _c, danger_close_button, button_qss
from gui_qt.help_marker import tip_text, make_help_marker, help_mark_qss
from gui_qt.wheel_guard import no_wheel
from gui_qt.worker import run_in_worker
from Utils import exe_launch
from Utils.wine_paths import to_wine_path


class ExeSettingsView(QWidget):
    """Scoped-tab body for configuring one custom exe."""

    # Emitted from the Java-install worker to re-enable the button on the UI thread.
    _install_java_done = Signal()

    def __init__(self, game, exe_path: Path, on_close, log_fn=None,
                 is_auto: bool = False):
        super().__init__()
        self._game = game
        self._exe_path = exe_path
        self._on_close = on_close or (lambda removed: None)
        self._log = log_fn or (lambda _m: None)
        # Auto-detected framework entry (installed script extender): not in
        # custom_exes.json, so "Remove" becomes "Hide from dropdown".
        self._is_auto = is_auto
        # Script extender: pinned to the game's prefix, so the Proton picker
        # is disabled below.
        self._is_framework = exe_launch.is_framework_launch_exe(
            game, exe_path.name)

        from Utils.steam_finder import list_installed_proton
        self._proton_versions = (
            ["Game default"] + [p.parent.name for p in list_installed_proton()]
        )

        self.setObjectName("ExeSettingsView")
        self._build()
        self._load_saved()
        self._install_java_done.connect(self._on_install_java_done)

    def _on_install_java_done(self):
        if hasattr(self, "_install_java_btn"):
            self._install_java_btn.setEnabled(True)
            self._install_java_btn.setText(self.tr("Install Java into prefix"))

    # ---- tooltip helpers --------------------------------------------------
    def _tip_text(self, text: str) -> str:
        return tip_text(text)

    def _help_marker(self, text: str) -> QLabel:
        return make_help_marker(text)

    # ---- layout -----------------------------------------------------------
    def _build(self):
        p = self._pal = active_palette()
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        # Header bar: title + Close.
        bar = QWidget(); bar.setObjectName("HeaderBar")
        hb = QHBoxLayout(bar); hb.setContentsMargins(12, 8, 8, 8); hb.setSpacing(8)
        title = QLabel(self.tr("Configure: {0}").format(self._exe_path.name))
        title.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-weight:600;")
        hb.addWidget(title)
        hb.addStretch(1)
        close = danger_close_button(pal=p)
        close.clicked.connect(lambda: self._on_close(False))
        hb.addWidget(close)
        v.addWidget(bar)

        # Scrollable body with the settings sections.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        body = QWidget()
        bv = QVBoxLayout(body)
        bv.setContentsMargins(12, 12, 12, 12)
        bv.setSpacing(10)

        def section(title_text: str, tip: str = "") -> tuple[QFrame, QVBoxLayout]:
            sec = QFrame()
            sec.setObjectName("SettingsSection")
            sec.setStyleSheet(
                f"#SettingsSection {{ background:{_c(p,'BG_PANEL')};"
                f" border:1px solid {_c(p,'BORDER')}; border-radius:6px; }}"
                f" {help_mark_qss(p)}")
            sv = QVBoxLayout(sec)
            sv.setContentsMargins(10, 8, 10, 8)
            sv.setSpacing(4)
            # Title row: label + a "?" marker whose tooltip carries the
            # description (matches the Settings menu - no inline hint text).
            head = QHBoxLayout(); head.setContentsMargins(0, 0, 0, 0); head.setSpacing(6)
            lbl = QLabel(title_text)
            lbl.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-weight:600;")
            head.addWidget(lbl)
            if tip:
                head.addWidget(self._help_marker(tip))
            head.addStretch(1)
            sv.addLayout(head)
            return sec, sv

        self._is_jar = exe_launch.is_jar(self._exe_path)
        # Native Linux launcher: runs on the host, so every Proton/Wine
        # control below is meaningless for it and the section is skipped.
        self._is_sh = exe_launch.is_shell_script(self._exe_path)

        # -- Java runtime (.jar only) ----------------------------------------
        if self._is_jar:
            sec_jar, sj = section(self.tr("Java runtime"), self.tr(
                "How to run this .jar:\n"
                "Host: run with your system's java (no Proton). Set the Java "
                "command in Launch Options, e.g. 'java -jar %command%' "
                "(%command% is the jar path).\n"
                "Proton prefix: click 'Install Java into prefix' once, then it "
                "runs automatically as 'java.exe -jar <jar>' - anything you put "
                "in Launch Options / Launch arguments is appended as extra "
                "flags. Which prefix follows the Proton version below "
                "('Game default' = the game's prefix; a specific version = an "
                "isolated prefix next to the jar)."))
            self._jar_runtime_combo = QComboBox()
            self._jar_runtime_combo.addItem(self.tr("Host (system java)"),
                                            exe_launch.JAR_RUNTIME_HOST)
            self._jar_runtime_combo.addItem(self.tr("Proton prefix (Windows Java)"),
                                            exe_launch.JAR_RUNTIME_PROTON)
            no_wheel(self._jar_runtime_combo)
            jr_row = QHBoxLayout(); jr_row.setSpacing(8)
            jr_row.addWidget(self._jar_runtime_combo)
            self._install_java_btn = QPushButton(self.tr("Install Java into prefix"))
            self._install_java_btn.setObjectName("FormButton")
            self._install_java_btn.setCursor(Qt.PointingHandCursor)
            self._install_java_btn.clicked.connect(self._install_java_into_prefix)
            jr_row.addWidget(self._install_java_btn)
            jr_row.addStretch(1)
            sj.addLayout(jr_row)
            bv.addWidget(sec_jar)

        # -- Launch arguments ------------------------------------------------
        args_help = self.tr(
            "Arguments passed to the script. The buttons below insert Linux "
            "paths for file arguments.") if self._is_sh else self.tr(
            "Arguments passed to the exe. Use Wine paths for file arguments "
            "(e.g. Z:\\home\\...) - the buttons below insert them for you.")
        sec_args, sa = section(self.tr("Launch arguments"), args_help)
        self._args_box = QPlainTextEdit()
        self._args_box.setFixedHeight(90)
        sa.addWidget(self._args_box)
        insert_row = QHBoxLayout()
        insert_row.setSpacing(6)
        btn_game = QPushButton(self.tr("Insert game path"))
        btn_game.setObjectName("FormButton")
        btn_game.setCursor(Qt.PointingHandCursor)
        btn_game.clicked.connect(self._insert_game_path)
        insert_row.addWidget(btn_game)
        self._insert_mod_btn = QPushButton(self.tr("Insert mod path ▼"))
        self._insert_mod_btn.setObjectName("FormButton")
        self._insert_mod_btn.setCursor(Qt.PointingHandCursor)
        self._insert_mod_btn.clicked.connect(self._open_mod_menu)
        insert_row.addWidget(self._insert_mod_btn)
        insert_row.addStretch(1)
        sa.addLayout(insert_row)
        bv.addWidget(sec_args)

        # -- Proton version ---------------------------------------------------
        # A .sh runs on the host, so it gets no Proton picker and no prefix
        # tools; _proton_combo / _winetricks_chk stay None for it.
        self._proton_combo = None
        self._winetricks_chk = None
        if not self._is_sh:
            proton_help = self.tr(
                "Use a specific Proton version with an isolated prefix next to the "
                "exe, instead of the game's prefix. Useful for tools that don't "
                "work with the game's Proton version. For Bethesda games the game "
                "path (registry), plugins.txt and My Games INIs are set up in the "
                "prefix automatically at launch.")
            if self._is_framework:
                proton_help = self.tr(
                    "Script extenders always run in the game's own prefix with the "
                    "game's Proton version: they launch the game itself, which "
                    "needs the game's Steam app ID and its INIs, saves and mod "
                    "DLLs. Change the game's Proton version in the game settings "
                    "instead.")
            sec_proton, sp = section(self.tr("Proton version"), proton_help)
            proton_row = QHBoxLayout()
            proton_row.setSpacing(8)
            self._proton_combo = QComboBox()
            self._proton_combo.addItems(self._proton_versions)
            no_wheel(self._proton_combo)
            if self._is_framework:
                self._proton_combo.setCurrentText("Game default")
                self._proton_combo.setEnabled(False)
                self._proton_combo.setToolTip(self._tip_text(proton_help))
            proton_row.addWidget(self._proton_combo)
            proton_row.addStretch(1)
            sp.addLayout(proton_row)
            if not self._is_jar:
                wt_help = self.tr(
                    "Run this exe with bare Wine against the same prefix instead "
                    "of a Proton session - no Steam client attach, so Steam Input "
                    "keeps the desktop controls (trackpad / on-screen keyboard). "
                    "The prefix is still created and updated through Proton. Env "
                    "vars in Launch Options still apply; wrappers and %command% "
                    "are skipped in this mode.")
                self._winetricks_chk = QCheckBox(
                    self.tr("Launch with plain Wine (winetricks-style)"))
                self._winetricks_chk.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')};")
                self._winetricks_chk.setToolTip(self._tip_text(wt_help))
                wt_row = QHBoxLayout(); wt_row.setContentsMargins(0, 0, 0, 0)
                wt_row.setSpacing(6)
                wt_row.addWidget(self._winetricks_chk)
                wt_row.addWidget(self._help_marker(wt_help))
                wt_row.addStretch(1)
                sp.addLayout(wt_row)
            tool_row = QHBoxLayout()
            tool_row.setSpacing(6)
            for label, cb in ((self.tr("Run EXE in prefix…"), self._run_exe_in_prefix),
                              (self.tr("Run winecfg"), self._run_winecfg_in_prefix),
                              (self.tr("Run winetricks"), self._run_winetricks_in_prefix),
                              (self.tr("Open prefix folder"), self._open_prefix_folder)):
                b = QPushButton(label)
                b.setObjectName("FormButton")
                b.setCursor(Qt.PointingHandCursor)
                b.clicked.connect(cb)
                tool_row.addWidget(b)
            tool_row.addStretch(1)
            sp.addLayout(tool_row)
            bv.addWidget(sec_proton)

        # -- Launch options ----------------------------------------------------
        sec_opts, so = section(self.tr("Launch Options"), self.tr(
            "Steam-style options: env vars (KEY=VALUE), wrappers (e.g. "
            "gamemoderun), and %command% as placeholder for the full command. "
            "Without %command%, appended as suffix."))
        self._options_edit = QLineEdit()
        self._options_edit.setPlaceholderText(
            self.tr("e.g. PROTON_ENABLE_WAYLAND=0 gamemoderun %command%"))
        so.addWidget(self._options_edit)
        bv.addWidget(sec_opts)

        # -- Deploy on run -----------------------------------------------------
        deploy_help = self.tr(
            "Runs the same deploy as the Deploy button, then launches this exe "
            "once the deploy finishes.")
        sec_deploy, sd = section(self.tr("Deploy on run"), deploy_help)
        self._deploy_on_run_chk = QCheckBox(
            self.tr("Deploy the modlist before running this exe"))
        self._deploy_on_run_chk.setStyleSheet(
            f"color:{_c(p,'TEXT_MAIN')};")
        self._deploy_on_run_chk.setToolTip(self._tip_text(deploy_help))
        sd.addWidget(self._deploy_on_run_chk)
        bv.addWidget(sec_deploy)

        bv.addStretch(1)
        scroll.setWidget(body)
        v.addWidget(scroll, 1)

        # -- Bottom bar ---------------------------------------------------------
        foot = QWidget(); foot.setObjectName("HeaderBar")
        fb = QHBoxLayout(foot); fb.setContentsMargins(12, 8, 12, 8); fb.setSpacing(6)
        remove = QPushButton(self.tr("Hide from dropdown") if self._is_auto
                             else self.tr("Remove EXE"))
        remove.setCursor(Qt.PointingHandCursor)
        remove.setStyleSheet(button_qss("BTN_DANGER", padding="6px 14px"))
        remove.clicked.connect(self._on_remove)
        fb.addWidget(remove)
        fb.addStretch(1)
        cancel = QPushButton(self.tr("Cancel"))
        cancel.setObjectName("FormButton")
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.clicked.connect(lambda: self._on_close(False))
        fb.addWidget(cancel)
        save = QPushButton(self.tr("Save"))
        save.setObjectName("PrimaryButton")
        save.setCursor(Qt.PointingHandCursor)
        save.clicked.connect(self._on_save)
        fb.addWidget(save)
        v.addWidget(foot)

    # ---- saved state --------------------------------------------------------
    def _load_saved(self):
        game, name = self._game, self._exe_path.name
        self._args_box.setPlainText(exe_launch.load_exe_args(game, name))
        self._options_edit.setText(exe_launch.load_launch_options(game, name))
        # Script extenders stay on "Game default" whatever was saved before the
        # picker was gated (the launch path ignores it too).
        if self._proton_combo is not None and not self._is_framework:
            saved = exe_launch.load_proton_override(game, name) or ""
            self._proton_combo.setCurrentText(self._best_proton_match(saved))
        self._deploy_on_run_chk.setChecked(
            exe_launch.load_deploy_on_run(game, name))
        if self._winetricks_chk is not None:
            self._winetricks_chk.setChecked(
                exe_launch.load_winetricks_style(game, name))
        if self._is_jar:
            runtime = exe_launch.load_jar_runtime(game, name)
            idx = self._jar_runtime_combo.findData(runtime)
            if idx >= 0:
                self._jar_runtime_combo.setCurrentIndex(idx)

    def _best_proton_match(self, name: str) -> str:
        """Exact match first, then prefix match ("Proton 10" → "Proton 10.0")."""
        if not name:
            return "Game default"
        if name in self._proton_versions:
            return name
        name_lower = name.lower()
        for v in self._proton_versions:
            if v.lower().startswith(name_lower):
                return v
        return "Game default"

    def _on_save(self):
        game, name = self._game, self._exe_path.name
        exe_launch.save_exe_args(game, name, self._args_box.toPlainText().strip())
        if self._proton_combo is not None:
            selected = self._proton_combo.currentText()
            exe_launch.save_proton_override(
                game, name, "" if selected == "Game default" else selected)
        exe_launch.save_launch_options(game, name,
                                       self._options_edit.text().strip())
        exe_launch.save_deploy_on_run(
            game, name, self._deploy_on_run_chk.isChecked())
        if self._winetricks_chk is not None:
            exe_launch.save_winetricks_style(
                game, name, self._winetricks_chk.isChecked())
        if self._is_jar:
            exe_launch.save_jar_runtime(
                game, name, self._jar_runtime_combo.currentData())
        self._log(f"[exe] settings saved for {name}")
        self._on_close(False)

    def _on_remove(self):
        if self._is_auto:
            # Auto-detected entry - persist the hide so it stays gone across
            # refreshes; also drop any duplicate manual entry for the same exe.
            exe_launch.hide_auto_exe(self._game, self._exe_path.name)
            exe_launch.remove_custom_exe(self._game, self._exe_path)
            self._log(f"[exe] hid {self._exe_path.name} from the exe dropdown")
        else:
            exe_launch.remove_custom_exe(self._game, self._exe_path)
            self._log(f"[exe] removed {self._exe_path.name} from the exe list")
        self._on_close(True)

    # ---- insert helpers -----------------------------------------------------
    def _insert_arg_text(self, text: str):
        existing = self._args_box.toPlainText()
        if existing and not existing.endswith(" "):
            text = " " + text
        self._args_box.moveCursor(self._args_box.textCursor().MoveOperation.End)
        self._args_box.insertPlainText(text)

    def _path_token(self, path) -> str:
        """Quoted path for the args box: a Wine path for Wine targets, the
        plain Linux path for a .sh, which runs on the host."""
        return f'"{path if self._is_sh else to_wine_path(path)}"'

    def _insert_game_path(self):
        game_path = (self._game.get_game_path()
                     if hasattr(self._game, "get_game_path") else None)
        if game_path is None:
            self._log("[exe] game path not set.")
            return
        self._insert_arg_text(self._path_token(game_path))

    def _mod_entries(self) -> list[tuple[str, Path]]:
        """overwrite + staging mod dirs, like Tk's insert-mod-path popup."""
        entries: list[tuple[str, Path]] = []
        mods_path = (self._game.get_effective_mod_staging_path()
                     if hasattr(self._game, "get_effective_mod_staging_path")
                     else None)
        overwrite = mods_path.parent / "overwrite" if mods_path else None
        if overwrite is not None and overwrite.is_dir():
            entries.append(("overwrite", overwrite))
        if mods_path is not None and mods_path.is_dir():
            for e in sorted(mods_path.iterdir(), key=lambda p: p.name.casefold()):
                if e.is_dir() and "_separator" not in e.name:
                    entries.append((e.name, e))
        return entries

    def _open_mod_menu(self):
        menu = QMenu(self)
        entries = self._mod_entries()
        if not entries:
            menu.addAction(self.tr("(no mods found)")).setEnabled(False)
        for name, path in entries:
            menu.addAction(name, lambda pa=path:
                           self._insert_arg_text(self._path_token(pa)))
        menu.exec(self._insert_mod_btn.mapToGlobal(
            self._insert_mod_btn.rect().bottomLeft()))

    # ---- prefix tools ---------------------------------------------------------
    # Workers only touch exe_launch + log_fn (thread-safe); the Proton pick is
    # read from the combo on the UI thread before the thread starts.

    def _selected_proton(self) -> str | None:
        selected = self._proton_combo.currentText()
        if selected == "Game default":
            self._log("Prefix tools: select a specific Proton version first.")
            return None
        return selected

    def _run_exe_in_prefix(self):
        selected = self._selected_proton()
        if selected is None:
            return
        game, exe_path, log = self._game, self._exe_path, self._log

        def worker():
            result = exe_launch.prepare_tool_prefix(exe_path, selected, game,
                                                    log_fn=log)
            if result is None:
                return
            proton_script, prefix_dir, env = result
            log(f"Prefix tools: initialised prefix at {prefix_dir}, "
                "opening file picker …")

            def on_picked(exe):
                # Fires on the picker's worker thread - no widgets touched.
                if exe is None:
                    return
                if not exe.is_file():
                    log(f"Prefix tools: file not found: {exe}")
                    return
                log(f"Prefix tools: launching {exe.name} …")
                from Utils.steam_finder import proton_run_command
                try:
                    subprocess.Popen(
                        # runinprefix: isolated tool prefix - no steam.exe
                        # shim, so Steam doesn't show the game as "Running".
                        proton_run_command(proton_script, "runinprefix",
                                           str(exe), env=env),
                        env=env, cwd=exe.parent,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    )
                except Exception as e:
                    log(f"Prefix tools error: {e}")

            from Utils.portal_filechooser import pick_exe_file
            pick_exe_file("Select EXE to run in prefix", on_picked)

        threading.Thread(target=worker, daemon=True,
                         name="exe-prefix-run").start()

    def _run_winetricks_in_prefix(self):
        selected = self._selected_proton()
        if selected is None:
            return
        game, exe_path, log = self._game, self._exe_path, self._log

        def launch():
            result = exe_launch.prepare_tool_prefix(exe_path, selected, game,
                                                    log_fn=log)
            if result is None:
                return
            _script, prefix_dir, _env = result
            exe_launch.launch_winetricks_in_prefix(prefix_dir / "pfx", log_fn=log)

        run_in_worker(launch, name="exe-prefix-winetricks")

    def _run_winecfg_in_prefix(self):
        selected = self._selected_proton()
        if selected is None:
            return
        game, exe_path, log = self._game, self._exe_path, self._log

        def launch():
            result = exe_launch.prepare_tool_prefix(exe_path, selected, game,
                                                    log_fn=log)
            if result is None:
                return
            proton_script, prefix_dir, env = result
            exe_launch.launch_wine_tool_in_prefix(
                proton_script, prefix_dir, env, "winecfg", log_fn=log)

        run_in_worker(launch, name="exe-prefix-winecfg")

    def _install_java_into_prefix(self):
        """Install a Windows JRE (with JavaFX) into the jar's target prefix.

        The target follows the Proton combo (game default → game prefix; a
        specific version → isolated prefix_<Proton>/ next to the jar), so we
        persist the current override first, then let resolve_jar_prefix_env
        pick the same prefix a Proton-mode launch would use.
        """
        game, jar_path, log = self._game, self._exe_path, self._log
        # Persist the chosen Proton override so the worker resolves the same
        # prefix the launch will use (Game default → game prefix).
        selected = self._proton_combo.currentText()
        exe_launch.save_proton_override(
            game, jar_path.name, "" if selected == "Game default" else selected)
        self._install_java_btn.setEnabled(False)
        self._install_java_btn.setText(self.tr("Installing Java …"))

        def worker():
            try:
                result = exe_launch.resolve_jar_prefix_env(jar_path, game, log_fn=log)
                if result is None:
                    log("Java: could not resolve a prefix - pick a Proton "
                        "version or deploy/launch the game once first.")
                    return
                _script, compat_data, _env = result
                from Utils.jre_prefix import install_windows_jre
                install_windows_jre(compat_data, log_fn=log)
            finally:
                safe_emit(self._install_java_done)

        threading.Thread(target=worker, daemon=True,
                         name="jar-install-java").start()

    def _open_prefix_folder(self):
        selected = self._selected_proton()
        if selected is None:
            return
        from Utils.steam_finder import find_any_installed_proton
        proton_script = find_any_installed_proton(selected)
        if proton_script is None:
            self._log(f"Prefix tools: could not find Proton '{selected}'.")
            return
        prefix_dir = self._exe_path.parent / f"prefix_{proton_script.parent.name}"
        if not prefix_dir.is_dir():
            self._log("Prefix tools: no prefix exists yet for this version - "
                      "run the exe once first.")
            return
        from Utils.xdg import xdg_open
        xdg_open(prefix_dir, log_fn=self._log)
