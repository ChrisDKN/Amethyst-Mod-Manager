"""
Configurable keyboard and mouse shortcuts for the Qt main window.

Port of the Tk ``src/gui/shortcuts.py`` - same behaviour, idiomatic Qt.

Default bindings:
    F2              Rename the selected mod or separator (modlist panel)
    F5              Refresh the modlist (fires even in a text field)
    Delete          Remove selected mod(s) (modlist panel)
    Return/Enter    Toggle enable/disable for selected mods/plugins
    Home            Scroll active list panel to the top
    End             Scroll active list panel to the bottom
    Ctrl+F          Focus the active panel's search bar (fires even in a field)
    Ctrl+A          Select all mods in the active separator (modlist), or all
                    plugins (plugin panel)
    Ctrl+D          Deploy
    Ctrl+M          Install mod
    Ctrl+N          Create an empty mod at the top of the modlist
    Ctrl+R          Restore
    Ctrl+S          Open Settings
    Alt+Up          Move selected mods/plugins/separators up
    Alt+Down        Move selected mods/plugins/separators down
    Shift+E         Expand/collapse all separators (modlist)
    Shift+F         Toggle the active panel's filter side panel
    Mouse 3         Open the clicked mod's source page
    Mouse 4         Browser back
    Mouse 5         Browser forward

Alt+Up/Down, F2, Ctrl+A, Home/End, Shift+F and the movers dispatch to whichever
panel (modlist or plugin) was most recently interacted with (focus or mouse).
Shortcuts are suppressed while a text-input widget has focus (except F5 and
Ctrl+F) and while an overlay / modal is open - mirroring the Tk guard.
"""

from __future__ import annotations

from dataclasses import dataclass

# Crash-proof diagnostic prints (Flatpak stdout can raise BrokenPipeError and
# kill worker threads). See Utils.app_log.safe_print.
from Utils.app_log import safe_print as print  # noqa: A004

from PySide6.QtCore import (
    Qt, QObject, QEvent, QItemSelection, QItemSelectionModel,
    QKeyCombination, QT_TRANSLATE_NOOP,
)
from PySide6.QtGui import QShortcut, QKeySequence
from PySide6.QtWidgets import (
    QApplication, QLineEdit, QPlainTextEdit, QTextEdit,
    QAbstractSpinBox, QComboBox, QWidget,
)


# ---------------------------------------------------------------------------
# Definitions and persistence
# ---------------------------------------------------------------------------

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


def _duplicate_groups(values: dict[str, str]) -> list[list[str]]:
    by_sequence: dict[tuple[str, int, int], list[str]] = {}
    for action_id, sequence in values.items():
        identity = binding_identity(sequence)
        if identity is not None:
            by_sequence.setdefault(identity, []).append(action_id)
    return [ids for ids in by_sequence.values() if len(ids) > 1]


def _validated_shortcuts(overrides: dict[str, str]) -> dict[str, str]:
    values = dict(DEFAULT_SHORTCUTS)
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
                if values[action_id] != DEFAULT_SHORTCUTS[action_id]:
                    values[action_id] = DEFAULT_SHORTCUTS[action_id]
                    changed = True
        if not changed:
            break
    return dict(DEFAULT_SHORTCUTS)


def load_shortcuts() -> dict[str, str]:
    """Load valid overrides on top of the built-in defaults."""
    from Utils.ui.config import load_shortcut_overrides

    try:
        overrides = load_shortcut_overrides()
    except Exception:
        overrides = {}
    return _validated_shortcuts(overrides)


_active_shortcuts: "dict[str, str] | None" = None


def current_shortcuts() -> dict[str, str]:
    global _active_shortcuts
    if _active_shortcuts is None:
        _active_shortcuts = load_shortcuts()
    return dict(_active_shortcuts)


def shortcut_text(action_id: str) -> str:
    return current_shortcuts().get(action_id, "")


def binding_matches_mouse(action_id: str, event) -> bool:
    try:
        button = event.button().value
        modifiers = (event.modifiers() & _EDITABLE_MODIFIERS).value
    except (AttributeError, TypeError, ValueError):
        return False
    parts = binding_parts(current_shortcuts().get(action_id, ""))
    return parts == ("mouse", modifiers, button)


def _binding_sequences(action_id: str, sequence: str) -> list[str]:
    """Expand the shared Return/Enter action to both physical Enter keys."""
    parts = shortcut_parts(sequence)
    if parts is None:
        return []
    if action_id != "toggle_selected":
        return [sequence]
    modifiers, key = parts
    if key not in (Qt.Key_Return.value, Qt.Key_Enter.value):
        return [sequence]
    return [shortcut_from_parts(modifiers, Qt.Key_Return.value),
            shortcut_from_parts(modifiers, Qt.Key_Enter.value)]


def apply_shortcuts(win, values: "dict[str, str] | None" = None) -> None:
    """Apply saved or supplied bindings to every live shortcut handler."""
    global _active_shortcuts
    if values is None:
        values = load_shortcuts()
    values = _validated_shortcuts(values)
    _active_shortcuts = dict(values)
    for action_id, sequence in values.items():
        expanded = _binding_sequences(action_id, sequence)
        for shortcut, variant in getattr(
                win, "_shortcut_bindings", {}).get(action_id, []):
            key = expanded[variant] if variant < len(expanded) else ""
            shortcut.setKey(QKeySequence(key))
    win._active_shortcut_keys = values
    tracker = getattr(win, "_shortcut_state_tracker", None)
    if tracker is not None:
        tracker.sync(QApplication.focusWidget())
    from gui_qt.mouse_navigation import refresh_navigation_shortcuts
    refresh_navigation_shortcuts()


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------

_TEXT_WIDGETS = (QLineEdit, QPlainTextEdit, QTextEdit, QAbstractSpinBox, QComboBox)


def _focus_is_text_input(_win) -> bool:
    """True when a text-entry widget has focus (typing should not be hijacked).

    The modlist/plugin QTreeViews are not text widgets, so list-focused
    shortcuts still fire."""
    return isinstance(QApplication.focusWidget(), _TEXT_WIDGETS)


def _overlay_open(win) -> bool:
    """True when a modal or a borderless overlay is up (the Qt analogue of the
    Tk "focus is inside a dialog" check)."""
    if getattr(win, "_filegraph_loading", False):
        return True
    if QApplication.activeModalWidget() is not None:
        return True
    try:
        for w in win.findChildren(QWidget):
            if (w.isVisible()
                    and (type(w).__name__.endswith("Overlay")
                         or w.objectName() == "OverlayBackdrop")):
                return True
    except Exception:
        pass
    return False


def _guard(win, fn):
    def _handler():
        if _focus_is_text_input(win) or _overlay_open(win):
            return
        fn(win)
    return _handler


def _unguarded(win, fn):
    """Fires even when a text input has focus (F5, Ctrl+F) - still suppressed
    while an overlay/modal is open."""
    def _handler():
        if _overlay_open(win):
            return
        fn(win)
    return _handler


class _ReturnOverride(QObject):
    """Hands Return/Enter back to text inputs and overlays.

    The list-panel Return/Enter shortcuts are scoped to their views, but keep
    this as a second line of defence for any text editor parented inside a list
    view and against a future shortcut-context change. Accepting the override
    whenever the guard would refuse the shortcut delivers the key press to the
    focused widget instead."""

    def __init__(self, win):
        super().__init__(win)
        self._win = win

    def eventFilter(self, obj, event):
        if (event.type() == QEvent.ShortcutOverride
                and event.key() in (Qt.Key_Return, Qt.Key_Enter)
                and (_focus_is_text_input(self._win)
                     or _overlay_open(self._win))):
            event.accept()
            return True
        return False


class _ShortcutStateTracker(QObject):
    """Keep window-level Ctrl+S inactive while the text editor owns it."""

    def __init__(self, win):
        super().__init__(win)
        self._win = win

    @staticmethod
    def _inside_text_editor(widget) -> bool:
        while isinstance(widget, QWidget):
            if widget.objectName() == "TextEditor":
                return True
            widget = widget.parentWidget()
        return False

    def sync(self, widget) -> None:
        inside = self._inside_text_editor(widget)
        save_key = shortcut_identity(
            QKeySequence(QKeySequence.Save).toString(
                QKeySequence.PortableText))
        for entries in getattr(self._win, "_shortcut_bindings", {}).values():
            for shortcut, _variant in entries:
                if shortcut.context() != Qt.WindowShortcut:
                    continue
                is_save = shortcut_identity(
                    shortcut.key().toString(QKeySequence.PortableText)) == save_key
                shortcut.setEnabled(not (inside and is_save))

    def eventFilter(self, obj, event):
        if event.type() == QEvent.FocusIn and isinstance(obj, QWidget):
            self.sync(obj)
        return False


# ---------------------------------------------------------------------------
# Active-panel routing
# ---------------------------------------------------------------------------

def _active_panel(win) -> str:
    """"mod" or "plugin" - whichever list the user last interacted with."""
    which = getattr(win, "_last_list_panel", "mod")
    if which == "plugin" and getattr(win, "_plugin_view", None) is not None:
        return "plugin"
    return "mod"


class _PanelTracker(QObject):
    """Event filter on both views: records the last-interacted list panel so
    keyboard shortcuts route to it (mirrors Tk's _last_list_panel)."""

    def __init__(self, win):
        super().__init__(win)
        self._win = win

    def eventFilter(self, obj, event):
        et = event.type()
        if et in (QEvent.FocusIn, QEvent.MouseButtonPress):
            win = self._win
            mv = getattr(win, "_modlist_view", None)
            pv = getattr(win, "_plugin_view", None)
            # obj is the view or its viewport.
            if mv is not None and (obj is mv or obj is mv.viewport()):
                win._last_list_panel = "mod"
            elif pv is not None and (obj is pv or obj is pv.viewport()):
                win._last_list_panel = "plugin"
        return False


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def _selected_rows(view) -> list[int]:
    return sorted({i.row() for i in view.selectionModel().selectedRows()})


def _rename_selected(win):
    view = getattr(win, "_modlist_view", None)
    if view is None:
        return
    rows = _selected_rows(view)
    if not rows:
        return
    from gui_qt.modlist_menu import _rename
    _rename(view, view.model(), rows[0])


def _refresh_modlist(win):
    if hasattr(win, "_on_refresh_modlist"):
        win._on_refresh_modlist()


def _deploy(win):
    if hasattr(win, "_on_deploy"):
        win._on_deploy()


def _restore(win):
    if hasattr(win, "_on_restore"):
        win._on_restore()


def _install_mod(win):
    if hasattr(win, "_on_install_mod"):
        win._on_install_mod()


def _create_empty_mod(win):
    """Create an empty mod at the top of the modlist body (just inside the
    Overwrite boundary) - the keyboard route to the context menu's 'Create an
    empty mod below'. No-ops without a staging dir, like the menu entry."""
    view = getattr(win, "_modlist_view", None)
    if view is None or getattr(view, "staging_dir", None) is None:
        return
    from gui_qt.modlist_menu import _create_empty_mod_at_boundary
    _create_empty_mod_at_boundary(view, view.model(), top=True)


def _open_settings(win):
    if hasattr(win, "_open_settings_modal"):
        win._open_settings_modal()


def _toggleable_rows(view) -> list[int]:
    """Selected rows that can be enable/disable-toggled or removed: non-
    separator, non-pinned, non-locked mods."""
    from gui_qt.modlist_model import _PINNED_NAMES
    m = view.model()
    out = []
    for r in _selected_rows(view):
        e = m.entry(r)
        if e.is_separator or e.name in _PINNED_NAMES or e.locked:
            continue
        out.append(r)
    return out


def _toggle_selected(win):
    """Flip enable/disable on the selection. Mixed selections all move to a
    single state (inverse of the first row's) in one batch (per user decision)."""
    view = getattr(win, "_modlist_view", None)
    if view is None:
        return
    rows = _toggleable_rows(view)
    if not rows:
        return
    m = view.model()
    target = not m.entry(rows[0]).enabled
    m.set_rows_enabled(rows, target)


def _toggle_selected_plugins(win):
    view = getattr(win, "_plugin_view", None)
    if view is None:
        return
    m = view.model()
    rows = [r for r in _selected_rows(view)
            if 0 <= r < m.rowCount() and not m.row(r).vanilla]
    if not rows:
        return
    target = not m.row(rows[0]).enabled
    from gui_qt.plugin_menu import _set_enabled
    _set_enabled(view, rows, target)


def _toggle_selected_active(win):
    if _active_panel(win) == "plugin":
        _toggle_selected_plugins(win)
    else:
        _toggle_selected(win)


def _delete_selected(win):
    """Remove the selected mods after ONE batch confirm (per user decision).

    Delegates to the context menu's remove handlers so the keyboard path gets
    the SAME guards: Profile Group entries route through the group-aware
    removal (which also deletes the owning member's copy - a plain remove
    would only drop the group's link and the mod would return on the next
    reconcile), and mods owned by a LOCKED member profile are refused."""
    view = getattr(win, "_modlist_view", None)
    if view is None:
        return
    rows = _toggleable_rows(view)
    if not rows:
        return
    from gui_qt.modlist_menu import _remove, _remove_mods_multi
    m = view.model()
    if len(rows) == 1:
        _remove(view, m, rows[0])
    else:
        _remove_mods_multi(view, m, rows)


def _scroll_top(win):
    view = _active_view(win)
    if view is not None:
        view.scrollToTop()


def _scroll_bottom(win):
    view = _active_view(win)
    if view is not None:
        view.scrollToBottom()


def _toggle_all_seps(win):
    if hasattr(win, "_on_toggle_collapse_all"):
        win._on_toggle_collapse_all()


def _toggle_filters(win):
    if _active_panel(win) == "plugin":
        if hasattr(win, "_toggle_plugin_filters"):
            win._toggle_plugin_filters()
    else:
        if hasattr(win, "_toggle_modlist_filters"):
            win._toggle_modlist_filters()


def _focus_search(win):
    edit = None
    if _active_panel(win) == "plugin":
        pe = getattr(win, "_plugins_search", None)
        if pe is not None and pe.isVisible():
            edit = pe
    if edit is None:
        edit = getattr(win, "_modlist_search", None)
    if edit is None:
        return
    edit.setFocus()
    edit.selectAll()


def _active_view(win):
    if _active_panel(win) == "plugin":
        return getattr(win, "_plugin_view", None)
    return getattr(win, "_modlist_view", None)


def _open_selected_mod_page(win):
    view = getattr(win, "_modlist_view", None)
    if view is None:
        return
    rows = _selected_rows(view)
    if rows:
        row = rows[0]
    else:
        current = view.currentIndex()
        if not current.isValid():
            return
        row = current.row()
    if hasattr(view, "_open_source_page"):
        view._open_source_page(row)


# ---- Ctrl+A: select all in separator / all plugins ------------------------

def _apply_row_selection(view, rows) -> None:
    rows = sorted(rows)
    if not rows:
        return
    m = view.model()
    sm = view.selectionModel()
    sel = QItemSelection()
    last = m.columnCount() - 1
    for r in rows:
        sel.select(m.index(r, 0), m.index(r, last))
    sm.select(sel, QItemSelectionModel.ClearAndSelect | QItemSelectionModel.Rows)
    sm.setCurrentIndex(m.index(rows[0], 0),
                       QItemSelectionModel.NoUpdate)


def _select_all(win):
    if _active_panel(win) == "plugin":
        view = getattr(win, "_plugin_view", None)
        if view is None:
            return
        _apply_row_selection(view, view._visible_rows())
        return

    view = getattr(win, "_modlist_view", None)
    if view is None:
        return
    m = view.model()
    from gui_qt.modlist_model import _PINNED_NAMES
    visible = set(view._visible_rows())
    if not visible:
        return

    sel = _selected_rows(view)
    anchor = sel[0] if sel else min(visible)

    # Walk up to the owning (non-pinned) separator, if any is visible.
    sep_row = -1
    e = m.entry(anchor)
    if e.is_separator and e.name not in _PINNED_NAMES and anchor in visible:
        sep_row = anchor
    else:
        for i in range(anchor - 1, -1, -1):
            ei = m.entry(i)
            if ei.is_separator and ei.name not in _PINNED_NAMES:
                if i in visible:
                    sep_row = i
                break

    if sep_row >= 0:
        start = sep_row + 1
    else:
        # No owning separator: the anchor is in the implicit group of mods
        # above the first separator, so scope to the top of the list.
        start = 0
    end = m.rowCount()
    for i in range(start, m.rowCount()):
        ei = m.entry(i)
        if ei.is_separator and ei.name not in _PINNED_NAMES:
            end = i
            break

    rows = [r for r in range(start, end)
            if r in visible
            and not m.entry(r).is_separator
            and m.entry(r).name not in _PINNED_NAMES]
    _apply_row_selection(view, rows)


# ---- Alt+Up / Alt+Down: move selection --------------------------------------

def _move_up(win):
    if _active_panel(win) == "plugin":
        _move_plugins(win, -1)
    else:
        _move_modlist(win, -1)


def _move_down(win):
    if _active_panel(win) == "plugin":
        _move_plugins(win, +1)
    else:
        _move_modlist(win, +1)


def _move_modlist(win, direction: int):
    view = getattr(win, "_modlist_view", None)
    if view is None:
        return
    m = view.model()

    # A non-priority column sort blocks row moves - clear it first (drag parity).
    key, _asc = m.sort_state()
    if key and not m.reverse_mode_active:
        view._apply_sort(-1, None, True)

    sel = _selected_rows(view)
    if not sel:
        return
    block = view._drag_block_for(sel[0])
    if not block:
        return
    block = sorted(block)
    # move_block / move_block_display require a contiguous block.
    if block[-1] - block[0] != len(block) - 1:
        return
    first, last = block[0], block[-1]

    vis = view._visible_rows()
    if direction < 0:
        prev = [r for r in vis if r < first]
        if not prev:
            return
        dest = prev[-1]
    else:
        nxt = [r for r in vis if r > last]
        if not nxt:
            return
        dest = nxt[0] + 1

    if m.reverse_mode_active:
        hidden = {r for r in range(m.rowCount())
                  if view.isRowHidden(r, view.rootIndex())}
        moved = m.move_block_display(block, dest, hidden=hidden)
    else:
        moved = m.move_block(block, dest)
    if moved:
        view._apply_separator_spanning()
        view.apply_collapse()


def _move_plugins(win, direction: int):
    view = getattr(win, "_plugin_view", None)
    if view is None:
        return
    m = view.model()
    sel = sorted({i.row() for i in view.selectionModel().selectedRows()})
    if not sel:
        return
    block = view._drag_block_for(sel[0])
    if not block:
        return
    block = sorted(block)
    if block[-1] - block[0] != len(block) - 1:
        return
    first, last = block[0], block[-1]

    vis = view._visible_rows()
    if direction < 0:
        prev = [r for r in vis if r < first]
        if not prev:
            return
        dest = prev[-1]
    else:
        nxt = [r for r in vis if r > last]
        if not nxt:
            return
        dest = nxt[0] + 1
    m.move_rows(block, dest)


_MOUSE_ACTION_HANDLERS = {
    "rename": _rename_selected,
    "refresh": _refresh_modlist,
    "deploy": _deploy,
    "install_mod": _install_mod,
    "create_empty_mod": _create_empty_mod,
    "restore": _restore,
    "open_settings": _open_settings,
    "focus_search": _focus_search,
    "select_all": _select_all,
    "move_up": _move_up,
    "move_down": _move_down,
    "remove": _delete_selected,
    "toggle_selected": _toggle_selected_active,
    "scroll_top": _scroll_top,
    "scroll_bottom": _scroll_bottom,
    "toggle_separators": _toggle_all_seps,
    "toggle_filters": _toggle_filters,
}
_TEXT_INPUT_ALLOWED_ACTIONS = {"refresh", "focus_search"}


class _MouseShortcutDispatcher(QObject):
    """Dispatch non-contextual mouse bindings within the main window."""

    def __init__(self, win):
        super().__init__(win)
        self._win = win

    def _track_panel(self, watched: QWidget) -> None:
        win = self._win
        for panel, attr in (("mod", "_modlist_view"),
                            ("plugin", "_plugin_view")):
            view = getattr(win, attr, None)
            if (view is not None
                    and (watched is view or view.isAncestorOf(watched))):
                win._last_list_panel = panel
                return

    def eventFilter(self, watched, event):
        if (event.type() != QEvent.MouseButtonPress
                or not isinstance(watched, QWidget)):
            return False
        win = self._win
        if watched is not win and not win.isAncestorOf(watched):
            return False
        if _overlay_open(win):
            return False

        values = getattr(win, "_active_shortcut_keys", None)
        if values is None:
            values = current_shortcuts()
        try:
            button = event.button().value
            modifiers = (event.modifiers() & _EDITABLE_MODIFIERS).value
        except (AttributeError, TypeError, ValueError):
            return False
        identity = ("mouse", modifiers, button)
        for action_id, handler in _MOUSE_ACTION_HANDLERS.items():
            if binding_identity(values.get(action_id, "")) != identity:
                continue
            if (_focus_is_text_input(win)
                    and action_id not in _TEXT_INPUT_ALLOWED_ACTIONS):
                return False
            self._track_panel(watched)
            handler(win)
            event.accept()
            return True
        return False


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register_shortcuts(win) -> None:
    """Install configurable shortcuts and panel tracking on the window."""
    win._last_list_panel = "mod"

    tracker = _PanelTracker(win)
    win._panel_tracker = tracker

    override = _ReturnOverride(win)
    win._return_override = override
    QApplication.instance().installEventFilter(override)
    for attr in ("_modlist_view", "_plugin_view"):
        view = getattr(win, attr, None)
        if view is not None:
            view.installEventFilter(tracker)
            view.viewport().installEventFilter(tracker)

    shortcuts = getattr(win, "_shortcuts", None)
    if shortcuts is None:
        shortcuts = win._shortcuts = []
    configured = load_shortcuts()
    bindings = win._shortcut_bindings = {}

    def make_shortcut(seq, fn, guarded=True, *, parent=None,
                      context=Qt.WindowShortcut, auto_repeat=True):
        s = QShortcut(QKeySequence(seq), parent or win)
        s.setContext(context)
        s.setAutoRepeat(auto_repeat)
        s.activated.connect((_guard if guarded else _unguarded)(win, fn))
        shortcuts.append(s)
        return s

    def sc(action_id, fn, guarded=True, *, parent=None,
           context=Qt.WindowShortcut, auto_repeat=True, variant=0):
        expanded = _binding_sequences(action_id, configured[action_id])
        seq = expanded[variant] if variant < len(expanded) else ""
        shortcut = make_shortcut(
            seq, fn, guarded, parent=parent, context=context,
            auto_repeat=auto_repeat)
        bindings.setdefault(action_id, []).append((shortcut, variant))
        return shortcut

    sc("rename", _rename_selected)
    sc("refresh", _refresh_modlist, guarded=False)
    sc("deploy", _deploy)
    sc("install_mod", _install_mod, auto_repeat=False)
    sc("create_empty_mod", _create_empty_mod, auto_repeat=False)
    sc("restore", _restore)
    sc("open_settings", _open_settings, auto_repeat=False)
    sc("focus_search", _focus_search, guarded=False)
    sc("select_all", _select_all)
    sc("move_up", _move_up)
    sc("move_down", _move_down)
    sc("remove", _delete_selected)
    mod_view = getattr(win, "_modlist_view", None)
    if mod_view is not None:
        for variant in (0, 1):
            sc("toggle_selected", _toggle_selected, parent=mod_view,
               context=Qt.WidgetWithChildrenShortcut, auto_repeat=False,
               variant=variant)
        sc("open_mod_page", _open_selected_mod_page, parent=mod_view,
           context=Qt.WidgetWithChildrenShortcut, auto_repeat=False)
    plugin_view = getattr(win, "_plugin_view", None)
    if plugin_view is not None:
        for variant in (0, 1):
            sc("toggle_selected", _toggle_selected_plugins, parent=plugin_view,
               context=Qt.WidgetWithChildrenShortcut, auto_repeat=False,
               variant=variant)
    sc("scroll_top", _scroll_top)
    sc("scroll_bottom", _scroll_bottom)
    sc("toggle_separators", _toggle_all_seps)
    sc("toggle_filters", _toggle_filters)

    state_tracker = _ShortcutStateTracker(win)
    win._shortcut_state_tracker = state_tracker
    QApplication.instance().installEventFilter(state_tracker)

    mouse_dispatcher = _MouseShortcutDispatcher(win)
    win._mouse_shortcut_dispatcher = mouse_dispatcher
    QApplication.instance().installEventFilter(mouse_dispatcher)

    apply_shortcuts(win, configured)

    # Perf instrumentation (MM_PERFTRACE=1): F11 = timing summary table,
    # Shift+F11 = reset counters (perftrace.install only binds Tk keys, so
    # the Qt window wires its own). Unguarded - the table should dump even
    # with an overlay open or a text box focused.
    from Utils.diagnostics import performance as perftrace
    if perftrace.is_enabled():
        make_shortcut("F11", lambda _win: perftrace.dump(), guarded=False)
        make_shortcut(
            "Shift+F11", lambda _win: perftrace.reset(), guarded=False)
        import sys
        print("[PERF] perftrace enabled - F11 = summary table, "
              "Shift+F11 = reset counters.", file=sys.stderr)
        print("[PERF] work tags: [CPU] in-memory computation; [FS I/O] "
              "filesystem calls (which may be cache-backed); [DB I/O] "
              "SQLite/catalog access; [BACKGROUND] outside the button's "
              "critical path.", file=sys.stderr)
