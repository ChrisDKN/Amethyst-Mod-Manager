from __future__ import annotations

import json
from pathlib import Path
import subprocess
import threading
import time

from PySide6.QtCore import QSize, QTimer, Qt
from PySide6.QtGui import QIcon, QImage, QPainter, QPixmap
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QPlainTextEdit, QPushButton, QStyle, QVBoxLayout, QWidget,
)

from gui_qt.launchers.provider_settings import LauncherTask
from gui_qt.dialogs.portal_picker import pick_file, pick_folder
from amethyst_plugins.launchers import get_entry, normalize_entry
from gui_qt.dialogs.base import OverlayBase
from . import provider, steam
from .editor import LauncherEntryOverlay
from .i18n import tr


class Backend:
    def __init__(self, command):
        self.command = command
        self.process = None
        self.sequence = 0

    def call(self, method, params=None):
        if self.process is None:
            self.process = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=subprocess.DEVNULL, text=True, bufsize=1)
        self.sequence += 1
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.sequence,
                                            "method": method, "params": params or {}}) + "\n")
        self.process.stdin.flush()
        line = self.process.stdout.readline()
        if not line:
            raise ValueError(tr('The launch backend stopped unexpectedly.'))
        response = json.loads(line)
        if response.get("id") != self.sequence:
            raise ValueError(tr('Unexpected launch backend response.'))
        if "error" in response:
            raise ValueError(response["error"]["message"])
        return response["result"]

    def job(self, method, params, log, stopping=None):
        job = self.call(method, params)["job"]
        last = 0
        while job["state"] not in {"succeeded", "failed", "cancelled"}:
            time.sleep(0.15)
            if stopping is not None and stopping.is_set():
                launch = self.call("launch.get").get("launch")
                if launch and launch["state"] == "running":
                    self.call("launch.stop", {"launch_id": launch["id"], "confirm": True})
                    stopping.clear()
                else:
                    self.call("jobs.cancel", {"job_id": job["id"]})
            job = self.call("jobs.get", {"job_id": job["id"]})["job"]
            for event in job.get("logs", [])[last:]:
                log(str(event))
            last = len(job.get("logs", []))
        if job["state"] != "succeeded":
            raise ValueError((job.get("error") or {}).get("message", tr('Operation cancelled.')))
        return job.get("result") or {}

    def close(self):
        if self.process is not None:
            self.process.stdin.close()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.terminate()
            self.process = None


class SteamShortcutOverlay(OverlayBase):
    CARD_W = 560
    CARD_H = 300

    def __init__(self, host, entry, accounts, on_done):
        super().__init__(host, on_done=on_done)
        card, layout = self._make_card("SteamShortcutCard")
        title = QLabel(tr('Add Steam shortcut'))
        title.setStyleSheet("font-size:16px; font-weight:600;")
        layout.addWidget(title)
        name = QLabel(entry["name"])
        name.setTextFormat(Qt.PlainText)
        name.setWordWrap(True)
        layout.addWidget(name)
        layout.addWidget(QLabel(tr('Steam account')))
        self.account = QComboBox()
        for account in accounts:
            self.account.addItem(account.label, account)
            self.account.setItemData(self.account.count() - 1, str(account.root), Qt.ToolTipRole)
        layout.addWidget(self.account)
        hint = QLabel(tr("Exit Steam completely before adding. The shortcut uses this application's saved launch settings."))
        hint.setWordWrap(True)
        layout.addWidget(hint)
        layout.addStretch()
        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel = QPushButton(tr('Cancel'))
        cancel.clicked.connect(lambda: self._finish())
        buttons.addWidget(cancel)
        add = QPushButton(tr('Add shortcut'))
        add.setObjectName("PrimaryButton")
        add.clicked.connect(lambda: self._finish(self.account.currentData()))
        buttons.addWidget(add)
        layout.addLayout(buttons)
        self._present()
        self.account.setFocus()


class ApplicationLauncher(QWidget):
    preferred_size = QSize(920, 760)

    def __init__(self, context, parent, on_close):
        super().__init__(parent)
        if context.get("language"):
            tr.language = context["language"]
        self.context = {**context, "provider_id": "amethyst.application_launcher/applications",
                        "log": lambda message: None}
        self.on_close = on_close
        self.worker = None
        self.editor = None
        self.changed = False
        self.entries = []
        self.icon_cache = {}
        self.active = None
        self.installing = None
        self.stopping = threading.Event()
        self.backend = Backend(context["headless_command"])
        layout = QVBoxLayout(self)
        hint = QLabel(tr("Add a Windows application or run an installer in its own prefix. Supported games appear in Amethyst's Add Game list."))
        hint.setWordWrap(True)
        layout.addWidget(hint)
        row = QHBoxLayout()
        for label, callback in ((tr('Add EXE…'), self._add), (tr('Install Application…'), lambda: self._add(True)),
                                (tr('Edit…'), self._edit), (tr('Remove…'), self._remove), (tr('Refresh'), self._refresh)):
            button = QPushButton(label)
            button.clicked.connect(lambda checked=False, fn=callback: fn())
            row.addWidget(button)
        layout.addLayout(row)
        self.library = QListWidget()
        self.library.setIconSize(QSize(64, 64))
        self.library.setUniformItemSizes(True)
        self.library.itemDoubleClicked.connect(lambda item: self._edit())
        self.library.currentItemChanged.connect(lambda: self._steam_enabled())
        layout.addWidget(self.library, 1)
        prefix_row = QHBoxLayout()
        prefix_row.addWidget(QLabel(tr('Default prefixes location')))
        self.default_prefix = QLineEdit(str(Path(context["plugin_dir"]) / "prefixes"))
        prefix_row.addWidget(self.default_prefix, 1)
        browse = QPushButton(tr('Browse…'))
        browse.clicked.connect(self._default_location)
        prefix_row.addWidget(browse)
        save_default = QPushButton(tr('Save default'))
        save_default.clicked.connect(self._save_default)
        prefix_row.addWidget(save_default)
        layout.addLayout(prefix_row)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.logs = QPlainTextEdit()
        self.logs.setReadOnly(True)
        self.logs.setMaximumBlockCount(300)
        self.logs.setMaximumHeight(150)
        layout.addWidget(self.logs)
        footer = QHBoxLayout()
        self.close_button = QPushButton(tr('Close'))
        self.close_button.clicked.connect(self.request_close)
        footer.addWidget(self.close_button)
        self.steam_button = QPushButton(tr('Add Steam shortcut…'))
        self.steam_button.setEnabled(False)
        self.steam_button.clicked.connect(self._add_steam)
        footer.addWidget(self.steam_button)
        footer.addStretch()
        self.stop_button = QPushButton(tr('Stop'))
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self._stop)
        footer.addWidget(self.stop_button)
        self.launch_button = QPushButton(tr('Launch'))
        self.launch_button.setObjectName("PrimaryButton")
        self.launch_button.clicked.connect(self._launch)
        footer.addWidget(self.launch_button)
        layout.addLayout(footer)
        self.controls = row
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self._poll)
        QTimer.singleShot(0, self._refresh)

    def _work(self, operation, done):
        if self.worker is not None:
            return
        task = self.worker = LauncherTask(operation, self)
        self._steam_enabled()
        task.message.connect(self.logs.appendPlainText)
        task.completed.connect(lambda result, error: setattr(task, "outcome", (result, error)))
        def finished():
            self.worker = None
            self._steam_enabled()
            result, error = task.outcome
            task.deleteLater()
            if error:
                self.status.setText(error)
                if self.active is None:
                    self.stop_button.setEnabled(False)
                    self.launch_button.setEnabled(True)
            else:
                done(result)
        task.finished.connect(finished)
        task.start()

    def _refresh(self):
        def operation(log):
            provider.recover(self.context)
            with provider.library(self.context) as data:
                root = data.get("prefix_root", str(Path(self.context["plugin_dir"]) / "prefixes"))
            entries = [normalize_entry(row, self.context["provider_id"], "Application Launcher", True)
                       for row in provider.list_entries(self.context)]
            from Utils.executables.icon import extract_exe_icon
            icons = {}
            for entry in entries:
                path = Path(entry["executable"])
                try:
                    stat = path.stat()
                    key = (str(path), stat.st_size, stat.st_mtime_ns)
                    if key not in self.icon_cache:
                        self.icon_cache[key] = extract_exe_icon(path)
                    icons[entry["id"]] = self.icon_cache[key]
                except OSError:
                    pass
            return entries, root, icons
        def done(result):
            self.entries, root, icons = result
            selected = self._selected()
            self.library.clear()
            self.default_prefix.setText(root)
            for entry in self.entries:
                suffix = "" if entry["installed"] else tr(' · Installer (choose installed EXE after setup)')
                item = QListWidgetItem(entry["name"] + suffix + "\n" + entry["executable"])
                image = QImage.fromData(icons.get(entry["id"]) or b"", "ICO")
                size = self.library.iconSize()
                ratio = self.library.devicePixelRatioF()
                pixels = size * ratio
                pixmap = (QPixmap.fromImage(image) if not image.isNull()
                          else self.style().standardIcon(QStyle.SP_FileIcon).pixmap(pixels))
                pixmap = pixmap.scaled(pixels, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                pixmap.setDevicePixelRatio(ratio)
                canvas = QPixmap(pixels)
                canvas.setDevicePixelRatio(ratio)
                canvas.fill(Qt.transparent)
                painter = QPainter(canvas)
                painter.drawPixmap(round((size.width() - pixmap.width() / ratio) / 2),
                                   round((size.height() - pixmap.height() / ratio) / 2), pixmap)
                painter.end()
                item.setIcon(QIcon(canvas))
                item.setSizeHint(QSize(0, max(size.height(), self.library.fontMetrics().lineSpacing() * 2) + 12))
                item.setData(Qt.UserRole, entry["id"])
                self.library.addItem(item)
                if selected and entry["id"] == selected["id"]:
                    self.library.setCurrentItem(item)
        self._work(operation, done)

    def _selected(self):
        item = self.library.currentItem()
        if item is None:
            return None
        return next((e for e in self.entries if e["id"] == item.data(Qt.UserRole)), None)

    def _steam_enabled(self):
        entry = self._selected()
        self.steam_button.setEnabled(bool(entry and entry["installed"] and not self.worker
                                          and not self.active and self.editor is None))

    def _add_steam(self):
        entry = self._selected()
        if not entry or not entry["installed"] or self.worker or self.active or self.editor is not None:
            return
        def show(accounts):
            if not accounts:
                self.status.setText(tr('No local Steam accounts found. Sign into Steam once, then exit it and try again.'))
                return
            def selected(account):
                self.editor = None
                self._steam_enabled()
                if account is None:
                    return
                def operation(log):
                    current = get_entry(self.context["provider_id"], entry["id"], log=log)
                    return steam.add_shortcut(self.context, current, account)
                def done(result):
                    message = (tr("Updated Steam shortcut for {account}. Open Steam to use it.", account=result["account"])
                               if result["updated"] else tr("Added Steam shortcut for {account}. Open Steam to use it.", account=result["account"]))
                    self.status.setText(message)
                self._work(operation, done)
            self.editor = SteamShortcutOverlay(self.window(), entry, accounts, selected)
            self._steam_enabled()
        self._work(lambda log: steam.accounts(), show)

    def _add(self, installer=False):
        if self.worker or self.active:
            return
        pick_file(self, (tr('Choose installer') if installer else tr('Add application')),
                  lambda path: self._add_picked(path, installer), filters=[
                      (tr('Windows applications'), ["*.exe", "*.EXE", "*.msi", "*.MSI"])])

    def _add_picked(self, path, installer):
        if self.worker or self.active:
            return
        if not path:
            return
        def done(row):
            self.changed = True
            entry = normalize_entry(row, self.context["provider_id"], "Application Launcher", True)
            self._editor(entry, launch_installer=installer)
        self._work(lambda log: provider.add_entry(self.context, path, installer=installer), done)

    def _edit(self):
        entry = self._selected()
        if entry and not self.worker and not self.active:
            self._work(lambda log: get_entry(self.context["provider_id"], entry["id"], log=log), self._editor)

    def _editor(self, entry, launch_installer=False):
        from amethyst_plugins.launchers import linked_profiles
        def find_game(log):
            links = linked_profiles(self.context["provider_id"], [entry["id"]])
            return next((game for game, profile in links
                         if game.game_id == self.context.get("game_id") and profile.name == self.context.get("profile")),
                        links[0][0] if links else None)
        self._work(find_game, lambda game: self._show_editor(entry, game, launch_installer))

    def _show_editor(self, entry, game, launch_installer):
        def done(updated):
            self.editor = None
            if updated is not None:
                self.changed = True
            if launch_installer and updated is not None:
                self._launch(updated)
            else:
                self._refresh()
        self.editor = LauncherEntryOverlay(self.window(), entry, done, entries=self.entries, game=game,
                                           show_deploy_before_launch=False)

    def _remove(self):
        entry = self._selected()
        if not entry or self.worker or self.active:
            return
        from gui_qt.dialogs.confirm import ConfirmOverlay
        def remove(delete_prefix=False):
            self._work(lambda log: provider.remove_entry(self.context, entry["id"], delete_prefix=delete_prefix,
                                                         revision=entry["revision"]),
                       lambda result: (setattr(self, "changed", True), self._refresh()))
        def remove_prefix():
            ConfirmOverlay.show_over(self.window(), tr('Remove application and prefix?'),
                tr('Permanently delete this prefix, including installed applications, settings and saves?\n\n{prefix_path}', prefix_path=entry['prefix_path']),
                lambda approved: remove(True) if approved else None, confirm_label=tr('Delete prefix'), card_h=310)
        def done(approved):
            if approved:
                remove()
        ConfirmOverlay.show_over(self.window(), tr('Remove application?'),
                                 tr('Remove {name} from the launcher? Its prefix will be kept unless you choose to delete it too.', name=entry['name']), done,
                                 confirm_label=tr('Keep prefix'), secondary_action=(tr('Remove prefix too…'), remove_prefix), card_h=280)

    def _launch(self, entry=None):
        entry = entry if isinstance(entry, dict) else self._selected()
        if not entry or self.worker or self.active:
            return
        self.stopping.clear()
        self.status.setText(tr('Preparing application…'))
        self.stop_button.setEnabled(True)
        def done(result):
            self.active = result["launch"]
            self._steam_enabled()
            self.installing = entry["id"] if not entry["installed"] else None
            self.status.setText(tr('Running: {name}', name=entry['name']))
            self.launch_button.setEnabled(False)
            self.timer.start()
        self._work(lambda log: self.backend.job("launchers.entry.run.start",
            {"provider_id": self.context["provider_id"], "launcher_id": entry["id"]}, log, self.stopping), done)

    def _poll(self):
        if self.worker or not self.active:
            return
        def done(result):
            self.active = result["launch"]
            if self.active["active"]:
                self.stop_button.setEnabled(self.active["stop_supported"])
                return
            state = self.active["state"]
            self.active = None
            self._steam_enabled()
            self.timer.stop()
            self.stop_button.setEnabled(False)
            self.launch_button.setEnabled(True)
            messages = {"finished": tr("Application finished."), "stopped": tr("Application stopped."),
                        "failed": tr("Application failed.")}
            self.status.setText(messages.get(state, tr("Application {state}.", state=state)))
            if self.installing and state == "finished":
                entry_id, self.installing = self.installing, None
                self._select_installed(entry_id)
        self._work(lambda log: self.backend.call("launch.get", {"launch_id": self.active["id"]}), done)

    def _select_installed(self, entry_id):
        pick_file(self, tr('Choose the installed application EXE'),
                  lambda path: self._installed_picked(entry_id, path),
                  filters=[(tr('Windows applications'), ["*.exe", "*.EXE"])])

    def _installed_picked(self, entry_id, path):
        if not path:
            self.status.setText(tr('Installer entry kept. Use Edit to choose its installed EXE and enable game detection.'))
            self._refresh()
            return
        from amethyst_plugins.launchers import update_entry
        def operation(log):
            entry = get_entry(self.context["provider_id"], entry_id, log=log)
            return update_entry(self.context["provider_id"], entry_id,
                {"executable": str(path), "game_path": str(provider.infer_game_root(Path(path))), "installed": True}, entry["revision"], log=log)
        self._work(operation, lambda result: (setattr(self, "changed", True), self._refresh()))

    def _stop(self):
        if self.worker:
            self.stopping.set()
        elif self.active:
            self._work(lambda log: self.backend.call("launch.stop", {"launch_id": self.active["id"], "confirm": True}),
                       lambda result: self.status.setText(tr('Stopping application…')))

    def _default_location(self):
        pick_folder(self, tr('Default prefixes directory'),
                    lambda path: self.default_prefix.setText(str(path)) if path is not None else None)

    def _save_default(self):
        path = Path(self.default_prefix.text()).expanduser()
        if not path.is_absolute() or self.worker or self.active:
            self.status.setText(tr('Choose an absolute prefix directory while the launcher is idle.'))
            return
        def operation(log):
            with provider.library(self.context, write=True) as data:
                data["prefix_root"] = str(path)
                provider.save(self.context, data)
        self._work(operation, lambda result: self.status.setText(tr('Default location saved for new applications.')))

    def request_close(self):
        if self.worker or self.active or self.editor is not None:
            self.status.setText(tr('Finish the current operation and stop running applications before closing.'))
            return
        self.timer.stop()
        self._work(lambda log: self.backend.close(), lambda result: self.on_close(self.changed))


def create_view(context, parent, on_close):
    return ApplicationLauncher(context, parent, on_close)
