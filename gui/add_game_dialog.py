"""
add_game_dialog.py
Modal dialog for locating and registering a game installation.

Scans all Steam library paths for the game's exe automatically,
with a manual folder-picker fallback via zenity.
"""

from __future__ import annotations

import shutil
import subprocess
import threading
from pathlib import Path
from typing import Optional

import customtkinter as ctk
import tkinter as tk

from Games.base_game import BaseGame
from Utils.deploy import LinkMode
from Utils.steam_finder import find_steam_libraries, find_game_in_libraries, find_prefix

# ---------------------------------------------------------------------------
# Colors / fonts (kept in sync with gui.py)
# ---------------------------------------------------------------------------
BG_DEEP    = "#1a1a1a"
BG_PANEL   = "#252526"
BG_HEADER  = "#2a2a2b"
BG_ROW     = "#2d2d2d"
BG_HOVER   = "#094771"
ACCENT     = "#0078d4"
ACCENT_HOV = "#1084d8"
TEXT_MAIN  = "#d4d4d4"
TEXT_DIM   = "#858585"
TEXT_SEP   = "#b0b0b0"
BORDER     = "#444444"
TEXT_OK    = "#98c379"
TEXT_ERR   = "#e06c75"
TEXT_WARN  = "#e5c07b"
RED_BTN    = "#a83232"
RED_HOV    = "#c43c3c"

FONT_NORMAL = ("Segoe UI", 12)
FONT_BOLD   = ("Segoe UI", 12, "bold")
FONT_SMALL  = ("Segoe UI", 10)
FONT_MONO   = ("Courier New", 11)


# ---------------------------------------------------------------------------
# AddGameDialog
# ---------------------------------------------------------------------------

class AddGameDialog(ctk.CTkToplevel):
    """
    Modal dialog that locates a game on disk and saves its path.

    Usage:
        dialog = AddGameDialog(parent, game)
        parent.wait_window(dialog)
        if dialog.result:
            print(f"Configured: {dialog.result}")
    """

    WIDTH  = 560
    HEIGHT = 780

    def __init__(self, parent, game: BaseGame):
        super().__init__(parent, fg_color=BG_DEEP)
        self.title(f"Add Game — {game.name}")
        self.geometry(f"{self.WIDTH}x{self.HEIGHT}")
        self.resizable(False, False)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)

        self._game = game
        self._found_path: Optional[Path] = None
        self._found_prefix: Optional[Path] = None
        self._custom_staging: Optional[Path] = None
        self.result: Optional[Path] = None
        self.removed: bool = False
        self._deploy_mode_var = tk.StringVar(value="hardlink")

        self._build_ui()

        # Defer grab_set until the window is fully rendered
        self.after(100, self._make_modal)

        # If already configured, pre-populate both fields
        if game.is_configured():
            self._set_path(game.get_game_path(), status="configured")
            existing_pfx = game.get_prefix_path()
            if existing_pfx and existing_pfx.is_dir():
                self._set_prefix(existing_pfx, status="configured")
            elif game.steam_id:
                self._start_prefix_scan()
            if hasattr(game, "get_deploy_mode"):
                mode = game.get_deploy_mode()
                self._deploy_mode_var.set({
                    LinkMode.SYMLINK: "symlink",
                    LinkMode.COPY:    "copy",
                }.get(mode, "hardlink"))
            # Pre-populate staging path if a custom one is saved
            if hasattr(game, "_staging_path") and game._staging_path is not None:
                self._custom_staging = game._staging_path
                self._set_staging(game._staging_path, status="configured")
            else:
                self._set_staging_text(str(game.get_mod_staging_path()))
        else:
            self._start_scan()
            self._set_staging_text(str(game.get_mod_staging_path()))

    def _make_modal(self):
        """Grab input focus once the window is viewable."""
        try:
            self.grab_set()
            self.focus_set()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self):
        self.grid_rowconfigure(0, weight=0)  # title bar
        self.grid_rowconfigure(1, weight=1)  # body
        self.grid_rowconfigure(2, weight=0)  # button bar
        self.grid_columnconfigure(0, weight=1)

        # Title bar
        title_bar = ctk.CTkFrame(self, fg_color=BG_HEADER, corner_radius=0, height=40)
        title_bar.grid(row=0, column=0, sticky="ew")
        title_bar.grid_propagate(False)
        ctk.CTkLabel(
            title_bar, text=f"Add Game: {self._game.name}",
            font=FONT_BOLD, text_color=TEXT_MAIN, anchor="w"
        ).pack(side="left", padx=12, pady=8)

        # Body
        body = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0)
        body.grid(row=1, column=0, sticky="nsew")
        body.grid_columnconfigure(0, weight=1)

        # --- Game path section ---
        ctk.CTkLabel(
            body, text="Game Installation Folder",
            font=FONT_BOLD, text_color=TEXT_SEP, anchor="w"
        ).grid(row=0, column=0, sticky="ew", padx=16, pady=(16, 2))

        self._status_label = ctk.CTkLabel(
            body, text="Scanning Steam libraries…",
            font=FONT_NORMAL, text_color=TEXT_WARN, anchor="w"
        )
        self._status_label.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 4))

        self._path_box = ctk.CTkTextbox(
            body, height=48, font=FONT_MONO,
            fg_color=BG_ROW, text_color=TEXT_MAIN,
            state="disabled", wrap="none", corner_radius=4
        )
        self._path_box.grid(row=2, column=0, sticky="ew", padx=16, pady=(0, 4))

        self._browse_btn = ctk.CTkButton(
            body, text="Browse manually…", width=160, height=28,
            font=FONT_SMALL, fg_color=BG_HEADER, hover_color=BG_HOVER,
            text_color=TEXT_MAIN, command=self._on_browse
        )
        self._browse_btn.grid(row=3, column=0, sticky="w", padx=16, pady=(0, 12))

        # Divider
        ctk.CTkFrame(body, fg_color=BORDER, height=1, corner_radius=0).grid(
            row=4, column=0, sticky="ew", padx=16, pady=4
        )

        # --- Proton prefix section (only shown when steam_id is set) ---
        ctk.CTkLabel(
            body, text="Proton Prefix (compatdata/pfx)",
            font=FONT_BOLD, text_color=TEXT_SEP, anchor="w"
        ).grid(row=5, column=0, sticky="ew", padx=16, pady=(8, 2))

        self._prefix_status_label = ctk.CTkLabel(
            body,
            text="Scanning for prefix…" if self._game.steam_id else "No Steam ID — prefix not applicable.",
            font=FONT_NORMAL,
            text_color=TEXT_WARN if self._game.steam_id else TEXT_DIM,
            anchor="w"
        )
        self._prefix_status_label.grid(row=6, column=0, sticky="ew", padx=16, pady=(0, 4))

        self._prefix_box = ctk.CTkTextbox(
            body, height=48, font=FONT_MONO,
            fg_color=BG_ROW, text_color=TEXT_MAIN,
            state="disabled", wrap="none", corner_radius=4
        )
        self._prefix_box.grid(row=7, column=0, sticky="ew", padx=16, pady=(0, 4))

        self._prefix_browse_btn = ctk.CTkButton(
            body, text="Browse manually…", width=160, height=28,
            font=FONT_SMALL, fg_color=BG_HEADER, hover_color=BG_HOVER,
            text_color=TEXT_MAIN, command=self._on_browse_prefix,
            state="normal" if self._game.steam_id else "disabled"
        )
        self._prefix_browse_btn.grid(row=8, column=0, sticky="w", padx=16, pady=(0, 8))

        # Divider
        ctk.CTkFrame(body, fg_color=BORDER, height=1, corner_radius=0).grid(
            row=9, column=0, sticky="ew", padx=16, pady=4
        )

        # --- Mod Staging Folder section ---
        ctk.CTkLabel(
            body, text="Mod Staging Folder",
            font=FONT_BOLD, text_color=TEXT_SEP, anchor="w"
        ).grid(row=10, column=0, sticky="ew", padx=16, pady=(8, 2))

        self._staging_status_label = ctk.CTkLabel(
            body, text="Default location will be used.",
            font=FONT_NORMAL, text_color=TEXT_DIM, anchor="w"
        )
        self._staging_status_label.grid(row=11, column=0, sticky="ew", padx=16, pady=(0, 4))

        self._staging_box = ctk.CTkTextbox(
            body, height=48, font=FONT_MONO,
            fg_color=BG_ROW, text_color=TEXT_MAIN,
            state="disabled", wrap="none", corner_radius=4
        )
        self._staging_box.grid(row=12, column=0, sticky="ew", padx=16, pady=(0, 4))

        _staging_btn_frame = ctk.CTkFrame(body, fg_color="transparent")
        _staging_btn_frame.grid(row=13, column=0, sticky="w", padx=16, pady=(0, 8))

        ctk.CTkButton(
            _staging_btn_frame, text="Browse manually…", width=160, height=28,
            font=FONT_SMALL, fg_color=BG_HEADER, hover_color=BG_HOVER,
            text_color=TEXT_MAIN, command=self._on_browse_staging
        ).pack(side="left", padx=(0, 6))

        ctk.CTkButton(
            _staging_btn_frame, text="Reset to default", width=130, height=28,
            font=FONT_SMALL, fg_color=BG_HEADER, hover_color=BG_HOVER,
            text_color=TEXT_MAIN, command=self._on_reset_staging
        ).pack(side="left")

        # Divider
        ctk.CTkFrame(body, fg_color=BORDER, height=1, corner_radius=0).grid(
            row=14, column=0, sticky="ew", padx=16, pady=4
        )

        # --- Deploy method section ---
        ctk.CTkLabel(
            body, text="Deploy Method",
            font=FONT_BOLD, text_color=TEXT_SEP, anchor="w"
        ).grid(row=15, column=0, sticky="ew", padx=16, pady=(8, 4))

        _mode_options = [
            ("Hardlink (Recommended)", "hardlink"),
            ("Symlink",                "symlink"),
            ("Direct Copy",            "copy"),
        ]
        for idx, (label, value) in enumerate(_mode_options):
            is_last = idx == len(_mode_options) - 1
            ctk.CTkRadioButton(
                body, text=label,
                variable=self._deploy_mode_var, value=value,
                font=FONT_NORMAL, text_color=TEXT_MAIN,
                fg_color=ACCENT, hover_color=ACCENT_HOV,
            ).grid(row=16 + idx,
                   column=0, sticky="w", padx=24,
                   pady=(2, 12) if is_last else 2)

        # Button bar
        btn_bar = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0, height=52)
        btn_bar.grid(row=2, column=0, sticky="ew")
        btn_bar.grid_propagate(False)
        ctk.CTkFrame(btn_bar, fg_color=BORDER, height=1, corner_radius=0).pack(
            side="top", fill="x"
        )

        self._cancel_btn = ctk.CTkButton(
            btn_bar, text="Cancel", width=100, height=30, font=FONT_NORMAL,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_cancel
        )
        self._cancel_btn.pack(side="right", padx=(4, 12), pady=10)

        self._add_btn = ctk.CTkButton(
            btn_bar, text="Add Game", width=110, height=30, font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
            state="disabled", command=self._on_add
        )
        self._add_btn.pack(side="right", padx=4, pady=10)

        # "Remove Instance" — only visible when the game is already configured
        if self._game.is_configured():
            self._remove_btn = ctk.CTkButton(
                btn_bar, text="Remove Instance", width=140, height=30,
                font=FONT_BOLD, fg_color=RED_BTN, hover_color=RED_HOV,
                text_color="white", command=self._on_remove
            )
            self._remove_btn.pack(side="left", padx=12, pady=10)

    # ------------------------------------------------------------------
    # Steam scan (runs in background thread)
    # ------------------------------------------------------------------

    def _start_scan(self):
        self._status_label.configure(text="Scanning Steam libraries…", text_color=TEXT_WARN)
        self._add_btn.configure(state="disabled")
        self._set_path_text("")
        thread = threading.Thread(target=self._scan_worker, daemon=True)
        thread.start()

    def _scan_worker(self):
        libraries = find_steam_libraries()
        found = find_game_in_libraries(libraries, self._game.exe_name)
        # Marshal result back to the main thread
        self.after(0, lambda: self._on_scan_complete(found))

    def _on_scan_complete(self, found: Optional[Path]):
        if found:
            self._set_path(found, status="found")
        else:
            self._status_label.configure(
                text="Not found in Steam libraries. Browse manually to locate the game folder.",
                text_color=TEXT_ERR
            )
            self._set_path_text("")
            self._add_btn.configure(state="disabled")

        # Kick off prefix scan regardless (game path scan result doesn't affect it)
        if self._game.steam_id:
            self._start_prefix_scan()

    # ------------------------------------------------------------------
    # Prefix scan (runs in background thread)
    # ------------------------------------------------------------------

    def _start_prefix_scan(self):
        self._prefix_status_label.configure(
            text="Scanning for Proton prefix…", text_color=TEXT_WARN
        )
        self._set_prefix_text("")
        thread = threading.Thread(target=self._prefix_scan_worker, daemon=True)
        thread.start()

    def _prefix_scan_worker(self):
        found = find_prefix(self._game.steam_id)
        self.after(0, lambda: self._on_prefix_scan_complete(found))

    def _on_prefix_scan_complete(self, found: Optional[Path]):
        if found:
            self._set_prefix(found, status="found")
        else:
            self._prefix_status_label.configure(
                text="Prefix not found automatically. Browse manually if needed.",
                text_color=TEXT_WARN
            )

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    def _set_path(self, path: Path, status: str = "found"):
        self._found_path = path
        self._set_path_text(str(path))
        if status == "configured":
            self._status_label.configure(
                text="Game already configured. You can update the path below.",
                text_color=TEXT_OK
            )
        else:
            self._status_label.configure(
                text="Found via Steam libraries.",
                text_color=TEXT_OK
            )
        self._add_btn.configure(state="normal")

    def _set_path_text(self, text: str):
        self._path_box.configure(state="normal")
        self._path_box.delete("1.0", "end")
        if text:
            self._path_box.insert("end", text)
        self._path_box.configure(state="disabled")

    def _set_prefix(self, path: Path, status: str = "found"):
        self._found_prefix = path
        self._set_prefix_text(str(path))
        if status == "configured":
            self._prefix_status_label.configure(
                text="Prefix already configured. You can update the path below.",
                text_color=TEXT_OK
            )
        else:
            self._prefix_status_label.configure(
                text="Found via Steam compatdata.",
                text_color=TEXT_OK
            )

    def _set_prefix_text(self, text: str):
        self._prefix_box.configure(state="normal")
        self._prefix_box.delete("1.0", "end")
        if text:
            self._prefix_box.insert("end", text)
        self._prefix_box.configure(state="disabled")

    def _set_staging(self, path: Path, status: str = "found"):
        self._custom_staging = path
        self._set_staging_text(str(path))
        if status == "configured":
            self._staging_status_label.configure(
                text="Custom staging folder already configured.",
                text_color=TEXT_OK
            )
        else:
            self._staging_status_label.configure(
                text="Custom staging folder selected.",
                text_color=TEXT_OK
            )

    def _set_staging_text(self, text: str):
        self._staging_box.configure(state="normal")
        self._staging_box.delete("1.0", "end")
        if text:
            self._staging_box.insert("end", text)
        self._staging_box.configure(state="disabled")

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    def _run_zenity(self, title: str, callback):
        """Run zenity in a background thread so the Tkinter event loop stays
        responsive.  The grab is released before zenity opens and re-acquired
        once it closes — without this, the modal grab blocks all X11 events
        and freezes the entire desktop while the file picker is open.

        callback(chosen: Path | None) is called on the main thread with the
        selected directory, or None if the user cancelled or zenity is missing.
        """
        self.grab_release()

        def _worker():
            chosen = None
            try:
                result = subprocess.run(
                    ["zenity", "--file-selection", "--directory",
                     f"--title={title}"],
                    capture_output=True, text=True,
                )
                if result.returncode == 0:
                    p = Path(result.stdout.strip())
                    if p.is_dir():
                        chosen = p
            except FileNotFoundError:
                chosen = None  # zenity not installed
            self.after(0, lambda: self._zenity_done(chosen, callback))

        threading.Thread(target=_worker, daemon=True).start()

    def _zenity_done(self, chosen: Optional[Path], callback):
        """Called on the main thread after zenity closes."""
        try:
            self.grab_set()
        except Exception:
            pass
        callback(chosen)

    def _on_browse(self):
        """Open a zenity folder picker so the user can locate the game manually."""
        def _apply(chosen: Optional[Path]):
            if chosen:
                self._set_path(chosen, status="found")
                self._status_label.configure(
                    text="Folder selected manually.", text_color=TEXT_OK
                )
            else:
                self._status_label.configure(
                    text="No folder selected or zenity not found.",
                    text_color=TEXT_WARN
                )
        self._run_zenity(
            f"Select {self._game.name} installation folder", _apply
        )

    def _on_browse_prefix(self):
        """Open a zenity folder picker so the user can locate the prefix manually."""
        def _apply(chosen: Optional[Path]):
            if chosen:
                self._set_prefix(chosen, status="found")
                self._prefix_status_label.configure(
                    text="Prefix folder selected manually.", text_color=TEXT_OK
                )
            else:
                self._prefix_status_label.configure(
                    text="No folder selected or zenity not found.",
                    text_color=TEXT_WARN
                )
        self._run_zenity(
            f"Select Proton prefix folder (pfx/) for {self._game.name}", _apply
        )

    def _on_browse_staging(self):
        """Open a zenity folder picker to choose a custom mod staging folder."""
        def _apply(chosen: Optional[Path]):
            if chosen:
                self._set_staging(chosen, status="found")
            else:
                self._staging_status_label.configure(
                    text="No folder selected or zenity not found.",
                    text_color=TEXT_WARN
                )
        self._run_zenity(
            f"Select mod staging folder for {self._game.name}", _apply
        )

    def _on_reset_staging(self):
        """Clear any custom staging path and revert to the default location."""
        self._custom_staging = None
        # Show the default path (bypassing any currently-saved custom path)
        from Utils.config_paths import get_profiles_dir
        default_path = get_profiles_dir() / self._game.name / "mods"
        self._set_staging_text(str(default_path))
        self._staging_status_label.configure(
            text="Default location will be used.", text_color=TEXT_DIM
        )

    def _on_remove(self):
        """Ask for confirmation, then restore the game, delete the staging
        folder, and remove paths.json."""
        from Utils.config_paths import get_game_config_path
        from Utils.deploy import restore_root_folder

        profile_root = self._game.get_profile_root()
        paths_json = get_game_config_path(self._game.name)

        # Build a warning message listing what will be deleted
        lines = [
            f"This will permanently remove all data for {self._game.name}:\n",
            f"  • Restore the game to its vanilla state\n",
            f"  • Staging folder (all installed mods, profiles, overwrite):\n"
            f"      {profile_root}\n",
            f"  • Game configuration:\n"
            f"      {paths_json}\n",
            "\nThis action cannot be undone. Continue?",
        ]
        msg = "\n".join(lines)

        confirm = _RemoveConfirmDialog(self, self._game.name, msg)
        self.wait_window(confirm)
        if not confirm.confirmed:
            return

        # Restore the game to vanilla state before deleting anything
        try:
            if hasattr(self._game, "restore"):
                self._game.restore()
        except Exception:
            pass

        try:
            root_folder_dir = profile_root / "Root_Folder"
            game_root = self._game.get_game_path()
            if root_folder_dir.is_dir() and game_root:
                restore_root_folder(root_folder_dir, game_root)
        except Exception:
            pass

        # Delete the staging / profile folder
        if profile_root.is_dir():
            shutil.rmtree(profile_root, ignore_errors=True)

        # Delete the paths.json (and its parent dir if empty)
        if paths_json.is_file():
            paths_json.unlink(missing_ok=True)
            try:
                paths_json.parent.rmdir()          # remove empty game dir
            except OSError:
                pass

        self.result = None
        self.removed = True
        self.grab_release()
        self.destroy()

    def _on_add(self):
        if self._found_path is None:
            return
        self._game.set_game_path(self._found_path)
        if self._found_prefix is not None:
            self._game.set_prefix_path(self._found_prefix)
        if hasattr(self._game, "set_deploy_mode"):
            mode = {
                "symlink": LinkMode.SYMLINK,
                "copy":    LinkMode.COPY,
            }.get(self._deploy_mode_var.get(), LinkMode.HARDLINK)
            self._game.set_deploy_mode(mode)
        if hasattr(self._game, "set_staging_path"):
            self._game.set_staging_path(self._custom_staging)
        _create_profile_structure(self._game)
        self.result = self._found_path
        self.grab_release()
        self.destroy()

    def _on_cancel(self):
        self.result = None
        self.grab_release()
        self.destroy()


# ---------------------------------------------------------------------------
# Remove-confirmation dialog
# ---------------------------------------------------------------------------

class _RemoveConfirmDialog(ctk.CTkToplevel):
    """Modal yes/no dialog warning the user before removing a game instance."""

    WIDTH  = 480
    HEIGHT = 320

    def __init__(self, parent, game_name: str, message: str):
        super().__init__(parent, fg_color=BG_DEEP)
        self.title(f"Remove {game_name}?")
        self.geometry(f"{self.WIDTH}x{self.HEIGHT}")
        self.resizable(False, False)
        self.transient(parent)
        self.confirmed = False

        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)

        # Header
        header = ctk.CTkFrame(self, fg_color=RED_BTN, corner_radius=0, height=40)
        header.grid(row=0, column=0, sticky="ew")
        header.grid_propagate(False)
        ctk.CTkLabel(
            header, text=f"Remove {game_name}?",
            font=FONT_BOLD, text_color="white", anchor="w"
        ).pack(side="left", padx=12, pady=8)

        # Body
        body = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0)
        body.grid(row=1, column=0, sticky="nsew")
        body.grid_columnconfigure(0, weight=1)
        body.grid_rowconfigure(0, weight=1)

        msg_label = ctk.CTkLabel(
            body, text=message,
            font=FONT_NORMAL, text_color=TEXT_MAIN,
            anchor="nw", justify="left", wraplength=self.WIDTH - 40
        )
        msg_label.grid(row=0, column=0, sticky="nsew", padx=16, pady=16)

        # Button bar
        btn_bar = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0, height=52)
        btn_bar.grid(row=2, column=0, sticky="ew")
        btn_bar.grid_propagate(False)
        ctk.CTkFrame(btn_bar, fg_color=BORDER, height=1, corner_radius=0).pack(
            side="top", fill="x"
        )

        ctk.CTkButton(
            btn_bar, text="Cancel", width=100, height=30, font=FONT_NORMAL,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._cancel
        ).pack(side="right", padx=(4, 12), pady=10)

        ctk.CTkButton(
            btn_bar, text="Remove", width=110, height=30, font=FONT_BOLD,
            fg_color=RED_BTN, hover_color=RED_HOV, text_color="white",
            command=self._confirm
        ).pack(side="right", padx=4, pady=10)

        self.after(100, self._make_modal)

    def _make_modal(self):
        try:
            self.grab_set()
            self.focus_set()
        except Exception:
            pass

    def _confirm(self):
        self.confirmed = True
        self.grab_release()
        self.destroy()

    def _cancel(self):
        self.confirmed = False
        self.grab_release()
        self.destroy()


# ---------------------------------------------------------------------------
# Profile folder helper
# ---------------------------------------------------------------------------

def sync_modlist_with_mods_folder(modlist_path: Path, mods_dir: Path) -> None:
    """
    Sync modlist_path against mods_dir:
      - Prepend any mod folders not yet in modlist as disabled entries.
      - Remove any non-separator entries whose folder no longer exists.
    Skips MO2 separator dummy folders (_separator suffix).
    Creates modlist_path if it does not exist.
    """
    if not mods_dir.is_dir():
        if not modlist_path.exists():
            modlist_path.touch()
        return

    on_disk: set[str] = {
        d.name for d in mods_dir.iterdir()
        if d.is_dir() and not d.name.endswith("_separator")
    }

    # Parse existing modlist lines, dropping entries whose folder is gone
    existing_lines: list[str] = []
    existing_names: set[str] = set()
    if modlist_path.exists():
        for line in modlist_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped[0] in ("+", "-", "*"):
                name = stripped[1:]
                # Keep separators always; only keep mods that exist on disk
                if name.endswith("_separator") or name in on_disk:
                    existing_lines.append(stripped)
                    existing_names.add(name)
            else:
                existing_lines.append(stripped)

    new_mods = sorted(on_disk - existing_names)
    new_lines = [f"-{name}" for name in new_mods]

    all_lines = new_lines + existing_lines
    modlist_path.write_text("\n".join(all_lines) + ("\n" if all_lines else ""), encoding="utf-8")


def _create_profile_structure(game: BaseGame) -> None:
    """
    Create the standard profile folder structure for a game if it doesn't exist.

    Profiles/<game.name>/
      mods/           — staging area for installed mods
      overwrite/      — MO2-compatible catch-all for game/tool-generated files
      profiles/
        Profile 1/
          modlist.txt
          plugins.txt
    """
    # get_profile_root() returns the directory that contains mods/, profiles/, etc.
    # - Default: Profiles/<game>/ (mods/ is a subfolder)
    # - Custom staging: the staging path itself is the root
    game_profile_root = game.get_profile_root()
    mods_dir = game.get_mod_staging_path()

    # mods/        — staging area for installed mods
    mods_dir.mkdir(parents=True, exist_ok=True)

    # overwrite/   — MO2-compatible catch-all for files written by the game/tools
    (game_profile_root / "overwrite").mkdir(parents=True, exist_ok=True)

    # Root_Folder/ — files here are deployed to the game's root directory
    (game_profile_root / "Root_Folder").mkdir(parents=True, exist_ok=True)

    # Applications/ — exe files (and shortcuts) to run via Proton
    (game_profile_root / "Applications").mkdir(parents=True, exist_ok=True)

    # profiles/default/  — default profile with empty mod/plugin lists
    profile_dir = game_profile_root / "profiles" / "default"
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "plugins.txt").touch()
    sync_modlist_with_mods_folder(profile_dir / "modlist.txt", mods_dir)
