"""
Profile Settings overlay — manage profiles (rename/remove) over the modlist panel.
"""

from __future__ import annotations

import os
import shlex
import shutil
import threading
import tkinter as tk

from Utils.xdg import xdg_open
from pathlib import Path
from typing import Callable, Optional

import customtkinter as ctk
from PIL import Image, ImageTk

from gui.wheel_compat import LEGACY_WHEEL_REDUNDANT
from gui.theme import (
    ACCENT,
    ACCENT_HOV,
    BG_DEEP,
    BG_HEADER,
    BG_HOVER,
    BG_PANEL,
    BORDER,
    FONT_BOLD,
    FONT_NORMAL,
    FONT_SMALL,
    TEXT_MAIN,
    TEXT_DIM,
)
from gui import game_helpers as _gh
from gui.game_helpers import _profiles_for_game
from Utils.profile_state import merge_profile_settings, read_profile_settings
from gui.ctk_components import CTkAlert
from Utils.mo2_import import validate_mo2_folder, count_mo2_mods, import_mo2


class ProfileSettingsOverlay(tk.Frame):
    """
    Overlay for managing profiles (rename / remove).
    Placed over the ModListPanel via place(relx=0, rely=0, relwidth=1, relheight=1).

    Callbacks:
        on_close()       — called when the overlay should be dismissed
        on_profile_renamed(old, new) — called after a successful rename
        on_profile_removed(name)     — called after a successful removal
    """

    def __init__(
        self,
        parent: tk.Widget,
        game_name: str,
        current_profile: str,
        on_close: Optional[Callable[[], None]] = None,
        on_profile_renamed: Optional[Callable[[str, str], None]] = None,
        on_profile_removed: Optional[Callable[[str], None]] = None,
        on_profiles_changed: Optional[Callable[[], None]] = None,
        log_fn: Optional[Callable[[str], None]] = None,
    ):
        super().__init__(parent, bg=BG_DEEP)
        self._game_name = game_name
        self._current_profile = current_profile
        self._on_close = on_close
        self._on_profile_renamed = on_profile_renamed
        self._on_profile_removed = on_profile_removed
        self._on_profiles_changed = on_profiles_changed
        self._log = log_fn or (lambda msg: None)

        self._rename_frame: tk.Frame | None = None
        self._rename_entry: ctk.CTkEntry | None = None
        self._rename_target: str | None = None

        _icons_dir = Path(__file__).resolve().parent.parent / "icons"
        _lock_path = _icons_dir / "lock.png"
        if _lock_path.is_file():
            img = Image.open(_lock_path).convert("RGBA").resize((16, 16), Image.LANCZOS)
            self._lock_icon: ImageTk.PhotoImage | None = ImageTk.PhotoImage(img)
        else:
            self._lock_icon = None

        self._build()

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def _build(self):
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)

        # Toolbar
        toolbar = tk.Frame(self, bg=BG_HEADER, height=42)
        toolbar.grid(row=0, column=0, sticky="ew")
        toolbar.grid_propagate(False)

        tk.Label(
            toolbar, text="Profile Settings",
            font=FONT_BOLD, fg=TEXT_MAIN, bg=BG_HEADER,
        ).pack(side="left", padx=12, pady=8)

        ctk.CTkButton(
            toolbar, text="✕ Close", width=85, height=30,
            fg_color="#6b3333", hover_color="#8c4444", text_color="white",
            font=FONT_BOLD, command=self._do_close,
        ).pack(side="right", padx=(6, 12), pady=5)

        ctk.CTkButton(
            toolbar, text="Import MO2", width=100, height=30,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
            font=FONT_BOLD, command=self._on_import_mo2,
        ).pack(side="right", padx=(6, 0), pady=5)

        # Content area
        content = tk.Frame(self, bg=BG_DEEP)
        content.grid(row=1, column=0, sticky="nsew")
        content.grid_rowconfigure(0, weight=1)
        content.grid_columnconfigure(0, weight=1)

        # Scrollable inner frame
        outer = tk.Frame(content, bg=BG_DEEP)
        outer.grid(row=0, column=0, sticky="nsew")
        outer.grid_rowconfigure(0, weight=1)
        outer.grid_columnconfigure(0, weight=1)

        canvas = tk.Canvas(outer, bg=BG_DEEP, highlightthickness=0, bd=0)
        vsb = tk.Scrollbar(outer, orient="vertical", command=canvas.yview,
                           bg="#383838", troughcolor=BG_DEEP,
                           highlightthickness=0, bd=0)
        canvas.configure(yscrollcommand=vsb.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        self._canvas = canvas

        self._list_frame = tk.Frame(canvas, bg=BG_DEEP)
        self._list_frame_id = canvas.create_window((0, 0), window=self._list_frame, anchor="nw")

        self._list_frame.bind("<Configure>", self._on_frame_configure)
        canvas.bind("<Configure>", self._on_canvas_configure)
        def _on_wheel(e):
            canvas.yview_scroll(-3 if (getattr(e, "delta", 0) or 0) > 0 else 3, "units")
        canvas.bind("<MouseWheel>", _on_wheel)
        self._list_frame.bind("<MouseWheel>", _on_wheel)
        if not LEGACY_WHEEL_REDUNDANT:
            canvas.bind("<Button-4>", lambda e: canvas.yview_scroll(-3, "units"))
            canvas.bind("<Button-5>", lambda e: canvas.yview_scroll(3, "units"))
            self._list_frame.bind("<Button-4>", lambda e: canvas.yview_scroll(-3, "units"))
            self._list_frame.bind("<Button-5>", lambda e: canvas.yview_scroll(3, "units"))

        self._populate_list()

    def _on_frame_configure(self, _event=None):
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))

    def _on_canvas_configure(self, event):
        self._canvas.itemconfigure(self._list_frame_id, width=event.width)

    # ------------------------------------------------------------------
    # Populate profile rows
    # ------------------------------------------------------------------

    def _populate_list(self):
        for child in self._list_frame.winfo_children():
            child.destroy()
        self._rename_frame = None
        self._rename_entry = None
        self._rename_target = None

        profiles = _profiles_for_game(self._game_name)

        for i, profile in enumerate(profiles):
            row_bg = BG_PANEL if i % 2 == 0 else BG_DEEP
            row = tk.Frame(self._list_frame, bg=row_bg, height=44)
            row.pack(fill="x", pady=(0, 1))
            row.grid_propagate(False)
            row.grid_rowconfigure(0, weight=1)

            is_default = profile == "default" or self._is_original_default(profile)
            is_active = profile == self._current_profile
            is_locked = self._is_profile_locked(profile)

            # Lock toggle — drawn box with lock icon inside (matches separator lock style)
            BOX = 18
            lock_canvas = tk.Canvas(
                row, width=BOX, height=BOX,
                bg=row_bg, highlightthickness=0, bd=0,
            )
            lock_canvas.grid(row=0, column=0, padx=(8, 0))

            box_fill = BG_DEEP if is_locked else row_bg
            lock_canvas.create_rectangle(
                1, 1, BOX - 1, BOX - 1,
                outline=BORDER, width=1, fill=box_fill, tags="box",
            )
            if self._lock_icon:
                lock_canvas.create_image(
                    BOX // 2, BOX // 2, anchor="center",
                    image=self._lock_icon,
                    state="normal" if is_locked else "hidden",
                    tags="mark",
                )
            else:
                lock_canvas.create_text(
                    BOX // 2, BOX // 2, anchor="center",
                    text="🔒", fill=TEXT_MAIN,
                    state="normal" if is_locked else "hidden",
                    tags="mark",
                )

            if not is_default:
                lock_canvas.configure(cursor="hand2")
                lock_canvas.bind(
                    "<ButtonRelease-1>",
                    lambda e, p=profile, c=lock_canvas, bg=row_bg: self._on_lock_canvas_click(p, c, bg),
                )

            # Profile name label
            label_text = profile
            if is_default:
                label_text += "  (default)"  # covers both "default" and renamed originals
            if is_active:
                label_text += "  ★"
            lbl_color = ACCENT if is_active else TEXT_MAIN
            tk.Label(
                row, text=label_text,
                font=FONT_BOLD if is_active else FONT_NORMAL,
                fg=lbl_color, bg=row_bg, anchor="w",
            ).grid(row=0, column=1, sticky="ew", padx=(4, 12))

            row.grid_columnconfigure(1, weight=1)

            btn_frame = tk.Frame(row, bg=row_bg)
            btn_frame.grid(row=0, column=2, padx=8)

            # Rename button (always available)
            ctk.CTkButton(
                btn_frame, text="Rename", width=72, height=28, font=FONT_SMALL,
                fg_color=BG_HOVER, hover_color=ACCENT_HOV, text_color=TEXT_MAIN,
                command=lambda p=profile, r=row, rb=row_bg: self._show_rename(p, r, rb),
            ).pack(side="left", padx=(0, 6))

            # Open folder button
            ctk.CTkButton(
                btn_frame, text="Open", width=60, height=28, font=FONT_SMALL,
                fg_color=BG_HOVER, hover_color=ACCENT_HOV, text_color=TEXT_MAIN,
                command=lambda p=profile: self._open_profile_folder(p),
            ).pack(side="left", padx=(0, 6))

            # Steam Command button
            ctk.CTkButton(
                btn_frame, text="Steam Cmd", width=90, height=28, font=FONT_SMALL,
                fg_color="#1a3a5c", hover_color="#1c5998", text_color="white",
                command=lambda p=profile: self._show_steam_command(p),
            ).pack(side="left", padx=(0, 6))

            # Remove button (disabled for locked/default profiles)
            can_remove = not is_default and not is_locked
            remove_btn = ctk.CTkButton(
                btn_frame, text="Remove", width=72, height=28, font=FONT_SMALL,
                fg_color="#6b3333" if can_remove else "#3a3a3a",
                hover_color="#8c4444" if can_remove else "#3a3a3a",
                text_color="white" if can_remove else TEXT_DIM,
                state="normal" if can_remove else "disabled",
                command=(lambda p=profile: self._on_remove(p)) if can_remove else lambda: None,
            )
            remove_btn.pack(side="left")

    # ------------------------------------------------------------------
    # Rename
    # ------------------------------------------------------------------

    def _show_rename(self, profile: str, row: tk.Frame, row_bg: str):
        """Inline rename bar inserted below the profile row."""
        # Close any existing rename bar first
        if self._rename_frame is not None:
            self._rename_frame.destroy()
            self._rename_frame = None

        self._rename_target = profile

        frame = tk.Frame(self._list_frame, bg=BG_HEADER, pady=6)
        frame.pack(fill="x", after=row)
        self._rename_frame = frame

        tk.Label(
            frame, text=f"Rename '{profile}' to:",
            font=FONT_SMALL, fg=TEXT_DIM, bg=BG_HEADER,
        ).pack(side="left", padx=(12, 6))

        entry = ctk.CTkEntry(
            frame, width=180, height=28, font=FONT_NORMAL,
            fg_color=BG_PANEL, text_color=TEXT_MAIN,
            border_color=BORDER,
        )
        entry.insert(0, profile)
        entry.select_range(0, "end")
        entry.pack(side="left", padx=(0, 6))
        entry.focus_set()
        self._rename_entry = entry

        ctk.CTkButton(
            frame, text="OK", width=50, height=28, font=FONT_SMALL,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
            command=self._do_rename,
        ).pack(side="left", padx=(0, 4))

        ctk.CTkButton(
            frame, text="Cancel", width=60, height=28, font=FONT_SMALL,
            fg_color=BG_HOVER, hover_color="#555", text_color=TEXT_MAIN,
            command=self._cancel_rename,
        ).pack(side="left")

        entry.bind("<Return>", lambda e: self._do_rename())
        entry.bind("<Escape>", lambda e: self._cancel_rename())

        # Scroll so the rename bar is visible
        self.after(50, lambda: self._canvas.yview_moveto(1.0))

    def _cancel_rename(self):
        if self._rename_frame is not None:
            self._rename_frame.destroy()
            self._rename_frame = None
        self._rename_target = None
        self._rename_entry = None

    def _open_profile_folder(self, profile: str):
        folder = self._get_profile_dir(profile)
        folder.mkdir(parents=True, exist_ok=True)
        xdg_open(folder)

    def _do_rename(self):
        if self._rename_entry is None or self._rename_target is None:
            return
        old_name = self._rename_target
        new_name = self._rename_entry.get().strip()

        if not new_name:
            self._log("Profile name cannot be empty.")
            return
        if new_name == old_name:
            self._cancel_rename()
            return
        if new_name.lower() == "default":
            self._log("Cannot rename to 'default'.")
            return

        existing = _profiles_for_game(self._game_name)
        if new_name in existing:
            self._log(f"Profile '{new_name}' already exists.")
            return

        # Perform the rename (directory rename)
        profile_dir = self._get_profile_dir(old_name)
        new_dir = profile_dir.parent / new_name
        was_original_default = old_name == "default" or self._is_original_default_dir(profile_dir)
        try:
            profile_dir.rename(new_dir)
        except OSError as e:
            self._log(f"Rename failed: {e}")
            return

        # If renaming the original default, mark the new dir so it stays unremovable
        if was_original_default:
            self._mark_original_default(new_dir)

        self._log(f"Profile '{old_name}' renamed to '{new_name}'.")
        if old_name == self._current_profile:
            self._current_profile = new_name

        self._cancel_rename()
        self._populate_list()

        if self._on_profile_renamed:
            self._on_profile_renamed(old_name, new_name)

    # ------------------------------------------------------------------
    # Remove
    # ------------------------------------------------------------------

    def _on_remove(self, profile: str):
        if self._is_original_default(profile) or self._is_profile_locked(profile):
            return
        alert = CTkAlert(
            state="warning",
            title="Remove Profile",
            body_text=f"Are you sure you want to remove the '{profile}' profile?\n\nThe game will be restored first if this profile is deployed.",
            btn1="Remove",
            btn2="Cancel",
            parent=self.winfo_toplevel(),
        )
        if alert.get() != "Remove":
            return

        game = _gh._GAMES.get(self._game_name)
        profile_dir = self._get_profile_dir(profile)

        # Restore deployed mod files before deleting
        if game is not None and game.is_configured():
            game.set_active_profile_dir(profile_dir)
            try:
                if hasattr(game, "restore"):
                    game.restore()
            except Exception:
                pass
            try:
                from Utils.deploy import restore_root_folder
                root_folder_dir = game.get_effective_root_folder_path()
                game_root = game.get_game_path()
                if root_folder_dir.is_dir() and game_root:
                    restore_root_folder(root_folder_dir, game_root)
            except Exception:
                pass
            game.set_active_profile_dir(None)

        if profile_dir.is_dir():
            from gui.game_helpers import profile_uses_specific_mods
            if profile_uses_specific_mods(profile_dir):
                confirm = CTkAlert(
                    state="warning",
                    title="Remove Profile",
                    body_text=(
                        f"The '{profile}' profile has profile-specific mods.\n\n"
                        "Removing it will permanently delete its installed mods "
                        "and modlist. Continue?"
                    ),
                    btn1="Remove",
                    btn2="Cancel",
                    parent=self.winfo_toplevel(),
                )
                if confirm.get() != "Remove":
                    return
            shutil.rmtree(profile_dir)

        self._log(f"Profile '{profile}' removed.")

        if profile == self._current_profile:
            self._current_profile = "default"

        self._populate_list()

        if self._on_profile_removed:
            self._on_profile_removed(profile)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _is_profile_locked(self, profile: str) -> bool:
        """Return True if this profile has been locked (or is the default)."""
        if self._is_original_default(profile):
            return True
        profile_dir = self._get_profile_dir(profile)
        try:
            return bool(read_profile_settings(profile_dir, None).get("profile_locked", False))
        except Exception:
            return False

    def _on_lock_canvas_click(self, profile: str, canvas: tk.Canvas, row_bg: str):
        """Toggle the lock state for a profile when the drawn checkbox is clicked."""
        currently_locked = self._is_profile_locked(profile)
        new_locked = not currently_locked
        profile_dir = self._get_profile_dir(profile)
        try:
            merge_profile_settings(profile_dir, {"profile_locked": new_locked})
        except Exception as e:
            self._log(f"Could not save lock state: {e}")
            return
        # Update the canvas visually
        canvas.itemconfigure("box", fill=BG_DEEP if new_locked else row_bg)
        canvas.itemconfigure("mark", state="normal" if new_locked else "hidden")
        # Repopulate so the Remove button state updates
        self._populate_list()

    def _is_original_default(self, profile: str) -> bool:
        """Return True if this profile was originally the default (by name or flag)."""
        if profile == "default":
            return True
        return self._is_original_default_dir(self._get_profile_dir(profile))

    def _is_original_default_dir(self, profile_dir: Path) -> bool:
        try:
            return bool(read_profile_settings(profile_dir, None).get("original_default", False))
        except Exception:
            return False

    def _mark_original_default(self, profile_dir: Path):
        try:
            profile_dir.mkdir(parents=True, exist_ok=True)
            merge_profile_settings(profile_dir, {"original_default": True})
        except Exception:
            pass

    def _get_profile_dir(self, profile: str) -> Path:
        game = _gh._GAMES.get(self._game_name)
        if game is not None:
            return game.get_profile_root() / "profiles" / profile
        from Utils.config_paths import get_profiles_dir
        return get_profiles_dir() / self._game_name / "profiles" / profile

    # ------------------------------------------------------------------
    # MO2 Import
    # ------------------------------------------------------------------

    def _on_import_mo2(self):
        from Utils.portal_filechooser import pick_folder

        pick_folder("Select MO2 Folder",
                    lambda f: self.after(0, self._on_mo2_folder_chosen, f))

    def _on_mo2_folder_chosen(self, folder: "Path | None"):
        if folder is None:
            return

        err = validate_mo2_folder(folder)
        if err:
            CTkAlert(
                state="warning", title="Invalid MO2 Folder",
                body_text=err, btn1="OK", btn2="",
                parent=self.winfo_toplevel(),
            )
            return

        mod_count = count_mo2_mods(folder)
        has_overwrite = (folder / "overwrite").is_dir()
        has_profiles = (folder / "profiles").is_dir()

        parts = [f"{mod_count} mod(s)"]
        if has_overwrite:
            parts.append("overwrite folder")
        if has_profiles:
            parts.append("profiles folder")

        alert = CTkAlert(
            state="warning",
            title="Import from MO2",
            body_text=(
                f"This will move the following from:\n{folder}\n\n"
                f"• {', '.join(parts)}\n\n"
                "into this game's staging directory.\n\n"
                "Are you sure?"
            ),
            btn1="Import",
            btn2="Cancel",
            parent=self.winfo_toplevel(),
            width=500,
            height=300,
        )
        if alert.get() != "Import":
            return

        game = _gh._GAMES.get(self._game_name)
        if game is None:
            self._log("No game handler found.")
            return

        staging_root = game.get_profile_root()
        self._log(f"Importing MO2 mods from {folder} …")

        def _do():
            try:
                import_mo2(folder, staging_root, log_fn=self._log)
                self.after(0, self._mo2_import_done)
            except Exception as exc:
                self._log(f"MO2 import failed: {exc}")
                self.after(0, lambda: CTkAlert(
                    state="warning", title="Import Failed",
                    body_text=str(exc), btn1="OK", btn2="",
                    parent=self.winfo_toplevel(),
                ))

        threading.Thread(target=_do, daemon=True).start()

    def _mo2_import_done(self):
        profiles = _profiles_for_game(self._game_name)
        profile_list = "\n".join(f"  • {p}" for p in profiles)
        body = f"MO2 profiles have been imported.\n\nAvailable profiles:\n{profile_list}"
        line_count = body.count("\n") + 1
        height = max(220, 140 + line_count * 22)
        CTkAlert(
            state="info",
            title="MO2 Import Complete",
            body_text=body,
            btn1="OK",
            btn2="",
            parent=self.winfo_toplevel(),
            width=450,
            height=height,
        )
        self._populate_list()
        if self._on_profiles_changed:
            self._on_profiles_changed()

    # ------------------------------------------------------------------
    # Steam Command
    # ------------------------------------------------------------------

    def _show_steam_command(self, profile: str):
        """Show a dialog with the CLI + Steam launch command for this profile."""
        game = _gh._GAMES.get(self._game_name)
        game_id = getattr(game, "game_id", self._game_name) if game else self._game_name
        steam_id = getattr(game, "steam_id", "") if game else ""

        appimage_path = os.environ.get("APPIMAGE", "")
        flatpak_id = os.environ.get("FLATPAK_ID", "")
        if appimage_path:
            deploy_cmd = f"{shlex.quote(appimage_path)} deploy {shlex.quote(game_id)} {shlex.quote(profile)}"
        elif flatpak_id:
            deploy_cmd = f"flatpak run --command=amethyst-mod-manager-cli {shlex.quote(flatpak_id)} deploy {shlex.quote(game_id)} {shlex.quote(profile)}"
        else:
            deploy_cmd = f"amethyst-mod-manager-cli deploy {shlex.quote(game_id)} {shlex.quote(profile)}"

        cmd = deploy_cmd
        if steam_id:
            cmd += f" && steam 'steam://rungameid/{steam_id}'"

        win = tk.Toplevel(self.winfo_toplevel())
        win.title("Steam Launch Command")
        win.configure(bg=BG_DEEP)
        win.resizable(True, False)
        win.geometry("900x190")
        win.transient(self.winfo_toplevel())
        win.update_idletasks()
        win.grab_set()

        tk.Label(
            win, text=f"Steam launch command for '{profile}':",
            font=FONT_BOLD, fg=TEXT_MAIN, bg=BG_DEEP,
        ).pack(anchor="w", padx=16, pady=(14, 2))

        tk.Label(
            win,
            text="Paste this as a non-Steam game launch option, or use it in a desktop shortcut.",
            font=FONT_SMALL, fg=TEXT_DIM, bg=BG_DEEP, justify="left",
        ).pack(anchor="w", padx=16, pady=(0, 8))

        entry_var = tk.StringVar(value=cmd)
        entry = tk.Entry(
            win, textvariable=entry_var, font=FONT_NORMAL,
            fg=TEXT_MAIN, bg=BG_PANEL, readonlybackground=BG_PANEL,
            relief="flat", state="readonly", insertbackground=TEXT_MAIN,
        )
        entry.pack(fill="x", padx=16, ipady=5)

        btn_frame = tk.Frame(win, bg=BG_DEEP)
        btn_frame.pack(anchor="w", padx=16, pady=(10, 14))

        copy_btn = ctk.CTkButton(
            btn_frame, text="Copy", width=80, height=30, font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
            command=lambda: [
                win.clipboard_clear(),
                win.clipboard_append(cmd),
                copy_btn.configure(text="Copied!"),
                win.after(1500, lambda: copy_btn.configure(text="Copy")),
            ],
        )
        copy_btn.pack(side="left", padx=(0, 8))

        ctk.CTkButton(
            btn_frame, text="Close", width=80, height=30, font=FONT_BOLD,
            fg_color=BG_HOVER, hover_color="#555", text_color=TEXT_MAIN,
            command=win.destroy,
        ).pack(side="left")

    def _do_close(self):
        if self._on_close:
            self._on_close()
        else:
            self.destroy()
