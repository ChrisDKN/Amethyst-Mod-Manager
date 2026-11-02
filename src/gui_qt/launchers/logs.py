from __future__ import annotations

from pathlib import Path
import re

from PySide6.QtCore import QTimer

from gui_qt.files.text_editor import TextEditor
from Utils.executables.logs import latest_log


_MAX_BYTES = 2 * 1024 * 1024
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


class LaunchLogEditor(TextEditor):
    def __init__(self, directory: Path, kind: str, label: str):
        super().__init__(directory / (kind + ".log"), f"{kind.upper()} — {label}")
        self._directory = directory
        self._kind = kind
        self._stamp = None
        self._edit.setReadOnly(True)
        self._save_btn.hide()
        self._save_sc.setEnabled(False)
        self._revert_btn.setText(self.tr("Refresh"))
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self.refresh)
        self.refresh()

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()
        self._timer.start()

    def hideEvent(self, event):
        self._timer.stop()
        super().hideEvent(event)

    def revert(self):
        self._stamp = None
        self.refresh()

    def refresh(self):
        path = latest_log(self._directory, self._kind)
        try:
            stat = path.stat() if path else None
            stamp = (path, stat.st_mtime_ns, stat.st_size) if stat else (None,)
            if stamp == self._stamp:
                return
            if path is None:
                self._path = self._directory / (self._kind + ".log")
                self._edit.setProperty("amethyst_context_path", None)
                self._label.setToolTip("")
                content = self.tr(
                    "No {0} log is available for the latest launch.\n"
                    "Press Play to capture logs. Native and plain Wine launches "
                    "do not produce Proton logs.").format(self._kind.upper())
            else:
                with path.open("rb") as source:
                    source.seek(max(0, stat.st_size - _MAX_BYTES))
                    content = source.read(_MAX_BYTES).decode("utf-8", errors="replace")
                if stat.st_size > _MAX_BYTES:
                    content = self.tr("Showing the last 2 MiB.\n") + content
                self._path = path
                self._edit.setProperty("amethyst_context_path", str(path))
                self._label.setToolTip(str(path))
        except OSError as exc:
            stamp = None
            content = self.tr("Could not read log: {0}").format(exc)
        bar = self._edit.verticalScrollBar()
        at_end = bar.value() == bar.maximum()
        position = bar.value()
        self._original = _ANSI_ESCAPE.sub("", content)
        self._edit.blockSignals(True)
        self._edit.setPlainText(self._original)
        self._edit.blockSignals(False)
        bar.setValue(bar.maximum() if at_end else position)
        self._stamp = stamp
        if self._find.text():
            self._on_find(self._find.text())
