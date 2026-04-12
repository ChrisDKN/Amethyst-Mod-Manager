"""
Global keyboard shortcuts for the Mod Manager main window.

Bindings:
    F2              Rename the selected mod (modlist panel)
    Ctrl+D          Deploy
    Ctrl+R          Restore
    Alt+Up          Move selected mods/plugins/separators up
    Alt+Down        Move selected mods/plugins/separators down

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
    # Use first selected non-separator mod
    sel = sorted(panel._sel_set) if panel._sel_set else (
        [panel._sel_idx] if panel._sel_idx >= 0 else []
    )
    for idx in sel:
        if 0 <= idx < len(panel._entries) and not panel._entries[idx].is_separator:
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
