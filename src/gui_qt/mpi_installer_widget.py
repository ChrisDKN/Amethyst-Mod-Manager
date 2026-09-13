from __future__ import annotations

import queue
import threading
from pathlib import Path

from PySide6.QtCore import QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget

from gui_qt.safe_emit import safe_emit
from Utils.bethesda.ttw import INSTALLER_NEXUS_URL


class MPIInstallerWidget(QWidget):
    ready = Signal(object)
    running_changed = Signal(bool)
    _event = Signal(object, str, object)
    _picked = Signal(object)

    def __init__(self, game, get_api=None, log_fn=None, parent=None, *, can_start=None):
        super().__init__(parent)
        self._game = game
        self._get_api = get_api
        self._can_start = can_start
        self._log = log_fn or (lambda message: None)
        self._cancel = threading.Event()
        self._running = False
        self._waiting = False
        self._selected = queue.Queue()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        note = QLabel(self.tr(
            "Install the TTW / MPI installer from Nexus Mods (site mod 1657).\n"
            "Premium accounts download the latest Main file automatically. "
            "Free users download it in their browser; ZIP or 7z archives containing 1657 in "
            "their name are detected in the enabled locations in the Downloads tab.\n"
            "You can also select the installer archive below."), self)
        note.setWordWrap(True)
        layout.addWidget(note)
        row = QHBoxLayout()
        self.archive_box = QLineEdit(self)
        self.archive_box.setClearButtonEnabled(True)
        self.archive_box.setPlaceholderText(self.tr("Installer archive (ZIP or 7z, optional)"))
        self.archive_box.returnPressed.connect(self._use_archive_path)
        row.addWidget(self.archive_box, 1)
        self._browse = QPushButton(self.tr("Choose archive…"), self)
        self._browse.clicked.connect(self._choose)
        row.addWidget(self._browse)
        layout.addLayout(row)
        self.status = QLabel(self)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        buttons = QHBoxLayout()
        self.install_button = QPushButton(self.tr("Install"), self)
        self.install_button.clicked.connect(self.start)
        buttons.addWidget(self.install_button)
        self._page = QPushButton(self.tr("Open Nexus download page"), self)
        self._page.clicked.connect(self._open_page)
        buttons.addWidget(self._page)
        self._cancel_button = QPushButton(self.tr("Cancel"), self)
        self._cancel_button.clicked.connect(self._cancel_install)
        self._cancel_button.hide()
        buttons.addWidget(self._cancel_button)
        layout.addLayout(buttons)
        self._event.connect(self._received)
        self._picked.connect(self._on_picked)

    def _use_archive_path(self):
        path = self.archive_box.text().strip()
        if path:
            self._on_picked(Path(path).expanduser())

    def _choose(self):
        from Utils.ui.portal import pick_file
        pick_file(self.tr("Select the TTW / MPI installer archive"),
                  lambda path: safe_emit(self._picked, path),
                  filters=[(self.tr("Installer archives (*.zip, *.7z)"),
                            ["*.zip", "*.ZIP", "*.7z", "*.7Z"])])

    def _on_picked(self, path):
        if not self.isVisible() or path is None or (self._running and not self._waiting):
            return
        self.archive_box.setText(str(path))
        if self._waiting:
            self._selected.put(Path(path))
        else:
            self.start()

    def _open_page(self):
        if not self._running:
            self.start(manual=True)
        QDesktopServices.openUrl(QUrl(INSTALLER_NEXUS_URL))

    def start(self, checked=False, *, manual=False):
        if self._running:
            return
        if self._can_start is not None and not self._can_start():
            self.status.setText(self.tr("Wait for the current operation to finish before installing this tool."))
            return
        self._cancel = threading.Event()
        self._running = True
        self._waiting = False
        self._selected = queue.Queue()
        cancel = self._cancel
        self.destroyed.connect(lambda: cancel.set())
        selected = self._selected
        api = None
        if self._get_api is not None and not manual:
            try:
                api = self._get_api()
            except Exception as exc:
                self._log(f"Could not access Nexus account: {exc}")
        archive_text = self.archive_box.text().strip()
        archive = Path(archive_text).expanduser() if archive_text and not manual else None
        self.install_button.setEnabled(False)
        self._browse.setEnabled(False)
        self._cancel_button.show()
        self.status.setText(self.tr("Preparing MPI installer…"))
        self.running_changed.emit(True)
        if cancel.is_set():
            return

        def emit(kind, value=None):
            if not cancel.is_set():
                safe_emit(self._event, selected, kind, value)

        def worker():
            from Utils.bethesda.ttw import (
                ManualInstallerDownloadRequired, download_installer, find_installer_archive,
            )
            status = lambda message: emit("status", message)
            try:
                try:
                    if manual and archive is None:
                        raise ManualInstallerDownloadRequired()
                    exe = download_installer(self._game, status, self._log,
                                             api=api, archive_path=archive, cancel=cancel)
                except ManualInstallerDownloadRequired as exc:
                    emit("waiting", str(exc))
                    if not manual:
                        emit("browser")
                    observed = {}
                    while not cancel.is_set():
                        try:
                            path = selected.get(timeout=2)
                        except queue.Empty:
                            path = find_installer_archive(self._game, observed)
                        if path is None:
                            continue
                        if cancel.is_set():
                            return
                        emit("extracting", path)
                        exe = download_installer(self._game, status, self._log,
                                                 archive_path=path, cancel=cancel)
                        break
                    else:
                        return
                emit("ready", exe)
            except Exception as exc:
                self._log(f"MPI installer: {exc}")
                emit("error", str(exc))

        threading.Thread(target=worker, daemon=True, name="mpi-installer").start()

    def _received(self, selected, kind, value):
        if self._cancel.is_set() or selected is not self._selected:
            return
        if kind == "status":
            self.status.setText(value)
        elif kind == "browser":
            QDesktopServices.openUrl(QUrl(INSTALLER_NEXUS_URL))
        elif kind == "waiting":
            self._waiting = True
            self._browse.setEnabled(True)
            self.status.setText(self.tr(
                "{0}\nWaiting for the completed installer archive in your download locations. "
                "You can also choose the archive manually.").format(value).strip())
        elif kind == "extracting":
            self._waiting = False
            self._browse.setEnabled(False)
            self.archive_box.setText(str(value))
        elif kind in {"ready", "error"}:
            self._running = self._waiting = False
            self._cancel_button.hide()
            self._browse.setEnabled(True)
            self.install_button.setEnabled(True)
            self.running_changed.emit(False)
            if kind == "ready":
                self.status.setText(self.tr("MPI installer ready."))
                self.ready.emit(value)
            else:
                self.status.setText(self.tr("Install error: {0}").format(value))

    def _cancel_install(self):
        self.stop()
        self.status.setText(self.tr("Installer setup cancelled."))

    def stop(self):
        was_running = self._running
        self._cancel.set()
        self._selected = queue.Queue()
        self._running = self._waiting = False
        self.install_button.setEnabled(True)
        self._browse.setEnabled(True)
        self._cancel_button.hide()
        if was_running:
            self.running_changed.emit(False)

    def hideEvent(self, event):
        self.stop()
        super().hideEvent(event)

    def showEvent(self, event):
        if self._cancel.is_set():
            self._cancel = threading.Event()
        super().showEvent(event)
