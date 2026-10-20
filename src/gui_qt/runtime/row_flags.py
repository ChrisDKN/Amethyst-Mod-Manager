from dataclasses import dataclass
from functools import lru_cache

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon


RowFlagsRole = Qt.UserRole + 100


@dataclass(frozen=True)
class RowFlag:
    key: str
    icon: str
    tooltip: str


def row_flags(index):
    return (index.data(RowFlagsRole) or ()) if index is not None else ()


def flag_tip(hit):
    return hit.tooltip if isinstance(hit, RowFlag) else None


def flag_icon(name, size, builtin):
    return _plugin_icon(name.icon) if isinstance(name, RowFlag) else builtin(name, size)


@lru_cache(maxsize=256)
def _plugin_icon(path):
    return QIcon(path)


def set_row_flags(model, flags, column):
    if flags == model._row_flags:
        return
    model._row_flags = flags
    if model.rowCount():
        model.dataChanged.emit(model.index(0, column),
                               model.index(model.rowCount() - 1, column),
                               [RowFlagsRole, Qt.DisplayRole])
    model._resort_if_key("flags")
