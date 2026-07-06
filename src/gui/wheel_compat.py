"""
Mouse wheel event compatibility between Tk 8.6 and Tk 9.0 on Linux.

Tk 8.6 (AppImage): X11 wheel notches arrive as <Button-4>/<Button-5> only.
Tk 9.0 (Flatpak):  TIP 474 translates Button-4/5 into <MouseWheel> with
                   event.delta of +/-120 per notch, but bindings on the
                   literal <Button-4>/<Button-5> still fire. Without a guard
                   every notch scrolls twice.

Usage: wrap <Button-4>/<Button-5> handlers with ``skip_if_mousewheel`` (or
check ``LEGACY_WHEEL_REDUNDANT`` directly) so they no-op on Tk >= 8.7.
"""

from __future__ import annotations

import sys
import tkinter as tk
from typing import Callable

LEGACY_WHEEL_REDUNDANT: bool = float(tk.TkVersion) >= 8.7


def skip_if_mousewheel(fn: Callable) -> Callable:
    """Make a <Button-4>/<Button-5> handler no-op when Tk also fires <MouseWheel>.

    On Tk >= 8.7 the equivalent <MouseWheel> event already runs this widget's
    wheel logic for the same notch, so firing the Button-4/5 handler too would
    double-scroll. Return ``None`` (not ``"break"``) so other unrelated
    Button-4/5 bindings on the widget still run normally.
    """
    if not LEGACY_WHEEL_REDUNDANT:
        return fn

    def _wrapped(*_args, **_kwargs):
        return None

    return _wrapped


def bind_scrollable_wheel(scrollable) -> None:
    """Make Linux X11 wheel notches (Button-4/5) scroll a CTkScrollableFrame.

    On Tk 8.6 the bundled CTkScrollableFrame only listens for <MouseWheel>,
    which X11 never fires; users have to drag the scrollbar. This helper
    binds Button-4/5 on the frame itself, its inner canvas, and every
    descendant — and re-binds whenever new children appear, so dynamically
    rebuilt step pages keep working.

    No-op on Tk >= 8.7 where TIP 474 already delivers <MouseWheel>.
    """
    if LEGACY_WHEEL_REDUNDANT:
        return

    try:
        canvas = scrollable._parent_canvas
    except Exception:
        return

    def _up(_e):
        try:
            canvas.yview_scroll(-3, "units")
        except Exception:
            pass
        return "break"

    def _down(_e):
        try:
            canvas.yview_scroll(3, "units")
        except Exception:
            pass
        return "break"

    # Track which widgets already carry our Button-4/5 handlers so repeated
    # tree walks (the <Enter> rebind below) stay idempotent. Without this,
    # every pointer-enter stacked another add="+" handler on each widget, so a
    # notch scrolled 3 units × (times the frame had been entered) — i.e. the
    # speed grew over the session and differed per widget depending on how many
    # rebinds happened while it existed.
    bound: set[str] = set()

    def _bind_tree(widget):
        try:
            key = str(widget)
            if key not in bound:
                widget.bind("<Button-4>", _up, add="+")
                widget.bind("<Button-5>", _down, add="+")
                bound.add(key)
        except Exception:
            return
        for child in widget.winfo_children():
            _bind_tree(child)

    _bind_tree(scrollable)
    try:
        _bind_tree(canvas)
    except Exception:
        pass

    # Re-walk descendants when the pointer enters the scroller — cheap, and
    # covers rows added after the initial bind (multi-step wizard pages).
    # Only widgets not already in ``bound`` get a handler, so this never
    # double-binds existing rows.
    def _on_enter(_e=None):
        _bind_tree(scrollable)
        try:
            _bind_tree(canvas)
        except Exception:
            pass

    try:
        scrollable.bind("<Enter>", _on_enter, add="+")
    except Exception:
        pass


def patch_ctk_scrollable_frame() -> None:
    """Patch CTkScrollableFrame's Linux wheel handler to scale event.delta.

    The bundled customtkinter calls ``yview_scroll(-event.delta, "units")`` on
    Linux, which was fine on Tk 8.6 (delta was +/-1). On Tk 9.0 TIP 474 makes
    delta +/-120 per notch, so a single wheel tick scrolls 120 units and
    instantly flings the content to the end. Reduce it to +/-3 units per notch
    to match the rest of the app.
    """
    try:
        import customtkinter as ctk
        ScrollableFrame = ctk.CTkScrollableFrame
    except Exception:
        return

    def _patched_mouse_wheel_all(self, event):
        check_fn = getattr(self, "_check_if_valid_scroll", None) or getattr(self, "check_if_master_is_canvas", None)
        if check_fn and not check_fn(event.widget):
            return
        delta = getattr(event, "delta", 0) or 0
        if delta == 0:
            return
        if sys.platform.startswith("win"):
            units = -int(delta / 6)
        elif sys.platform == "darwin":
            units = -delta
        else:
            units = -3 if delta > 0 else 3
        if self._shift_pressed:
            if self._parent_canvas.xview() != (0.0, 1.0):
                self._parent_canvas.xview("scroll", units, "units")
        else:
            if self._parent_canvas.yview() != (0.0, 1.0):
                self._parent_canvas.yview("scroll", units, "units")

    ScrollableFrame._mouse_wheel_all = _patched_mouse_wheel_all

    _patch_ctk_scrollable_frame_destroy(ScrollableFrame)


# Sequences CTkScrollableFrame.__init__ registers via bind_all but never cleans
# up in destroy() — a memory leak (the bound methods pin the whole frame).
_SCROLLFRAME_ALL_SEQS = (
    "<MouseWheel>",
    "<KeyPress-Shift_L>", "<KeyPress-Shift_R>",
    "<KeyRelease-Shift_L>", "<KeyRelease-Shift_R>",
)


def _patch_ctk_scrollable_frame_destroy(ScrollableFrame) -> None:
    """Make CTkScrollableFrame.destroy() remove its OWN root-level bindings.

    Upstream binds 5 sequences with ``bind_all(seq, self._method, add="+")`` and
    never unbinds them, so every destroyed scroll-frame stays pinned by the root
    interpreter's bind table forever (it strands the frame's entire subtree —
    category checkboxes, images — the Nexus-browser leak).

    We can't use ``unbind_all(seq)`` — that is global and would kill scrolling on
    the always-open plugin/status scroll-frames. Instead we record the per-bind
    funcid at bind time (wrapping bind_all) and delete exactly those funcids from
    the root bind script on destroy, leaving other frames' bindings intact.
    """
    if getattr(ScrollableFrame, "_mm_destroy_patched", False):
        return

    _orig_init = ScrollableFrame.__init__
    _orig_destroy = ScrollableFrame.destroy

    def _patched_init(self, *args, **kwargs):
        self._mm_all_funcids = []
        # Capture funcids only during the original __init__'s bind_all calls.
        _real_bind_all = self.bind_all

        def _recording_bind_all(sequence=None, func=None, add=None):
            funcid = _real_bind_all(sequence, func, add)
            if sequence in _SCROLLFRAME_ALL_SEQS and funcid:
                self._mm_all_funcids.append((sequence, funcid))
            return funcid

        self.bind_all = _recording_bind_all
        try:
            _orig_init(self, *args, **kwargs)
        finally:
            # Restore the real method so later bind_all calls behave normally.
            try:
                del self.bind_all
            except Exception:
                self.bind_all = _real_bind_all

    def _patched_destroy(self):
        root = self._root()
        for sequence, funcid in getattr(self, "_mm_all_funcids", []):
            try:
                # Surgically drop only THIS funcid's line from the global
                # ('bind', 'all', seq) script and delete its Tcl command —
                # exactly what tkinter.unbind(funcid) does, but for bind_all.
                # Other scroll-frames' bindings on the same sequence survive.
                root._unbind(("bind", "all", sequence), funcid)
            except Exception:
                # Fallback: at least release the command so *self* is freed.
                try:
                    root.deletecommand(funcid)
                except Exception:
                    pass
        self._mm_all_funcids = []
        _orig_destroy(self)

    ScrollableFrame.__init__ = _patched_init
    ScrollableFrame.destroy = _patched_destroy
    ScrollableFrame._mm_destroy_patched = True
