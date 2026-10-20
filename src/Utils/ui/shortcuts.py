from __future__ import annotations

from dataclasses import dataclass
from PySide6.QtCore import Qt, QKeyCombination, QT_TRANSLATE_NOOP
from PySide6.QtGui import QKeySequence


@dataclass(frozen=True)
class ShortcutDefinition:
    action_id: str
    label: str
    default: str


SHORTCUT_DEFINITIONS = (
    ShortcutDefinition("rename", QT_TRANSLATE_NOOP(
        "ShortcutEditor", "Rename selected mod or separator"), "F2"),
    ShortcutDefinition("refresh", QT_TRANSLATE_NOOP(
        "ShortcutEditor", "Refresh mod list"), "F5"),
    ShortcutDefinition("deploy", QT_TRANSLATE_NOOP(
        "ShortcutEditor", "Deploy mods"), "Ctrl+D"),
    ShortcutDefinition("install_mod", QT_TRANSLATE_NOOP(
        "ShortcutEditor", "Install a mod"), "Ctrl+M"),
    ShortcutDefinition("create_empty_mod", QT_TRANSLATE_NOOP(
        "ShortcutEditor", "Create an empty mod"), "Ctrl+N"),
    ShortcutDefinition("restore", QT_TRANSLATE_NOOP(
        "ShortcutEditor", "Restore game files"), "Ctrl+R"),
    ShortcutDefinition("open_settings", QT_TRANSLATE_NOOP(
        "ShortcutEditor", "Open Settings"), "Ctrl+S"),
    ShortcutDefinition("focus_search", QT_TRANSLATE_NOOP(
        "ShortcutEditor", "Focus search"), "Ctrl+F"),
    ShortcutDefinition("select_all", QT_TRANSLATE_NOOP(
        "ShortcutEditor", "Select all in the active group"), "Ctrl+A"),
    ShortcutDefinition("move_up", QT_TRANSLATE_NOOP(
        "ShortcutEditor", "Move selection up"), "Alt+Up"),
    ShortcutDefinition("move_down", QT_TRANSLATE_NOOP(
        "ShortcutEditor", "Move selection down"), "Alt+Down"),
    ShortcutDefinition("remove", QT_TRANSLATE_NOOP(
        "ShortcutEditor", "Remove selected mods"), "Delete"),
    ShortcutDefinition("toggle_selected", QT_TRANSLATE_NOOP(
        "ShortcutEditor", "Enable or disable selection"), "Enter"),
    ShortcutDefinition("scroll_top", QT_TRANSLATE_NOOP(
        "ShortcutEditor", "Scroll active list to the top"), "Home"),
    ShortcutDefinition("scroll_bottom", QT_TRANSLATE_NOOP(
        "ShortcutEditor", "Scroll active list to the bottom"), "End"),
    ShortcutDefinition("toggle_separators", QT_TRANSLATE_NOOP(
        "ShortcutEditor", "Expand or collapse all separators"), "Shift+E"),
    ShortcutDefinition("toggle_filters", QT_TRANSLATE_NOOP(
        "ShortcutEditor", "Show or hide active filters"), "Shift+F"),
    ShortcutDefinition("open_mod_page", QT_TRANSLATE_NOOP(
        "ShortcutEditor", "Open a mod's source page"), "Mouse 3"),
    ShortcutDefinition("browser_back", QT_TRANSLATE_NOOP(
        "ShortcutEditor", "Browser back"), "Mouse 4"),
    ShortcutDefinition("browser_forward", QT_TRANSLATE_NOOP(
        "ShortcutEditor", "Browser forward"), "Mouse 5"),
)

MOUSE_ACTION_IDS = (
    "open_mod_page", "browser_back", "browser_forward",
)

_EDITABLE_MODIFIERS = (
    Qt.ControlModifier | Qt.AltModifier |
    Qt.ShiftModifier | Qt.MetaModifier
)
_MAX_MOUSE_BUTTON = Qt.ExtraButton24.value
_RESERVED_KEYS = {
    0, Qt.Key_unknown.value, Qt.Key_Escape.value,
    Qt.Key_Control.value, Qt.Key_Shift.value, Qt.Key_Alt.value,
    Qt.Key_Meta.value, Qt.Key_AltGr.value,
}


def shortcut_from_parts(modifiers: int, key: int) -> str:
    """Return one portable Qt key sequence from modifier/key integer values."""
    if (key in _RESERVED_KEYS
            or modifiers & ~_EDITABLE_MODIFIERS.value):
        return ""
    try:
        combination = QKeyCombination(
            Qt.KeyboardModifier(modifiers), Qt.Key(key))
        return QKeySequence(combination).toString(QKeySequence.PortableText)
    except (TypeError, ValueError):
        return ""


def shortcut_parts(sequence: str) -> "tuple[int, int] | None":
    """Return ``(modifiers, key)`` for a valid single-stroke sequence."""
    try:
        parsed = QKeySequence.fromString(
            str(sequence), QKeySequence.PortableText)
        if parsed.isEmpty() or parsed.count() != 1:
            return None
        combination = parsed[0]
        modifiers = combination.keyboardModifiers().value
        key = combination.key().value
        if (key in _RESERVED_KEYS
                or modifiers & ~_EDITABLE_MODIFIERS.value):
            return None
        return modifiers, key
    except (TypeError, ValueError):
        return None


def normalise_shortcut(sequence: str) -> str:
    parts = shortcut_parts(sequence)
    return shortcut_from_parts(*parts) if parts is not None else ""


def shortcut_identity(sequence: str) -> "tuple[int, int] | None":
    """Comparable identity; Return and keypad Enter count as one shortcut."""
    parts = shortcut_parts(sequence)
    if parts is None:
        return None
    modifiers, key = parts
    if key in (Qt.Key_Return.value, Qt.Key_Enter.value):
        key = Qt.Key_Return.value
    return modifiers, key


def shortcut_key_text(key: int) -> str:
    return shortcut_from_parts(Qt.NoModifier.value, key)


def shortcut_modifier_text(modifiers: int) -> str:
    if not modifiers:
        return ""
    sequence = shortcut_from_parts(modifiers, Qt.Key_A.value)
    return sequence.rsplit("+", 1)[0] if "+" in sequence else ""


def mouse_shortcut_from_parts(modifiers: int, button: int) -> str:
    """Return a mouse binding, excluding the reserved left/right buttons."""
    if (button < Qt.MiddleButton.value or button > _MAX_MOUSE_BUTTON
            or button & (button - 1)
            or modifiers & ~_EDITABLE_MODIFIERS.value):
        return ""
    prefix = shortcut_modifier_text(modifiers)
    name = f"Mouse {button.bit_length()}"
    return f"{prefix}+{name}" if prefix else name


def mouse_shortcut_parts(sequence: str) -> "tuple[int, int] | None":
    head, marker, number = str(sequence).strip().rpartition("Mouse ")
    if not marker or not number.isdigit():
        return None
    mouse_number = int(number)
    if not 3 <= mouse_number <= _MAX_MOUSE_BUTTON.bit_length():
        return None
    modifiers = Qt.NoModifier.value
    if head:
        if not head.endswith("+"):
            return None
        parsed = shortcut_parts(f"{head}A")
        if parsed is None or parsed[1] != Qt.Key_A.value:
            return None
        modifiers = parsed[0]
    button = 1 << (mouse_number - 1)
    return ((modifiers, button)
            if mouse_shortcut_from_parts(modifiers, button) else None)


def mouse_shortcut_button_text(button: int) -> str:
    return mouse_shortcut_from_parts(Qt.NoModifier.value, button)


def binding_from_parts(kind: str, modifiers: int, input_value: int) -> str:
    if kind == "mouse":
        return mouse_shortcut_from_parts(modifiers, input_value)
    if kind == "keyboard":
        return shortcut_from_parts(modifiers, input_value)
    return ""


def binding_parts(sequence: str) -> "tuple[str, int, int] | None":
    mouse = mouse_shortcut_parts(sequence)
    if mouse is not None:
        return "mouse", *mouse
    keyboard = shortcut_parts(sequence)
    if keyboard is not None:
        return "keyboard", *keyboard
    return None


def normalise_binding(sequence: str) -> str:
    parts = binding_parts(sequence)
    return binding_from_parts(*parts) if parts is not None else ""


def binding_identity(sequence: str) -> "tuple[str, int, int] | None":
    parts = binding_parts(sequence)
    if parts is None:
        return None
    kind, modifiers, input_value = parts
    if (kind == "keyboard"
            and input_value in (Qt.Key_Return.value, Qt.Key_Enter.value)):
        input_value = Qt.Key_Return.value
    return kind, modifiers, input_value


def binding_input_text(kind: str, input_value: int) -> str:
    if kind == "mouse":
        return mouse_shortcut_button_text(input_value)
    if kind == "keyboard":
        return shortcut_key_text(input_value)
    return ""


DEFAULT_SHORTCUTS = {
    definition.action_id: normalise_binding(definition.default)
    for definition in SHORTCUT_DEFINITIONS
}


def plugin_shortcut_id(value):
    import re
    return isinstance(value, str) and re.fullmatch(
        r"plugin/[a-z0-9][a-z0-9_.-]{0,99}/[a-z0-9][a-z0-9_.-]{0,99}", value) is not None


def shortcut_definitions():
    from amethyst_plugins.catalog import discover
    plugins, _errors = discover(None)
    return (*SHORTCUT_DEFINITIONS, *(
        ShortcutDefinition(f"plugin/{plugin.id}/{command.id}",
                           f"Plugins · {plugin.name}: {command.label}", "")
        for plugin in plugins for command in plugin.commands))


def shortcut_defaults():
    return {item.action_id: normalise_binding(item.default) for item in shortcut_definitions()}


def _duplicate_groups(values: dict[str, str]) -> list[list[str]]:
    by_sequence: dict[tuple[str, int, int], list[str]] = {}
    for action_id, sequence in values.items():
        identity = binding_identity(sequence)
        if identity is not None:
            by_sequence.setdefault(identity, []).append(action_id)
    return [ids for ids in by_sequence.values() if len(ids) > 1]


def _validated_shortcuts(overrides: dict[str, str]) -> dict[str, str]:
    defaults = shortcut_defaults()
    values = dict(defaults)
    for action_id, raw in overrides.items():
        if action_id in values:
            sequence = normalise_binding(raw)
            if sequence:
                values[action_id] = sequence

    # A hand-edited or old config must not create ambiguous QShortcuts. Prefer
    # an action still using its default and roll colliding overrides back.
    for _attempt in range(len(values) + 1):
        groups = _duplicate_groups(values)
        if not groups:
            return values
        changed = False
        for action_ids in groups:
            for action_id in action_ids:
                if values[action_id] != defaults[action_id]:
                    values[action_id] = defaults[action_id]
                    changed = True
        if not changed:
            break
    return dict(defaults)


def load_shortcuts() -> dict[str, str]:
    """Load valid overrides on top of the built-in defaults."""
    from Utils.ui.config import load_shortcut_overrides

    try:
        overrides = load_shortcut_overrides()
    except Exception:
        overrides = {}
    return _validated_shortcuts(overrides)


