from __future__ import annotations

import threading

from PySide6.QtCore import Qt, QSignalBlocker, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QLabel, QLineEdit

from gui_qt.safe_emit import safe_emit
from gui_qt.theme_qt import _c


class _ConnectionWork:
    def __init__(self):
        self.generation = 0
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.closed = False

    def invalidate(self):
        self.stop.set()
        self.stop = threading.Event()
        self.generation += 1
        return self.generation

    def current(self, generation):
        return not self.closed and generation == self.generation

    def close(self):
        self.closed = True
        self.stop.set()


def _modio_module(name):
    from wizards_qt.modio_settings_view import _load_bg3_modio
    return _load_bg3_modio(name)


class ConnectionsSettingsMixin:
    def _build_connections(self):
        self._connection_jobs = {key: _ConnectionWork() for key in ("modio", "loverslab")}
        self._connection_sections = {}
        self._connection_entries = {}
        self._connection_statuses = {}
        self._connection_buttons = {}
        self._connection_result.connect(self._connection_done)
        jobs = tuple(self._connection_jobs.values())
        self.destroyed.connect(lambda: [job.close() for job in jobs])

        grid = self._section(self.tr("Nexus Mods"))
        self._connection_sections["nexus"] = grid.parentWidget()
        self._nexus_account_info = self._connection_label(grid, self.tr("Account"))
        self._nexus_membership_info = self._connection_label(grid, self.tr("Membership"))
        self._nexus_limits_info = self._connection_label(grid, self.tr("API requests remaining"))
        self._nexus_connect_button = self._action_row(
            grid, self.tr("Login via SSO"), self._window._nexus_login_sso)
        self._nexus_code_button = self._action_row(
            grid, self.tr("Paste login code…"), self._window._nexus_paste_code)
        self._nexus_clear_button = self._action_row(
            grid, self.tr("Clear credentials"), self._window._nexus_clear_credentials)
        self._finish_section(grid)

        grid = self._section(self.tr("mod.io"))
        self._connection_sections["modio"] = grid.parentWidget()
        self._connection_note(grid, self.tr(
            "Enable update checks for Baldur's Gate 3 mods using the API path and read-only key from your mod.io API Access page."))
        self._connection_fields(grid, "modio", self.tr("API path"), self.tr("API key"))
        self._connection_entries["modio"][0].setPlaceholderText("https://u-123.modapi.io/v1")
        self._action_row(grid, self.tr("Get my API key"),
                         lambda: QDesktopServices.openUrl(QUrl("https://mod.io/me/access")))
        self._connection_actions(grid, "modio")

        grid = self._section(self.tr("LoversLab"))
        self._connection_sections["loverslab"] = grid.parentWidget()
        self._connection_note(grid, self.tr(
            "Sign in to automatically download LoversLab files during Wabbajack installs. Credentials are stored securely. Site security checks may require a manual download."))
        self._connection_fields(grid, "loverslab", self.tr("Email"), self.tr("Password"))
        self._connection_actions(grid, "loverslab")

        self._tabs.widget(self._connections_tab_index).verticalScrollBar().rangeChanged.connect(
            self._reveal_connection)

        self._nexus_connection_timer = QTimer(self)
        self._nexus_connection_timer.setInterval(3000)
        self._nexus_connection_timer.timeout.connect(self._refresh_nexus_connection)
        self._nexus_connection_timer.start()
        for name in ("_oauth_event", "_nexus_validated", "_nexus_api_initialized"):
            signal = getattr(self._window, name, None)
            if signal is not None:
                signal.connect(self._refresh_nexus_connection)
        self._refresh_nexus_connection()
        for section in self._connection_jobs:
            self._start_connection_work(section, "load")

    def show_connection(self, section="nexus"):
        self._tabs.setCurrentIndex(self._connections_tab_index)
        self._ensure_tab_built(self._connections_tab_index)
        box = self._connection_sections.get(section, self._connection_sections["nexus"])
        self._connection_target = box
        QTimer.singleShot(0, self, self._reveal_connection)

    def _reveal_connection(self, *_):
        box = getattr(self, "_connection_target", None)
        if self._done or box is None:
            return
        scroll = self._tabs.widget(self._connections_tab_index)
        if self._tabs.currentWidget() is not scroll:
            self._connection_target = None
            return
        body = scroll.widget()
        body.ensurePolished()
        body.layout().activate()
        if body.sizeHint().height() > scroll.viewport().height() and not scroll.verticalScrollBar().maximum():
            return
        scroll.ensureWidgetVisible(box, 0, 12)
        self._connection_target = None

    def _connection_row(self, grid, label, widget):
        row = self._next_row(grid)
        title = QLabel(label)
        title.setWordWrap(True)
        grid.addWidget(title, row, self.COL_LABEL)
        grid.addWidget(widget, row, self.COL_CTRL)

    def _connection_label(self, grid, label):
        value = QLabel()
        value.setTextFormat(Qt.PlainText)
        value.setWordWrap(True)
        self._connection_row(grid, label, value)
        return value

    def _connection_note(self, grid, text):
        note = QLabel(text)
        note.setWordWrap(True)
        note.setTextFormat(Qt.PlainText)
        note.setObjectName("Help")
        grid.addWidget(note, self._next_row(grid), self.COL_LABEL, 1, 2)

    def _connection_fields(self, grid, section, identity_label, secret_label):
        identity, secret = QLineEdit(), QLineEdit()
        identity.setObjectName(section + "_identity")
        secret.setObjectName(section + "_secret")
        secret.setEchoMode(QLineEdit.Password)
        self._connection_row(grid, identity_label, identity)
        self._connection_row(grid, secret_label, secret)
        self._connection_entries[section] = (identity, secret)
        status = QLabel(self.tr("Loading saved credentials…"))
        status.setWordWrap(True)
        status.setTextFormat(Qt.PlainText)
        grid.addWidget(status, self._next_row(grid), self.COL_LABEL, 1, 2)
        self._connection_statuses[section] = status
        for entry in (identity, secret):
            entry.textChanged.connect(lambda _text, key=section: self._connection_edited(key))

    def _connection_actions(self, grid, section):
        save = self._action_row(grid, self.tr("Test && Save"),
                                lambda: self._start_connection_work(section, "save"))
        clear = self._action_row(grid, self.tr("Clear credentials"),
                                 lambda: self._start_connection_work(section, "clear"))
        self._connection_buttons[section] = (save, clear)
        self._finish_section(grid)

    def _connection_edited(self, section):
        self._connection_jobs[section].invalidate()
        self._set_connection_busy(section, False)
        self._connection_status(section, self.tr("Changes have not been saved."))

    def _set_connection_busy(self, section, busy, operation=""):
        save, clear = self._connection_buttons[section]
        entries = self._connection_entries[section]
        save.setEnabled(not busy and all(entry.text() for entry in entries))
        clear.setEnabled(not busy or operation in {"load", "save"})
        for entry in entries:
            entry.setEnabled(not busy or operation != "clear")

    def _connection_status(self, section, message, ok=None):
        status = self._connection_statuses[section]
        status.setText(message)
        status.setStyleSheet("color: " + _c(self._pal, "TEXT_ERR" if ok is False else "TEXT_MAIN") + ";")

    def _start_connection_work(self, section, operation):
        job = self._connection_jobs[section]
        generation = job.invalidate()
        stop = job.stop
        identity, secret = (entry.text() for entry in self._connection_entries[section])
        identity = identity.strip()
        if section == "modio":
            secret = secret.strip()
        if operation == "save" and (not identity or not secret):
            self._connection_status(section, self.tr("Complete both fields first."), False)
            return
        self._set_connection_busy(section, True, operation)
        messages = {"load": self.tr("Loading saved credentials…"),
                    "save": self.tr("Checking credentials…"), "clear": self.tr("Clearing credentials…")}
        self._connection_status(section, messages[operation])
        signal = self._connection_result

        def worker():
            payload = {"ok": False, "error": ""}
            try:
                if section == "modio":
                    module = _modio_module("modio_key")
                    load = module.load_modio_credentials
                    clear = module.clear_modio_key
                    save = lambda first, second: module.save_modio_credentials(second, first)
                else:
                    from Utils.loverslab.credentials import (
                        load_loverslab_credentials, save_loverslab_credentials, clear_loverslab_credentials)
                    load, save, clear = load_loverslab_credentials, save_loverslab_credentials, clear_loverslab_credentials
                if operation == "load":
                    with job.lock:
                        if not job.current(generation):
                            return
                        values = load()
                    if section == "modio":
                        key, api_path = values
                        payload["values"] = (api_path, key)
                    else:
                        payload["values"] = (values.email, values.password) if values else ("", "")
                elif operation == "clear":
                    with job.lock:
                        if generation != job.generation:
                            return
                        clear()
                else:
                    if not job.current(generation):
                        return
                    normalized = identity
                    if section == "modio":
                        api = _modio_module("modio_api")
                        normalized = api.normalize_api_path(identity)
                        if not api.ModioAPI(secret, normalized).test_key():
                            raise ValueError
                    else:
                        from Utils.loverslab.client import LoversLabClient
                        from Utils.loverslab.credentials import LoversLabCredentials
                        client = LoversLabClient(LoversLabCredentials(identity, secret), stop=stop)
                        try:
                            client.login()
                        finally:
                            client.close()
                    with job.lock:
                        if not job.current(generation):
                            return
                        save(normalized, secret)
                    payload["identity"] = normalized
                payload["ok"] = True
            except InterruptedError:
                return
            except Exception as exc:
                from Utils.loverslab.credentials import CredentialStorageError
                from Utils.wabbajack.http import DownloadUnavailable
                if isinstance(exc, (CredentialStorageError, DownloadUnavailable)):
                    payload["error"] = str(exc)
                else:
                    payload["error"] = operation
            safe_emit(signal, section, generation, operation, payload)

        threading.Thread(target=worker, daemon=True, name=section + "-credentials").start()

    def _connection_done(self, section, generation, operation, payload):
        job = self._connection_jobs[section]
        if not job.current(generation) or self._done:
            return
        entries = self._connection_entries[section]
        if payload["ok"]:
            if operation in {"load", "clear"}:
                values = payload.get("values", ("", ""))
                for entry, value in zip(entries, values):
                    with QSignalBlocker(entry):
                        entry.setText(value)
            elif "identity" in payload:
                with QSignalBlocker(entries[0]):
                    entries[0].setText(payload["identity"])
            if operation == "save":
                message = (self.tr("Login verified and saved. Automatic LoversLab downloads are enabled.")
                           if section == "loverslab" else self.tr("Key verified and saved. mod.io update checks are enabled."))
            elif operation == "clear":
                message = self.tr("Credentials cleared.")
            else:
                message = self.tr("Credentials saved.") if all(entry.text() for entry in entries) else self.tr("Not connected.")
        else:
            errors = {"load": self.tr("Could not load saved credentials. Unlock your keyring or enter them again."),
                      "save": self.tr("Could not verify or save credentials. Check both fields and your connection."),
                      "clear": self.tr("Could not clear credentials. Please try again.")}
            message = errors.get(payload["error"], payload["error"])
        self._set_connection_busy(section, False)
        self._connection_status(section, message, payload["ok"])

    def _refresh_nexus_connection(self, *_):
        if self._done:
            return
        api = getattr(self._window, "_nexus_api", None)
        user = getattr(api, "_cached_user", None)
        oauth = getattr(self._window, "_oauth_client", None)
        running = (getattr(self._window, "_nexus_login_starting", False)
                   or oauth is not None and oauth.is_running)
        clearing = getattr(self._window, "_nexus_credentials_clearing", False)
        if clearing:
            account = self.tr("Clearing credentials…")
        elif getattr(self._window, "_nexus_validation_error", ""):
            account = self.tr("Could not verify the saved login. Check your connection or sign in again.")
        elif user is not None:
            account = user.name
        elif running:
            account = self.tr("Waiting for browser login…")
        elif api is not None or getattr(self._window, "_nexus_api_init_running", False):
            account = self.tr("Checking saved login…")
        else:
            account = self.tr("Not connected.")
        self._nexus_account_info.setText(account)
        membership = self.tr("Unknown")
        if user is not None:
            membership = (self.tr("Premium") if user.is_premium else
                          self.tr("Supporter") if user.is_supporter else self.tr("Free"))
        self._nexus_membership_info.setText(membership)
        limits = getattr(api, "rate_limits", None)
        hourly, daily = getattr(limits, "hourly_remaining", -1), getattr(limits, "daily_remaining", -1)
        self._nexus_limits_info.setText(self.tr("Hourly: {0} · Daily: {1}").format(
            f"{hourly:,}" if hourly >= 0 else "—", f"{daily:,}" if daily >= 0 else "—"))
        self._nexus_connect_button.setEnabled(not clearing and not running)
        self._nexus_code_button.setEnabled(not clearing and oauth is not None and oauth.is_running)
        self._nexus_clear_button.setEnabled(not clearing)

    def _close_connections(self):
        for job in getattr(self, "_connection_jobs", {}).values():
            job.close()
