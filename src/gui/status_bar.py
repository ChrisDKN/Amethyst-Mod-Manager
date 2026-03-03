"""
Status bar: log area and collapse/expand. Used by App.
"""

from datetime import datetime
import tkinter as tk
import customtkinter as ctk

from Utils.config_paths import get_logs_dir
from gui.ctk_components import CTkProgressPopup
from gui.theme import (
    BG_DEEP,
    BG_HEADER,
    BG_HOVER,
    BG_PANEL,
    BORDER,
    FONT_MONO,
    FONT_SMALL,
    TEXT_DIM,
    TEXT_MAIN,
)


# ---------------------------------------------------------------------------
# StatusBar
# ---------------------------------------------------------------------------
class StatusBar(ctk.CTkFrame):
    _COLLAPSED_H = 22   # height when log is hidden (just the label bar)
    _EXPANDED_H  = 100  # height when log is visible

    def __init__(self, parent):
        super().__init__(parent, fg_color=BG_DEEP, corner_radius=0,
                         height=self._COLLAPSED_H)
        self.grid_propagate(False)

        self._visible = False  # hidden by default

        ctk.CTkFrame(self, fg_color=BORDER, height=1, corner_radius=0).pack(
            side="top", fill="x"
        )

        label_bar = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0, height=20)
        label_bar.pack(side="top", fill="x")
        ctk.CTkLabel(
            label_bar, text="Log", font=FONT_SMALL, text_color=TEXT_DIM
        ).pack(side="left", padx=8)

        self._toggle_btn = ctk.CTkButton(
            label_bar, text="▲ Show", width=70, height=16,
            fg_color=BG_HEADER, hover_color=BG_HOVER,
            text_color=TEXT_DIM, font=FONT_SMALL,
            command=self._toggle_log,
        )
        self._toggle_btn.pack(side="right", padx=6, pady=2)

        self._progress_popup: CTkProgressPopup | None = None
        self._progress_bind_id: str | None = None

        self._textbox = ctk.CTkTextbox(
            self, font=FONT_MONO, fg_color=BG_DEEP,
            text_color=TEXT_MAIN, state="disabled",
            wrap="none", corner_radius=0
        )
        # Start hidden — don't pack the textbox yet

        # One log file per session, named with a timestamp
        _ts = datetime.now().strftime("%m-%d-%y-%H%M%S")
        self._log_file = get_logs_dir() / f"amethyst-{_ts}.log"

    def _toggle_log(self):
        self._visible = not self._visible
        if self._visible:
            self._textbox.pack(fill="both", expand=True)
            self.configure(height=self._EXPANDED_H)
            self._toggle_btn.configure(text="▼ Hide")
        else:
            self._textbox.pack_forget()
            self.configure(height=self._COLLAPSED_H)
            self._toggle_btn.configure(text="▲ Show")

    def show_log(self):
        """Ensure the log panel is expanded (no-op if already visible)."""
        if not self._visible:
            self._toggle_log()

    def _reposition_popup(self, *_) -> None:
        """Place the progress popup in the bottom-right corner of the root window."""
        p = self._progress_popup
        if p is None or not p.winfo_exists():
            return
        root = self.winfo_toplevel()
        x = root.winfo_width() - p.width - 20
        y = root.winfo_height() - p.height - 20
        p.place(x=x, y=y)

    def set_progress(self, done: int, total: int, phase: str | None = None) -> None:
        """Show / update the deploy progress popup. Call from main thread only."""
        root = self.winfo_toplevel()
        if self._progress_popup is None or not self._progress_popup.winfo_exists():
            self._progress_popup = CTkProgressPopup(
                root,
                title="Deploying",
                label=phase or "Working...",
                message=f"{done} / {total}",
            )
            # Silence the popup's own <Configure> handler (calls update_idletasks twice per event)
            self._progress_popup.update_position = lambda *_: None
            self._reposition_popup()
            self._progress_bind_id = root.bind("<Configure>", self._reposition_popup, add="+")
        frac = done / total if total > 0 else 0
        self._progress_popup.update_progress(frac)
        self._progress_popup.update_message(f"{done} / {total}")
        if phase is not None:
            self._progress_popup.update_label(phase)

    def clear_progress(self) -> None:
        """Close the deploy progress popup."""
        bid = getattr(self, "_progress_bind_id", None)
        if bid is not None:
            try:
                self.winfo_toplevel().unbind("<Configure>", bid)
            except Exception:
                pass
            self._progress_bind_id = None
        if self._progress_popup is not None and self._progress_popup.winfo_exists():
            self._progress_popup.destroy()
        self._progress_popup = None

    def log(self, message: str):
        timestamp = datetime.now().strftime("%H:%M:%S")
        self._textbox.configure(state="normal")
        self._textbox.insert("end", f"[{timestamp}]  {message}\n")
        self._textbox.see("end")
        self._textbox.configure(state="disabled")
        # Append to log file with full timestamp
        try:
            with open(self._log_file, "a", encoding="utf-8") as f:
                full_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                f.write(f"[{full_ts}]  {message}\n")
        except OSError:
            pass


# ---------------------------------------------------------------------------
# App update check
# ---------------------------------------------------------------------------
_APP_UPDATE_VERSION_URL = "https://raw.githubusercontent.com/ChrisDKN/Amethyst-Mod-Manager/main/src/version.py"
_APP_UPDATE_RELEASES_URL = "https://github.com/ChrisDKN/Amethyst-Mod-Manager/releases"

