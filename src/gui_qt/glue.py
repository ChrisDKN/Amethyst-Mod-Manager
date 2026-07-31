"""Register the toolkit-neutral backend hooks with Qt implementations.

The backend (Utils/*) talks to the UI only through these six registries
(established in Phase-1 decoupling). The Qt app registers concrete impls here
at startup; the backend imports no Qt. Mirrors the Tk app's
App._register_ui_hooks + import-time probe registration.
"""

from __future__ import annotations

from PySide6.QtCore import QTimer


def register_all(app, *, log, parent_window, ask_choice=None, warn=None,
                 file_pickers=None) -> list[str]:
    """Wire every glue point. Returns a list of human-readable status lines.

    *log* — callable(str) appending to the log surface (main thread).
    *parent_window* — the QMainWindow, for modal parenting.
    *ask_choice* / *warn* — optional real handlers (stubs used if omitted).
    *file_pickers* — optional dict(folder/file/files/save) of QFileDialog impls.
    """
    done: list[str] = []

    # 1. app_log — backend logs from any thread; drain on the Qt main thread.
    try:
        from Utils.app_log import set_app_log
        set_app_log(log, lambda ms, cb: QTimer.singleShot(ms, cb))
        done.append("app_log")
    except Exception as e:  # pragma: no cover
        done.append(f"app_log FAILED: {e!r}")

    # 2. main-thread dispatcher (portal_filechooser, generic worker→UI hops).
    #    This is always called FROM the picker worker thread, so the timer needs
    #    an explicit context object: the receiver-less singleShot overload
    #    creates its timer in the *calling* thread, which has no event loop, so
    #    the callback would never fire and the worker would block forever
    #    waiting on it.
    try:
        from Utils.portal_filechooser import set_main_thread_dispatcher
        set_main_thread_dispatcher(lambda fn: QTimer.singleShot(0, app, fn))
        done.append("main_thread_dispatcher")
    except Exception as e:
        done.append(f"dispatcher FAILED: {e!r}")

    # 3. ui_hooks — ask_choice / warn.
    try:
        from Utils import ui_hooks
        if ask_choice is None:
            def ask_choice(**kw):
                log(f"[ui_hooks] ask_choice (stub): {kw.get('title')}")
                opts = kw.get("options") or [None]
                return opts[kw.get("default_index", 0)]
        if warn is None:
            def warn(title, message, **kw):
                log(f"[ui_hooks] WARN (stub): {title}")
        ui_hooks.set_choice_handler(ask_choice)
        ui_hooks.set_warning_handler(warn)
        done.append("ui_hooks")
    except Exception as e:
        done.append(f"ui_hooks FAILED: {e!r}")

    # 4. screen probe — Qt owns DPI/scale via QScreen.
    try:
        from Utils.ui_config import set_screen_probe

        def _probe():
            scr = app.primaryScreen()
            g = scr.geometry()
            return g.width(), g.height(), scr.devicePixelRatio()

        set_screen_probe(_probe)
        done.append("screen_probe")
    except Exception as e:
        done.append(f"screen_probe FAILED: {e!r}")

    # 5. theme override resolver — same gui.themes.<mode> source as Tk.
    try:
        from Utils.ui_config import set_theme_override_resolver

        def _theme_overrides(mode):
            import importlib
            mod = importlib.import_module(f"Utils.themes.{mode}")
            raw = getattr(mod, "THEME_DEFAULTS_OVERRIDE", None)
            return raw if isinstance(raw, dict) else {}

        set_theme_override_resolver(_theme_overrides)
        done.append("theme_override_resolver")
    except Exception as e:
        done.append(f"theme_resolver FAILED: {e!r}")

    # 6. toolkit file pickers (QFileDialog) — last-resort behind portal/zenity.
    try:
        from Utils.portal_filechooser import set_toolkit_pickers
        fp = file_pickers or {}
        set_toolkit_pickers(
            folder=fp.get("folder"), file=fp.get("file"),
            files=fp.get("files"), save=fp.get("save"),
        )
        done.append("toolkit_pickers")
    except Exception as e:
        done.append(f"toolkit_pickers FAILED: {e!r}")

    return done
