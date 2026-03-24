"""
Status bar: log area and collapse/expand. Used by App.
"""

from datetime import datetime
import os
import subprocess
import sys
import threading
import webbrowser
import tkinter as tk
import customtkinter as ctk

from pathlib import Path

from Utils.config_paths import get_logs_dir, get_download_cache_dir, get_profiles_dir, get_config_dir
from Utils.xdg import xdg_open
from Utils.ui_config import (
    load_ui_scale, save_ui_scale, detect_hidpi_scale,
    load_collection_settings, save_collection_settings,
)
from gui.ctk_components import CTkProgressPopup, CTkAlert, CTkNotification
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
    TEXT_ERR,
    TEXT_MAIN,
    TEXT_OK,
    TEXT_WARN,
)


def _fmt_size(n_bytes: int) -> str:
    if n_bytes <= 0:
        return "—"
    for unit, threshold in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if n_bytes >= threshold:
            return f"{n_bytes / threshold:.1f} {unit}"
    return f"{n_bytes} B"


def _get_dir_size(path: Path) -> int:
    if not path.is_dir():
        return 0
    total = 0
    try:
        for p in path.rglob("*"):
            if p.is_file():
                total += p.stat().st_size
    except OSError:
        pass
    return total


def _get_orphaned_tmp_dirs() -> list:
    """Return a list of orphaned modmgr_* temp dirs across all known staging paths."""
    import json
    found = []
    search_roots: list[Path] = []

    # Collect staging paths from all games' paths.json
    try:
        games_dir = get_config_dir() / "games"
        for paths_json in games_dir.rglob("paths.json"):
            try:
                data = json.loads(paths_json.read_text())
                sp = data.get("staging_path", "")
                if sp:
                    search_roots.append(Path(sp))
            except Exception:
                pass
    except Exception:
        pass

    # Also include the env-var profiles dir as a fallback
    try:
        search_roots.append(get_profiles_dir())
    except Exception:
        pass

    seen: set[Path] = set()
    for root in search_roots:
        if root in seen or not root.is_dir():
            continue
        seen.add(root)
        try:
            for tmp_dir in root.rglob("modmgr_*"):
                if tmp_dir.is_dir():
                    found.append(tmp_dir)
        except Exception:
            pass
    return found


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

        self._count_label = ctk.CTkLabel(
            label_bar, text="", font=FONT_SMALL, text_color=TEXT_DIM
        )
        self._count_label.pack(side="left", padx=(4, 0))

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

        ctk.CTkButton(
            label_bar, text="♥ Endorse AMM", width=105, height=16,
            fg_color="#7a2a2a", hover_color="#9a3535",
            text_color="#ffffff", font=FONT_SMALL,
            command=self._endorse_amm,
        ).pack(side="right", padx=(0, 2), pady=2)

        ctk.CTkButton(
            label_bar, text="Github", width=60, height=16,
            fg_color="#24292e", hover_color="#3a3f44",
            text_color="#ffffff", font=FONT_SMALL,
            command=lambda: webbrowser.open("https://github.com/ChrisDKN/Amethyst-Mod-Manager"),
        ).pack(side="right", padx=(0, 2), pady=2)

        ctk.CTkButton(
            label_bar, text="Changelog", width=75, height=16,
            fg_color="#24292e", hover_color="#3a3f44",
            text_color="#ffffff", font=FONT_SMALL,
            command=self._open_changelog,
        ).pack(side="right", padx=(0, 2), pady=2)

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

    def _open_changelog(self):
        app = self.winfo_toplevel()
        mod_panel = getattr(app, "_mod_panel", None)
        if mod_panel is not None:
            mod_panel._on_changelog()

    def _open_settings(self):
        root = self.winfo_toplevel()
        if hasattr(root, "show_settings_panel"):
            root.show_settings_panel()

    def _endorse_amm(self):
        _AMM_URL = "https://www.nexusmods.com/site/mods/1714"
        app = self.winfo_toplevel()
        api = getattr(app, "_nexus_api", None)
        if api is None:
            webbrowser.open(_AMM_URL)
            return

        def _notify(state, message):
            self.log(f"Nexus: {message}")
            CTkNotification(app, state=state, message=message)

        def _worker():
            import requests
            try:
                result = api.endorse_mod("site", 1714)
            except requests.HTTPError as exc:
                try:
                    result = exc.response.json()
                except Exception:
                    self.after(0, lambda e=exc: _notify("error", f"Endorse AMM failed — {e}"))
                    return
            except Exception as exc:
                self.after(0, lambda e=exc: _notify("error", f"Endorse AMM failed — {e}"))
                return

            status = result.get("status", "")
            message = result.get("message", "")
            if status == "Endorsed" or message == "IS_OWN_MOD":
                self.after(0, lambda: _notify("info", "Thank you for endorsing"))
            elif message == "ALREADY_ENDORSED":
                self.after(0, lambda: _notify("info", "You've already endorsed, Thank you"))
            else:
                self.after(0, lambda s=status, m=message: _notify("warning", f"Endorse AMM: {m or s}"))

        threading.Thread(target=_worker, daemon=True).start()

    def _open_logs_folder(self):
        logs_dir = get_logs_dir()
        logs_dir.mkdir(parents=True, exist_ok=True)
        xdg_open(logs_dir)

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

    def set_mod_count(self, text: str) -> None:
        """Update the x/y mods active label in the log bar."""
        self._count_label.configure(text=text)

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

    def set_progress(self, done: int, total: int, phase: str | None = None,
                     title: str = "Deploying") -> None:
        """Show / update the deploy/extract progress popup. Call from main thread only."""
        root = self.winfo_toplevel()
        if self._progress_popup is None or not self._progress_popup.winfo_exists():
            self._progress_popup = CTkProgressPopup(
                root,
                title=title,
                label=phase or "Working...",
                message=f"{done} / {total}",
            )
            self._progress_popup.update_position = self._reposition_popup
            self._progress_popup._configure_bid = root.bind("<Configure>", self._reposition_popup, add="+")
            self._reposition_popup()
        if total > 0:
            pb = self._progress_popup.progressbar
            if pb.cget("mode") == "indeterminate":
                pb.stop()
                pb.configure(mode="determinate")
            frac = done / total
            self._progress_popup.update_progress(frac)
            self._progress_popup.update_message(f"{done} / {total}")
        else:
            # Indeterminate — animate the bar
            pb = self._progress_popup.progressbar
            if pb.cget("mode") != "indeterminate":
                pb.configure(mode="indeterminate")
                pb.start()
            self._progress_popup.update_message("")
        if phase is not None:
            self._progress_popup.update_label(phase)

    def clear_progress(self) -> None:
        """Close the deploy/extract progress popup."""
        p = getattr(self, "_progress_popup", None)
        if p is not None and p.winfo_exists():
            try:
                if p.progressbar.cget("mode") == "indeterminate":
                    p.progressbar.stop()
            except Exception:
                pass
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

        self._scale_var = tk.DoubleVar(value=round(init_scale * 20) / 20)
        self._slider = ctk.CTkSlider(
            body, from_=1.0, to=2.0, number_of_steps=20,
            variable=self._scale_var,
            width=scaled(220),
            command=self._on_slider,
        )
        self._slider.grid(row=1, column=0, sticky="w", padx=(0, 10))

        self._scale_lbl = ctk.CTkLabel(body, text=f"{round(init_scale * 20) / 20:.2f}×",
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

        # ---- download cache ----
        ctk.CTkFrame(body, fg_color=BORDER, height=1).grid(
            row=3, column=0, columnspan=3, sticky="ew", pady=(20, 0))

        ctk.CTkLabel(body, text="Download Cache", font=FONT_NORMAL, text_color=TEXT_MAIN,
                     anchor="w").grid(row=4, column=0, columnspan=3, sticky="w", pady=(12, 4))

        cache_row = ctk.CTkFrame(body, fg_color="transparent")
        cache_row.grid(row=5, column=0, columnspan=3, sticky="w")

        self._clear_cache_btn = ctk.CTkButton(
            cache_row, text="Clear Cache (—)",
            height=scaled(28), font=FONT_NORMAL,
            fg_color="#5a3a00", hover_color="#7a5200", text_color="#ffffff",
            command=self._on_clear_cache,
        )
        self._clear_cache_btn.pack(side="left")

        self._cache_status_lbl = ctk.CTkLabel(
            cache_row, text="", font=FONT_SMALL, text_color=TEXT_DIM, anchor="w")
        self._cache_status_lbl.pack(side="left", padx=(10, 0))

        self.after(100, self._refresh_cache_size)

        # ---- collections ----
        ctk.CTkFrame(body, fg_color=BORDER, height=1).grid(
            row=6, column=0, columnspan=3, sticky="ew", pady=(20, 0))

        ctk.CTkLabel(body, text="Collections", font=FONT_NORMAL, text_color=TEXT_MAIN,
                     anchor="w").grid(row=7, column=0, columnspan=3, sticky="w", pady=(12, 8))

        _col_cfg = load_collection_settings()

        # Downloads sub-label
        ctk.CTkLabel(body, text="Downloads", font=FONT_SMALL, text_color=TEXT_DIM,
                     anchor="w").grid(row=8, column=0, columnspan=3, sticky="w", pady=(0, 4))

        dl_order_row = ctk.CTkFrame(body, fg_color="transparent")
        dl_order_row.grid(row=9, column=0, columnspan=3, sticky="w", pady=(0, 6))

        ctk.CTkLabel(dl_order_row, text="Order:", font=FONT_NORMAL, text_color=TEXT_MAIN,
                     ).pack(side="left", padx=(0, 8))

        self._dl_order_var = tk.StringVar(value=_col_cfg["download_order"])
        ctk.CTkOptionMenu(
            dl_order_row,
            variable=self._dl_order_var,
            values=["largest", "smallest"],
            width=scaled(120),
            font=FONT_NORMAL,
        ).pack(side="left")

        dl_concurrent_row = ctk.CTkFrame(body, fg_color="transparent")
        dl_concurrent_row.grid(row=10, column=0, columnspan=3, sticky="w", pady=(0, 8))

        ctk.CTkLabel(dl_concurrent_row, text="Max concurrent:", font=FONT_NORMAL, text_color=TEXT_MAIN,
                     ).pack(side="left", padx=(0, 8))

        self._max_concurrent_var = tk.DoubleVar(value=float(_col_cfg["max_concurrent"]))
        ctk.CTkSlider(
            dl_concurrent_row, from_=1, to=5, number_of_steps=4,
            variable=self._max_concurrent_var,
            width=scaled(200),
            command=lambda _v: self._max_concurrent_lbl.configure(
                text=str(int(round(self._max_concurrent_var.get())))),
        ).pack(side="left")

        self._max_concurrent_lbl = ctk.CTkLabel(
            dl_concurrent_row, text=str(_col_cfg["max_concurrent"]),
            font=FONT_NORMAL, text_color=TEXT_MAIN, width=scaled(20))
        self._max_concurrent_lbl.pack(side="left", padx=(6, 0))

        # Install sub-label
        ctk.CTkLabel(body, text="Install", font=FONT_SMALL, text_color=TEXT_DIM,
                     anchor="w").grid(row=11, column=0, columnspan=3, sticky="w", pady=(0, 4))

        inst_row = ctk.CTkFrame(body, fg_color="transparent")
        inst_row.grid(row=12, column=0, columnspan=3, sticky="w")

        ctk.CTkLabel(inst_row, text="Order:", font=FONT_NORMAL, text_color=TEXT_MAIN,
                     ).pack(side="left", padx=(0, 8))

        self._inst_order_var = tk.StringVar(value=_col_cfg["install_order"])
        ctk.CTkOptionMenu(
            inst_row,
            variable=self._inst_order_var,
            values=["smallest", "largest"],
            width=scaled(120),
            font=FONT_NORMAL,
        ).pack(side="left")

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

        ctk.CTkButton(foot, text="Save", width=scaled(80), height=scaled(28),
                      fg_color="#3a5a3a", hover_color="#4a7a4a", text_color="#ffffff",
                      font=FONT_NORMAL, command=self._save_no_restart,
                      ).pack(side="right", padx=(0, 4), pady=8)

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
        v = round(self._scale_var.get() * 20) / 20
        self._scale_lbl.configure(text=f"{v:.2f}×")

    def _on_auto_toggle(self):
        self._update_slider_state()
        if self._auto_var.get():
            detected = detect_hidpi_scale()
            self._scale_var.set(round(detected * 20) / 20)
            self._scale_lbl.configure(text=f"{round(detected * 20) / 20:.2f}×")

    def _update_slider_state(self):
        self._slider.configure(state="disabled" if self._auto_var.get() else "normal")

    def _refresh_cache_size(self):
        cache_dir = get_download_cache_dir()

        def _worker():
            orphans = _get_orphaned_tmp_dirs()
            size = _get_dir_size(cache_dir) + sum(_get_dir_size(d) for d in orphans)
            try:
                self.after(0, lambda: self._update_clear_cache_btn(size))
            except Exception:
                pass

        import threading
        threading.Thread(target=_worker, daemon=True).start()

    def _update_clear_cache_btn(self, size_bytes: int):
        try:
            if hasattr(self, "_clear_cache_btn") and self._clear_cache_btn.winfo_exists():
                self._clear_cache_btn.configure(text=f"Clear Cache ({_fmt_size(size_bytes)})")
        except Exception:
            pass

    def _on_clear_cache(self):
        import shutil, threading
        cache_dir = get_download_cache_dir()
        self._cache_status_lbl.configure(text="Calculating…", text_color=TEXT_DIM)

        def _size_worker():
            orphans = _get_orphaned_tmp_dirs()
            size = _get_dir_size(cache_dir) + sum(_get_dir_size(d) for d in orphans)
            self.after(0, lambda: _show_confirm(size, orphans))

        def _show_confirm(size, orphans):
            try:
                if not self._cache_status_lbl.winfo_exists():
                    return
            except Exception:
                return
            self._cache_status_lbl.configure(text="", text_color=TEXT_DIM)
            if size <= 0 and not orphans:
                self._cache_status_lbl.configure(text="Cache is empty.", text_color=TEXT_DIM)
                return

            alert = CTkAlert(
                state="warning",
                title="Clear Download Cache",
                body_text=(
                    f"Clear {_fmt_size(size)} of cached downloads?\n\n"
                    f"Location: {cache_dir}\n\n"
                    "This removes archives downloaded for collection installs. "
                    "They will be re-downloaded if you install collections again."
                ),
                btn1="Clear",
                btn2="Cancel",
                parent=self.winfo_toplevel(),
                height=280,
            )
            if alert.get() != "Clear":
                return

            def _clear_worker():
                cleared = 0
                try:
                    for p in cache_dir.iterdir():
                        try:
                            if p.is_file():
                                p.unlink(missing_ok=True)
                                cleared += 1
                            elif p.is_dir():
                                shutil.rmtree(p, ignore_errors=True)
                                cleared += 1
                        except OSError:
                            pass
                    # Remove orphaned modmgr_* temp dirs left in profile directories
                    for tmp_dir in orphans:
                        try:
                            shutil.rmtree(tmp_dir, ignore_errors=True)
                            cleared += 1
                        except OSError:
                            pass
                    self.after(0, lambda: _done(cleared))
                except Exception as exc:
                    self.after(0, lambda: self._cache_status_lbl.configure(
                        text=f"Failed: {exc}", text_color=TEXT_ERR))

            def _done(n):
                self._cache_status_lbl.configure(
                    text=f"Cleared ({n} items).", text_color=TEXT_OK)
                self._refresh_cache_size()

            self._cache_status_lbl.configure(text="Clearing…", text_color=TEXT_DIM)
            threading.Thread(target=_clear_worker, daemon=True).start()

        threading.Thread(target=_size_worker, daemon=True).start()

    def _save_no_restart(self):
        """Save collection settings (and scale) without restarting."""
        if self._auto_var.get():
            save_ui_scale("auto")
        else:
            save_ui_scale(round(self._scale_var.get() * 20) / 20)
        save_collection_settings(
            download_order=self._dl_order_var.get(),
            max_concurrent=int(round(self._max_concurrent_var.get())),
            install_order=self._inst_order_var.get(),
        )
        self._on_done(self)

    def _on_close(self):
        self._on_done(self)

    def _apply(self):
        if self._auto_var.get():
            save_ui_scale("auto")
        else:
            save_ui_scale(round(self._scale_var.get() * 20) / 20)
        save_collection_settings(
            download_order=self._dl_order_var.get(),
            max_concurrent=int(round(self._max_concurrent_var.get())),
            install_order=self._inst_order_var.get(),
        )
        self._on_done(self)
        python = sys.executable
        os.execv(python, [python] + sys.argv)


# ---------------------------------------------------------------------------
# App update check
# ---------------------------------------------------------------------------
_APP_UPDATE_VERSION_URL = "https://raw.githubusercontent.com/ChrisDKN/Amethyst-Mod-Manager/main/src/version.py"
_APP_UPDATE_RELEASES_URL = "https://github.com/ChrisDKN/Amethyst-Mod-Manager/releases"

