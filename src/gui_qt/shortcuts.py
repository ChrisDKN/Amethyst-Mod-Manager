"""
Global keyboard shortcuts for the Qt Mod Manager main window.

Port of the Tk ``src/gui/shortcuts.py`` - same behaviour, idiomatic Qt.

Bindings:
    F2              Rename the selected mod or separator (modlist panel)
    F5              Refresh the modlist (fires even in a text field)
    Delete          Remove selected mod(s) (modlist panel)
    Return/Enter    Toggle enable/disable for selected mods (modlist panel)
    Home            Scroll active list panel to the top
    End             Scroll active list panel to the bottom
    Ctrl+F          Focus the active panel's search bar (fires even in a field)
    Ctrl+A          Select all mods in the active separator (modlist), or all
                    plugins (plugin panel)
    Ctrl+D          Deploy
    Ctrl+R          Restore
    Alt+Up          Move selected mods/plugins/separators up
    Alt+Down        Move selected mods/plugins/separators down
    Shift+E         Expand/collapse all separators (modlist)
    Shift+F         Toggle the active panel's filter side panel

Alt+Up/Down, F2, Ctrl+A, Home/End, Shift+F and the movers dispatch to whichever
panel (modlist or plugin) was most recently interacted with (focus or mouse).
Shortcuts are suppressed while a text-input widget has focus (except F5 and
Ctrl+F) and while an overlay / modal is open - mirroring the Tk guard.
"""

from __future__ import annotations

# Crash-proof diagnostic prints (Flatpak stdout can raise BrokenPipeError and
# kill worker threads). See Utils.app_log.safe_print.
from Utils.app_log import safe_print as print  # noqa: A004

from PySide6.QtCore import Qt, QObject, QEvent, QItemSelection, QItemSelectionModel
from PySide6.QtGui import QShortcut, QKeySequence
from PySide6.QtWidgets import (
    QApplication, QLineEdit, QPlainTextEdit, QTextEdit,
    QAbstractSpinBox, QComboBox, QWidget,
)


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
    Tk "focus is inside a dialog" check). Overlays are non-modal QWidget
    children whose class name ends in 'Overlay'."""
    if QApplication.activeModalWidget() is not None:
        return True
    try:
        for w in win.findChildren(QWidget):
            if w.isVisible() and type(w).__name__.endswith("Overlay"):
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

    The window-level Return/Enter shortcuts (toggle selected mods) consume the
    key during Qt's ShortcutOverride phase even when ``_guard`` would then
    no-op - QLineEdit only claims printable/editing keys in that phase, so
    ``returnPressed`` never fired anywhere in the main window (e.g. the Nexus
    browser page box). Accepting the override whenever the guard would refuse
    the shortcut delivers the key press to the focused widget instead."""

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


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register_shortcuts(win) -> None:
    """Install the global keyboard shortcuts + panel tracking on the window."""
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

    def sc(seq, fn, guarded=True):
        s = QShortcut(QKeySequence(seq), win)
        s.setContext(Qt.WindowShortcut)
        s.activated.connect((_guard if guarded else _unguarded)(win, fn))
        shortcuts.append(s)
        return s

    sc("F2", _rename_selected)
    sc("F5", _refresh_modlist, guarded=False)
    sc("Ctrl+D", _deploy)
    sc("Ctrl+R", _restore)
    sc("Ctrl+F", _focus_search, guarded=False)
    sc("Ctrl+A", _select_all)
    sc("Alt+Up", _move_up)
    sc("Alt+Down", _move_down)
    sc("Delete", _delete_selected)
    sc("Return", _toggle_selected)   # main Enter
    sc("Enter", _toggle_selected)    # keypad Enter
    sc("Home", _scroll_top)
    sc("End", _scroll_bottom)
    sc("Shift+E", _toggle_all_seps)
    sc("Shift+F", _toggle_filters)

    # Perf instrumentation (MM_PERFTRACE=1): F11 = timing summary table,
    # Shift+F11 = reset counters (perftrace.install only binds Tk keys, so
    # the Qt window wires its own). Unguarded - the table should dump even
    # with an overlay open or a text box focused.
    from Utils import perftrace
    if perftrace.is_enabled():
        sc("F11", lambda _win: perftrace.dump(), guarded=False)
        sc("Shift+F11", lambda _win: perftrace.reset(), guarded=False)
        import sys
        print("[PERF] perftrace enabled - F11 = summary table, "
              "Shift+F11 = reset counters.", file=sys.stderr)
