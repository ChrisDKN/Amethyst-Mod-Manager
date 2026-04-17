"""
Tooltip helper for plain tkinter widgets — compatible with X11 and Wayland.

**Canvas / manual mode** — call :meth:`show` / :meth:`hide` directly::

    tip = TkTooltip(parent_widget, bg="#1a1a2e", fg="#ff6b6b")
    tip.show(event.x_root, event.y_root, "Some text")
    tip.hide()

**Widget-binding mode** — call :meth:`attach` once and let it manage
``<Enter>`` / ``<Leave>`` bindings automatically::

    tip = TkTooltip(parent, bg=BG, fg=FG, font=FONT, alpha=0.95)
    tip.attach(some_widget, "Tooltip text")
"""

from __future__ import annotations

import tkinter as tk
from typing import Callable, Union

# A tk font spec: either a string ("TkDefaultFont"), a (family, size) tuple,
# or (family, size, style) — matches what Tk.Label accepts.
FontSpec = Union[str, tuple[str, int], tuple[str, int, str]]


class TkTooltip:
    """Debounced tooltip for plain tkinter widgets, compatible with X11 and Wayland.

    **X11**: ``overrideredirect`` and client-side ``wm_geometry`` positioning
    are natively supported; the ``-type tooltip`` EWMH hint is honoured by all
    major window managers (i3, Openbox, KDE, GNOME, XFWM, …).

    **Wayland / XWayland** (e.g. Hyprland): a plain ``Toplevel`` with
    ``overrideredirect(True)`` can steal keyboard focus, firing a ``<Leave>``
    event on the host widget and instantly destroying the tooltip — causing
    visible flicker.  This class fixes the problem by:

    * Declaring the window as ``-type tooltip`` so the compositor never
      gives it focus (wrapped in ``try/except`` for portability).
    * Delaying creation until the cursor has been stationary for
      *delay_ms* milliseconds, which also prevents rapid create/destroy
      cycles on fast mouse motion.
    * Ignoring motion within a *jitter*-pixel radius of the trigger point
      so the tooltip stays stable while the cursor moves inside an element.
    """

    def __init__(
        self,
        parent: tk.Widget,
        *,
        bg: str = "#1a1a2e",
        fg: str = "#ff6b6b",
        font: FontSpec = ("TkDefaultFont", 10),
        wraplength: int = 350,
        padx: int = 8,
        pady: int = 4,
        alpha: float = 1.0,
        delay_ms: int = 400,
        jitter: int = 8,
    ) -> None:
        self._parent = parent
        self._bg = bg
        self._fg = fg
        self._font = font
        self._wraplength = wraplength
        self._padx = padx
        self._pady = pady
        self._alpha = alpha
        self._delay_ms = delay_ms
        self._jitter = jitter

        self._win: tk.Toplevel | None = None
        self._text: str = ""
        self._after_id: str | None = None
        self._trigger_x: int = 0
        self._trigger_y: int = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def show(self, x: int, y: int, text: str) -> None:
        """Show the tooltip at screen coords (*x*, *y*).

        Placement: to the left of the cursor (suits canvas column icons).
        If the same text is already shown and the cursor has moved less than
        *jitter* pixels, this is a no-op. If the text is already shown but the
        cursor moved further, the window is repositioned in place (no flicker).
        If the tooltip is already visible with different text, it swaps
        instantly; otherwise the first appearance is debounced by *delay_ms*.
        """
        if (self._win is not None
                and self._text == text
                and abs(x - self._trigger_x) <= self._jitter
                and abs(y - self._trigger_y) <= self._jitter):
            return

        if self._win is not None and self._text == text:
            self._trigger_x = x
            self._trigger_y = y
            tip_w = self._win.winfo_reqwidth()
            self._win.wm_geometry(f"+{x - tip_w - 4}+{y + 8}")
            return

        self._schedule(text, lambda tw: self._place_left_of(tw, x, y),
                       immediate=self._win is not None)
        self._trigger_x = x
        self._trigger_y = y

    def attach(
        self,
        widget: tk.Widget,
        text: str,
        *,
        recursive_depth: int = 3,
        offset_x: int = 12,
        offset_y: int = 12,
    ) -> None:
        """Bind *widget* (and its children) so the tooltip appears on hover.

        Placement: right/below the cursor with screen-edge clamping.
        Applies the same Wayland-safe debounced creation as :meth:`show`.

        Parameters
        ----------
        widget:
            The root widget to bind.
        text:
            Tooltip content.
        recursive_depth:
            How many levels of children to also bind (default 3).
        offset_x / offset_y:
            Pixel offset from the cursor when the tooltip appears.
        """
        def _enter(event: tk.Event) -> None:
            if self._win is not None and self._text == text:
                return
            rx, ry = event.x_root, event.y_root
            self._schedule(
                text,
                lambda tw: self._place_clamped(tw, rx, ry, offset_x, offset_y),
                immediate=False,
            )

        def _leave(event: tk.Event) -> None:
            self.hide()

        def _bind_recursive(w: tk.Widget, depth: int = 0) -> None:
            w.bind("<Enter>", _enter, add="+")
            w.bind("<Leave>", _leave, add="+")
            if depth < recursive_depth:
                for child in w.winfo_children():
                    _bind_recursive(child, depth + 1)

        _bind_recursive(widget)

    def hide(self) -> None:
        """Cancel any pending show and destroy the tooltip window."""
        if self._after_id is not None:
            self._parent.after_cancel(self._after_id)
            self._after_id = None
        if self._win is not None:
            self._win.destroy()
            self._win = None
        self._text = ""
        self._trigger_x = 0
        self._trigger_y = 0

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _schedule(
        self,
        text: str,
        place: Callable[[tk.Toplevel], None],
        *,
        immediate: bool,
    ) -> None:
        """Cancel any pending show and schedule a new one.

        If *immediate* is True (tooltip is already visible — user is actively
        hovering), swap now with no debounce. Otherwise wait *delay_ms* so a
        fast fly-by doesn't flash a tooltip.
        """
        if self._after_id is not None:
            self._parent.after_cancel(self._after_id)
            self._after_id = None

        self._text = text

        def _do_show() -> None:
            self._after_id = None
            if self._win is not None:
                self._win.destroy()
            tw = self._make_window(text)
            tw.update_idletasks()
            place(tw)
            tw.deiconify()
            self._win = tw

        if immediate:
            _do_show()
        else:
            self._after_id = self._parent.after(self._delay_ms, _do_show)

    def _place_left_of(self, tw: tk.Toplevel, x: int, y: int) -> None:
        """Position *tw* to the left of (*x*, *y*) in screen coordinates."""
        tip_w = tw.winfo_reqwidth()
        tw.wm_geometry(f"+{x - tip_w - 4}+{y + 8}")

    def _place_clamped(
        self,
        tw: tk.Toplevel,
        rx: int,
        ry: int,
        offset_x: int,
        offset_y: int,
    ) -> None:
        """Position *tw* near (*rx*, *ry*) with screen-edge clamping."""
        w = tw.winfo_reqwidth()
        h = tw.winfo_reqheight()
        sw = tw.winfo_screenwidth()
        sh = tw.winfo_screenheight()
        x = rx + offset_x
        y = ry + offset_y
        if x + w > sw:
            x = rx - w - offset_x
        if y + h > sh:
            y = ry - h - offset_y
        tw.wm_geometry(f"+{x}+{y}")

    def _make_window(self, text: str) -> tk.Toplevel:
        """Create and return a withdrawn, decorated Toplevel (not yet placed)."""
        tw = tk.Toplevel(self._parent)
        tw.withdraw()
        tw.wm_overrideredirect(True)
        try:
            tw.wm_attributes("-type", "tooltip")
        except tk.TclError:
            pass
        if self._alpha < 1.0:
            try:
                tw.wm_attributes("-alpha", self._alpha)
            except tk.TclError:
                pass
        tw.configure(bg=self._bg)
        tk.Label(
            tw,
            text=text,
            justify="left",
            bg=self._bg,
            fg=self._fg,
            font=self._font,
            padx=self._padx,
            pady=self._pady,
            wraplength=self._wraplength,
        ).pack()
        return tw
