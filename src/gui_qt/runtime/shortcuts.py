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
    Ctrl+Up/Down    Extend the selection to the previous/next row in the
                    active list panel (handled by the views themselves)
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

# Crash-proof diagnostic prints (Flatpak stdout can raise BrokenPipeError and
# kill worker threads). See Utils.app_log.safe_print.
from Utils.app_log import safe_print as print  # noqa: A004

from PySide6.QtCore import (
    Qt, QObject, QEvent, QItemSelection, QItemSelectionModel,
)
from PySide6.QtGui import QShortcut, QKeySequence
from PySide6.QtWidgets import (
    QApplication, QLineEdit, QPlainTextEdit, QTextEdit,
    QAbstractSpinBox, QComboBox, QWidget,
)


# ---------------------------------------------------------------------------
# Definitions and persistence
# ---------------------------------------------------------------------------

from Utils.ui.shortcuts import (
    ShortcutDefinition, SHORTCUT_DEFINITIONS, MOUSE_ACTION_IDS,
    _EDITABLE_MODIFIERS, _MAX_MOUSE_BUTTON, _RESERVED_KEYS,
    shortcut_from_parts, shortcut_parts, normalise_shortcut,
    shortcut_identity, shortcut_key_text, shortcut_modifier_text,
    mouse_shortcut_from_parts, mouse_shortcut_parts, mouse_shortcut_button_text,
    binding_from_parts, binding_parts, normalise_binding,
    binding_identity, binding_input_text, DEFAULT_SHORTCUTS,
    shortcut_definitions, shortcut_defaults,
    _duplicate_groups, _validated_shortcuts, load_shortcuts,
)


_active_shortcuts: "dict[str, str] | None" = None
_disabled_shortcut_inputs: "set[str] | None" = None


def current_shortcuts() -> dict[str, str]:
    global _active_shortcuts, _disabled_shortcut_inputs
    if _active_shortcuts is None:
        _active_shortcuts = load_shortcuts()
    if _disabled_shortcut_inputs is None:
        from Utils.ui.config import load_disabled_shortcut_inputs
        _disabled_shortcut_inputs = load_disabled_shortcut_inputs()
    if not _disabled_shortcut_inputs:
        return dict(_active_shortcuts)
    return {
        action_id: "" if (parts := binding_parts(sequence)) is not None
        and parts[0] in _disabled_shortcut_inputs else sequence
        for action_id, sequence in _active_shortcuts.items()
    }


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
    global _active_shortcuts, _disabled_shortcut_inputs
    from Utils.ui.config import load_disabled_shortcut_inputs

    if values is None:
        values = load_shortcuts()
    values = _validated_shortcuts(values)
    _active_shortcuts = dict(values)
    _disabled_shortcut_inputs = load_disabled_shortcut_inputs()
    values = current_shortcuts()
    for action_id, sequence in values.items():
        expanded = _binding_sequences(action_id, sequence)
        for shortcut, variant in getattr(
                win, "_shortcut_bindings", {}).get(action_id, []):
            key = expanded[variant] if variant < len(expanded) else ""
            shortcut.setKey(QKeySequence(key))
    win._active_shortcut_keys = values
    commands = getattr(getattr(win, "_plugin_ui", None), "commands", None)
    if commands is not None:
        commands.apply_bindings(values)
    tracker = getattr(win, "_shortcut_state_tracker", None)
    if tracker is not None:
        tracker.sync(QApplication.focusWidget())
    from gui_qt.runtime.mouse_navigation import refresh_navigation_shortcuts
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
                win._last_modlist_view = mv
                win._last_plugin_panel_tab = None
            elif pv is not None and (obj is pv or obj is pv.viewport()):
                win._last_list_panel = "plugin"
                win._last_plugin_view = pv
                win._last_plugin_panel_tab = None
        return False


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def _active_modlist_view(win):
    view = getattr(win, "_last_modlist_view", None)
    if view is not None and view.isVisible():
        return view
    return getattr(win, "_modlist_view", None)


def _selected_rows(view) -> list[int]:
    from gui_qt.modlist.external import external
    model = view.model()
    return sorted({i.row() for i in view.selectionModel().selectedRows()
                   if not hasattr(model, 'entry') or not external(model.entry(i.row()))})


def _rename_selected(win):
    view = _active_modlist_view(win)
    if view is None:
        return
    rows = _selected_rows(view)
    if not rows:
        return
    from gui_qt.modlist.menu import _rename
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
    view = _active_modlist_view(win)
    if view is None or getattr(view, "staging_dir", None) is None:
        return
    from gui_qt.modlist.menu import _create_empty_mod_at_boundary
    _create_empty_mod_at_boundary(view, view.model(), top=True)


def _open_settings(win):
    if hasattr(win, "_open_settings_modal"):
        win._open_settings_modal()


def _toggleable_rows(view) -> list[int]:
    """Selected rows that can be enable/disable-toggled or removed: non-
    separator, non-pinned, non-locked mods."""
    from gui_qt.modlist.model import _PINNED_NAMES
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
    view = _active_modlist_view(win)
    if view is None:
        return
    rows = _toggleable_rows(view)
    if not rows:
        return
    m = view.model()
    target = not m.entry(rows[0]).enabled
    m.set_rows_enabled(rows, target)


def _active_plugin_view(win):
    view = getattr(win, "_last_plugin_view", None)
    if view is not None and view.isVisible():
        return view
    return getattr(win, "_plugin_view", None)


def _toggle_selected_plugins(win):
    view = _active_plugin_view(win)
    if view is None:
        return
    m = view.model()
    rows = [r for r in _selected_rows(view)
            if 0 <= r < m.rowCount() and not m.row(r).vanilla]
    if not rows:
        return
    target = not m.row(rows[0]).enabled
    from gui_qt.plugins.menu import _set_enabled
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
    view = _active_modlist_view(win)
    if view is None:
        return
    rows = _toggleable_rows(view)
    if not rows:
        return
    from gui_qt.modlist.menu import _remove, _remove_mods_multi
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
    tab = getattr(win, "_modlist_tab", None)
    if tab is not None and _active_modlist_view(win) is tab.view:
        tab.toggle_collapse()
        return
    if hasattr(win, "_on_toggle_collapse_all"):
        win._on_toggle_collapse_all()


def _toggle_filters(win):
    panel = getattr(win, "_last_plugin_panel_tab", None)
    focus = QApplication.focusWidget()
    if (panel is not None and panel.isVisible()
            and (focus is None or focus is panel or panel.isAncestorOf(focus))):
        if panel.filters is not None:
            panel._toggle_filters()
        return
    tab = getattr(win, "_modlist_tab", None)
    if _active_panel(win) == "mod" and tab is not None and _active_modlist_view(win) is tab.view:
        tab.toggle_filters()
        return
    if _active_panel(win) == "plugin":
        if hasattr(win, "_toggle_plugin_filters"):
            win._toggle_plugin_filters()
    else:
        if hasattr(win, "_toggle_modlist_filters"):
            win._toggle_modlist_filters()


def _focus_search(win):
    panel = getattr(win, "_last_plugin_panel_tab", None)
    focus = QApplication.focusWidget()
    if (panel is not None and panel.isVisible() and panel.search is not None
            and (focus is None or focus is panel or panel.isAncestorOf(focus))):
        panel.search.setFocus()
        panel.search.selectAll()
        return
    edit = None
    if _active_panel(win) == "plugin":
        pe = getattr(win, "_plugins_search", None)
        if pe is not None and pe.isVisible():
            edit = pe
    if edit is None:
        tab = getattr(win, "_modlist_tab", None)
        edit = (tab.search if tab is not None and _active_modlist_view(win) is tab.view
                else getattr(win, "_modlist_search", None))
    if edit is None:
        return
    edit.setFocus()
    edit.selectAll()


def _active_view(win):
    if _active_panel(win) == "plugin":
        return _active_plugin_view(win)
    return _active_modlist_view(win)


def _open_selected_mod_page(win):
    view = _active_modlist_view(win)
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
        view = _active_plugin_view(win)
        if view is None:
            return
        _apply_row_selection(view, view._visible_rows())
        return

    view = _active_modlist_view(win)
    if view is None:
        return
    m = view.model()
    from gui_qt.modlist.model import _PINNED_NAMES
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



# ---- Ctrl+Up / Ctrl+Down: extend the selection ------------------------------

def ctrl_arrow_extend(view, key) -> bool:
    """Handle Ctrl+Up/Down in a list panel by adding the next visible row to
    the selection. Qt's own Ctrl+Arrow only walks the current index and leaves
    the selection alone, which reads as "nothing happened" in a view whose
    delegate paints selection but no focus ring. Returns True if handled."""
    if key not in (Qt.Key_Up, Qt.Key_Down):
        return False
    visible = view._visible_rows()
    if not visible:
        return False

    sm = view.selectionModel()
    current = sm.currentIndex()
    step = -1 if key == Qt.Key_Up else 1
    if current.isValid() and current.row() in visible:
        i = visible.index(current.row()) + step
        if not 0 <= i < len(visible):
            return True          # already at an end - swallow, don't wrap
        row = visible[i]
    else:
        row = visible[0] if step > 0 else visible[-1]

    m = view.model()
    idx = m.index(row, 0)
    sm.select(QItemSelection(idx, m.index(row, m.columnCount() - 1)),
              QItemSelectionModel.Select | QItemSelectionModel.Rows)
    sm.setCurrentIndex(idx, QItemSelectionModel.NoUpdate)
    view.scrollTo(idx)
    return True


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
    view = _active_modlist_view(win)
    if view is None:
        return
    m = view.model()

    if m.flat_sort_active:
        return

    sel = _selected_rows(view)
    if not sel:
        return
    block = view._drag_block_for(sel[0])
    if not block:
        return
    block = sorted(block)
    # Keyboard moves only contiguous selections.
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
    view = _active_plugin_view(win)
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
        for panel in getattr(win, "_plugin_panel_tabs", {}).values():
            if watched is panel or panel.isAncestorOf(watched):
                win._last_plugin_panel_tab = panel
                if panel.index == 0:
                    win._last_list_panel = "plugin"
                    win._last_plugin_view = panel.view
                return
        tab = getattr(win, "_modlist_tab", None)
        if tab is not None and (watched is tab.view or tab.view.isAncestorOf(watched)):
            win._last_list_panel = "mod"
            win._last_modlist_view = tab.view
            return
        for panel, attr in (("mod", "_modlist_view"),
                            ("plugin", "_plugin_view")):
            view = getattr(win, attr, None)
            if (view is not None
                    and (watched is view or view.isAncestorOf(watched))):
                win._last_list_panel = panel
                win._last_plugin_panel_tab = None
                if panel == "mod":
                    win._last_modlist_view = view
                else:
                    win._last_plugin_view = view
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


class _PanelTabShortcutScope(QObject):
    def __init__(self, win, view, panel="mod"):
        super().__init__(view)
        self._win = win
        self._view = view
        self._panel_kind = panel
        self.panel = view.parentWidget()
        self.shortcuts = []
        self.panel.installEventFilter(self)

    def sync(self):
        detached = self._view.window() is not self._win.window()
        for shortcut in self.shortcuts:
            shortcut.setEnabled(detached)

    def activate(self, original):
        if self._panel_kind in ("mod", "plugin"):
            self._win._last_list_panel = self._panel_kind
        if self._panel_kind == "plugin":
            self._win._last_plugin_view = self._view
            self._win._last_plugin_panel_tab = self.panel
        elif self._panel_kind == "mod":
            self._win._last_modlist_view = self._view
            self._win._last_plugin_panel_tab = None
        else:
            self._win._last_plugin_panel_tab = self.panel
        original.activated.emit()

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.ParentChange, QEvent.Show):
            self.sync()
        return False


def register_panel_tab_shortcuts(win, view, *, panel="mod"):
    view._tab_shortcuts = []
    scope = view._tab_shortcut_scope = _PanelTabShortcutScope(win, view, panel)
    for action, bindings in list(win._shortcut_bindings.items()):
        if panel == "files" and action not in {"focus_search", "toggle_filters", "refresh"}:
            continue
        for original, variant in list(bindings):
            if original.context() != Qt.WindowShortcut:
                continue
            shortcut = QShortcut(original.key(), scope.panel)
            shortcut.setContext(Qt.WidgetWithChildrenShortcut)
            shortcut.setAutoRepeat(original.autoRepeat())
            shortcut.activated.connect(lambda s=original: scope.activate(s))
            bindings.append((shortcut, variant))
            scope.shortcuts.append(shortcut)
            view._tab_shortcuts.append(shortcut)
    scope.sync()
    values = getattr(win, "_active_shortcut_keys", None) or current_shortcuts()
    actions = {"plugin": (("toggle_selected", _toggle_selected_plugins),),
               "files": (),
               "mod": (("toggle_selected", _toggle_selected),
                       ("open_mod_page", _open_selected_mod_page))}[panel]
    for action, callback in actions:
        keys = _binding_sequences(action, values[action])
        for variant in range(2 if action == "toggle_selected" else 1):
            key = keys[variant] if variant < len(keys) else ""
            shortcut = QShortcut(QKeySequence(key), view)
            shortcut.setContext(Qt.WidgetWithChildrenShortcut)
            shortcut.setAutoRepeat(False)
            shortcut.activated.connect(_guard(win, callback))
            win._shortcut_bindings.setdefault(action, []).append((shortcut, variant))
            view._tab_shortcuts.append(shortcut)


def unregister_panel_tab_shortcuts(win, view):
    for bindings in getattr(win, "_shortcut_bindings", {}).values():
        bindings[:] = [(shortcut, variant) for shortcut, variant in bindings
                       if shortcut not in view._tab_shortcuts]
    scope = view._tab_shortcut_scope
    scope.panel.removeEventFilter(scope)
    scope.deleteLater()
    for shortcut in view._tab_shortcuts:
        shortcut.setEnabled(False)
        shortcut.deleteLater()
    view._tab_shortcuts.clear()
