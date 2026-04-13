"""
Global keyboard shortcuts for the Mod Manager main window.

Bindings:
    F2              Rename the selected mod or separator (modlist panel)
    Delete          Remove selected mod(s) (modlist panel)
    Home            Scroll active list panel to the top
    End             Scroll active list panel to the bottom
    Ctrl+D          Deploy
    Ctrl+R          Restore
    Alt+Up          Move selected mods/plugins/separators up
    Alt+Down        Move selected mods/plugins/separators down
    Shift+E         Expand/collapse all separators
    Shift+F         Toggle filter panel for the active list panel
    Shift+Scroll    4x scroll speed

Reorder shortcuts require the Alt modifier so plain Up/Down can still be
used for normal navigation/scrolling without accidentally shuffling the
selection. Alt+arrow matches the "move line" convention from VS Code /
JetBrains.

Alt+Up/Down and F2 dispatch to whichever panel (modlist or plugin) was
most recently interacted with via mouse. Shortcuts are suppressed while a
text input widget (Entry/Text/etc.) has focus so typing isn't hijacked.
"""

import tkinter as tk


_TEXT_WIDGET_CLASSES = {
    "Entry", "TEntry", "Text", "TCombobox", "Spinbox", "TSpinbox",
    "CTkEntry",
}


def _focus_is_text_input(app) -> bool:
    try:
        w = app.focus_get()
    except Exception:
        return False
    if w is None:
        return False
    try:
        return w.winfo_class() in _TEXT_WIDGET_CLASSES
    except Exception:
        return False


def _active_list_panel(app):
    """Return ("mod", panel) or ("plugin", panel) based on last-interacted panel.

    Falls back to the modlist panel if neither has been touched yet.
    """
    which = getattr(app, "_last_list_panel", "mod")
    if which == "plugin":
        panel = getattr(app, "_plugin_panel", None)
        if panel is not None:
            return "plugin", panel
    panel = getattr(app, "_mod_panel", None)
    if panel is not None:
        return "mod", panel
    return None, None


def _rename_selected(app):
    kind, panel = _active_list_panel(app)
    if kind != "mod" or panel is None:
        return
    sel = sorted(panel._sel_set) if panel._sel_set else (
        [panel._sel_idx] if panel._sel_idx >= 0 else []
    )
    for idx in sel:
        if not (0 <= idx < len(panel._entries)):
            continue
        entry = panel._entries[idx]
        if entry.is_separator:
            panel._rename_separator(idx)
            return
        else:
            panel._rename_mod(idx)
            return


def _deploy(app):
    topbar = getattr(app, "_topbar", None)
    if topbar is not None and hasattr(topbar, "_on_deploy"):
        topbar._on_deploy()


def _restore(app):
    topbar = getattr(app, "_topbar", None)
    if topbar is not None and hasattr(topbar, "_on_restore"):
        topbar._on_restore()


def _move_up(app):
    kind, panel = _active_list_panel(app)
    if panel is None:
        return
    if kind == "mod":
        panel._move_up()
    else:
        panel._move_plugins_up()


def _move_down(app):
    kind, panel = _active_list_panel(app)
    if panel is None:
        return
    if kind == "mod":
        panel._move_down()
    else:
        panel._move_plugins_down()


def _delete_selected(app):
    kind, panel = _active_list_panel(app)
    if kind != "mod" or panel is None:
        return
    if getattr(panel, "_modlist_path", None) is None:
        return
    sel = sorted(panel._sel_set) if panel._sel_set else (
        [panel._sel_idx] if panel._sel_idx >= 0 else []
    )
    # Filter out separators and locked mods
    removable = []
    for idx in sel:
        if not (0 <= idx < len(panel._entries)):
            continue
        entry = panel._entries[idx]
        if entry.is_separator:
            continue
        if getattr(entry, "locked", False):
            continue
        removable.append(idx)
    if not removable:
        return
    if len(removable) == 1:
        panel._remove_mod(removable[0])
    else:
        panel._remove_selected_mods(removable)


def _scroll_list(app, fraction: float):
    kind, panel = _active_list_panel(app)
    if panel is None:
        return
    if kind == "mod":
        canvas = getattr(panel, "_canvas", None)
        redraw = getattr(panel, "_schedule_redraw", None) or getattr(panel, "_redraw", None)
    else:
        canvas = getattr(panel, "_pcanvas", None)
        redraw = getattr(panel, "_schedule_predraw", None)
    if canvas is None:
        return
    canvas.yview_moveto(fraction)
    if redraw is not None:
        redraw()


def _scroll_to_top(app):
    _scroll_list(app, 0.0)


def _scroll_to_bottom(app):
    _scroll_list(app, 1.0)


def _toggle_all_seps(app):
    kind, panel = _active_list_panel(app)
    if kind != "mod" or panel is None:
        return
    panel._toggle_all_separators()


def _toggle_filters(app):
    kind, panel = _active_list_panel(app)
    if panel is None:
        return
    if kind == "mod":
        panel._on_open_filters()
    else:
        panel._toggle_plugin_filter_panel()


def register_shortcuts(app) -> None:
    """Install the global keyboard shortcuts on the main App window."""
    app._last_list_panel = "mod"

    def _guard(fn):
        def _handler(event=None):
            if _focus_is_text_input(app):
                return
            fn(app)
            return "break"
        return _handler

    app.bind_all("<F2>",           _guard(_rename_selected), add="+")
    app.bind_all("<Control-d>",    _guard(_deploy),          add="+")
    app.bind_all("<Control-D>",    _guard(_deploy),          add="+")
    app.bind_all("<Control-r>",    _guard(_restore),         add="+")
    app.bind_all("<Control-R>",    _guard(_restore),         add="+")
    app.bind_all("<Alt-Up>",       _guard(_move_up),         add="+")
    app.bind_all("<Alt-Down>",     _guard(_move_down),       add="+")
    app.bind_all("<Delete>",       _guard(_delete_selected), add="+")
    app.bind_all("<Home>",         _guard(_scroll_to_top),    add="+")
    app.bind_all("<End>",          _guard(_scroll_to_bottom), add="+")
    app.bind_all("<Shift-E>",      _guard(_toggle_all_seps),  add="+")
    app.bind_all("<Shift-F>",      _guard(_toggle_filters),   add="+")

    # Shift+mousewheel = 4x scroll speed
    # Generate 3 extra scroll events so total = 4x normal speed.
    def _fast_scroll(event):
        w = event.widget
        # Determine the base event type (Button-4 = up, Button-5 = down)
        btn = 4 if event.num == 4 else 5
        for _ in range(3):
            w.event_generate(f"<Button-{btn}>")

    app.bind_all("<Shift-Button-4>", _fast_scroll, add="+")
    app.bind_all("<Shift-Button-5>", _fast_scroll, add="+")
