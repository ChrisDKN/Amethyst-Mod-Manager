"""Download and install a Steam Workshop item."""

from __future__ import annotations

import threading

from PySide6.QtCore import QCoreApplication, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPixmap
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QPlainTextEdit, QProgressBar, QVBoxLayout, QWidget,
)

from gui_qt.safe_emit import safe_emit
from Utils.games.workshop import workshop_app_id
from Utils.wizards.workshop import (
    DownloadCancelled, DownloaderProcess, download_item, fetch_item,
    forget_account, parse_item_id, saved_account,
)
from wizards_qt._view_base import GREEN, RED, WizardViewBase


class WorkshopView(WizardViewBase):
    _event_sig = Signal(str, object)

    def __init__(self, game, log_fn=None, on_close=None, ctx=None, **_extra):
        super().__init__(game, log_fn, on_close, ctx,
                         title=self.tr("Install Steam Workshop Mod"))
        self._app_id = workshop_app_id(game)
        self._cancel = threading.Event()
        cancel = self._cancel
        self.destroyed.connect(lambda: cancel.set())
        self._process = None
        QCoreApplication.instance().aboutToQuit.connect(self._abort)
        self._item = None
        self._download = None
        self._event_sig.connect(self._guard(self._on_event))

        page, layout = self._step_page(game.name)
        self._make_note(layout, self.tr(
            "Download an individual public Workshop mod into this profile. "
            "Required Workshop items must be installed separately."))
        columns = QHBoxLayout()
        layout.addLayout(columns, 1)
        left = QVBoxLayout()
        right = QVBoxLayout()
        columns.addLayout(left, 1)
        columns.addLayout(right, 1)
        form = QFormLayout()
        left.addLayout(form)
        self._item_input = QLineEdit()
        self._item_input.setPlaceholderText(self.tr("Item ID or Steam Workshop URL"))
        form.addRow(self.tr("Workshop item"), self._item_input)
        target = QLabel(self.tr("{0} · App ID {1}").format(
            getattr(ctx, "profile_name", "default"), self._app_id))
        target.setTextFormat(Qt.PlainText)
        target.setWordWrap(True)
        form.addRow(self.tr("Install into"), target)
        self._mode = QComboBox()
        self._mode.addItem(self.tr("QR code (Steam mobile app)"), "qr")
        self._mode.addItem(self.tr("Steam account and password"), "password")
        self._mode.addItem(self.tr("Saved account"), "saved")
        self._mode.addItem(self.tr("Anonymous (where supported)"), "anonymous")
        form.addRow(self.tr("Sign in"), self._mode)
        self._username = QLineEdit(saved_account())
        self._username.setPlaceholderText(self.tr("Steam account name, not display name"))
        self._username_label = QLabel(self.tr("Account name"))
        form.addRow(self._username_label, self._username)
        self._remember = QCheckBox(self.tr("Remember this account"))
        self._remember.setToolTip(self.tr(
            "Keep the Steam session on this device. Enter the account name "
            "to use it for later downloads."))
        left.addWidget(self._remember)
        self._forget = self._orange_btn(self.tr("Forget saved account"))
        self._forget.clicked.connect(self._forget_account)
        left.addWidget(self._forget)
        self._mode.currentIndexChanged.connect(self._mode_changed)
        self._remember.toggled.connect(self._mode_changed)
        if self._username.text():
            self._mode.setCurrentIndex(self._mode.findData("saved"))
        self._mode_changed()
        self._title = QLabel("")
        self._title.setTextFormat(Qt.PlainText)
        self._title.setWordWrap(True)
        left.addWidget(self._title)
        self._status = self._make_status(left)
        self._status.setTextFormat(Qt.PlainText)
        self._progress = QProgressBar()
        self._progress.setVisible(False)
        left.addWidget(self._progress)
        open_page = self._accent_btn(self.tr("Open Workshop page"))
        open_page.clicked.connect(self._open_item)
        left.addWidget(open_page)
        left.addStretch()

        self._qr = QLabel()
        self._qr.setAlignment(Qt.AlignCenter)
        self._qr.hide()
        right.addWidget(self._qr)
        self._prompt_box = QWidget()
        prompt_layout = QVBoxLayout(self._prompt_box)
        prompt_layout.setContentsMargins(0, 0, 0, 0)
        self._prompt_label = QLabel()
        self._prompt_label.setWordWrap(True)
        prompt_layout.addWidget(self._prompt_label)
        self._response = QLineEdit()
        self._response.setEchoMode(QLineEdit.Password)
        self._response.returnPressed.connect(self._send_response)
        prompt_layout.addWidget(self._response)
        send = self._accent_btn(self.tr("Continue sign-in"))
        send.clicked.connect(self._send_response)
        prompt_layout.addWidget(send)
        right.addWidget(self._prompt_box)
        self._prompt_box.hide()
        self._output = QPlainTextEdit()
        self._output.setReadOnly(True)
        self._output.setMaximumBlockCount(300)
        right.addWidget(self._output, 1)

        buttons = QHBoxLayout()
        self._install = self._green_btn(self.tr("Install downloaded files"))
        self._install.setEnabled(False)
        self._install.clicked.connect(self._install_download)
        buttons.addWidget(self._install)
        buttons.addStretch()
        self._stop = self._red_btn(self.tr("Cancel download"))
        self._stop.setEnabled(False)
        self._stop.clicked.connect(self._cancel_download)
        buttons.addWidget(self._stop)
        self._start = self._accent_btn(self.tr("Download and install"))
        self._start.clicked.connect(self._start_download)
        buttons.addWidget(self._start)
        layout.addLayout(buttons)
        self._stack.addWidget(page)

    def _mode_changed(self, *_args):
        mode = self._mode.currentData()
        needs_name = mode in {"password", "saved"} or (
            mode == "qr" and self._remember.isChecked())
        self._username.setVisible(needs_name)
        self._username_label.setVisible(needs_name)
        self._remember.setVisible(mode in {"qr", "password"})

    def _busy(self, busy: bool):
        self._lock_close(busy, self.tr("Cancel the download before closing."))
        for widget in (self._item_input, self._mode, self._username,
                       self._remember, self._forget, self._start):
            widget.setEnabled(not busy)
        self._stop.setEnabled(busy)
        self._install.setEnabled(not busy and self._download is not None)
        self._progress.setVisible(busy)
        if not busy:
            self._prompt_box.hide()
            self._response.clear()
            self._qr.hide()

    def _start_download(self):
        try:
            item_id = parse_item_id(self._item_input.text())
            mode = self._mode.currentData()
            username = self._username.text().strip()
            remember = self._remember.isChecked() and mode in {"qr", "password"}
            if (mode in {"password", "saved"} or remember) and not username:
                raise ValueError(self.tr("Enter your Steam account name."))
            if not getattr(self._ctx, "install_archive", None):
                raise RuntimeError(self.tr("The mod installer is unavailable."))
        except (ValueError, RuntimeError) as exc:
            self._set_status(self._status, str(exc), RED)
            return
        self._cancel.clear()
        self._download = None
        self._item = None
        self._output.clear()
        self._title.clear()
        self._busy(True)
        self._progress.setRange(0, 0)
        self._set_status(self._status, self.tr("Looking up Workshop item…"))
        emit = lambda kind, value: safe_emit(self._event_sig, kind, value)
        self._process = DownloaderProcess(self._cancel, emit)
        process, app_id = self._process, self._app_id

        def worker():
            try:
                item = fetch_item(app_id, item_id)
                if process.cancel.is_set():
                    raise DownloadCancelled()
                emit("item", item)
                archive = download_item(item, process, mode=mode,
                                        username=username, remember=remember)
                emit("downloaded", archive)
            except DownloadCancelled:
                emit("cancelled", None)
            except Exception as exc:
                emit("failed", str(exc))

        threading.Thread(target=worker, daemon=True, name="workshop-download").start()

    def _on_event(self, kind, value):
        if kind == "item":
            self._item = value
            self._title.setText(value.title)
        elif kind == "status":
            self._set_status(self._status, value)
        elif kind == "progress":
            done, total = value
            self._progress.setRange(0, 100 if total else 0)
            if total:
                self._progress.setValue(min(100, int(done * 100 / total)))
        elif kind == "output":
            self._output.appendPlainText(value)
        elif kind == "qr":
            rows = value.splitlines()
            width = max(map(len, rows), default=0)
            if not width:
                return
            bitmap = QImage(width, len(rows) * 2, QImage.Format_RGB32)
            bitmap.fill(QColor("white"))
            black = QColor("black")
            for y, row in enumerate(rows):
                for x, char in enumerate(row):
                    if char in "█▀":
                        bitmap.setPixelColor(x, y * 2, black)
                    if char in "█▄":
                        bitmap.setPixelColor(x, y * 2 + 1, black)
            self._qr.setPixmap(QPixmap.fromImage(bitmap).scaled(
                320, 320, Qt.KeepAspectRatio, Qt.FastTransformation))
            self._qr.show()
        elif kind == "authenticated":
            self._qr.hide()
            self._set_status(self._status, self.tr("Signed in. Downloading Workshop files…"))
        elif kind == "prompt":
            self._response.clear()
            self._prompt_box.setVisible(bool(value))
            if value:
                self._prompt_label.setText(self.tr("Steam account password") if value == "password"
                                           else self.tr("Steam Guard code from your email or authenticator"))
                self._response.setFocus()
        elif kind == "downloaded":
            self._download = value
            self._busy(False)
            self._log(f"Workshop: downloaded {self._item.title} ({self._item.item_id}) to {value}")
            self._install_download()
        elif kind in {"failed", "cancelled"}:
            self._busy(False)
            message = value if kind == "failed" else self.tr("Download cancelled. Nothing was installed.")
            self._set_status(self._status, message, RED if kind == "failed" else "")
        elif kind == "installed":
            self._start.setEnabled(True)
            names, handoff = value
            if names:
                self._set_status(self._status, self.tr("Installed: {0}").format(
                    ", ".join(names)), GREEN)
            elif handoff:
                self._set_status(self._status, self.tr(
                    "Complete the mod installer tab to finish installation."))
            else:
                self._install.setEnabled(True)
                self._set_status(self._status, self.tr(
                    "Installation did not complete. The download is kept so you can try again."), RED)

    def _send_response(self):
        if self._process is None:
            return
        try:
            self._process.submit(self._response.text())
        except ValueError as exc:
            self._set_status(self._status, str(exc), RED)
            return
        self._response.clear()
        self._prompt_box.hide()

    def _cancel_download(self):
        self._abort()
        self._stop.setEnabled(False)
        self._prompt_box.hide()
        self._response.clear()
        self._set_status(self._status, self.tr("Cancelling download…"))

    def _abort(self):
        self._cancel.set()
        if self._process is not None:
            self._process.abort()

    def _install_download(self):
        if self._download is None or self._item is None:
            return
        self._start.setEnabled(False)
        self._install.setEnabled(False)
        try:
            self._set_status(self._status, self.tr("Installing the downloaded mod…"))
            self._ctx.install_archive(str(self._download), self._item.mod_meta(self._download),
                                      lambda names, handoff: safe_emit(
                                          self._event_sig, "installed", (names, handoff)))
        except Exception as exc:
            self._start.setEnabled(True)
            self._install.setEnabled(True)
            self._set_status(self._status, str(exc), RED)
            return

    def _open_item(self):
        try:
            item_id = parse_item_id(self._item_input.text())
            self._open_url(f"https://steamcommunity.com/sharedfiles/filedetails/?id={item_id}")
        except ValueError as exc:
            self._set_status(self._status, str(exc), RED)

    def _forget_account(self):
        try:
            forget_account()
            self._username.clear()
            self._mode.setCurrentIndex(self._mode.findData("qr"))
            self._remember.setChecked(False)
            self._set_status(self._status, self.tr("Saved Steam account removed from Amethyst."))
        except Exception as exc:
            self._set_status(self._status, str(exc), RED)
