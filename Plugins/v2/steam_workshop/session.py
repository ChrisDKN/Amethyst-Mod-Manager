import threading

from PySide6.QtCore import QCoreApplication, QObject, Qt, Signal, QTimer
from PySide6.QtGui import QColor, QImage, QPixmap
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea,
    QVBoxLayout, QWidget,
)

from .download import (
    DownloadCancelled, DownloaderProcess, download_items, login_account, saved_account,
)
from gui_qt.runtime.safe_emit import safe_emit


class WorkshopLoginView(QWidget):
    _event = Signal(str, object)

    def __init__(self, host, app_id, *, on_done=None, allow_browse=False):
        super().__init__(host)
        self._on_done = on_done
        self._done = False
        self._app_id = app_id
        self._process = None
        self.worker = None
        self.closing = False
        self._event.connect(self._received)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        heading = QLabel(self.tr("Sign in to Steam"))
        heading.setStyleSheet("font-size: 22px; font-weight: 600;")
        layout.addWidget(heading)
        note = QLabel(self.tr(
            "Connect once to download Workshop mods and their requirements. "
            "Use an account that owns the game."))
        note.setWordWrap(True)
        note.setObjectName("Help")
        layout.addWidget(note)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        body = QWidget()
        form = QVBoxLayout(body)
        form.setContentsMargins(0, 0, 8, 0)
        form.setSpacing(10)
        form.addWidget(QLabel(self.tr("Steam account name")))
        self._username = QLineEdit(saved_account())
        self._username.setPlaceholderText(self.tr("Account name, not profile display name"))
        form.addWidget(self._username)
        self._method = QComboBox()
        self._method.addItem(self.tr("Scan with the Steam mobile app"), "qr")
        self._method.addItem(self.tr("Use my password"), "password")
        form.addWidget(self._method)
        self._qr = QLabel()
        self._qr.setAlignment(Qt.AlignCenter)
        self._qr.hide()
        form.addWidget(self._qr)
        self._prompt = QWidget()
        prompt_layout = QVBoxLayout(self._prompt)
        prompt_layout.setContentsMargins(0, 0, 0, 0)
        self._prompt_label = QLabel()
        self._prompt_label.setWordWrap(True)
        prompt_layout.addWidget(self._prompt_label)
        self._response = QLineEdit()
        self._response.setEchoMode(QLineEdit.Password)
        self._response.returnPressed.connect(self._submit)
        prompt_layout.addWidget(self._response)
        submit = QPushButton(self.tr("Continue"))
        submit.setObjectName("PrimaryButton")
        submit.clicked.connect(self._submit)
        prompt_layout.addWidget(submit)
        form.addWidget(self._prompt)
        self._prompt.hide()
        self._status = QLabel()
        self._status.setTextFormat(Qt.PlainText)
        self._status.setWordWrap(True)
        form.addWidget(self._status)
        form.addStretch()
        scroll.setWidget(body)
        layout.addWidget(scroll, 1)

        saved_note = QLabel(self.tr(
            "Your Steam session is saved on this device; your password is not. "
            "Use Remove saved login in the Workshop browser to clear it."))
        saved_note.setObjectName("Help")
        saved_note.setWordWrap(True)
        layout.addWidget(saved_note)
        actions = QHBoxLayout()
        self._cancel_button = QPushButton(self.tr("Cancel"))
        self._cancel_button.setObjectName("FormButton")
        self._cancel_button.clicked.connect(lambda: self._finish(None))
        actions.addWidget(self._cancel_button)
        self._start_button = QPushButton(self.tr("Sign in"))
        self._start_button.setObjectName("PrimaryButton")
        self._start_button.clicked.connect(self._start)
        actions.addWidget(self._start_button, 1)
        layout.addLayout(actions)
        self._browse_button = QPushButton(self.tr("Browse without signing in"))
        self._browse_button.setObjectName("FormButton")
        self._browse_button.clicked.connect(lambda: self._finish("browse"))
        self._browse_button.setVisible(allow_browse)
        layout.addWidget(self._browse_button)
        self._username.setFocus()

    def _start(self):
        username = self._username.text().strip()
        if not username:
            self._status.setText(self.tr("Enter your Steam account name."))
            self._username.setFocus()
            return
        mode = self._method.currentData()
        self._set_busy(True)
        self._status.setText(self.tr("Connecting to Steam…"))
        event, app_id = self._event, self._app_id
        cancelled = self.tr("Sign-in cancelled.")
        process = DownloaderProcess(threading.Event(), lambda kind, value: safe_emit(event, kind, value))
        self._process = process
        self.destroyed.connect(process.abort)
        QCoreApplication.instance().aboutToQuit.connect(process.abort)

        def work():
            try:
                login_account(app_id, process, mode=mode, username=username)
                safe_emit(event, "connected", None)
            except DownloadCancelled:
                safe_emit(event, "failed", cancelled)
            except Exception as exc:
                safe_emit(event, "failed", str(exc))

        self.worker = threading.Thread(target=work, daemon=True, name="WorkshopLogin")
        self.worker.start()

    def _set_busy(self, busy):
        for widget in (self._username, self._method, self._start_button, self._browse_button):
            widget.setEnabled(not busy)
        if not busy:
            self._prompt.hide()
            self._response.clear()
            self._qr.hide()

    def _received(self, kind, value):
        if self._done or self.closing:
            return
        if kind in {"connected", "failed"} and self.worker is not None and self.worker.is_alive():
            QTimer.singleShot(25, self, lambda: self._received(kind, value))
            return
        if kind == "status":
            self._status.setText(value)
        elif kind == "qr":
            rows = value.splitlines()
            width = max((len(row) for row in rows), default=0)
            if not width:
                return
            bitmap = QImage(width, len(rows) * 2, QImage.Format_RGB32)
            bitmap.fill(QColor("white"))
            for y, row in enumerate(rows):
                for x, char in enumerate(row):
                    if char in "█▀":
                        bitmap.setPixelColor(x, y * 2, QColor("black"))
                    if char in "█▄":
                        bitmap.setPixelColor(x, y * 2 + 1, QColor("black"))
            self._qr.setPixmap(QPixmap.fromImage(bitmap).scaled(
                240, 240, Qt.KeepAspectRatio, Qt.FastTransformation))
            self._qr.show()
        elif kind == "prompt":
            self._response.clear()
            self._prompt.setVisible(bool(value))
            if value:
                self._qr.hide()
                self._prompt_label.setText(self.tr("Steam password") if value == "password" else
                                           self.tr("Steam Guard code from your email or authenticator"))
                self._response.setFocus()
        elif kind == "connected":
            QCoreApplication.instance().aboutToQuit.disconnect(self._process.abort)
            self._process = None
            self._finish("connected")
        elif kind == "failed":
            if self._process is not None:
                QCoreApplication.instance().aboutToQuit.disconnect(self._process.abort)
            self._process = None
            self._set_busy(False)
            self._status.setText(value)

    def _submit(self):
        if self._process is None:
            return
        try:
            self._process.submit(self._response.text())
        except ValueError as exc:
            self._status.setText(str(exc))
            return
        self._response.clear()
        self._prompt.hide()
        self._status.setText(self.tr("Waiting for Steam…"))

    def _finish(self, result=None):
        if self.worker is not None and self.worker.is_alive():
            self.closing = True
            if self._process is not None:
                self._process.abort()
            self._status.setText(self.tr("Waiting for Steam sign-in to stop…"))
            QTimer.singleShot(25, self, lambda: self._finish(result))
            return
        if self._process is not None:
            self._process.abort()
            QCoreApplication.instance().aboutToQuit.disconnect(self._process.abort)
            self._process = None
        self._response.clear()
        if self._done:
            return
        self._done = True
        self.hide()
        if self._on_done:
            self._on_done(result)

    def request_close(self):
        self._finish(None)


class WorkshopDownload(QObject):
    event = Signal(str, object)

    def __init__(self, parent, items, username, *, game_name=""):
        super().__init__(parent)
        self.items = tuple(items)
        self.username = username
        self.game_name = game_name
        self.needs_login = False
        self.process = DownloaderProcess(threading.Event(), self._emit)
        self.destroyed.connect(self.process.abort)
        QCoreApplication.instance().aboutToQuit.connect(self.process.abort)

    def _emit(self, kind, value):
        if (kind == "prompt" and value) or (kind == "output" and
                "access token was rejected" in value.lower()):
            self.needs_login = True
            self.process.abort()
        safe_emit(self.event, kind, value)

    def start(self):
        def work():
            try:
                from Utils.config_paths import get_download_cache_dir_for_game
                output = get_download_cache_dir_for_game(self.game_name)
                downloads = download_items(self.items, self.process, username=self.username,
                                           output_dir=output)
                safe_emit(self.event, "downloaded", downloads)
            except DownloadCancelled:
                safe_emit(self.event, "login_required" if self.needs_login else "cancelled", None)
            except Exception as exc:
                safe_emit(self.event, "login_required" if self.needs_login else "failed", str(exc))

        self.worker = threading.Thread(target=work, daemon=True, name="WorkshopDownload")
        self.worker.start()
