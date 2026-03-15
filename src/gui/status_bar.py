"""
Status bar: log area and collapse/expand. Used by App.
"""

from datetime import datetime
import os
import subprocess
import sys
import tkinter as tk
import customtkinter as ctk

from Utils.config_paths import get_logs_dir
from Utils.ui_config import load_ui_scale, save_ui_scale, detect_hidpi_scale
from gui.ctk_components import CTkProgressPopup
from gui.theme import (
    ACCENT,
    ACCENT_HOV,
    BG_DEEP,
    BG_HEADER,
    BG_HOVER,
    BG_PANEL,
    BORDER,
    FONT_MONO,
    FONT_SMALL,
    FONT_NORMAL,
    scaled,
    TEXT_DIM,
    TEXT_MAIN,
    TEXT_WARN,
)


# ---------------------------------------------------------------------------
# StatusBar
# ---------------------------------------------------------------------------
class StatusBar(ctk.CTkFrame):
    _COLLAPSED_H = scaled(22)   # height when log is hidden (just the label bar)
    _EXPANDED_H  = scaled(100)  # height when log is visible

    def __init__(self, parent):
        super().__init__(parent, fg_color=BG_DEEP, corner_radius=0,
                         height=self._COLLAPSED_H)
        self.grid_propagate(False)

        self._visible = False  # hidden by default

        ctk.CTkFrame(self, fg_color=BORDER, height=1, corner_radius=0).pack(
            side="top", fill="x"
        )

        label_bar = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0, height=scaled(20))
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

        ctk.CTkButton(
            label_bar, text="Open Logs", width=70, height=16,
            fg_color=BG_HEADER, hover_color=BG_HOVER,
            text_color=TEXT_DIM, font=FONT_SMALL,
            command=self._open_logs_folder,
        ).pack(side="right", padx=(0, 0), pady=2)

        ctk.CTkButton(
            label_bar, text="Settings", width=70, height=16,
            fg_color="#b35a00", hover_color="#d06a00",
            text_color="#ffffff", font=FONT_SMALL,
            command=self._open_settings,
        ).pack(side="right", padx=(0, 4), pady=2)

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

    def _open_settings(self):
        root = self.winfo_toplevel()
        if hasattr(root, "show_settings_panel"):
            root.show_settings_panel()

    def _open_logs_folder(self):
        logs_dir = get_logs_dir()
        logs_dir.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(["xdg-open", str(logs_dir)])

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
        """Position the progress popup (CTkToplevel) at bottom-right."""
        p = self._progress_popup
        if p is None or not p.winfo_exists():
            return
        p._update_geometry()

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
            self._progress_popup.update_position = self._reposition_popup
            self._progress_popup._configure_bid = root.bind("<Configure>", self._reposition_popup, add="+")
            self._reposition_popup()
        frac = done / total if total > 0 else 0
        self._progress_popup.update_progress(frac)
        self._progress_popup.update_message(f"{done} / {total}")
        if phase is not None:
            self._progress_popup.update_label(phase)

    def clear_progress(self) -> None:
        """Close the deploy progress popup."""
        p = getattr(self, "_progress_popup", None)
        if p is not None and p.winfo_exists():
            bid = getattr(p, "_configure_bid", None)
            if bid is not None:
                try:
                    self.winfo_toplevel().unbind("<Configure>", bid)
                except Exception:
                    pass
            p.destroy()
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
# Settings overlay (inline panel — parents to plugin_panel_container)
# ---------------------------------------------------------------------------
class SettingsPanel(ctk.CTkFrame):
    """Inline settings panel that overlays the plugin panel."""

    def __init__(self, parent, on_done=None):
        super().__init__(parent, fg_color=BG_DEEP, corner_radius=0)
        self._on_done = on_done or (lambda p: None)
        self._build()

    def _build(self):
        self.grid_rowconfigure(0, weight=0)
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)

        # ---- title bar ----
        title_bar = ctk.CTkFrame(self, fg_color=BG_HEADER, corner_radius=0, height=scaled(40))
        title_bar.grid(row=0, column=0, sticky="ew")
        title_bar.grid_propagate(False)
        ctk.CTkLabel(title_bar, text="Settings", font=FONT_NORMAL, text_color=TEXT_MAIN,
                     anchor="w").pack(side="left", padx=12, pady=8)
        ctk.CTkButton(
            title_bar, text="✕", width=scaled(32), height=scaled(32), font=FONT_NORMAL,
            fg_color="transparent", hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_close,
        ).pack(side="right", padx=4, pady=4)

        # ---- body ----
        body = ctk.CTkFrame(self, fg_color=BG_DEEP, corner_radius=0)
        body.grid(row=1, column=0, sticky="nsew", padx=20, pady=20)

        ctk.CTkLabel(body, text="UI Scaling", font=FONT_NORMAL, text_color=TEXT_MAIN,
                     anchor="w").grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))

        current_ini = self._read_raw_ini()
        is_auto = (current_ini == "auto")
        init_scale = detect_hidpi_scale() if is_auto else (float(current_ini) if current_ini else 1.0)

        self._scale_var = tk.DoubleVar(value=round(init_scale, 1))
        self._slider = ctk.CTkSlider(
            body, from_=1.0, to=2.0, number_of_steps=10,
            variable=self._scale_var,
            width=scaled(220),
            command=self._on_slider,
        )
        self._slider.grid(row=1, column=0, sticky="w", padx=(0, 10))

        self._scale_lbl = ctk.CTkLabel(body, text=f"{init_scale:.1f}×",
                                       font=FONT_NORMAL, text_color=TEXT_MAIN, width=scaled(40))
        self._scale_lbl.grid(row=1, column=1, sticky="w")

        self._auto_var = tk.BooleanVar(value=is_auto)
        ctk.CTkCheckBox(
            body, text="Auto", variable=self._auto_var,
            font=FONT_NORMAL, text_color=TEXT_MAIN,
            command=self._on_auto_toggle,
        ).grid(row=1, column=2, sticky="w", padx=(8, 0))

        ctk.CTkLabel(body, text="Changes take effect after restart.",
                     font=FONT_SMALL, text_color=TEXT_WARN, anchor="w",
                     ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(12, 0))

        self._update_slider_state()

        # ---- footer ----
        foot = ctk.CTkFrame(self, fg_color=BG_HEADER, corner_radius=0, height=scaled(44))
        foot.grid(row=2, column=0, sticky="ew")
        foot.grid_propagate(False)
        self.grid_rowconfigure(2, weight=0)

        ctk.CTkButton(foot, text="Cancel", width=scaled(80), height=scaled(28),
                      fg_color=BG_DEEP, hover_color=BG_HOVER, text_color=TEXT_DIM,
                      font=FONT_NORMAL, command=self._on_close,
                      ).pack(side="right", padx=8, pady=8)

        ctk.CTkButton(foot, text="Apply & Restart", width=scaled(120), height=scaled(28),
                      fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="#ffffff",
                      font=FONT_NORMAL, command=self._apply,
                      ).pack(side="right", padx=(0, 0), pady=8)

    def _read_raw_ini(self) -> str:
        import configparser
        from Utils.ui_config import get_ui_config_path
        path = get_ui_config_path()
        if not path.is_file():
            return "auto"
        try:
            p = configparser.ConfigParser()
            p.read(path)
            return p.get("ui", "scale", fallback="auto").strip().lower()
        except Exception:
            return "auto"

    def _on_slider(self, _value=None):
        self._scale_lbl.configure(text=f"{round(self._scale_var.get(), 1):.1f}×")

    def _on_auto_toggle(self):
        self._update_slider_state()
        if self._auto_var.get():
            detected = detect_hidpi_scale()
            self._scale_var.set(round(detected, 1))
            self._scale_lbl.configure(text=f"{detected:.1f}×")

    def _update_slider_state(self):
        self._slider.configure(state="disabled" if self._auto_var.get() else "normal")

    def _on_close(self):
        self._on_done(self)

    def _apply(self):
        if self._auto_var.get():
            save_ui_scale("auto")
        else:
            save_ui_scale(round(self._scale_var.get(), 1))
        self._on_done(self)
        python = sys.executable
        os.execv(python, [python] + sys.argv)


# ---------------------------------------------------------------------------
# App update check
# ---------------------------------------------------------------------------
_APP_UPDATE_VERSION_URL = "https://raw.githubusercontent.com/ChrisDKN/Amethyst-Mod-Manager/main/src/version.py"
_APP_UPDATE_RELEASES_URL = "https://github.com/ChrisDKN/Amethyst-Mod-Manager/releases"

