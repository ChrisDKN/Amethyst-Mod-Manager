import copy
from pathlib import Path

from PySide6.QtCore import QSize, QTimer, Qt
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QProgressBar, QPushButton, QVBoxLayout, QWidget,
)

from gui_qt.dialogs.confirm import ConfirmOverlay
from Utils.games.registry import _GAMES
from Utils.mods.copy import resolve_target_staging

from .backend import JsonProcess
from .download import forget_account, saved_account
from .helper import helper_path
from .handoff import take_operation, save_result
from Utils.ui.config import load_download_only
from .session import WorkshopDownload, WorkshopLoginView
from .support import workshop_app_id


class WorkshopPanel(QWidget):
    def __init__(self, context, parent, on_close, *, operation=None):
        super().__init__(parent)
        self.context = dict(context)
        self.on_close = on_close
        self.operation = operation
        self.result_written = False
        self.available = False
        self.changed = self.closing = self.cancelling = self.busy = False
        self.login = self.download = None
        self.job = None
        self.conflict_dialog = None
        self.queue = []
        self.backend_ready = False
        self.backend_error = None
        self.backend = JsonProcess("Amethyst backend", self)
        self.backend.failed.connect(self._backend_failed)
        self.backend.stopped.connect(self._backend_stopped)
        self.backend.process.started.connect(self._handshake)
        self.poll = QTimer(self)
        self.poll.setSingleShot(True)
        self.poll.setInterval(250)
        self.poll.timeout.connect(self._poll_job)
        self.browser = None
        self.game = self.profile = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.status = QLabel()
        self.status.setTextFormat(Qt.PlainText)
        self.status.setWordWrap(True)
        self.progress = QProgressBar()
        self.progress.hide()
        self.footer = QWidget()
        footer = QHBoxLayout(self.footer)
        footer.setContentsMargins(0, 0, 0, 0)
        footer.addWidget(self.status, 1)
        self.cancel = QPushButton("Cancel operation")
        self.cancel.clicked.connect(self._cancel)
        self.cancel.setEnabled(False)
        footer.addWidget(self.cancel)
        close = QPushButton("Close")
        close.clicked.connect(self.request_close)
        footer.addWidget(close)
        try:
            self.game = copy.copy(next(g for g in _GAMES.values() if g.game_id == context['game_id']))
            name = context['profile']
            if not name or Path(name).name != name or name in {'.', '..'}:
                raise ValueError("Select a profile before opening Workshop.")
            self.profile = Path(self.game.get_profile_root()) / 'profiles' / name
            if not self.profile.is_dir() or self.profile.is_symlink():
                raise ValueError("Select an available profile before opening Workshop.")
            self.game.set_active_profile_dir(self.profile)
            self.game.load_paths(read_only=True)
            if not workshop_app_id(self.game):
                raise ValueError("The selected game does not have a Steam App ID.")
            helper_path()
            self.available = True
            from PySide6.QtWidgets import QListWidget
            self.items = QListWidget()
            for item in (operation or {}).get('items', []):
                self.items.addItem(item['title'])
            for name in (operation or {}).get('requirements', {}).values():
                self.items.addItem(f"Enable installed requirement: {name}")
            layout.addWidget(self.items, 1)
            self.status.setText("Browse Workshop in its tab. Downloads and installs run here.")
        except Exception as exc:
            self.status.setText(str(exc))
        layout.addWidget(self.progress)
        layout.addWidget(self.footer)
        if self.available and operation is not None:
            QTimer.singleShot(0, self, self._dispatch_operation)

    def _dispatch_operation(self):
        if self.closing:
            return
        operation = self.operation
        if operation.get('action') == 'invalid':
            self._log(operation['message'])
        elif operation['action'] == 'login':
            self._login()
        elif operation['action'] == 'forget':
            self._forget()
        else:
            from .download import WorkshopItem
            items = [WorkshopItem(**item) for item in operation['items']]
            if any(item.app_id != workshop_app_id(self.game) for item in items):
                self._log("The selected Workshop items belong to another game. Select them again in the browser.")
                return
            self._request_download(items, (self.game, self.profile), enable_requirements=operation['requirements'])

    def _write_result(self, message):
        if self.operation is not None and self.operation.get('id'):
            try:
                save_result(self.operation, message)
                self.result_written = True
            except OSError as exc:
                self._log(f"Could not update the browser: {exc}")

    def _log(self, message):
        if not self.closing:
            self.status.setText(str(message))

    def _login(self, continuation=None):
        if self.closing or self.busy or self.login is not None or not self.available:
            return
        def finished(result):
            view = self.login
            self.login = None
            self.layout().removeWidget(view)
            view.deleteLater()
            self.items.show()
            self.footer.show()
            if self.closing:
                self._maybe_close()
            elif result == "connected" and continuation:
                continuation()
            elif result == "connected":
                self._write_result("Signed in to Steam.")
        self.items.hide()
        self.progress.hide()
        self.footer.hide()
        self.login = WorkshopLoginView(self, workshop_app_id(self.game), on_done=finished)
        self.layout().insertWidget(0, self.login, 1)

    def _request_download(self, items, context, *, enable_requirements=None):
        if self.busy or self.closing or context != (self.game, self.profile):
            return
        from Utils.profiles.groups import is_group
        if not load_download_only() and is_group(self.profile):
            self._log("Select a member profile to install, or choose Download only.")
            return
        if not saved_account() and items:
            self._login(lambda: self._request_download(items, context, enable_requirements=enable_requirements))
            return
        download_only = bool(load_download_only())
        action = "Download" if download_only else "Download and install"
        note = "Files will be kept in Amethyst's Downloads cache."
        if not download_only:
            note += " Installers that require choices will be left in Downloads for manual installation."
        def confirmed(yes):
            if yes and not self.busy and not self.closing:
                self.download_mode = download_only
                self.requirements = dict(enable_requirements or {})
                self.busy = True
                self.cancelling = False
                self._controls()
                if items:
                    self._start_download(items)
                else:
                    self._begin_install([])
        ConfirmOverlay.show_over(self, f"{action} Workshop mods?", note, confirmed,
                                 confirm_label=action, list_items=[item.title for item in items] +
                                 [f"Enable installed requirement: {name}" for name in (enable_requirements or {}).values()])

    def _controls(self):
        if self.browser is not None:
            self.browser.setEnabled(not self.busy and not self.closing)
        self.cancel.setEnabled(self.busy and not self.cancelling)
        self.progress.setVisible(self.busy)
        if self.busy:
            self.progress.setRange(0, 0)

    def _start_download(self, items):
        self.changed = True
        self.download = WorkshopDownload(self, items, saved_account(), game_name=self.game.name)
        self.download.event.connect(self._download_event)
        self.download.start()

    def _download_event(self, kind, value):
        if self.download is None:
            return
        if kind in {"downloaded", "failed", "cancelled", "login_required"}:
            if self.download.worker.is_alive():
                QTimer.singleShot(25, self, lambda: self._download_event(kind, value))
                return
            from PySide6.QtCore import QCoreApplication
            QCoreApplication.instance().aboutToQuit.disconnect(self.download.process.abort)
            self.download.deleteLater()
            self.download = None
            if self.cancelling or self.closing or kind == "cancelled":
                self._finish("Workshop operation cancelled. Downloaded archives have been kept.")
            elif kind == "login_required":
                self._finish("Your Steam session needs a new sign-in.")
                self._login()
            elif kind == "failed":
                self._finish(str(value))
            elif self.download_mode:
                self.changed = True
                self._finish("Workshop downloads are ready in Downloads.")
            else:
                self._begin_install(value)
        elif kind == "item":
            self._log(f"Downloading: {value.title}")
        elif kind == "status":
            self._log(str(value))
        elif kind == "progress":
            done, total = value
            self.progress.setRange(0, 100 if total else 0)
            if total:
                self.progress.setValue(min(100, int(100 * done / total)))

    def _begin_install(self, downloads):
        self.downloads = list(downloads)
        if not self.backend.running:
            command = self.context.get("headless_command")
            if not isinstance(command, list) or not command:
                self._finish("This manager did not provide a headless backend command. Archives remain in Downloads.")
                return
            self.backend.start(command[0], command[1:])
            self._log("Connecting to Amethyst's installer…")
            QTimer.singleShot(30000, self, self._startup_timeout)
        elif self.backend_ready:
            self._inventory()

    def _startup_timeout(self):
        if self.busy and not self.backend_ready and self.backend.running:
            self._backend_failed("The installer did not start in time. Archives remain in Downloads.")
            self.backend.close(terminate=True)

    def _handshake(self):
        self._call("system.handshake", {"protocol_version": 1}, self._handshaken)

    def _handshaken(self, result):
        required = {"downloads.list", "downloads.install.start", "mods.set_enabled.start", "jobs.get", "jobs.cancel"}
        if result.get("protocol", {}).get("name") != "amethyst.headless" or not required.issubset(result.get("capabilities", {}).get("methods", [])):
            self._backend_failed("This manager's backend does not provide the required installer API.")
            return
        self.backend_ready = True
        self._inventory()

    def _target(self):
        return {"game": self.context['game_id'], "profile": self.context['profile']}

    def _call(self, method, params, callback):
        def received(result, error):
            if error:
                message = f"{error.get('message', 'Backend request failed.')}\nDownloaded archives have been kept."
                if self.job is not None or method == "system.handshake":
                    if self.job is not None:
                        self.backend.request("jobs.cancel", {"job_id": self.job}, lambda *_: None)
                    self._backend_failed(message)
                else:
                    self._finish(message)
            else:
                try:
                    callback(result)
                except Exception as exc:
                    self._finish(f"{exc}\nDownloaded archives have been kept.")
        self.backend.request(method, params, received)

    def _inventory(self):
        if self.cancelling or self.closing:
            self._finish("Workshop operation cancelled.")
            return
        self._call("downloads.list", self._target(), self._located)

    def _located(self, result):
        by_path = {str(Path(item['path']).resolve()): item for item in result['archives']}
        self.queue = []
        for item, path in self.downloads:
            archive = by_path.get(str(Path(path).resolve()))
            if archive is None:
                raise RuntimeError(f"Downloaded archive is not visible in Downloads: {path}")
            self.queue.append((item, archive['id']))
        self._next_install()

    def _next_install(self):
        if self.cancelling or self.closing:
            self._finish("Workshop operation cancelled. Completed installs and archives have been kept.")
        elif self.queue:
            item, archive_id = self.queue.pop(0)
            self._log(f"Installing: {item.title}")
            self.current_install = {**self._target(), "archive_id": archive_id}
            self._run_job("downloads.install.start", self.current_install, self._next_install)
        elif self.requirements:
            from .client import installed_items
            installed = installed_items(resolve_target_staging(self.game, self.profile), workshop_app_id(self.game))
            changes = []
            for item_id, name in self.requirements.items():
                names = installed.get(item_id, [])
                if not names:
                    raise RuntimeError(f"Installed Workshop requirement is no longer available: {name}")
                changes.append({"name": name if name in names else min(names), "enabled": True})
            self.requirements = {}
            self._run_job("mods.set_enabled.start", {**self._target(), "changes": changes}, self._next_install)
        else:
            self._finish("Workshop installation complete. Close this panel to return to the browser.")

    def _run_job(self, method, params, done):
        self.changed = True
        self.job_done = done
        self._call(method, params, self._job_started)

    def _job_started(self, result):
        self.job = result['job']['id']
        if self.cancelling or self.closing:
            self.backend.request("jobs.cancel", {"job_id": self.job}, lambda *_: None)
        self._job_received(result)

    def _poll_job(self):
        self._call("jobs.get", {"job_id": self.job}, self._job_received)

    def _job_received(self, result):
        job = result['job']
        if job['state'] not in {"succeeded", "failed", "cancelled"}:
            progress = job.get('progress') or {}
            if progress.get('message') or progress.get('phase'):
                self._log(progress.get('message') or progress['phase'])
            self.poll.start()
            return
        self.job = None
        if job['state'] == 'succeeded':
            self.job_done()
        else:
            error = job.get('error') or {}
            if (error.get('name') == 'mod_already_exists' and job['kind'] == 'downloads.install'
                    and not self.closing and not self.cancelling):
                self._existing_mod(error.get('details') or {})
                return
            self._finish(f"{error.get('message') or 'Installation cancelled.'}\nArchives remain in Downloads. Completed installs have been kept.")

    def _existing_mod(self, details):
        def chosen(choice):
            self.conflict_dialog = None
            if self.closing or self.cancelling:
                self._finish('Installation cancelled. Archives remain in Downloads.')
            elif choice == 'replace':
                self.current_install['if_exists'] = 'replace'
                self._run_job('downloads.install.start', self.current_install, self._next_install)
            elif isinstance(choice, str) and choice.startswith('rename:'):
                self.current_install.update(name=choice[7:], if_exists='fail')
                self._run_job('downloads.install.start', self.current_install, self._next_install)
            else:
                self._finish('Installation cancelled. Archives remain in Downloads.')
        if details.get('mod'):
            from gui_qt.installers.mod_exists import ModExistsOverlay
            self.conflict_dialog = ModExistsOverlay.show_over(self, details['mod'], False, chosen)
        else:
            self.conflict_dialog = ConfirmOverlay.show_over(self, 'Mods already exist',
                'Replace the existing mods from this archive?',
                lambda yes: chosen('replace' if yes else 'cancel'),
                confirm_label='Replace All', list_items=details.get('mods', []))

    def _cancel(self):
        self.cancelling = True
        self.cancel.setEnabled(False)
        self._log("Cancelling; waiting for the current operation to finish…")
        if self.download is not None:
            self.download.process.abort()
        elif self.job is not None:
            self.backend.request("jobs.cancel", {"job_id": self.job}, lambda *_: None)
        elif self.conflict_dialog is not None:
            self.conflict_dialog._finish(False)

    def _backend_failed(self, message):
        self.backend_ready = False
        self.poll.stop()
        if self.backend.running:
            self.backend_error = message
            self.busy = self.cancelling = True
            self._log("Waiting for the installer to stop…")
            self.backend.close()
            self._controls()
            return
        self.job = None
        self._finish(message)

    def _backend_stopped(self):
        if self.backend_error is not None:
            message, self.backend_error = self.backend_error, None
            self.job = None
            self._finish(message)
        else:
            self._maybe_close()

    def _finish(self, message):
        self.busy = False
        self.queue = []
        self._write_result(message)
        self._log(message)
        self._controls()
        if self.browser is not None and not self.closing:
            self.browser.refresh_installed()
        self._maybe_close()

    def request_close(self):
        if not self.closing:
            self.closing = True
            if self.browser is not None:
                self.browser.dispose()
                self.browser.setEnabled(False)
            if self.login is not None:
                self.login._finish()
            if self.busy:
                self._cancel()
        self._maybe_close()

    def _maybe_close(self):
        if not self.closing or self.busy or self.login is not None or self.download is not None:
            return
        if self.backend.running:
            self.backend.close(terminate=True)
        elif self.on_close is not None:
            if not self.result_written:
                self._write_result("Workshop action closed.")
            callback, self.on_close = self.on_close, None
            callback(self.changed)


class AccountPanel(QWidget):
    def __init__(self, context, parent, on_close, operation=None):
        super().__init__(parent)
        self.on_close = on_close
        self.operation = operation
        self.login = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        if operation is not None and operation['action'] == 'forget':
            self.preferred_size = QSize(480, 200)
            label = QLabel('Remove the saved Steam login from this device?')
            label.setWordWrap(True)
            layout.addWidget(label)
            self.status = QLabel()
            self.status.setWordWrap(True)
            self.status.setTextFormat(Qt.PlainText)
            self.status.hide()
            layout.addWidget(self.status)
            layout.addStretch(1)
            actions = QHBoxLayout()
            actions.addStretch(1)
            cancel = QPushButton('Cancel')
            cancel.clicked.connect(self.request_close)
            actions.addWidget(cancel)
            remove = QPushButton('Remove saved login')
            remove.clicked.connect(self._forget)
            actions.addWidget(remove)
            layout.addLayout(actions)
        else:
            game = next((game for game in _GAMES.values() if game.game_id == context['game_id']), None)
            app_id = workshop_app_id(game)
            if not app_id:
                raise ValueError('The selected game does not have a Steam App ID.')
            self.login = WorkshopLoginView(self, app_id, on_done=self._logged_in)
            layout.addWidget(self.login)

    def _forget(self):
        try:
            forget_account()
        except Exception as exc:
            self.status.setText(str(exc))
            self.status.show()
            return
        self._complete('Saved Steam login removed.')

    def _logged_in(self, result):
        self._complete('Signed in to Steam.' if result == 'connected' else 'Steam sign-in cancelled.')

    def _complete(self, message):
        if self.on_close is None:
            return
        if self.operation is not None:
            try:
                save_result(self.operation, message)
            except OSError as exc:
                from Utils.app_log import app_log
                app_log(f'[workshop] Could not update browser: {exc}')
        callback, self.on_close = self.on_close, None
        callback(False)

    def request_close(self):
        if self.login is not None:
            self.login.request_close()
        else:
            self._complete('Saved Steam login unchanged.')


def create_view(context, parent, on_close):
    operation = take_operation(context)
    if operation is None or operation['action'] in {'login', 'forget'}:
        return AccountPanel(context, parent, on_close, operation)
    return WorkshopPanel(context, parent, on_close, operation=operation)


def create_widget(context, parent):
    from .tab import WorkshopTab
    return WorkshopTab(context, parent)


def create_service(context):
    from .support_service import WorkshopSupport
    return WorkshopSupport(context)
