from pathlib import Path
from threading import Event

from PySide6.QtCore import QSize, Qt, QThread
from PySide6.QtWidgets import (
    QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QPlainTextEdit, QProgressBar, QPushButton, QVBoxLayout, QWidget,
)

from .skeleton import build
from gui_qt.dialogs.portal_picker import pick_folder


class _Cancelled(Exception):
    pass


class _BuildThread(QThread):
    def __init__(self, text, output, parent):
        super().__init__(parent)
        self.text = text
        self.output = output
        self.cancelled = Event()
        self.result = None
        self.error = None

    def check_cancelled(self):
        if self.cancelled.is_set():
            raise _Cancelled()

    def run(self):
        try:
            self.result = build(self.text, self.output, self.check_cancelled)
        except _Cancelled:
            self.error = "Generation cancelled. Any files already created have been kept."
        except Exception as exc:
            self.error = f"Generation failed: {exc}"


class SkeletonView(QWidget):
    preferred_size = QSize(820, 680)

    def __init__(self, context, parent, on_close):
        super().__init__(parent)
        self.context = context
        self.on_close = on_close
        self.data = dict(context["data"])
        self.worker = None
        self.closing = False

        layout = QVBoxLayout(self)
        description = QLabel(
            "Paste a saved SteamDB depot Files page or its HTML response below. "
            "Generate once per depot to merge them into the same output folder. "
            "Existing files are kept.", self,
        )
        description.setWordWrap(True)
        layout.addWidget(description)

        self.options = QWidget(self)
        form = QFormLayout(self.options)
        form.setContentsMargins(0, 0, 0, 0)
        location_row = QHBoxLayout()
        location = self.data.get("output_location", str(Path.home()))
        self.location = QLineEdit(location if isinstance(location, str) else str(Path.home()), self)
        self.location.setPlaceholderText("Choose the parent directory…")
        self.location.editingFinished.connect(self._save_options)
        location_row.addWidget(self.location, 1)
        browse = QPushButton("Browse…", self)
        browse.clicked.connect(self._browse)
        location_row.addWidget(browse)
        form.addRow("Output location", location_row)
        folder = self.data.get("output_folder", "output")
        self.folder = QLineEdit(folder if isinstance(folder, str) else "output", self)
        self.folder.setPlaceholderText("Folder name, for example Skyrim SE")
        self.folder.editingFinished.connect(self._save_options)
        form.addRow("Output folder name", self.folder)
        self.destination = QLabel(self)
        self.destination.setTextFormat(Qt.PlainText)
        self.destination.setWordWrap(True)
        self.destination.setTextInteractionFlags(Qt.TextSelectableByMouse)
        form.addRow("Destination", self.destination)
        layout.addWidget(self.options)
        self.location.textChanged.connect(self._update_destination)
        self.folder.textChanged.connect(self._update_destination)
        self._update_destination()

        self.text = QPlainTextEdit(self)
        self.text.setPlaceholderText("Paste the SteamDB HTML response here…")
        layout.addWidget(self.text, 1)
        self.status = QLabel(self)
        self.status.setTextFormat(Qt.PlainText)
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.status)
        self.progress = QProgressBar(self)
        self.progress.setRange(0, 0)
        self.progress.hide()
        layout.addWidget(self.progress)

        buttons = QHBoxLayout()
        self.clear_button = QPushButton("Clear text", self)
        self.clear_button.clicked.connect(self.text.clear)
        buttons.addWidget(self.clear_button)
        buttons.addStretch()
        self.close_button = QPushButton("Close", self)
        self.close_button.clicked.connect(self.request_close)
        buttons.addWidget(self.close_button)
        self.cancel_button = QPushButton("Cancel generation", self)
        self.cancel_button.clicked.connect(self._cancel)
        self.cancel_button.hide()
        buttons.addWidget(self.cancel_button)
        self.generate_button = QPushButton("Generate", self)
        self.generate_button.clicked.connect(self._generate)
        buttons.addWidget(self.generate_button)
        layout.addLayout(buttons)

    def _output_path(self):
        location = self.location.text().strip()
        folder = self.folder.text().strip()
        if not location or not Path(location).expanduser().is_absolute():
            raise ValueError("Choose an absolute output location.")
        if (not folder or folder in {".", ".."} or "/" in folder or "\\" in folder
                or any(ord(char) < 32 for char in folder)):
            raise ValueError("Enter a single output folder name without path separators.")
        output = Path(location).expanduser() / folder
        if output.is_symlink():
            raise ValueError("The output folder must not be a symbolic link.")
        return output

    def _update_destination(self):
        try:
            self.destination.setText(str(self._output_path()))
        except (ValueError, OSError) as exc:
            self.destination.setText(str(exc))

    def _save_options(self):
        data = {**self.data, "output_location": self.location.text().strip(),
                "output_folder": self.folder.text().strip()}
        if data != self.data:
            try:
                self.context["save_data"](data)
            except Exception as exc:
                self.status.setText(f"Could not save output settings: {exc}")
                return False
            self.data = data
        return True

    def _browse(self):
        def picked(path):
            if path is not None and self.worker is None:
                self.location.setText(str(path))
                self._save_options()
        pick_folder(self, "Choose output location", picked)

    def _set_busy(self, busy):
        self.options.setEnabled(not busy)
        self.text.setReadOnly(busy)
        self.clear_button.setEnabled(not busy)
        self.generate_button.setEnabled(not busy)
        self.progress.setVisible(busy)
        self.cancel_button.setVisible(busy)
        self.cancel_button.setEnabled(busy)

    def _generate(self):
        if self.worker is not None:
            return
        text = self.text.toPlainText()
        if not text.strip():
            self.status.setText("Paste a SteamDB depot Files page first.")
            return
        try:
            output = self._output_path()
        except (ValueError, OSError) as exc:
            self.status.setText(str(exc))
            return
        if not self._save_options():
            return
        self.status.setText(f"Generating skeleton in {output}…")
        self._set_busy(True)
        self.worker = _BuildThread(text, output, self)
        self.worker.finished.connect(self._finished)
        self.worker.start()

    def _cancel(self):
        if self.worker is not None:
            self.worker.cancelled.set()
            self.cancel_button.setEnabled(False)
            self.status.setText("Stopping generation…")

    def _finished(self):
        worker, self.worker = self.worker, None
        self._set_busy(False)
        if worker.error:
            self.status.setText(worker.error)
        elif not worker.result["matched"]:
            self.status.setText("No depot rows found. Paste a saved SteamDB depot Files page or its HTML response.")
        else:
            result = worker.result
            self.status.setText(
                f"Created {result['files']} empty files and {result['dirs']} folders in {worker.output}. "
                f"Skipped {result['skipped']} existing or unsafe entries."
            )
        worker.deleteLater()
        if self.closing:
            self._close()

    def _close(self):
        if self.on_close is not None:
            callback, self.on_close = self.on_close, None
            callback(False)

    def request_close(self):
        self._save_options()
        if self.worker is not None:
            self.closing = True
            self.close_button.setEnabled(False)
            self._cancel()
        else:
            self._close()


def create_view(context, parent, on_close):
    return SkeletonView(context, parent, on_close)
