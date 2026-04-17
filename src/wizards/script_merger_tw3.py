"""
script_merger_wizard.py
Wizard for running WitcherScriptMerger with The Witcher 3.

Workflow
--------
1. Deploy the modlist.
2. Prompt the user to download Script Merger from Nexus Mods (if not already
   extracted to the Applications folder).
3. Locate the downloaded archive and extract it to
   Profiles/<game>/Applications/ScriptMerger/.
4. Install .NET 8 into the game's Proton prefix via winetricks (skipped if
   already recorded in the prefix's amethyst_deps.json).
5. Run WitcherScriptMerger.exe via Proton.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from pathlib import Path
from typing import TYPE_CHECKING

import customtkinter as ctk

from Utils.xdg import open_url
from Utils.portal_filechooser import pick_file

if TYPE_CHECKING:
    from Games.base_game import BaseGame

from gui.theme import (
    ACCENT, ACCENT_HOV, BG_DEEP, BG_HEADER, BG_PANEL,
    TEXT_DIM, TEXT_MAIN,
    FONT_NORMAL, FONT_BOLD, FONT_SMALL,
)

_NEXUS_URL    = "https://www.nexusmods.com/witcher3/mods/8405?tab=files&file_id=59566"
_MERGER_EXE   = "WitcherScriptMerger.exe"
_MERGER_DIR   = "ScriptMerger"
_DEPS_FILE    = "amethyst_deps.json"
_NET8_URL     = "https://builds.dotnet.microsoft.com/dotnet/WindowsDesktop/8.0.25/windowsdesktop-runtime-8.0.25-win-x64.exe"
_NET8_FILENAME = "windowsdesktop-runtime-8.0.25-win-x64.exe"


def _get_applications_dir(game: "BaseGame") -> Path:
    """Return Profiles/<game>/Applications/ScriptMerger/."""
    return game.get_mod_staging_path().parent / "Applications" / _MERGER_DIR


def _merger_exe_path(game: "BaseGame") -> Path | None:
    """Return the WitcherScriptMerger.exe path if it exists."""
    p = _get_applications_dir(game) / _MERGER_EXE
    return p if p.is_file() else None


_NET8_DEP_KEY = "dotnet8_windowsdesktop"


def _is_net8_installed(prefix_path: Path) -> bool:
    """Return True if .NET 8 is recorded as installed in the prefix."""
    deps_file = prefix_path.parent / _DEPS_FILE
    if not deps_file.is_file():
        return False
    try:
        data = json.loads(deps_file.read_text(encoding="utf-8"))
        return _NET8_DEP_KEY in data.get("installed", [])
    except Exception:
        return False


def _mark_net8_installed(prefix_path: Path) -> None:
    """Record .NET 8 as installed in the prefix's deps file."""
    deps_file = prefix_path.parent / _DEPS_FILE
    try:
        data: dict = {}
        if deps_file.is_file():
            data = json.loads(deps_file.read_text(encoding="utf-8"))
        installed: list = data.get("installed", [])
        if _NET8_DEP_KEY not in installed:
            installed.append(_NET8_DEP_KEY)
        data["installed"] = installed
        deps_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


def _find_merger_archive(downloads_dir: Path) -> Path | None:
    """Return the most-recently-modified archive whose name looks like
    Script Merger, or None."""
    if not downloads_dir.is_dir():
        return None
    candidates = [
        p for p in downloads_dir.iterdir()
        if p.is_file()
        and p.suffix.lower() in {".zip", ".7z", ".rar"}
        and "sm-fae" in p.name.lower()
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _get_downloads_dir() -> Path:
    return Path.home() / "Downloads"


class ScriptMergerWizard(ctk.CTkFrame):
    """Step-by-step wizard to deploy mods and run WitcherScriptMerger."""

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
        self._archive_path: Path | None = None

        title_bar = ctk.CTkFrame(self, fg_color=BG_HEADER, corner_radius=0, height=40)
        title_bar.pack(fill="x")
        title_bar.pack_propagate(False)
        ctk.CTkLabel(
            title_bar,
            text=f"Run Script Merger \u2014 {game.name}",
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

    def _on_done(self):
        """Restore (to pick up merged files), refresh the modlist, then close."""
        try:
            topbar = self.winfo_toplevel()._topbar
        except Exception:
            topbar = None

        self._on_close_cb()

        def _restore_and_refresh():
            try:
                self._game.restore(log_fn=self._log)
            except Exception as exc:
                self._log(f"Script Merger Wizard: restore warning: {exc}")
            if topbar is not None:
                try:
                    topbar.after(0, topbar._reload_mod_panel)
                except Exception:
                    pass

        threading.Thread(target=_restore_and_refresh, daemon=True).start()

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
        from gui.plugin_panel import _resolve_compat_data, _read_prefix_runner
        compat_data = _resolve_compat_data(prefix_path)
        proton_script = find_proton_for_game(steam_id) if steam_id else None

        if proton_script is None:
            preferred_runner = _read_prefix_runner(compat_data)
            proton_script = find_any_installed_proton(preferred_runner)
            if proton_script is None:
                return None, None, prefix_path

        steam_root  = find_steam_root_for_proton_script(proton_script)
        if steam_root is None:
            return None, None, prefix_path

        env = os.environ.copy()
        env["STEAM_COMPAT_DATA_PATH"]          = str(compat_data)
        env["STEAM_COMPAT_CLIENT_INSTALL_PATH"] = str(steam_root)
        game_path = self._game.get_game_path()
        if game_path:
            env["STEAM_COMPAT_INSTALL_PATH"] = str(game_path)
        if steam_id:
            env.setdefault("SteamAppId",  steam_id)
            env.setdefault("SteamGameId", steam_id)

        return proton_script, env, prefix_path

    # ------------------------------------------------------------------
    # Step 1 — Deploy modlist
    # ------------------------------------------------------------------

    def _show_step_deploy(self):
        self._clear_body()

        ctk.CTkLabel(
            self._body, text="Step 1: Deploy Modlist",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(pady=(0, 12))

        self._deploy_status = ctk.CTkLabel(
            self._body, text="Deploying\u2026",
            font=FONT_NORMAL, text_color=TEXT_DIM, justify="center", wraplength=460,
        )
        self._deploy_status.pack(pady=(0, 12))

        ctk.CTkButton(
            self._body, text="Skip", width=100, height=32,
            font=FONT_NORMAL,
            fg_color=BG_HEADER, hover_color="#3d3d3d", text_color=TEXT_DIM,
            command=self._advance_from_deploy,
        ).pack(side="bottom")

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

            # Determine active profile
            try:
                root_win = self.winfo_toplevel()
                profile  = root_win._topbar._profile_var.get()
            except Exception:
                profile = "default"

            def _tlog(msg):
                self.after(0, lambda m=msg: self._log(m))

            # Restore previous deployment first
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
            self.after(0, self._advance_from_deploy)

        except Exception as exc:
            self._set_label("_deploy_status", f"Deploy error: {exc}", color="#e06c6c")
            self._log(f"Script Merger Wizard: deploy error: {exc}")

    def _advance_from_deploy(self):
        # Skip download step if WitcherScriptMerger.exe is already present.
        if _merger_exe_path(self._game) is not None:
            self._show_step_net8()
        else:
            self._show_step_download()

    # ------------------------------------------------------------------
    # Step 2 — Download Script Merger (skipped if already extracted)
    # ------------------------------------------------------------------

    def _show_step_download(self):
        self._clear_body()

        ctk.CTkLabel(
            self._body, text="Step 2: Download Script Merger",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(pady=(0, 12))

        ctk.CTkLabel(
            self._body,
            text=(
                "Click the button below to open the Script Merger download page\n"
                "on Nexus Mods, then download the archive.\n\n"
                "Once downloaded, click Next."
            ),
            font=FONT_NORMAL, text_color=TEXT_DIM, justify="center",
        ).pack(pady=(0, 16))

        ctk.CTkButton(
            self._body, text="Open Download Page", width=220, height=36,
            font=FONT_BOLD,
            fg_color="#da8e35", hover_color="#e5a04a", text_color="white",
            command=lambda: open_url(_NEXUS_URL),
        ).pack(pady=(0, 20))

        ctk.CTkButton(
            self._body, text="Next \u2192", width=120, height=36,
            font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
            command=self._show_step_locate,
        ).pack(side="bottom")

    # ------------------------------------------------------------------
    # Step 3 — Locate archive and extract
    # ------------------------------------------------------------------

    def _show_step_locate(self):
        self._clear_body()

        ctk.CTkLabel(
            self._body, text="Step 3: Locate the Archive",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(pady=(0, 12))

        self._locate_status = ctk.CTkLabel(
            self._body, text="Searching Downloads folder\u2026",
            font=FONT_NORMAL, text_color=TEXT_DIM, justify="center", wraplength=460,
        )
        self._locate_status.pack(pady=(0, 12))

        btn_frame = ctk.CTkFrame(self._body, fg_color="transparent")
        btn_frame.pack(side="bottom", pady=(8, 0))

        ctk.CTkButton(
            btn_frame, text="Try Again", width=100, height=36,
            font=FONT_BOLD,
            fg_color=BG_HEADER, hover_color="#3d3d3d", text_color=TEXT_MAIN,
            command=self._scan_downloads,
        ).pack(side="right", padx=(8, 0))

        ctk.CTkButton(
            btn_frame, text="Browse\u2026", width=100, height=36,
            font=FONT_BOLD,
            fg_color=BG_HEADER, hover_color="#3d3d3d", text_color=TEXT_MAIN,
            command=self._browse_archive,
        ).pack(side="right")

        self._scan_downloads()

    def _scan_downloads(self):
        found = _find_merger_archive(_get_downloads_dir())
        if found:
            self._archive_path = found
            self._locate_status.configure(
                text=f"Found: {found.name}", text_color="#6bc76b",
            )
            self.after(300, self._show_step_extract)
        else:
            self._archive_path = None
            self._locate_status.configure(
                text=(
                    "Script Merger archive not found in Downloads.\n"
                    "Make sure you downloaded it, then press Try Again,\n"
                    "or use Browse to select it manually."
                ),
                text_color="#e06c6c",
            )
    def _browse_archive(self):
        def _on_picked(path: Path | None) -> None:
            if path and path.is_file():
                self._archive_path = path
                self._locate_status.configure(
                    text=f"Selected: {path.name}", text_color="#6bc76b",
                )
                self.after(300, self._show_step_extract)

        pick_file(
            "Select the Script Merger archive",
            lambda p: self.after(0, lambda: _on_picked(p)),
        )

    # ------------------------------------------------------------------
    # Step 4 — Extract archive
    # ------------------------------------------------------------------

    def _show_step_extract(self):
        self._clear_body()

        ctk.CTkLabel(
            self._body, text="Step 4: Extract Script Merger",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(pady=(0, 12))

        self._extract_status = ctk.CTkLabel(
            self._body, text="Extracting\u2026",
            font=FONT_NORMAL, text_color=TEXT_DIM, justify="center", wraplength=460,
        )
        self._extract_status.pack(pady=(0, 16))

        threading.Thread(target=self._do_extract, daemon=True).start()

    def _do_extract(self):
        try:
            from wizards.script_extender import _extract_archive

            archive = self._archive_path
            if archive is None or not archive.is_file():
                raise RuntimeError("Archive not found.")

            dest = _get_applications_dir(self._game)
            dest.mkdir(parents=True, exist_ok=True)

            self._set_label("_extract_status", f"Extracting {archive.name}\u2026")
            self._log(f"Script Merger Wizard: extracting {archive.name} \u2192 {dest}")

            paths = _extract_archive(archive, dest)
            file_count = len([p for p in paths if p.is_file()])
            self._log(f"Script Merger Wizard: extracted {file_count} file(s).")

            # Verify the exe landed
            exe = dest / _MERGER_EXE
            if not exe.is_file():
                raise RuntimeError(
                    f"{_MERGER_EXE} not found after extraction.\n"
                    f"Check that the archive contains {_MERGER_EXE} at its root."
                )

            self._set_label(
                "_extract_status",
                f"Extracted {file_count} file(s).",
                color="#6bc76b",
            )
            self.after(0, self._show_step_net8)

        except Exception as exc:
            self._set_label("_extract_status", f"Error: {exc}", color="#e06c6c")
            self._log(f"Script Merger Wizard: extract error: {exc}")

    # ------------------------------------------------------------------
    # Step 5 — Install .NET 8
    # ------------------------------------------------------------------

    def _show_step_net8(self):
        self._clear_body()

        ctk.CTkLabel(
            self._body, text="Step 5: Install .NET 8",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(pady=(0, 12))

        self._net8_status = ctk.CTkLabel(
            self._body, text="Checking .NET 8\u2026",
            font=FONT_NORMAL, text_color=TEXT_DIM, justify="center", wraplength=460,
        )
        self._net8_status.pack(pady=(0, 12))

        threading.Thread(target=self._do_install_net8, daemon=True).start()

    def _do_install_net8(self):
        import urllib.request
        from Utils.config_paths import get_dotnet_cache_dir

        proton_script, env, prefix_path = self._get_proton_env()

        if prefix_path is None:
            self._set_label(
                "_net8_status",
                "No Proton prefix configured for this game.\n"
                "Configure the prefix in Game Settings, then reopen this wizard.",
                color="#e06c6c",
            )
            return

        # Already installed — skip
        if _is_net8_installed(prefix_path):
            self._set_label("_net8_status", ".NET 8 already installed — skipping.", color="#6bc76b")
            self.after(500, self._show_step_run)
            return

        if proton_script is None:
            self._set_label(
                "_net8_status",
                "Could not find Proton — check that the prefix is configured.",
                color="#e06c6c",
            )
            return

        cache_path = get_dotnet_cache_dir() / _NET8_FILENAME

        try:
            if not cache_path.is_file():
                self._set_label("_net8_status", "Downloading .NET 8 runtime\u2026")
                self._log("Script Merger Wizard: downloading .NET 8 runtime \u2026")
                urllib.request.urlretrieve(_NET8_URL, cache_path)
                self._log("Script Merger Wizard: .NET 8 download complete.")
            else:
                self._log("Script Merger Wizard: using cached .NET 8 installer.")

            self._set_label(
                "_net8_status",
                "Installing .NET 8 into game prefix\u2026\n(this may take a few minutes)",
            )
            self._log("Script Merger Wizard: launching .NET 8 installer in game prefix \u2026")

            proc = subprocess.run(
                ["python3", str(proton_script), "run", str(cache_path), "/quiet", "/norestart"],
                env=env,
                cwd=str(cache_path.parent),
            )

            # Exit codes from the .NET desktop runtime installer:
            #   0    = installed successfully
            #   102  = already installed / no-op
            #   1638 = another version already installed
            #   3010 = installed, reboot required
            _ok_codes = {0, 102, 1638, 3010}
            if proc.returncode not in _ok_codes:
                raise RuntimeError(f".NET 8 installer exited with code {proc.returncode}.")

            _mark_net8_installed(prefix_path)
            self._set_label("_net8_status", ".NET 8 installed successfully.", color="#6bc76b")
            self.after(500, self._show_step_run)

        except Exception as exc:
            self._set_label("_net8_status", f"Error: {exc}", color="#e06c6c")
            self._log(f"Script Merger Wizard: .NET 8 install error: {exc}")

    # ------------------------------------------------------------------
    # Step 6 — Run Script Merger
    # ------------------------------------------------------------------

    def _show_step_run(self):
        self._clear_body()

        ctk.CTkLabel(
            self._body, text="Step 6: Run Script Merger",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(pady=(0, 12))

        exe = _merger_exe_path(self._game)
        if exe is None:
            ctk.CTkLabel(
                self._body,
                text=(
                    f"{_MERGER_EXE} was not found.\n"
                    "Please restart the wizard and install Script Merger first."
                ),
                font=FONT_NORMAL, text_color="#e06c6c", justify="center",
            ).pack(pady=(0, 16))
            ctk.CTkButton(
                self._body, text="Close", width=120, height=36,
                font=FONT_BOLD,
                fg_color=BG_HEADER, hover_color="#3d3d3d", text_color=TEXT_MAIN,
                command=self._on_close_cb,
            ).pack(side="bottom")
            return

        self._run_status = ctk.CTkLabel(
            self._body, text="Launching WitcherScriptMerger\u2026",
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
        proton_script, env, prefix_path = self._get_proton_env()
        if proton_script is None:
            self._set_label(
                "_run_status",
                "Could not find Proton — check that the prefix is configured.",
                color="#e06c6c",
            )
            return

        # Update Script Merger config to point at the game folder
        game_root = self._game.get_game_path()
        if game_root:
            try:
                from Utils.exe_args_builder import update_witcher3_script_merger_config
                update_witcher3_script_merger_config(game_root, exe)
                self._log("Script Merger Wizard: updated Script Merger config with game path.")
            except Exception as cfg_exc:
                self._log(f"Script Merger Wizard: config update warning: {cfg_exc}")

        self._log(f"Script Merger Wizard: launching {exe} via Proton")
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
                "WitcherScriptMerger is running.\nMerge your conflicts, then close it and click Done.",
                color="#6bc76b",
            )
            self.after(0, lambda: self._done_btn.configure(state="normal"))
            proc.wait()
            self._log("Script Merger Wizard: WitcherScriptMerger closed.")
            self._set_label("_run_status", "WitcherScriptMerger closed.", color="#6bc76b")
            self.after(0, self._on_done)
        except Exception as exc:
            self._set_label("_run_status", f"Launch error: {exc}", color="#e06c6c")
            self._log(f"Script Merger Wizard: launch error: {exc}")
