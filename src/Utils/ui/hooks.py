"""Toolkit-neutral UI interaction hooks.

The backend sometimes needs to interact with the user mid-operation - ask a
multiple-choice question, or surface a warning. It must not import the GUI
toolkit to do so (so it stays headless-safe and portable across Tk/Qt).

The GUI registers concrete implementations at startup via the ``set_*``
functions below; the backend calls ``ask_choice`` / ``warn`` and gets a no-op
fallback when no GUI is attached (headless / collection / CLI installs).

This mirrors the logging glue in ``Utils.app_log`` (``set_app_log``) and the
file-picker dispatcher in ``Utils.ui.portal``
(``set_main_thread_dispatcher``).

Navigation sentinels
--------------------
A choice handler may return one of the module-level sentinels ``BACK`` or
``USE_DEFAULTS`` instead of a label string, to drive multi-page wizards. They
are defined here so both the backend and the GUI compare against the *same*
objects (identity comparison). Headless callers never receive them.
"""

from __future__ import annotations

from typing import Callable, Optional

# Navigation sentinels - see module docstring.
BACK = object()
USE_DEFAULTS = object()

# Type aliases for the registered handlers.
#   choice handler: (title, prompt, options, default_index, log_fn, page,
#                    total_pages) -> chosen label | None | BACK | USE_DEFAULTS
ChoiceHandler = Callable[..., object]
#   warning handler: (title, message, **kw) -> None
WarningHandler = Callable[..., None]

_choice_handler: Optional[ChoiceHandler] = None
_warning_handler: Optional[WarningHandler] = None


def set_choice_handler(fn: Optional[ChoiceHandler]) -> None:
    """Register the GUI's multiple-choice prompt. Pass None to clear."""
    global _choice_handler
    _choice_handler = fn


def set_warning_handler(fn: Optional[WarningHandler]) -> None:
    """Register the GUI's warning popup. Pass None to clear."""
    global _warning_handler
    _warning_handler = fn


def has_choice_handler() -> bool:
    """True if a GUI choice prompt is attached (i.e. interactive install)."""
    return _choice_handler is not None


def ask_choice(title: str, prompt: str, options: "list[str]",
               default_index: int = 0, log_fn: "Callable[[str], None] | None" = None,
               page: int = 0, total_pages: int = 0) -> object:
    """Ask the user to pick one of *options*.

    Returns the chosen label, ``None`` (cancel / no handler), or one of the
    navigation sentinels ``BACK`` / ``USE_DEFAULTS``. When no GUI handler is
    registered (headless), returns ``None`` and logs the reason.
    """
    if _choice_handler is None:
        if log_fn:
            log_fn("  [ui_hooks] no choice handler registered - keeping defaults.")
        return None
    return _choice_handler(
        title=title, prompt=prompt, options=options,
        default_index=default_index, log_fn=log_fn,
        page=page, total_pages=total_pages,
    )


def warn(title: str, message: str, **kwargs) -> None:
    """Surface a non-blocking warning to the user. No-op when headless."""
    if _warning_handler is None:
        return
    try:
        _warning_handler(title, message, **kwargs)
    except Exception:
        # A failed popup must never break a backend operation.
        pass
