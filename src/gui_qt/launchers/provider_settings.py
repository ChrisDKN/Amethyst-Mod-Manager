from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QEvent, QThread, Qt, Signal
from PySide6.QtWidgets import (
    QBoxLayout, QCheckBox, QComboBox, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QMenu, QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy, QWidget,
)

from gui_qt.dialogs.base import OverlayBase
from gui_qt.dialogs.portal_picker import pick_file, pick_folder


class LauncherTask(QThread):
    completed = Signal(object, object)
    message = Signal(str)

    def __init__(self, operation, parent):
        super().__init__(parent)
        self.operation = operation

    def run(self):
        try:
            result, error = self.operation(self.message.emit), None
        except Exception as exc:
            result, error = None, str(exc) or type(exc).__name__
        self.completed.emit(result, error)


class LauncherEntryOverlay(OverlayBase):
    CARD_W = 620
    CARD_H = 780
    MIN_H = 360

    def __init__(self, host, entry, on_done=None, *, entries=(), game=None,
                 show_deploy_before_launch=True):
        super().__init__(host, on_done=on_done)
        self.entry = dict(entry)
        self.entries = entries
        self.game = game
        self.worker = None
        self.lsfg = dict(entry["lsfg"])
        self.mangohud = dict(entry["mangohud"])
        card, layout = self._make_card("LauncherEntryCard")
        title = QLabel(f"Configure: {entry['name']}")
        title.setWordWrap(True)
        title.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        layout.addWidget(title)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll = scroll
        body = QWidget()
        form = QFormLayout(body)
        form.setRowWrapPolicy(QFormLayout.WrapAllRows)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.name = QLineEdit(entry["name"])
        self.executable = QLineEdit(entry["executable"])
        self.game_path = QLineEdit(entry["game_path"])
        self.prefix = QLineEdit(entry["prefix_path"] or "")
        self.runner = QComboBox()
        self.runner.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.runner.setMinimumContentsLength(16)
        self.runner.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.runner.setMinimumWidth(0)
        from Utils.launchers.steam import list_installed_proton
        if entry["runner"]["kind"] == "proton":
            for path in list_installed_proton():
                self._select_runner(str(path), select=False)
        self._select_runner(entry["runner"].get("path", ""))
        self.runner.currentIndexChanged.connect(lambda: self.runner.setToolTip(self.runner.currentData() or ""))
        self.installed = QCheckBox("Include in game detection")
        self.installed.setChecked(entry["installed"])
        form.addRow("Name", self.name)
        form.addRow("Executable", self._browse_row(self.executable, False))
        form.addRow("Game directory", self._browse_row(self.game_path, True))
        form.addRow("Prefix directory", self._browse_row(self.prefix, True))
        self.move = QComboBox()
        self.move.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.move.setMinimumContentsLength(16)
        self.move.addItem("Use another existing/new prefix", "select")
        self.move.addItem("Move existing prefix to this location", "move")
        form.addRow("When changing prefix", self.move)
        self.shared = QComboBox()
        self.shared.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.shared.setMinimumContentsLength(16)
        self.shared.addItem("Dedicated / custom prefix", "")
        for other in entries:
            if other["id"] != entry["id"] and other["prefix_path"]:
                self.shared.addItem(other["name"], other["prefix_path"])
        self.shared.currentIndexChanged.connect(self._share)
        form.addRow("Use shared prefix", self.shared)
        runner_row = QHBoxLayout()
        runner_row.addWidget(self.runner, 1)
        custom = QPushButton("Browse…")
        custom.clicked.connect(lambda: pick_file(self, "Choose a custom Proton script", self._runner_picked,
                                                filters=[("All files", ["*"])]))
        runner_row.addWidget(custom)
        form.addRow("Proton version", runner_row)
        self.plain = QCheckBox("Launch with plain Wine")
        self.plain.setChecked(entry["plain_wine"])
        self.plain.setToolTip("Uses Wine from the selected Proton build. Initialize the prefix with Proton first. Launch-option wrappers are skipped.")
        self.arguments = QPlainTextEdit(entry["arguments"])
        self.arguments.setMaximumHeight(100)
        form.addRow("Launch arguments", self.arguments)
        self.options = QLineEdit(entry["launch_options"])
        self.options.setPlaceholderText("SteamDeck=0 gamemoderun %command%")
        form.addRow("Launch Options", self.options)
        self.wayland = QCheckBox("Launch with Wayland")
        self.wayland.setChecked(entry["wayland"])
        buttons = QHBoxLayout()
        self.options_buttons = buttons
        fg = QPushButton("MAKO / LSFG…")
        fg.clicked.connect(self._frame_generation)
        buttons.addWidget(fg)
        hud = QPushButton("MangoHud…")
        hud.clicked.connect(self._hud)
        buttons.addWidget(hud)
        tools = QPushButton("Proton tools ▾")
        tools.setMenu(self._tools_menu(tools))
        buttons.addWidget(tools)
        self.manager_controls = {}
        if game is not None:
            from Utils.executables import launch as shared
            if show_deploy_before_launch:
                deploy = QCheckBox("Deploy before launch")
                deploy.setChecked(shared.load_deploy_before_launch(game))
                self.manager_controls["deploy"] = deploy
            for toggle in getattr(game, "launch_toggles", ()):
                check = QCheckBox(toggle.label)
                check.setChecked(shared.load_launch_toggle(game, toggle.key, toggle.default))
                check.setToolTip(toggle.hint)
                self.manager_controls[toggle.key] = check
        for check in (self.installed, self.plain, self.wayland, *self.manager_controls.values()):
            form.addRow(check)
        form.addRow(buttons)
        self.controls = body
        for widget in body.findChildren(QWidget):
            widget.setMinimumWidth(0)
        for widget in body.findChildren(QComboBox):
            widget.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self._restore_controls()
        scroll.setWidget(body)
        layout.addWidget(scroll, 1)
        self.status = QLabel("" if entry.get("writable") else "This provider exposes read-only launch settings.")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        foot = QHBoxLayout()
        close = QPushButton("Cancel")
        close.clicked.connect(lambda: self._finish(None))
        foot.addWidget(close)
        foot.addStretch()
        self.save_button = QPushButton("Save")
        self.save_button.setObjectName("PrimaryButton")
        self.save_button.setEnabled(entry.get("writable", False) or game is not None)
        self.save_button.clicked.connect(self._save)
        foot.addWidget(self.save_button)
        layout.addLayout(foot)
        self._present()
        self._fit_options()

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Close and self.worker is not None:
            self.status.setText("Wait for the current prefix operation before closing Amethyst.")
            event.ignore()
            return True
        result = super().eventFilter(watched, event)
        if event.type() == QEvent.Resize and hasattr(self, "options_buttons"):
            self._fit_options()
        return result

    def _fit_options(self):
        self.options_buttons.setDirection(QBoxLayout.TopToBottom if self._card.width() < 520 else QBoxLayout.LeftToRight)

    def _restore_controls(self):
        self.controls.setEnabled(True)
        if not self.entry.get("writable", False):
            for widget in self.controls.findChildren(QWidget):
                widget.setEnabled(False)
            for widget in self.manager_controls.values():
                widget.setEnabled(True)

    def _browse_row(self, edit, directory):
        row = QHBoxLayout()
        row.addWidget(edit, 1)
        button = QPushButton("Browse…")
        def picked(path):
            if path is not None and self.worker is None:
                edit.setText(str(path))
                if edit is self.executable:
                    self.game_path.setText(str(path.parent))
        def browse():
            if directory:
                pick_folder(self, "Choose directory", picked)
            else:
                pick_file(self, "Choose executable", picked, filters=[
                    ("Windows applications", ["*.exe", "*.EXE", "*.msi", "*.MSI"]), ("All files", ["*"])])
        button.clicked.connect(browse)
        row.addWidget(button)
        return row

    def _share(self):
        path = self.shared.currentData()
        if path:
            self.prefix.setText(path)
            self.move.setCurrentIndex(0)
            other = next(e for e in self.entries if e["prefix_path"] == path)
            self._select_runner(other["runner"]["path"])

    def _select_runner(self, path, *, select=True):
        index = self.runner.findData(path)
        if index < 0:
            label = Path(path).parent.name if Path(path).name == "proton" else Path(path).name
            self.runner.addItem(label or "Native", path)
            index = self.runner.count() - 1
            self.runner.setItemData(index, path, Qt.ToolTipRole)
        if select:
            self.runner.setCurrentIndex(index)
            self.runner.setToolTip(path)

    def _runner_picked(self, path):
        if path is not None and self.worker is None:
            self._select_runner(str(path))

    def _frame_generation(self):
        from .lsfg import LsfgSettingsOverlay
        LsfgSettingsOverlay.show_over(self.window(), settings=self.lsfg, game_name="",
                                     on_done=lambda value: setattr(self, "lsfg", value) if value is not None else None)

    def _hud(self):
        from .mangohud import MangohudSettingsOverlay
        MangohudSettingsOverlay.show_over(self.window(), settings=self.mangohud,
                                         on_done=lambda value: setattr(self, "mangohud", value) if value is not None else None)

    def _tools_menu(self, parent):
        from Utils.wine import proton
        menu = QMenu(parent)
        for label, action in (("Run winecfg", lambda t, log: self._wine_gui(t, "winecfg", log)),
                              ("Run winetricks", self._winetricks),
                              ("Open wine registry", lambda t, log: self._wine_gui(t, "regedit", log)),
                              ("Install VC++ Redistributable", proton.install_vcredist),
                              ("Install d3dcompiler_47", proton.install_d3dcompiler_47),
                              ("Install XACT audio", proton.install_xact),
                              ("Install LAV Filters", proton.install_lavfilters)):
            menu.addAction(label, lambda checked=False, fn=action: self._tool(fn))
        for version in ("4.8", *proton.DOTNET_VERSIONS):
            menu.addAction(f"Install .NET {version}", lambda checked=False, v=version:
                self._tool(lambda target, log: proton.install_dotnet48(target, log_fn=log) if v == "4.8"
                           else proton.install_dotnet(target, v, log_fn=log)))
        menu.addAction("Run an EXE in this prefix…", self._run_exe)
        menu.addAction("Open prefix folder", self._open_prefix)
        menu.addAction("Wine DLL overrides…", self._dll_overrides)
        menu.addAction("Install Windows component…", self._windows_component)
        return menu

    def _open_prefix(self):
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        QDesktopServices.openUrl(QUrl.fromLocalFile(self.entry["prefix_path"]))

    def _run_exe(self):
        def picked(path):
            if path is not None:
                self._tool(lambda target, log: self._run_target_exe(target, str(path), log))
        pick_file(self, "Run in this prefix", picked, filters=[
            ("Windows applications", ["*.exe", "*.EXE", "*.msi", "*.MSI"])])

    def _run_target_exe(self, target, path, log):
        from Utils.launchers.steam import proton_run_command
        from Utils.processes.watch import run_process_logged
        payload = ["msiexec", "/i", path] if Path(path).suffix.casefold() == ".msi" else [path]
        command = proton_run_command(target.proton_script, "runinprefix", *payload, env=target.env, host_cwd=Path(path).parent)
        return run_process_logged(command, env=target.env, cwd=Path(path).parent, label=Path(path).name, log_fn=log)

    def _dll_overrides(self):
        self._tool(lambda target, log: self._wine_gui(target, "winecfg", log))

    def _windows_component(self):
        self._tool(self._winetricks)

    def _wine_gui(self, target, tool, log):
        from Utils.wine.proton import wine_tool_command
        from Utils.processes.watch import run_process_logged
        env = dict(target.env)
        command = wine_tool_command(target, target.proton_script, env, tool, log)
        return run_process_logged(command, env=env, label=tool, log_fn=log)

    def _winetricks(self, target, log):
        from Utils.wine.winetricks import build_winetricks_command
        from Utils.processes.watch import run_process_logged
        command, env = build_winetricks_command(target.get_prefix_path(), "--gui", game=target, log_fn=log)
        if command is None:
            raise ValueError("Winetricks is unavailable.")
        return run_process_logged(command, env=env, label="Winetricks", log_fn=log)

    def _tool(self, action):
        from amethyst_plugins.launcher_runtime import tools_target
        from amethyst_plugins.launchers import assert_idle, prefix_lock
        def operation(log):
            with prefix_lock(self.entry["prefix_path"]):
                assert_idle(self.entry)
                result = action(tools_target(self.entry, log), log)
                return result if isinstance(result, bool) else result == 0
        self._work(operation, lambda result: self._message(
            "Prefix action finished." if result else "Prefix action failed; check the log."))

    def _message(self, message):
        from Utils.app_log import app_log
        self.status.setText(message)
        app_log(f"Launcher settings ({self.entry['name']}): {message}")

    def _work(self, operation, done):
        if self.worker is not None:
            return
        self.controls.setEnabled(False)
        self.save_button.setEnabled(False)
        task = self.worker = LauncherTask(operation, self)
        task.message.connect(self._message)
        def finished():
            result, error = task.outcome
            self.worker = None
            self._restore_controls()
            self.save_button.setEnabled(self.entry.get("writable", False) or self.game is not None)
            task.deleteLater()
            if error:
                self._message(error)
            else:
                done(result)
        task.completed.connect(lambda result, error: setattr(task, "outcome", (result, error)))
        task.finished.connect(finished)
        task.start()

    def _save(self):
        from amethyst_plugins.launchers import update_entry
        runner_path = self.runner.currentData() or ""
        runner_path = str(Path(runner_path) / "proton") if Path(runner_path).is_dir() else runner_path
        changes = {"name": self.name.text().strip(), "executable": self.executable.text().strip(),
                   "game_path": self.game_path.text().strip(), "prefix_path": self.prefix.text().strip() or None,
                   "runner": ({"kind": self.entry["runner"]["kind"], "path": runner_path}
                              if self.entry["runner"]["kind"] != "native" else self.entry["runner"]),
                   "installed": self.installed.isChecked(), "arguments": self.arguments.toPlainText(),
                   "launch_options": self.options.text(), "plain_wine": self.plain.isChecked(),
                   "wayland": self.wayland.isChecked(), "lsfg": self.lsfg, "mangohud": self.mangohud}
        if changes["prefix_path"] != self.entry["prefix_path"]:
            changes["prefix_action"] = self.move.currentData()
        changes = {key: value for key, value in changes.items() if value != self.entry.get(key)}
        if not changes and self.game is None:
            self._finish(self.entry)
            return
        manager_values = {key: widget.isChecked() for key, widget in self.manager_controls.items()}
        def save(log):
            result = update_entry(self.entry["provider_id"], self.entry["id"], changes,
                                  self.entry["revision"], log=log) if changes else self.entry
            if self.game is not None and manager_values:
                from Utils.executables import launch as shared
                from Utils.deployment.locking import coordinated_game_mutation
                with coordinated_game_mutation(self.game, "launcher_settings"):
                    if "deploy" in manager_values:
                        shared.save_deploy_before_launch(self.game, manager_values.pop("deploy"))
                    for key, enabled in manager_values.items():
                        shared.save_launch_toggle(self.game, key, enabled)
            return result
        self._work(save, self._finish)

    def _finish(self, result=None):
        if self.worker is not None:
            self.status.setText("Wait for the current prefix operation to finish before closing.")
            return
        super()._finish(result)


def open_game_launcher(host, game, log=lambda message: None):
    from amethyst_plugins.launchers import selected_entry
    task = LauncherTask(lambda output: selected_entry(game, log=output), host)
    task.message.connect(log)
    def completed(result, error):
        task.outcome = (result, error)
    def finished():
        entry, error = task.outcome
        if error:
            log(error)
        elif entry is not None:
            LauncherEntryOverlay(host.window(), entry, game=game)
        task.deleteLater()
    task.completed.connect(completed)
    task.finished.connect(finished)
    def wait_for_task():
        try:
            task.wait(21000)
        except RuntimeError:
            pass
    host.destroyed.connect(wait_for_task)
    task.start()
