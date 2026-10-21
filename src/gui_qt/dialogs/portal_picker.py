from __future__ import annotations

from PySide6.QtCore import QObject, Qt, Signal, Slot

from gui_qt.runtime.safe_emit import safe_emit
from Utils.ui import portal


class _Request(QObject):
    completed = Signal(object)

    def __init__(self, parent, callback):
        super().__init__(parent)
        self.callback = callback
        self.completed.connect(self._deliver, Qt.QueuedConnection)

    @Slot(object)
    def _deliver(self, value):
        parent = self.parent()
        callback, self.callback = self.callback, None
        self.deleteLater()
        if callback is not None and not any(getattr(parent, key, False) is True
                                            for key in ("_done", "_closing", "_closed")):
            callback(value)


def _callback(parent, callback):
    request = _Request(parent, callback)
    completed = request.completed
    return lambda value: safe_emit(completed, value)


def pick_file(parent, title, callback, filters=None):
    portal.pick_file(title, _callback(parent, callback), filters=filters)


def pick_folder(parent, title, callback, *, direct_access=False):
    portal.pick_folder(title, _callback(parent, callback), direct_access=direct_access)


def pick_save_file(parent, title, callback, *, current_name="manifest.json", filters=None):
    portal.pick_save_file(title, _callback(parent, callback), current_name=current_name, filters=filters)
