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
    load_normalize_folder_case, save_normalize_folder_case,
    load_clear_archive_after_install, save_clear_archive_after_install,
    load_keep_fomod_archives, save_keep_fomod_archives,
    load_rename_mod_after_install, save_rename_mod_after_install,
    load_restore_on_close, save_restore_on_close,
    load_allow_prerelease, save_allow_prerelease,
    load_dev_mode,
    load_heroic_config_path, save_heroic_config_path,
    load_steam_libraries_vdf_path, save_steam_libraries_vdf_path,
    load_default_staging_path, save_default_staging_path,
    load_font_family, save_font_family, get_font_family,
    THEME_DEFAULTS, get_theme_color, save_theme_color,
    get_appearance_mode, save_appearance_mode,
)
from gui.ctk_components import CTkProgressPopup, CTkAlert, CTkNotification
from gui.version_check import is_appimage, is_flatpak
from gui.wheel_compat import LEGACY_WHEEL_REDUNDANT
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
    TEXT_ON_ACCENT,
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
        self._current_h = self._COLLAPSED_H

        self._log_buffer: list[tuple[str, str]] = []   # (line, tag)
        self._log_entries: list[tuple[str, str]] = []  # full history for filtering
        self._log_flush_id: str | None = None

        self._drag_y: int | None = None
        self._drag_h: int | None = None
        self._drag_target_h: int = self._COLLAPSED_H
        self._drag_pending: bool = False
        self._ghost_line: tk.Toplevel | None = None

        # Drag-resize handle — a thin canvas strip (no layout recalc on redraw)
        self._drag_handle = tk.Canvas(
            self, bg=BORDER, height=4, highlightthickness=0, cursor="sb_v_double_arrow"
        )
        self._drag_handle.pack(side="top", fill="x")
        self._drag_handle.bind("<ButtonPress-1>", self._on_drag_start)
        self._drag_handle.bind("<B1-Motion>", self._on_drag_motion)
        self._drag_handle.bind("<ButtonRelease-1>", self._on_drag_end)

        label_bar = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0, height=scaled(20))
        label_bar.pack(side="top", fill="x")
        ctk.CTkLabel(
            label_bar, text="Log", font=FONT_SMALL, text_color=TEXT_DIM
        ).pack(side="left", padx=8)

        self._count_label = ctk.CTkLabel(
            label_bar, text="", font=FONT_SMALL, text_color=TEXT_DIM
        )
        self._count_label.pack(side="left", padx=(4, 0))

        # Filter checkboxes — when checked, only matching levels are shown
        # Created but not packed; shown/hidden with the log panel.
        self._filter_err_var = tk.BooleanVar(value=False)
        self._filter_warn_var = tk.BooleanVar(value=False)
        self._filter_err_cb = ctk.CTkCheckBox(
            label_bar, text="Errors", variable=self._filter_err_var,
            font=FONT_SMALL, text_color=TEXT_ERR,
            width=24, height=16, checkbox_width=14, checkbox_height=14,
            fg_color=TEXT_ERR, hover_color=TEXT_ERR,
            border_color=TEXT_ERR,
            command=self._apply_filter,
        )
        self._filter_warn_cb = ctk.CTkCheckBox(
            label_bar, text="Warnings", variable=self._filter_warn_var,
            font=FONT_SMALL, text_color=TEXT_WARN,
            width=24, height=16, checkbox_width=14, checkbox_height=14,
            fg_color=TEXT_WARN, hover_color=TEXT_WARN,
            border_color=TEXT_WARN,
            command=self._apply_filter,
        )

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
            label_bar, text="Ko-Fi", width=55, height=16,
            fg_color="#7b2d8b", hover_color="#9a3aae",
            text_color="#ffffff", font=FONT_SMALL,
            command=lambda: webbrowser.open("https://ko-fi.com/chrisdkn"),
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

        self._rate_limit_label = ctk.CTkLabel(
            label_bar, text="Nexus: —", font=FONT_SMALL, text_color=TEXT_DIM,
        )
        self._rate_limit_label.pack(side="right", padx=(0, 8), pady=2)

        from gui.tk_tooltip import TkTooltip
        self._rate_limit_tooltip = TkTooltip(
            self, bg=BG_HEADER, fg=TEXT_MAIN, font=FONT_SMALL,
        )
        self._rate_limit_tooltip_text = "Nexus API rate limits — no data yet."

        def _rl_enter(event):
            self._rate_limit_tooltip.show(
                event.x_root + 12, event.y_root + 12,
                self._rate_limit_tooltip_text,
            )

        def _rl_leave(_event):
            self._rate_limit_tooltip.hide()

        self._rate_limit_label.bind("<Enter>", _rl_enter, add="+")
        self._rate_limit_label.bind("<Leave>", _rl_leave, add="+")
        self._schedule_rate_limit_refresh()

        self._progress_popup: CTkProgressPopup | None = None
        self._progress_bind_id: str | None = None

        self._textbox = ctk.CTkTextbox(
            self, font=FONT_MONO, fg_color=BG_DEEP,
            text_color=TEXT_MAIN, state="disabled",
            wrap="none", corner_radius=0
        )
        # Coloured tags for error / warning lines
        inner = self._textbox._textbox  # underlying tk.Text
        inner.tag_configure("error", foreground=TEXT_ERR)
        inner.tag_configure("warning", foreground=TEXT_WARN)
        # Start hidden — don't pack the textbox yet

        # One log file per session, named with a timestamp
        _ts = datetime.now().strftime("%m-%d-%y-%H%M%S")
        self._log_file = get_logs_dir() / f"amethyst-{_ts}.log"

    def _open_changelog(self):
        app = self.winfo_toplevel()
        mod_panel = getattr(app, "_mod_panel", None)
        if mod_panel is not None:
            mod_panel._on_changelog()

    def _schedule_rate_limit_refresh(self):
        """Periodically refresh the rate-limit label from cached API state.

        Reads `api.rate_limits` only — never makes a network call. Values are
        captured passively from response headers on every Nexus API request.
        """
        self._refresh_rate_limit_label()
        try:
            self.after(10_000, self._schedule_rate_limit_refresh)
        except tk.TclError:
            pass

    def _refresh_rate_limit_label(self):
        try:
            label = self._rate_limit_label
        except AttributeError:
            return
        if not label.winfo_exists():
            return
        app = self.winfo_toplevel()
        api = getattr(app, "_nexus_api", None)
        r = getattr(api, "rate_limits", None) if api is not None else None
        if r is None or (r.hourly_remaining < 0 and r.daily_remaining < 0):
            label.configure(text="Nexus: —", text_color=TEXT_DIM)
            self._rate_limit_tooltip_text = (
                "Nexus API rate limits — no data yet.\n"
                "Values will appear after the first API request."
            )
            return

        h = r.hourly_remaining
        d = r.daily_remaining
        h_str = f"{h:,}" if h >= 0 else "—"
        d_str = f"{d:,}" if d >= 0 else "—"
        label.configure(text=f"Nexus H:{h_str}  D:{d_str}")
        if h >= 0 and r.hourly_limit > 0 and h < r.hourly_limit * 0.1:
            label.configure(text_color=TEXT_WARN)
        elif h == 0 or d == 0:
            label.configure(text_color=TEXT_ERR)
        else:
            label.configure(text_color=TEXT_DIM)

        if r.last_updated is not None:
            age = (datetime.now(r.last_updated.tzinfo) - r.last_updated).total_seconds()
            if age < 60:
                age_str = f"{int(age)}s ago"
            elif age < 3600:
                age_str = f"{int(age // 60)}m ago"
            else:
                age_str = f"{int(age // 3600)}h ago"
        else:
            age_str = "unknown"
        hl = f"{r.hourly_limit:,}" if r.hourly_limit > 0 else "—"
        dl = f"{r.daily_limit:,}" if r.daily_limit > 0 else "—"
        self._rate_limit_tooltip_text = (
            f"Nexus API rate limits\n"
            f"Hourly: {h_str} / {hl} remaining\n"
            f"Daily:  {d_str} / {dl} remaining\n"
            f"Last updated: {age_str}"
        )

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
            self._filter_err_cb.pack(side="left", padx=(12, 0))
            self._filter_warn_cb.pack(side="left", padx=(8, 0))
            self._textbox.pack(fill="both", expand=True)
            self._current_h = self._EXPANDED_H
            self._set_height(self._current_h)
            self._toggle_btn.configure(text="▼ Hide")
        else:
            self._filter_err_cb.pack_forget()
            self._filter_warn_cb.pack_forget()
            self._textbox.pack_forget()
            self._current_h = self._COLLAPSED_H
            self._set_height(self._current_h)
            self._toggle_btn.configure(text="▲ Show")

    def _on_drag_start(self, event: tk.Event) -> None:
        # Use actual rendered height as the drag baseline, not _current_h,
        # so Show-then-drag starts from the real panel size.
        actual_h = self.winfo_height()
        self._current_h = actual_h
        self._drag_y = event.y_root
        self._drag_h = actual_h
        self._drag_target_h = actual_h
        win = self.winfo_toplevel()
        self._panel_bottom_y = win.winfo_rooty() + win.winfo_height()
        self._ghost_offset = event.y_root - (self._panel_bottom_y - actual_h)
        self._show_ghost(event.y_root)

    def _on_drag_motion(self, event: tk.Event) -> None:
        if self._drag_y is None:
            return
        delta = self._drag_y - event.y_root
        win = self.winfo_toplevel()
        max_h = int(win.winfo_height() * 0.85)
        new_h = max(self._COLLAPSED_H, min(self._drag_h + delta, max_h))
        self._drag_target_h = new_h
        ghost_y = self._panel_bottom_y - new_h + self._ghost_offset
        self._move_ghost(ghost_y)

    def _on_drag_end(self, event: tk.Event) -> None:
        self._drag_y = None
        self._destroy_ghost()
        new_h = self._drag_target_h
        if new_h > self._COLLAPSED_H and not self._visible:
            self._visible = True
            self._filter_err_cb.pack(side="left", padx=(12, 0))
            self._filter_warn_cb.pack(side="left", padx=(8, 0))
            self._textbox.pack(fill="both", expand=True)
            self._toggle_btn.configure(text="▼ Hide")
        elif new_h <= self._COLLAPSED_H and self._visible:
            self._visible = False
            self._filter_err_cb.pack_forget()
            self._filter_warn_cb.pack_forget()
            self._textbox.pack_forget()
            self._toggle_btn.configure(text="▲ Show")
        self._current_h = new_h
        self._set_height(new_h)

    # --- ghost line helpers ---------------------------------------------------

    def _show_ghost(self, y_root: int) -> None:
        win = self.winfo_toplevel()
        self._ghost_line = tk.Toplevel(win)
        self._ghost_line.overrideredirect(True)
        self._ghost_line.attributes("-alpha", 0.6)
        self._ghost_line.configure(bg=ACCENT)
        self._ghost_line.attributes("-topmost", True)
        self._move_ghost(y_root)

    def _move_ghost(self, y_root: int) -> None:
        if self._ghost_line is None:
            return
        win_x = self.winfo_rootx()
        win_w = self.winfo_toplevel().winfo_width()
        self._ghost_line.geometry(f"{win_w}x2+{win_x}+{y_root}")

    def _destroy_ghost(self) -> None:
        if self._ghost_line is not None:
            self._ghost_line.destroy()
            self._ghost_line = None

    def set_resize_callback(self, fn) -> None:
        """Register a callback(height) that the app uses to resize the status bar row."""
        self._resize_callback = fn

    def _set_height(self, h: int) -> None:
        fn = getattr(self, "_resize_callback", None)
        if fn:
            fn(h)
        else:
            # Fallback: try to drive it ourselves
            info = self.grid_info()
            if info:
                row = int(info["row"])
                self.master.grid_rowconfigure(row, minsize=h, weight=0)
            self._desired_height = h
            super(ctk.CTkFrame, self).configure(height=h)

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

    _ERR_WORDS = ("error", "failed", "could not", "exception", "traceback",
                   "unexpected", "not found")
    _WARN_WORDS = ("warning", "warn", "skipping", "skipped", "falling back",
                   "fallback", "not supported", "ignored")

    @staticmethod
    def _classify(msg: str) -> str:
        stripped = msg.lstrip()
        # Raw API response bodies (from _log_response) often contain user-
        # facing strings like "error" inside mod descriptions or notes,
        # which would otherwise trip the error/warning filters. They're
        # diagnostic dumps, not app-generated log entries.
        if stripped.startswith("Response:"):
            return ""
        low = msg.lower()
        for w in StatusBar._ERR_WORDS:
            if w in low:
                return "error"
        for w in StatusBar._WARN_WORDS:
            if w in low:
                return "warning"
        return ""

    def log(self, message: str):
        timestamp = datetime.now().strftime("%H:%M:%S")
        tag = self._classify(message)
        self._log_buffer.append((f"[{timestamp}]  {message}\n", tag))
        # Append to log file immediately (cheap I/O, keeps file in sync)
        try:
            with open(self._log_file, "a", encoding="utf-8") as f:
                full_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                f.write(f"[{full_ts}]  {message}\n")
        except OSError:
            pass
        # Coalesce rapid messages into one widget update
        if self._log_flush_id is None:
            self._log_flush_id = self.after(16, self._flush_log)

    def _active_filters(self) -> set[str]:
        """Return the set of tags currently selected for filtering (empty = show all)."""
        f: set[str] = set()
        if self._filter_err_var.get():
            f.add("error")
        if self._filter_warn_var.get():
            f.add("warning")
        return f

    def _flush_log(self):
        self._log_flush_id = None
        entries = self._log_buffer
        if not entries:
            return
        self._log_buffer = []
        self._log_entries.extend(entries)
        filters = self._active_filters()
        if filters:
            entries = [(l, t) for l, t in entries if t in filters]
            if not entries:
                return
        inner = self._textbox._textbox
        self._textbox.configure(state="normal")
        for line, tag in entries:
            if tag:
                inner.insert("end", line, tag)
            else:
                inner.insert("end", line)
        self._textbox.see("end")
        self._textbox.configure(state="disabled")

    def _apply_filter(self):
        """Re-render the textbox based on the current filter checkboxes."""
        filters = self._active_filters()
        inner = self._textbox._textbox
        self._textbox.configure(state="normal")
        inner.delete("1.0", "end")
        if filters:
            visible = [(l, t) for l, t in self._log_entries if t in filters]
        else:
            visible = self._log_entries
        for line, tag in visible:
            if tag:
                inner.insert("end", line, tag)
            else:
                inner.insert("end", line)
        self._textbox.see("end")
        self._textbox.configure(state="disabled")


# ---------------------------------------------------------------------------
# Settings overlay (inline panel — parents to plugin_panel_container)
# ---------------------------------------------------------------------------
class SettingsPanel(ctk.CTkFrame):
    """Inline settings panel that overlays the plugin panel."""

    def __init__(self, parent, on_done=None):
        super().__init__(parent, fg_color=BG_DEEP, corner_radius=0)
        self._on_done = on_done or (lambda p: None)
        self._build()
        self.after(50, self._bind_scroll_recursive)

    def _on_prerelease_toggle(self):
        """Persist the pre-release setting and re-run the update check immediately.

        When *unticking* (opting out), pass force_downgrade_prompt=True so the
        user is offered a switch to the latest stable even if it's older than
        the pre-release they're currently running. When *ticking*, no force —
        the normal upgrade check already handles "is there a newer build?".
        """
        new_val = self._allow_prerelease_var.get()
        save_allow_prerelease(new_val)
        app = self.winfo_toplevel()
        check = getattr(app, "_check_for_app_update", None)
        if callable(check):
            check(force_downgrade_prompt=not new_val, force_fresh=True)

    def _bind_scroll_recursive(self, widget=None):
        """Bind Linux scroll-wheel events to every child so they propagate to the scrollable body.

        Interactive widgets that consume scroll themselves (sliders, option menus,
        comboboxes, spinboxes, scrollbars) are skipped — otherwise scrolling over
        one would both change its value AND scroll the panel.
        """
        if widget is None:
            widget = self
        # Skip widgets that already react to the scroll wheel.
        cls_name = widget.__class__.__name__
        _SKIP = (
            "CTkSlider", "CTkOptionMenu", "CTkComboBox", "CTkScrollbar",
            "Scale", "Spinbox", "Combobox", "Scrollbar", "Listbox",
        )
        if cls_name in _SKIP:
            return
        try:
            if not LEGACY_WHEEL_REDUNDANT:
                widget.bind("<Button-4>",   lambda e: self._body._parent_canvas.yview_scroll(-3, "units"), add="+")
                widget.bind("<Button-5>",   lambda e: self._body._parent_canvas.yview_scroll( 3, "units"), add="+")
            # On Tk >= 8.7, CTkScrollableFrame's own bind_all("<MouseWheel>") handler
            # already scrolls the body (with event.delta=±120 per notch). Adding another
            # MouseWheel binding here would stack on top, making scrolling far too fast.
        except Exception:
            pass
        for child in widget.winfo_children():
            self._bind_scroll_recursive(child)

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

        # ---- body (scrollable) ----
        self._body = ctk.CTkScrollableFrame(self, fg_color=BG_DEEP, corner_radius=0)
        self._body.grid(row=1, column=0, sticky="nsew", padx=20, pady=20)
        body = self._body

        # Alternating section background colors
        _SECTION_BG_A = BG_PANEL
        _SECTION_BG_B = BG_HEADER
        _section_idx = [0]

        def _begin_section(title: str) -> ctk.CTkFrame:
            """Create a rounded section frame with a centered title + separator.
            Returns the content frame that widgets should be parented to.
            """
            bg = _SECTION_BG_A if _section_idx[0] % 2 == 0 else _SECTION_BG_B
            _section_idx[0] += 1

            section = ctk.CTkFrame(body, fg_color=bg, corner_radius=8)
            section.pack(fill="x", pady=(0, 12), padx=0)

            ctk.CTkLabel(
                section, text=title, font=FONT_NORMAL, text_color=TEXT_MAIN, anchor="center",
            ).pack(fill="x", pady=(10, 4), padx=12)
            ctk.CTkFrame(section, fg_color=BORDER, height=1).pack(
                fill="x", padx=12, pady=(0, 10))

            content = ctk.CTkFrame(section, fg_color="transparent")
            content.pack(fill="x", padx=14, pady=(0, 12))
            return content

        # ==== User Interface ====
        ui_sec = _begin_section("User Interface")

        current_ini = self._read_raw_ini()
        is_auto = (current_ini == "auto")
        init_scale = detect_hidpi_scale() if is_auto else (float(current_ini) if current_ini else 1.0)

        scale_row = ctk.CTkFrame(ui_sec, fg_color="transparent")
        scale_row.pack(anchor="w")

        self._scale_var = tk.DoubleVar(value=round(init_scale * 20) / 20)
        self._slider = ctk.CTkSlider(
            scale_row, from_=1.0, to=2.0, number_of_steps=20,
            variable=self._scale_var,
            width=scaled(220),
            command=self._on_slider,
        )
        self._slider.pack(side="left", padx=(0, 10))

        self._scale_lbl = ctk.CTkLabel(scale_row, text=f"{round(init_scale * 20) / 20:.2f}×",
                                       font=FONT_NORMAL, text_color=TEXT_MAIN, width=scaled(40))
        self._scale_lbl.pack(side="left")

        self._auto_var = tk.BooleanVar(value=is_auto)
        ctk.CTkCheckBox(
            ui_sec, text="Auto", variable=self._auto_var,
            font=FONT_NORMAL, text_color=TEXT_MAIN,
            command=self._on_auto_toggle,
        ).pack(anchor="w", pady=(6, 0))

        # Font family picker
        _FONT_OPTIONS = ["Noto Sans", "Cantarell", "DejaVu Sans", "Liberation Sans", "Roboto"]
        _DEFAULT_FONT = "Noto Sans"

        font_row = ctk.CTkFrame(ui_sec, fg_color="transparent")
        font_row.pack(anchor="w", pady=(10, 0))

        ctk.CTkLabel(font_row, text="Font:", font=FONT_NORMAL, text_color=TEXT_MAIN,
                     ).pack(side="left", padx=(0, 8))

        self._font_var = tk.StringVar(value=get_font_family())
        ctk.CTkOptionMenu(
            font_row, values=_FONT_OPTIONS, variable=self._font_var,
            font=FONT_NORMAL, text_color=TEXT_MAIN,
            fg_color=BG_PANEL, button_color=ACCENT, button_hover_color=ACCENT_HOV,
            width=scaled(180),
        ).pack(side="left")

        ctk.CTkButton(
            font_row, text="Default", width=scaled(70), height=scaled(28),
            font=FONT_SMALL, fg_color=BG_DEEP, hover_color=BG_HOVER, text_color=TEXT_DIM,
            command=lambda: self._font_var.set(_DEFAULT_FONT),
        ).pack(side="left", padx=(8, 0))

        ctk.CTkLabel(ui_sec, text="Changes take effect after restart.",
                     font=FONT_SMALL, text_color=TEXT_WARN, anchor="w",
                     ).pack(anchor="w", pady=(8, 0))

        self._update_slider_state()

        # ==== Downloads ====
        dl_sec = _begin_section("Downloads")

        cache_row = ctk.CTkFrame(dl_sec, fg_color="transparent")
        cache_row.pack(anchor="w")

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

        self._clear_archive_var = tk.BooleanVar(value=load_clear_archive_after_install())
        ctk.CTkCheckBox(
            dl_sec, text="Clear archive after install", variable=self._clear_archive_var,
            font=FONT_NORMAL, text_color=TEXT_MAIN,
        ).pack(anchor="w", pady=(10, 0))

        self._keep_fomod_archives_var = tk.BooleanVar(value=load_keep_fomod_archives())
        ctk.CTkCheckBox(
            dl_sec, text="Keep FOMOD archives", variable=self._keep_fomod_archives_var,
            font=FONT_NORMAL, text_color=TEXT_MAIN,
        ).pack(anchor="w", pady=(6, 0))
        ctk.CTkLabel(
            dl_sec,
            text="When enabled, archives for mods that use a FOMOD installer are\n"
                 "always kept, even if 'Clear archive after install' is on.",
            font=FONT_SMALL, text_color=TEXT_DIM, anchor="w", justify="left",
        ).pack(anchor="w", pady=(2, 0))

        # ==== Collections ====
        col_sec = _begin_section("Collections")

        _col_cfg = load_collection_settings()

        dl_order_row = ctk.CTkFrame(col_sec, fg_color="transparent")
        dl_order_row.pack(anchor="w", pady=(0, 6))

        ctk.CTkLabel(dl_order_row, text="Download Order:", font=FONT_NORMAL, text_color=TEXT_MAIN,
                     ).pack(side="left", padx=(0, 8))

        _DL_ORDER_LABELS = {"largest": "Largest first", "smallest": "Smallest first"}
        _DL_ORDER_FROM_LABEL = {v: k for k, v in _DL_ORDER_LABELS.items()}
        self._dl_order_var = tk.StringVar(value=_DL_ORDER_LABELS.get(_col_cfg["download_order"], "Largest first"))
        self._dl_order_from_label = _DL_ORDER_FROM_LABEL
        ctk.CTkOptionMenu(
            dl_order_row,
            variable=self._dl_order_var,
            values=["Smallest first", "Largest first"],
            width=scaled(140),
            font=FONT_NORMAL,
        ).pack(side="left")

        dl_concurrent_row = ctk.CTkFrame(col_sec, fg_color="transparent")
        dl_concurrent_row.pack(anchor="w", pady=(0, 0))

        ctk.CTkLabel(dl_concurrent_row, text="Max concurrent:", font=FONT_NORMAL, text_color=TEXT_MAIN,
                     ).pack(side="left", padx=(0, 8))

        self._max_concurrent_var = tk.DoubleVar(value=float(_col_cfg["max_concurrent"]))
        ctk.CTkSlider(
            dl_concurrent_row, from_=1, to=8, number_of_steps=7,
            variable=self._max_concurrent_var,
            width=scaled(200),
            command=lambda _v: self._max_concurrent_lbl.configure(
                text=str(int(round(self._max_concurrent_var.get())))),
        ).pack(side="left")

        self._max_concurrent_lbl = ctk.CTkLabel(
            dl_concurrent_row, text=str(_col_cfg["max_concurrent"]),
            font=FONT_NORMAL, text_color=TEXT_MAIN, width=scaled(20))
        self._max_concurrent_lbl.pack(side="left", padx=(6, 0))

        ext_concurrent_row = ctk.CTkFrame(col_sec, fg_color="transparent")
        ext_concurrent_row.pack(anchor="w", pady=(0, 0))

        ctk.CTkLabel(ext_concurrent_row, text="Max extractions:", font=FONT_NORMAL, text_color=TEXT_MAIN,
                     ).pack(side="left", padx=(0, 8))

        self._max_extract_var = tk.DoubleVar(value=float(_col_cfg["max_extract_workers"]))
        ctk.CTkSlider(
            ext_concurrent_row, from_=1, to=8, number_of_steps=7,
            variable=self._max_extract_var,
            width=scaled(200),
            command=lambda _v: self._max_extract_lbl.configure(
                text=str(int(round(self._max_extract_var.get())))),
        ).pack(side="left")

        self._max_extract_lbl = ctk.CTkLabel(
            ext_concurrent_row, text=str(_col_cfg["max_extract_workers"]),
            font=FONT_NORMAL, text_color=TEXT_MAIN, width=scaled(20))
        self._max_extract_lbl.pack(side="left", padx=(6, 0))

        ctk.CTkLabel(
            col_sec,
            text="Extractions are gated by available memory — large archives\n"
                 "will wait for RAM headroom even if worker slots are free.",
            font=FONT_SMALL, text_color=TEXT_DIM, anchor="w", justify="left",
        ).pack(anchor="w", pady=(2, 0))

        self._check_dl_locations_var = tk.BooleanVar(value=_col_cfg["check_download_locations"])
        ctk.CTkCheckBox(
            col_sec, text="Check downloads locations", variable=self._check_dl_locations_var,
            font=FONT_NORMAL, text_color=TEXT_MAIN,
        ).pack(anchor="w", pady=(10, 0))
        ctk.CTkLabel(
            col_sec,
            text="When enabled, the system downloads folder and any custom locations\n"
                 "are scanned before downloading — existing archives are used directly.",
            font=FONT_SMALL, text_color=TEXT_DIM, anchor="w", justify="left",
        ).pack(anchor="w", pady=(2, 0))

        self._col_clear_archive_var = tk.BooleanVar(value=_col_cfg["clear_archive_after_install"])
        ctk.CTkCheckBox(
            col_sec, text="Clear archive after install", variable=self._col_clear_archive_var,
            font=FONT_NORMAL, text_color=TEXT_MAIN,
        ).pack(anchor="w", pady=(10, 0))
        ctk.CTkLabel(
            col_sec,
            text="When enabled, collection-downloaded archives in the download cache\n"
                 "are always removed after install, overriding the Downloads settings.\n"
                 "Archives found in your downloads locations are never deleted.",
            font=FONT_SMALL, text_color=TEXT_DIM, anchor="w", justify="left",
        ).pack(anchor="w", pady=(2, 0))

        # ==== General Settings ====
        gen_sec = _begin_section("General Settings")

        self._norm_case_var = tk.BooleanVar(value=load_normalize_folder_case())
        ctk.CTkCheckBox(
            gen_sec, text="Normalise folder casing", variable=self._norm_case_var,
            font=FONT_NORMAL, text_color=TEXT_MAIN,
        ).pack(anchor="w")

        ctk.CTkLabel(
            gen_sec,
            text="When enabled, folder names are unified to a single casing across all mods.\n"
                 "Disable this on case-insensitive (casefold) filesystems to avoid path conflicts.",
            font=FONT_SMALL, text_color=TEXT_DIM, anchor="w", justify="left",
        ).pack(anchor="w", pady=(4, 0))

        self._rename_after_install_var = tk.BooleanVar(value=load_rename_mod_after_install())
        ctk.CTkCheckBox(
            gen_sec, text="Rename mod after install",
            variable=self._rename_after_install_var,
            font=FONT_NORMAL, text_color=TEXT_MAIN,
        ).pack(anchor="w", pady=(10, 0))

        ctk.CTkLabel(
            gen_sec,
            text="Show a rename prompt after installing a mod.",
            font=FONT_SMALL, text_color=TEXT_DIM, anchor="w", justify="left",
        ).pack(anchor="w", pady=(2, 0))

        self._restore_on_close_var = tk.BooleanVar(value=load_restore_on_close())
        ctk.CTkCheckBox(
            gen_sec, text="Restore on close",
            variable=self._restore_on_close_var,
            font=FONT_NORMAL, text_color=TEXT_MAIN,
        ).pack(anchor="w", pady=(10, 0))

        ctk.CTkLabel(
            gen_sec,
            text="Restore all deployed games to vanilla when the app is closed.",
            font=FONT_SMALL, text_color=TEXT_DIM, anchor="w", justify="left",
        ).pack(anchor="w", pady=(2, 0))

        # Pre-release channel toggle is only meaningful for AppImage installs
        # (Flatpak and AUR are managed externally and we can't auto-switch them).
        # Dev mode forces it visible so source-checkout users can exercise it.
        if (is_appimage() and not is_flatpak()) or load_dev_mode():
            self._allow_prerelease_var = tk.BooleanVar(value=load_allow_prerelease())
            ctk.CTkCheckBox(
                gen_sec, text="Use pre-release versions",
                variable=self._allow_prerelease_var,
                command=self._on_prerelease_toggle,
                font=FONT_NORMAL, text_color=TEXT_MAIN,
            ).pack(anchor="w", pady=(10, 0))

            ctk.CTkLabel(
                gen_sec,
                text="Also offer beta and release-candidate builds when checking for updates.\n"
                     "When disabled, only stable releases are offered.",
                font=FONT_SMALL, text_color=TEXT_DIM, anchor="w", justify="left",
            ).pack(anchor="w", pady=(2, 0))

        # ==== Theme ====
        theme_sec = _begin_section("Theme")

        # --- Appearance mode. Applied on next restart. Dropdown auto-populates
        # from every theme discovered under src/gui/themes/ — drop a new .py
        # file there and it appears here on next launch.
        mode_row = ctk.CTkFrame(theme_sec, fg_color="transparent")
        mode_row.pack(fill="x", pady=(0, 4))
        ctk.CTkLabel(
            mode_row, text="Appearance", font=FONT_NORMAL, text_color=TEXT_MAIN,
            anchor="w", width=scaled(220),
        ).pack(side="left")
        from gui.theme import available_themes
        _themes = available_themes() or {"dark": "Dark"}
        _mode_label_to_val = {display: tid for tid, display in _themes.items()}
        _mode_val_to_label = {tid: display for tid, display in _themes.items()}
        _current = get_appearance_mode()
        self._appearance_mode_var = tk.StringVar(
            value=_mode_val_to_label.get(_current, next(iter(_mode_val_to_label.values())))
        )
        def _on_appearance_change(choice: str) -> None:
            save_appearance_mode(_mode_label_to_val.get(choice, "dark"))
        ctk.CTkOptionMenu(
            mode_row, values=list(_mode_label_to_val.keys()),
            variable=self._appearance_mode_var,
            command=_on_appearance_change,
            width=scaled(120), height=scaled(28),
            font=FONT_NORMAL,
        ).pack(side="left")
        ctk.CTkLabel(
            theme_sec,
            text="Restart the app to apply a new appearance mode.",
            font=FONT_SMALL, text_color=TEXT_DIM, anchor="w", justify="left",
        ).pack(anchor="w", pady=(0, 12))

        ctk.CTkLabel(
            theme_sec,
            text="Customise row-highlight colours. Changes apply immediately.",
            font=FONT_SMALL, text_color=TEXT_DIM, anchor="w", justify="left",
        ).pack(anchor="w", pady=(0, 8))

        self._theme_swatches: dict[str, ctk.CTkFrame] = {}

        def _broadcast_theme_change() -> None:
            """Re-read theme colours in gui.theme and refresh affected panels."""
            try:
                import gui.theme as _theme_mod
                _theme_mod.refresh_theme_colors()
            except Exception:
                pass
            try:
                app = self.winfo_toplevel()
                ml = getattr(app, "_mod_panel", None)
                if ml is not None and hasattr(ml, "refresh_theme"):
                    ml.refresh_theme()
                pp = getattr(app, "_plugin_panel", None)
                if pp is not None and hasattr(pp, "refresh_theme"):
                    pp.refresh_theme()
            except Exception:
                pass

        def _set_color(key: str, hex_value: str) -> None:
            save_theme_color(key, hex_value)
            swatch = self._theme_swatches.get(key)
            if swatch is not None:
                try:
                    swatch.configure(fg_color=hex_value)
                except Exception:
                    pass
            _broadcast_theme_change()

        def _color_row(label: str, key: str) -> None:
            default_hex = THEME_DEFAULTS[key]
            current = get_theme_color(key)

            row = ctk.CTkFrame(theme_sec, fg_color="transparent")
            row.pack(fill="x", pady=(0, 6))

            ctk.CTkLabel(
                row, text=label, font=FONT_NORMAL, text_color=TEXT_MAIN,
                anchor="w", width=scaled(220),
            ).pack(side="left")

            swatch = ctk.CTkFrame(
                row, width=scaled(28), height=scaled(22),
                fg_color=current, corner_radius=4,
                border_width=1, border_color=BORDER,
            )
            swatch.pack(side="left", padx=(0, 8))
            swatch.pack_propagate(False)
            self._theme_swatches[key] = swatch

            def _pick(_key=key, _label=label, _default=default_hex):
                app = self.winfo_toplevel()
                show = getattr(app, "show_theme_color_panel", None)
                if show is None:
                    return
                def _on_result(hex_color, reset, __k=_key, __d=_default):
                    if reset:
                        _set_color(__k, __d)
                    elif hex_color:
                        _set_color(__k, hex_color)
                    # cancel (hex_color=None, reset=False) → no change
                show(_label, get_theme_color(_key), _on_result)

            ctk.CTkButton(
                row, text="Choose", width=scaled(60), height=scaled(28),
                font=FONT_SMALL, fg_color=BG_DEEP, hover_color=BG_HOVER,
                text_color=TEXT_MAIN, command=_pick,
            ).pack(side="left", padx=(0, 6))

            ctk.CTkButton(
                row, text="Default", width=scaled(70), height=scaled(28),
                font=FONT_SMALL, fg_color=BG_DEEP, hover_color=BG_HOVER,
                text_color=TEXT_DIM,
                command=lambda _k=key, _d=default_hex: _set_color(_k, _d),
            ).pack(side="left")

        _color_row("Conflict winner (green)",     "conflict_higher")
        _color_row("Conflict loser (red)",        "conflict_lower")
        _color_row("Cross-panel highlight",       "plugin_mod")
        _color_row("Plugin separator highlight",  "plugin_separator")
        _color_row("Separator conflict (gray)",   "conflict_separator")
        _color_row("Separator background",        "separator_bg")

        # ==== Paths ====
        paths_sec = _begin_section("Paths")

        ctk.CTkLabel(paths_sec, text="Default Mod Staging Folder:", font=FONT_NORMAL,
                     text_color=TEXT_MAIN, anchor="w").pack(anchor="w", pady=(0, 4))

        staging_entry_row = ctk.CTkFrame(paths_sec, fg_color="transparent")
        staging_entry_row.pack(fill="x")

        self._default_staging_var = tk.StringVar(value=load_default_staging_path())
        ctk.CTkEntry(
            staging_entry_row, textvariable=self._default_staging_var,
            font=FONT_NORMAL,
            placeholder_text=f"Default: {get_profiles_dir()}",
            height=scaled(28),
        ).pack(side="left", fill="x", expand=True, padx=(0, 4))

        ctk.CTkButton(
            staging_entry_row, text="Browse", width=scaled(70), height=scaled(28),
            font=FONT_NORMAL, fg_color=BG_HOVER, hover_color=ACCENT, text_color=TEXT_MAIN,
            command=self._browse_default_staging,
        ).pack(side="left", padx=(0, 4))

        ctk.CTkButton(
            staging_entry_row, text="Clear", width=scaled(56), height=scaled(28),
            font=FONT_NORMAL, fg_color=BG_DEEP, hover_color=BG_HOVER, text_color=TEXT_DIM,
            command=lambda: self._default_staging_var.set(""),
        ).pack(side="left")

        ctk.CTkLabel(
            paths_sec,
            text="When set, new games added after this point use <this>/<game name>\n"
                 "as their mod staging folder. Existing games are unaffected.",
            font=FONT_SMALL, text_color=TEXT_DIM, anchor="w", justify="left",
        ).pack(anchor="w", pady=(6, 0))

        ctk.CTkFrame(paths_sec, fg_color=BORDER, height=1).pack(fill="x", pady=(12, 8))

        ctk.CTkLabel(paths_sec, text="Heroic Config Location (Folder Containing config.json):", font=FONT_NORMAL,
                     text_color=TEXT_MAIN, anchor="w").pack(anchor="w", pady=(0, 4))

        heroic_entry_row = ctk.CTkFrame(paths_sec, fg_color="transparent")
        heroic_entry_row.pack(fill="x")

        self._heroic_path_var = tk.StringVar(value=load_heroic_config_path())
        ctk.CTkEntry(
            heroic_entry_row, textvariable=self._heroic_path_var,
            font=FONT_NORMAL, placeholder_text="Auto-detect (leave blank)",
            height=scaled(28),
        ).pack(side="left", fill="x", expand=True, padx=(0, 4))

        ctk.CTkButton(
            heroic_entry_row, text="Browse", width=scaled(70), height=scaled(28),
            font=FONT_NORMAL, fg_color=BG_HOVER, hover_color=ACCENT, text_color=TEXT_MAIN,
            command=self._browse_heroic_path,
        ).pack(side="left", padx=(0, 4))

        ctk.CTkButton(
            heroic_entry_row, text="Clear", width=scaled(56), height=scaled(28),
            font=FONT_NORMAL, fg_color=BG_DEEP, hover_color=BG_HOVER, text_color=TEXT_DIM,
            command=lambda: self._heroic_path_var.set(""),
        ).pack(side="left")

        ctk.CTkLabel(
            paths_sec,
            text="Leave blank to auto-detect (Flatpak and native locations).",
            font=FONT_SMALL, text_color=TEXT_DIM, anchor="w",
        ).pack(anchor="w", pady=(6, 0))

        ctk.CTkFrame(paths_sec, fg_color=BORDER, height=1).pack(fill="x", pady=(12, 8))

        ctk.CTkLabel(paths_sec, text="Steam libraryfolders.vdf:", font=FONT_NORMAL,
                     text_color=TEXT_MAIN, anchor="w").pack(anchor="w", pady=(0, 4))

        steam_entry_row = ctk.CTkFrame(paths_sec, fg_color="transparent")
        steam_entry_row.pack(fill="x")

        self._steam_vdf_var = tk.StringVar(value=load_steam_libraries_vdf_path())
        ctk.CTkEntry(
            steam_entry_row, textvariable=self._steam_vdf_var,
            font=FONT_NORMAL, placeholder_text="Auto-detect (leave blank)",
            height=scaled(28),
        ).pack(side="left", fill="x", expand=True, padx=(0, 4))

        ctk.CTkButton(
            steam_entry_row, text="Browse", width=scaled(70), height=scaled(28),
            font=FONT_NORMAL, fg_color=BG_HOVER, hover_color=ACCENT, text_color=TEXT_MAIN,
            command=self._browse_steam_vdf,
        ).pack(side="left", padx=(0, 4))

        ctk.CTkButton(
            steam_entry_row, text="Clear", width=scaled(56), height=scaled(28),
            font=FONT_NORMAL, fg_color=BG_DEEP, hover_color=BG_HOVER, text_color=TEXT_DIM,
            command=lambda: self._steam_vdf_var.set(""),
        ).pack(side="left")

        ctk.CTkLabel(
            paths_sec,
            text="Leave blank to auto-detect (Standard, Flatpak and Snap locations).",
            font=FONT_SMALL, text_color=TEXT_DIM, anchor="w",
        ).pack(anchor="w", pady=(6, 0))

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
                      fg_color=ACCENT, hover_color=ACCENT_HOV, text_color=TEXT_ON_ACCENT,
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
                    self.after(0, lambda e=exc: self._cache_status_lbl.configure(
                        text=f"Failed: {e}", text_color=TEXT_ERR))

            def _done(n):
                self._cache_status_lbl.configure(
                    text=f"Cleared ({n} items).", text_color=TEXT_OK)
                self._refresh_cache_size()

            self._cache_status_lbl.configure(text="Clearing…", text_color=TEXT_DIM)
            threading.Thread(target=_clear_worker, daemon=True).start()

        threading.Thread(target=_size_worker, daemon=True).start()

    def _browse_default_staging(self):
        from Utils.portal_filechooser import pick_folder

        def _on_chosen(chosen):
            if chosen:
                try:
                    self._default_staging_var.set(str(chosen))
                except Exception:
                    pass

        pick_folder("Select Default Mod Staging Folder", _on_chosen)

    def _browse_heroic_path(self):
        from Utils.portal_filechooser import pick_folder

        def _on_chosen(chosen):
            if chosen:
                try:
                    self._heroic_path_var.set(str(chosen))
                except Exception:
                    pass

        pick_folder("Select Heroic Config Folder", _on_chosen)

    def _browse_steam_vdf(self):
        from Utils.portal_filechooser import pick_folder

        def _on_chosen(chosen):
            if not chosen:
                return
            try:
                # The user might pick either the Steam root (containing
                # steamapps/) or the steamapps folder itself — try both and
                # validate before saving so we don't silently set a bogus
                # path.
                candidates = [
                    chosen / "steamapps" / "libraryfolders.vdf",
                    chosen / "libraryfolders.vdf",
                ]
                vdf = next((c for c in candidates if c.is_file()), None)
                if vdf is None:
                    try:
                        import tkinter.messagebox as mb
                        mb.showerror(
                            "Steam path",
                            f"libraryfolders.vdf not found under:\n{chosen}\n\n"
                            "Please pick your Steam installation folder "
                            "(the one containing the steamapps directory).",
                            parent=self,
                        )
                    except Exception:
                        pass
                    return
                self._steam_vdf_var.set(str(vdf))
            except Exception:
                pass

        pick_folder("Select Steam Installation Folder", _on_chosen)

    def _save_no_restart(self):
        """Save collection settings (and scale) without restarting."""
        if self._auto_var.get():
            save_ui_scale("auto")
        else:
            save_ui_scale(round(self._scale_var.get() * 20) / 20)
        save_font_family(self._font_var.get())
        save_normalize_folder_case(self._norm_case_var.get())
        save_clear_archive_after_install(self._clear_archive_var.get())
        save_keep_fomod_archives(self._keep_fomod_archives_var.get())
        save_rename_mod_after_install(self._rename_after_install_var.get())
        save_restore_on_close(self._restore_on_close_var.get())
        if hasattr(self, "_allow_prerelease_var"):
            save_allow_prerelease(self._allow_prerelease_var.get())
        save_collection_settings(
            download_order=self._dl_order_from_label.get(self._dl_order_var.get(), "largest"),
            max_concurrent=int(round(self._max_concurrent_var.get())),
            check_download_locations=self._check_dl_locations_var.get(),
            clear_archive_after_install=self._col_clear_archive_var.get(),
            max_extract_workers=int(round(self._max_extract_var.get())),
        )
        save_heroic_config_path(self._heroic_path_var.get())
        save_steam_libraries_vdf_path(self._steam_vdf_var.get())
        save_default_staging_path(self._default_staging_var.get())
        self._on_done(self)

    def _on_close(self):
        self._on_done(self)

    def _apply(self):
        if self._auto_var.get():
            save_ui_scale("auto")
        else:
            save_ui_scale(round(self._scale_var.get() * 20) / 20)
        save_font_family(self._font_var.get())
        save_normalize_folder_case(self._norm_case_var.get())
        save_clear_archive_after_install(self._clear_archive_var.get())
        save_keep_fomod_archives(self._keep_fomod_archives_var.get())
        save_rename_mod_after_install(self._rename_after_install_var.get())
        save_restore_on_close(self._restore_on_close_var.get())
        if hasattr(self, "_allow_prerelease_var"):
            save_allow_prerelease(self._allow_prerelease_var.get())
        save_collection_settings(
            download_order=self._dl_order_from_label.get(self._dl_order_var.get(), "largest"),
            max_concurrent=int(round(self._max_concurrent_var.get())),
            check_download_locations=self._check_dl_locations_var.get(),
            clear_archive_after_install=self._col_clear_archive_var.get(),
            max_extract_workers=int(round(self._max_extract_var.get())),
        )
        save_heroic_config_path(self._heroic_path_var.get())
        save_steam_libraries_vdf_path(self._steam_vdf_var.get())
        save_default_staging_path(self._default_staging_var.get())
        self._on_done(self)
        python = sys.executable
        os.execv(python, [python] + sys.argv)


# ---------------------------------------------------------------------------
# App update check
# ---------------------------------------------------------------------------
_APP_UPDATE_VERSION_URL = "https://raw.githubusercontent.com/ChrisDKN/Amethyst-Mod-Manager/main/src/version.py"
_APP_UPDATE_RELEASES_URL = "https://github.com/ChrisDKN/Amethyst-Mod-Manager/releases"

