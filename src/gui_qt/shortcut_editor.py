"""Keyboard and mouse shortcut editor embedded in Settings."""

from __future__ import annotations

from itertools import combinations

from PySide6.QtCore import QCoreApplication, QEvent, Qt, Signal
from PySide6.QtWidgets import (
    QComboBox, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QVBoxLayout,
)

from gui_qt.shortcuts import (
    DEFAULT_SHORTCUTS, MOUSE_ACTION_IDS, SHORTCUT_DEFINITIONS,
    apply_shortcuts, binding_from_parts, binding_identity,
    binding_input_text, binding_parts, load_shortcuts,
    mouse_shortcut_from_parts, shortcut_modifier_text,
)
from gui_qt.theme_qt import _c, active_palette
from gui_qt.wheel_guard import no_wheel
from Utils.ui import config as uc


_MODIFIER_KEYS = {
    Qt.Key_Control.value, Qt.Key_Shift.value, Qt.Key_Alt.value,
    Qt.Key_Meta.value, Qt.Key_AltGr.value,
}
_MODIFIER_FLAGS = (
    Qt.ControlModifier.value, Qt.AltModifier.value,
    Qt.ShiftModifier.value, Qt.MetaModifier.value,
)


def _modifier_values() -> list[int]:
    values = [Qt.NoModifier.value]
    for size in range(1, len(_MODIFIER_FLAGS) + 1):
        for flags in combinations(_MODIFIER_FLAGS, size):
            value = 0
            for flag in flags:
                value |= flag
            values.append(value)
    return values


class _BindingInput(QLineEdit):
    captured = Signal(str, int)

    def __init__(self, kind: str, input_value: int, parent=None):
        super().__init__(parent)
        self._kind = kind
        self._input_value = input_value
        self._capturing = False
        self.setReadOnly(True)
        self.setAlignment(Qt.AlignCenter)
        self.setFixedWidth(105)
        self.setCursor(Qt.PointingHandCursor)
        self.setContextMenuPolicy(Qt.NoContextMenu)
        self.setAccessibleName(self.tr("Shortcut key or mouse button"))
        self.setToolTip(self.tr(
            "Click here, then press a keyboard key, Mouse 3, or a side "
            "button. Escape cancels; left and right click are reserved."))
        self.set_binding(kind, input_value)

    def set_binding(self, kind: str, input_value: int) -> None:
        self._capturing = False
        self._kind = kind
        self._input_value = input_value
        self.setText(binding_input_text(kind, input_value))

    def _begin_capture(self) -> None:
        self._capturing = True
        self.setText(self.tr("Press key"))

    def _cancel_capture(self) -> None:
        self.set_binding(self._kind, self._input_value)

    def mousePressEvent(self, event):
        button = event.button().value
        if not self._capturing and button == Qt.LeftButton.value:
            self.setFocus(Qt.MouseFocusReason)
            self._begin_capture()
            event.accept()
            return
        if self._capturing:
            if mouse_shortcut_from_parts(Qt.NoModifier.value, button):
                self._capturing = False
                self.captured.emit("mouse", button)
            event.accept()
            return
        event.accept()

    def event(self, event):
        if self._capturing and event.type() == QEvent.ShortcutOverride:
            event.accept()
            return True
        if self._capturing and event.type() == QEvent.KeyPress:
            key = event.key()
            if key == Qt.Key_Escape.value:
                self._cancel_capture()
            elif (not event.isAutoRepeat()
                  and key not in _MODIFIER_KEYS
                  and key != Qt.Key_unknown.value):
                self._capturing = False
                self.captured.emit("keyboard", key)
            event.accept()
            return True
        return super().event(event)

    def focusOutEvent(self, event):
        if self._capturing:
            self._cancel_capture()
        super().focusOutEvent(event)


class ShortcutEditor(QGroupBox):
    def __init__(self, window, parent=None):
        super().__init__(QCoreApplication.translate(
            "ShortcutEditor", "Keyboard and mouse shortcuts"), parent)
        self._window = window
        self._values = load_shortcuts()
        self._rows = {}
        self._names = {
            definition.action_id: QCoreApplication.translate(
                "ShortcutEditor", definition.label)
            for definition in SHORTCUT_DEFINITIONS
        }
        palette = active_palette()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 8)
        outer.setSpacing(10)

        intro = QLabel(self.tr(
            "Click a binding, then press a key, Mouse 3, or a side button. "
            "Escape cancels. Left and right mouse buttons are reserved; "
            "modifiers are changed separately."))
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color:{_c(palette, 'TEXT_DIM')};")
        top = QHBoxLayout()
        copy = QVBoxLayout()
        copy.setSpacing(3)
        copy.addWidget(intro)
        self._status = QLabel()
        self._status.setWordWrap(True)
        self._status.hide()
        copy.addWidget(self._status)
        top.addLayout(copy, 1)
        reset = QPushButton(self.tr("Reset to defaults"))
        reset.setCursor(Qt.PointingHandCursor)
        reset.clicked.connect(self._reset_defaults)
        top.addWidget(reset, 0, Qt.AlignTop)
        outer.addLayout(top)

        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(7)
        grid.setColumnStretch(0, 1)
        for column, label in enumerate((
                self.tr("Action"), self.tr("Modifier"),
                self.tr("Key / button"), self.tr("Default"))):
            header = QLabel(label)
            header.setStyleSheet(
                f"color:{_c(palette, 'TEXT_DIM')}; font-weight:600;")
            if column:
                header.setAlignment(Qt.AlignCenter)
            grid.addWidget(header, 0, column)

        reset_qss = f"""
            QPushButton {{
                background:{_c(palette, 'BG_ROW')};
                color:{_c(palette, 'TEXT_MAIN')};
                border:1px solid {_c(palette, 'BORDER')};
                border-radius:4px;
            }}
            QPushButton:hover {{
                color:{_c(palette, 'TEXT_MAIN')};
                border-color:{_c(palette, 'ACCENT')};
            }}
        """
        modifier_values = _modifier_values()
        row = 1
        for definition in SHORTCUT_DEFINITIONS:
            action_id = definition.action_id
            if action_id == MOUSE_ACTION_IDS[0]:
                heading = QLabel(self.tr(
                    "Mouse actions (mouse buttons by default)"))
                heading.setContentsMargins(0, 8, 0, 1)
                heading.setStyleSheet(
                    f"color:{_c(palette, 'TEXT_MAIN')}; font-weight:600;")
                grid.addWidget(heading, row, 0, 1, 4)
                row += 1

            parts = (binding_parts(self._values[action_id])
                     or binding_parts(DEFAULT_SHORTCUTS[action_id]))
            kind, modifiers, input_value = parts
            action = QLabel(self._names[action_id])
            action.setWordWrap(True)

            combo = QComboBox()
            combo.setFixedWidth(145)
            combo.addItem(self.tr("None"), Qt.NoModifier.value)
            for value in modifier_values[1:]:
                combo.addItem(shortcut_modifier_text(value), value)
            no_wheel(combo)

            binding_input = _BindingInput(kind, input_value)
            default = QPushButton(f"↺ {DEFAULT_SHORTCUTS[action_id]}")
            default.setMinimumWidth(105)
            default.setCursor(Qt.PointingHandCursor)
            default.setStyleSheet(reset_qss)
            default.setToolTip(self.tr("Reset {0} to {1}").format(
                self._names[action_id], DEFAULT_SHORTCUTS[action_id]))

            grid.addWidget(action, row, 0)
            grid.addWidget(combo, row, 1)
            grid.addWidget(binding_input, row, 2)
            grid.addWidget(default, row, 3)
            self._rows[action_id] = (combo, binding_input)
            self._set_combo_value(combo, modifiers)

            combo.currentIndexChanged.connect(
                lambda _index, aid=action_id: self._modifier_changed(aid))
            binding_input.captured.connect(
                lambda kind, pressed, aid=action_id:
                self._input_changed(aid, kind, pressed))
            default.clicked.connect(
                lambda _checked=False, aid=action_id:
                self._reset_action(aid))
            row += 1

        outer.addLayout(grid)

    @staticmethod
    def _set_combo_value(combo: QComboBox, value: int) -> None:
        index = combo.findData(value)
        combo.blockSignals(True)
        combo.setCurrentIndex(max(0, index))
        combo.blockSignals(False)

    def _sync_row(self, action_id: str) -> None:
        parts = binding_parts(self._values[action_id])
        if parts is None:
            return
        kind, modifiers, input_value = parts
        combo, binding_input = self._rows[action_id]
        self._set_combo_value(combo, modifiers)
        binding_input.set_binding(kind, input_value)

    def _modifier_changed(self, action_id: str) -> None:
        parts = binding_parts(self._values[action_id])
        if parts is None:
            return
        kind, _old_modifiers, input_value = parts
        combo, _binding_input = self._rows[action_id]
        self._change(
            action_id, kind, int(combo.currentData()), input_value)

    def _input_changed(self, action_id: str,
                       kind: str, input_value: int) -> None:
        combo, _binding_input = self._rows[action_id]
        self._change(
            action_id, kind, int(combo.currentData()), input_value)

    def _change(self, action_id: str, kind: str,
                modifiers: int, input_value: int) -> bool:
        sequence = binding_from_parts(kind, modifiers, input_value)
        identity = binding_identity(sequence)
        if not sequence or identity is None:
            self._sync_row(action_id)
            self._set_status(self.tr(
                "That key or mouse button cannot be used as a shortcut."),
                error=True)
            return False

        for other_id, other_sequence in self._values.items():
            if (other_id != action_id
                    and binding_identity(other_sequence) == identity):
                self._sync_row(action_id)
                self._set_status(self.tr(
                    "{0} is already assigned to {1}.").format(
                        sequence, self._names[other_id]), error=True)
                return False

        updated = dict(self._values)
        updated[action_id] = sequence
        overrides = {
            aid: value for aid, value in updated.items()
            if value != DEFAULT_SHORTCUTS[aid]
        }
        try:
            uc.save_shortcut_overrides(overrides)
        except Exception as exc:
            self._sync_row(action_id)
            self._set_status(
                self.tr("Failed to save shortcut: {0}").format(exc), error=True)
            return False

        self._values = updated
        self._sync_row(action_id)
        apply_shortcuts(self._window, updated)
        self._set_status("")
        return True

    def _reset_action(self, action_id: str) -> None:
        parts = binding_parts(DEFAULT_SHORTCUTS[action_id])
        if parts is not None and self._change(action_id, *parts):
            self._set_status(self.tr("{0} reset to {1}.").format(
                self._names[action_id], DEFAULT_SHORTCUTS[action_id]))

    def _reset_defaults(self):
        try:
            uc.save_shortcut_overrides({})
        except Exception as exc:
            self._set_status(
                self.tr("Failed to reset shortcuts: {0}").format(exc),
                error=True)
            return
        self._values = dict(DEFAULT_SHORTCUTS)
        for action_id in self._rows:
            self._sync_row(action_id)
        apply_shortcuts(self._window, self._values)
        self._set_status(self.tr("Shortcuts reset to defaults."))

    def _set_status(self, text: str, error: bool = False) -> None:
        self._status.setText(text)
        self._status.setVisible(bool(text))
        colour = "TEXT_WARN" if error else "TEXT_OK"
        self._status.setStyleSheet(
            f"color:{_c(active_palette(), colour)};")
