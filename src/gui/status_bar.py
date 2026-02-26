"""
Status bar: log area and collapse/expand. Used by App.
"""

from datetime import datetime
import tkinter as tk
import customtkinter as ctk

from Utils.config_paths import get_config_dir
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

        # Progress bar + label (hidden until a deploy is in progress)
        self._progress_label = ctk.CTkLabel(
            label_bar, text="", font=FONT_SMALL, text_color=TEXT_DIM, width=120, anchor="e"
        )
        self._progress_bar = ctk.CTkProgressBar(
            label_bar, width=180, height=10,
            fg_color=BG_HEADER, progress_color="#7aa2f7", corner_radius=4
        )
        self._progress_bar.set(0)
        self._progress_visible = False
        self._progress_phase = ""

        self._textbox = ctk.CTkTextbox(
            self, font=FONT_MONO, fg_color=BG_DEEP,
            text_color=TEXT_MAIN, state="disabled",
            wrap="none", corner_radius=0
        )
        # Start hidden — don't pack the textbox yet

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

    def set_progress(self, done: int, total: int, phase: str | None = None) -> None:
        """Show / update the progress bar.  Call from main thread only.
        phase: optional label (e.g. 'Unpacking', 'Repacking'); kept until next set_progress with a different phase.
        """
        if not self._progress_visible:
            # Pack bar first (rightmost after toggle btn), then label to its left.
            self._progress_bar.pack(side="right", padx=(0, 8))
            self._progress_label.pack(side="right", padx=(0, 4))
            self._progress_visible = True
        if phase is not None:
            self._progress_phase = phase
        phase_str = getattr(self, "_progress_phase", "") or ""
        label = f"{phase_str}: {done} / {total}" if phase_str else f"{done} / {total}"
        frac = done / total if total > 0 else 0
        self._progress_bar.set(frac)
        self._progress_label.configure(text=label)

    def clear_progress(self) -> None:
        """Hide the progress bar when the operation finishes."""
        if self._progress_visible:
            self._progress_bar.pack_forget()
            self._progress_label.pack_forget()
            self._progress_visible = False
        self._progress_bar.set(0)
        self._progress_label.configure(text="")
        self._progress_phase = ""

    def log(self, message: str):
        timestamp = datetime.now().strftime("%H:%M:%S")
        self._textbox.configure(state="normal")
        self._textbox.insert("end", f"[{timestamp}]  {message}\n")
        self._textbox.see("end")
        self._textbox.configure(state="disabled")
        # Append to log file with full timestamp
        try:
            log_path = get_config_dir() / "amethyst.log"
            with open(log_path, "a", encoding="utf-8") as f:
                full_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                f.write(f"[{full_ts}]  {message}\n")
        except OSError:
            pass


# ---------------------------------------------------------------------------
# App update check
# ---------------------------------------------------------------------------
_APP_UPDATE_VERSION_URL = "https://raw.githubusercontent.com/ChrisDKN/Amethyst-Mod-Manager/main/src/version.py"
_APP_UPDATE_RELEASES_URL = "https://github.com/ChrisDKN/Amethyst-Mod-Manager/releases"

