"""
bodyslide.py
Wizards for running BodySlide x64.exe and OutfitStudio x64.exe.

Both tools are installed as regular mods (not into Applications/).  The wizard
only appears when the relevant exe is found under the mod staging folder.

Key requirement: the exe must be launched with cwd set to the game's Data
folder so it can locate game assets correctly.

Workflow
--------
1. Deploy the modlist.
2. Run the exe from <game_path>/Data via Proton.
"""

from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path
from typing import TYPE_CHECKING

import customtkinter as ctk

if TYPE_CHECKING:
    from Games.base_game import BaseGame

from gui.theme import (
    ACCENT, ACCENT_HOV, BG_DEEP, BG_HEADER, BG_PANEL,
    TEXT_DIM, TEXT_MAIN,
    FONT_NORMAL, FONT_BOLD,
)


def find_mod_exe(game: "BaseGame", exe_name: str) -> Path | None:
    """Search the mod staging directory for exe_name (used to decide whether to show the wizard)."""
    staging = game.get_effective_mod_staging_path()
    if not staging.is_dir():
        return None
    for candidate in staging.rglob(exe_name):
        if candidate.is_file():
            return candidate
    return None


def find_deployed_exe(game: "BaseGame", exe_name: str) -> Path | None:
    """Search the deployed Data directory for exe_name (used at launch time after deploy)."""
    data_path = game.get_mod_data_path()
    if data_path is None or not data_path.is_dir():
        return None
    for candidate in data_path.rglob(exe_name):
        if candidate.is_file():
            return candidate
    return None


# ---------------------------------------------------------------------------
# Base wizard
# ---------------------------------------------------------------------------

class _BodySlideBaseWizard(ctk.CTkFrame):

    _wizard_title = ""   # overridden by subclasses
    _exe_name     = ""   # overridden by subclasses

    def __init__(
        self,
        parent,
        game: "BaseGame",
        log_fn=None,
        *,
        on_close=None,
        **_kwargs,
    ):
        super().__init__(parent, fg_color=BG_DEEP, corner_radius=0)
        self._on_close_cb = on_close or (lambda: None)
        self._game        = game
        self._log         = log_fn or (lambda msg: None)

        title_bar = ctk.CTkFrame(self, fg_color=BG_HEADER, corner_radius=0, height=40)
        title_bar.pack(fill="x")
        title_bar.pack_propagate(False)
        ctk.CTkLabel(
            title_bar,
            text=f"{self._wizard_title} \u2014 {game.name}",
            font=FONT_BOLD, text_color=TEXT_MAIN, anchor="w",
        ).pack(side="left", padx=12, pady=8)
        ctk.CTkButton(
            title_bar, text="\u2715", width=32, height=32, font=FONT_BOLD,
            fg_color="transparent", hover_color=BG_PANEL, text_color=TEXT_MAIN,
            command=self._on_close_cb,
        ).pack(side="right", padx=4, pady=4)

        self._body = ctk.CTkFrame(self, fg_color=BG_DEEP)
        self._body.pack(fill="both", expand=True, padx=20, pady=20)

        self._show_step_deploy()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _clear_body(self):
        for w in self._body.winfo_children():
            w.destroy()

    def _set_label(self, attr: str, text: str, color: str = TEXT_DIM):
        def _apply(t=text, c=color):
            try:
                widget = getattr(self, attr, None)
                if widget is not None and widget.winfo_exists():
                    widget.configure(text=t, text_color=c)
            except Exception:
                pass
        self.after(0, _apply)

    def _get_proton_env(self):
        from Utils.steam_finder import (
            find_any_installed_proton,
            find_proton_for_game,
            find_steam_root_for_proton_script,
        )

        prefix_path = self._game.get_prefix_path()
        if prefix_path is None or not prefix_path.is_dir():
            return None, None, None

        steam_id    = getattr(self._game, "steam_id", "")
        compat_data = prefix_path.parent if prefix_path.name == "pfx" else prefix_path
        proton_script = find_proton_for_game(steam_id) if steam_id else None

        if proton_script is None:
            from gui.plugin_panel import _read_prefix_runner
            preferred_runner = _read_prefix_runner(compat_data)
            proton_script = find_any_installed_proton(preferred_runner)
            if proton_script is None:
                return None, None, prefix_path

        steam_root = find_steam_root_for_proton_script(proton_script)
        if steam_root is None:
            return None, None, prefix_path

        env = os.environ.copy()
        env["STEAM_COMPAT_DATA_PATH"]           = str(compat_data)
        env["STEAM_COMPAT_CLIENT_INSTALL_PATH"] = str(steam_root)
        game_path = self._game.get_game_path()
        if game_path:
            env["STEAM_COMPAT_INSTALL_PATH"] = str(game_path)
        if steam_id:
            env.setdefault("SteamAppId",  steam_id)
            env.setdefault("SteamGameId", steam_id)

        return proton_script, env, prefix_path

    def _on_done(self):
        try:
            topbar = self.winfo_toplevel()._topbar
        except Exception:
            topbar = None
        self._on_close_cb()
        if topbar is not None:
            try:
                topbar.after(0, topbar._reload_mod_panel)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Step 1 — Deploy
    # ------------------------------------------------------------------

    def _show_step_deploy(self):
        self._clear_body()

        ctk.CTkLabel(
            self._body, text="Step 1: Deploy Modlist",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(pady=(0, 12))

        ctk.CTkLabel(
            self._body,
            text=(
                f"{self._wizard_title} must be run from the deployed Data folder.\n\n"
                "Deploy your modlist first, then click Run."
            ),
            font=FONT_NORMAL, text_color=TEXT_DIM, justify="center", wraplength=460,
        ).pack(pady=(0, 20))

        self._deploy_status = ctk.CTkLabel(
            self._body, text="",
            font=FONT_NORMAL, text_color=TEXT_DIM, justify="center", wraplength=460,
        )
        self._deploy_status.pack(pady=(0, 8))

        btn_frame = ctk.CTkFrame(self._body, fg_color="transparent")
        btn_frame.pack(side="bottom", pady=(8, 0))

        ctk.CTkButton(
            btn_frame, text="Skip", width=100, height=36,
            font=FONT_BOLD,
            fg_color=BG_HEADER, hover_color="#3d3d3d", text_color=TEXT_DIM,
            command=self._show_step_run,
        ).pack(side="left", padx=(0, 8))

        ctk.CTkButton(
            btn_frame, text="Deploy", width=160, height=36,
            font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
            command=self._start_deploy,
        ).pack(side="left")

    def _start_deploy(self):
        from gui.dialogs import confirm_deploy_appdata
        if not confirm_deploy_appdata(self.winfo_toplevel(), self._game):
            self._set_label("_deploy_status", "Deploy cancelled — AppData folder missing.", color="#e06c6c")
            return
        for w in self._body.winfo_children():
            if isinstance(w, ctk.CTkButton):
                w.configure(state="disabled")
        self._set_label("_deploy_status", "Deploying\u2026")
        threading.Thread(target=self._do_deploy, daemon=True).start()

    def _do_deploy(self):
        try:
            from Utils.filemap import build_filemap
            from Utils.deploy import (
                LinkMode,
                deploy_root_folder,
                restore_root_folder,
                load_per_mod_strip_prefixes,
            )
            from Utils.profile_state import read_excluded_mod_files

            game      = self._game
            game_root = game.get_game_path()

            try:
                root_win = self.winfo_toplevel()
                profile  = root_win._topbar._profile_var.get()
            except Exception:
                profile = "default"

            def _tlog(msg):
                self.after(0, lambda m=msg: self._log(m))

            if getattr(game, "restore_before_deploy", True) and hasattr(game, "restore"):
                try:
                    game.restore(log_fn=_tlog)
                except RuntimeError:
                    pass
            restore_rf = game.get_effective_root_folder_path()
            if restore_rf.is_dir() and game_root:
                restore_root_folder(restore_rf, game_root, log_fn=_tlog)

            game.set_active_profile_dir(
                game.get_profile_root() / "profiles" / profile
            )

            profile_root = game.get_profile_root()
            staging      = game.get_effective_mod_staging_path()
            modlist_path = profile_root / "profiles" / profile / "modlist.txt"
            filemap_out  = staging.parent / "filemap.txt"

            if modlist_path.is_file():
                exc_raw = read_excluded_mod_files(modlist_path.parent, None)
                exc = {k: set(v) for k, v in exc_raw.items()} if exc_raw else None
                build_filemap(
                    modlist_path, staging, filemap_out,
                    strip_prefixes=game.mod_folder_strip_prefixes or None,
                    per_mod_strip_prefixes=load_per_mod_strip_prefixes(modlist_path.parent),
                    allowed_extensions=game.mod_install_extensions or None,
                    root_deploy_folders=game.mod_root_deploy_folders or None,
                    excluded_mod_files=exc,
                    conflict_ignore_filenames=getattr(game, "conflict_ignore_filenames", None) or None,
                )

            deploy_mode = game.get_deploy_mode() if hasattr(game, "get_deploy_mode") else LinkMode.HARDLINK
            game.deploy(log_fn=_tlog, profile=profile, mode=deploy_mode)

            from Utils.wine_dll_config import deploy_game_wine_dll_overrides
            pfx = game.get_prefix_path()
            if pfx and pfx.is_dir():
                deploy_game_wine_dll_overrides(game.name, pfx, game.wine_dll_overrides, log_fn=_tlog)

            game.save_last_deployed_profile(profile)

            target_rf  = game.get_effective_root_folder_path()
            rf_allowed = getattr(game, "root_folder_deploy_enabled", True)
            if rf_allowed and target_rf.is_dir() and game_root:
                deploy_root_folder(target_rf, game_root, mode=deploy_mode, log_fn=_tlog)

            self._set_label("_deploy_status", "Deploy complete.", color="#6bc76b")
            self.after(0, self._show_step_run)

        except Exception as exc:
            self._set_label("_deploy_status", f"Deploy error: {exc}", color="#e06c6c")
            self._log(f"{self._wizard_title} Wizard: deploy error: {exc}")

    # ------------------------------------------------------------------
    # Step 2 — Run
    # ------------------------------------------------------------------

    def _show_step_run(self):
        self._clear_body()

        ctk.CTkLabel(
            self._body, text=f"Step 2: Run {self._wizard_title}",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(pady=(0, 12))

        exe = find_deployed_exe(self._game, self._exe_name)
        if exe is None:
            ctk.CTkLabel(
                self._body,
                text=(
                    f"'{self._exe_name}' was not found in the deployed Data folder.\n\n"
                    f"Deploy your modlist first, then reopen this wizard."
                ),
                font=FONT_NORMAL, text_color="#e06c6c", justify="center", wraplength=460,
            ).pack(pady=(0, 16))
            ctk.CTkButton(
                self._body, text="Close", width=120, height=36,
                font=FONT_BOLD,
                fg_color=BG_HEADER, hover_color="#3d3d3d", text_color=TEXT_MAIN,
                command=self._on_close_cb,
            ).pack(side="bottom")
            return

        self._run_status = ctk.CTkLabel(
            self._body, text=f"Launching {self._wizard_title}\u2026",
            font=FONT_NORMAL, text_color=TEXT_DIM, justify="center", wraplength=460,
        )
        self._run_status.pack(pady=(0, 12))

        self._done_btn = ctk.CTkButton(
            self._body, text="Done", width=120, height=36,
            font=FONT_BOLD,
            fg_color="#2d7a2d", hover_color="#3a9e3a", text_color="white",
            command=self._on_done, state="disabled",
        )
        self._done_btn.pack(side="bottom")

        threading.Thread(target=lambda: self._do_run(exe), daemon=True).start()

    def _do_run(self, exe: Path):
        proton_script, env, _prefix = self._get_proton_env()
        if proton_script is None:
            self._set_label(
                "_run_status",
                "Could not find Proton — check that the prefix is configured.",
                color="#e06c6c",
            )
            return

        self._log(f"{self._wizard_title} Wizard: launching {exe} via Proton (cwd={exe.parent})")
        try:
            proc = subprocess.Popen(
                ["python3", str(proton_script), "run", str(exe)],
                env=env,
                cwd=str(exe.parent),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._set_label(
                "_run_status",
                f"{self._wizard_title} is running.\nClose it when you are done, then click Done.",
                color="#6bc76b",
            )
            self.after(0, lambda: self._done_btn.configure(state="normal"))
            proc.wait()
            self._log(f"{self._wizard_title} Wizard: {self._exe_name} closed.")
            self._set_label("_run_status", f"{self._wizard_title} finished.", color="#6bc76b")
            self.after(0, self._on_done)
        except Exception as exc:
            self._set_label("_run_status", f"Launch error: {exc}", color="#e06c6c")
            self._log(f"{self._wizard_title} Wizard: launch error: {exc}")


# ---------------------------------------------------------------------------
# Concrete wizards
# ---------------------------------------------------------------------------

class BodySlideWizard(_BodySlideBaseWizard):
    _wizard_title = "BodySlide"
    _exe_name     = "BodySlide x64.exe"


class OutfitStudioWizard(_BodySlideBaseWizard):
    _wizard_title = "Outfit Studio"
    _exe_name     = "OutfitStudio x64.exe"
