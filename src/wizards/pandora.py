"""
pandora.py
Wizard for running Pandora Behaviour Engine+.

Unlike other wizards, Pandora is installed as a regular mod (not into
Applications/), so this wizard only appears when
"Pandora Behaviour Engine+.exe" can be found under the mod staging folder.

Workflow
--------
1. User is prompted to delete any previous Pandora output mod, then deploy.
2. Silently install .NET 10 desktop runtime into the game prefix
   (skipped if already installed).
3. Run Pandora Behaviour Engine+.exe via Proton with:
     --tesv:<game_path>

   The output folder (<staging>/Pandora) is configured by rewriting
   Pandora's Settings.json inside the Wine prefix, because newer
   Pandora builds ignore the ``--output:`` CLI flag.
"""

from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path
from typing import TYPE_CHECKING

import customtkinter as ctk

from gui.path_utils import _to_wine_path

if TYPE_CHECKING:
    from Games.base_game import BaseGame

from gui.theme import (
    ACCENT, ACCENT_HOV, BG_DEEP, BG_HEADER, BG_PANEL,
    TEXT_ON_ACCENT,
    TEXT_DIM, TEXT_MAIN,
    FONT_NORMAL, FONT_BOLD,
)

_EXE_NAME = "Pandora Behaviour Engine+.exe"

_NET10_URL      = "https://builds.dotnet.microsoft.com/dotnet/WindowsDesktop/10.0.0/windowsdesktop-runtime-10.0.0-win-x64.exe"
_NET10_FILENAME = "windowsdesktop-runtime-10.0.0-win-x64.exe"
_NET10_DEP_KEY  = "dotnet10_windowsdesktop"


def find_pandora_exe(game: "BaseGame") -> Path | None:
    """Search the mod staging directory for Pandora Behaviour Engine+.exe."""
    staging = game.get_effective_mod_staging_path()
    if not staging.is_dir():
        return None
    for candidate in staging.rglob(_EXE_NAME):
        if candidate.is_file():
            return candidate
    return None


class PandoraWizard(ctk.CTkFrame):
    """Wizard to deploy mods and run Pandora Behaviour Engine+."""

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
            text=f"Run Pandora \u2014 {game.name}",
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

    def _safe_after(self, delay: int, fn):
        def _run():
            try:
                if self.winfo_exists():
                    fn()
            except Exception:
                pass
        self.after(delay, _run)

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
    # Step 1 — Delete previous output + deploy
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
                "Before deploying, please delete any output from a previous\n"
                "Pandora run (the 'Pandora' output mod in your mod list).\n\n"
                "Once you have done this, click Deploy."
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
            command=self._show_step_deps,
        ).pack(side="left", padx=(0, 8))

        ctk.CTkButton(
            btn_frame, text="Deploy", width=160, height=36,
            font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color=TEXT_ON_ACCENT,
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

            # Activate the last-deployed profile for restore so rescued
            # runtime files land in *that* profile's overwrite/ — not
            # the shared default. Critical for profile_specific_mods.
            last_deployed = game.get_last_deployed_profile()
            if last_deployed:
                game.set_active_profile_dir(
                    game.get_profile_root() / "profiles" / last_deployed
                )

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
            self._safe_after(0, self._show_step_deps)

        except Exception as exc:
            self._set_label("_deploy_status", f"Deploy error: {exc}", color="#e06c6c")
            self._log(f"Pandora Wizard: deploy error: {exc}")

    # ------------------------------------------------------------------
    # Step 2 — Install .NET 10 (silent)
    # ------------------------------------------------------------------

    def _show_step_deps(self):
        self._clear_body()

        ctk.CTkLabel(
            self._body, text="Step 2: Install Dependencies",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(pady=(0, 12))

        self._net10_status = ctk.CTkLabel(
            self._body, text="Checking .NET 10\u2026",
            font=FONT_NORMAL, text_color=TEXT_DIM, justify="center", wraplength=460,
        )
        self._net10_status.pack(pady=(0, 6))

        threading.Thread(target=self._do_install_deps, daemon=True).start()

    def _do_install_deps(self):
        import urllib.request
        from Utils.config_paths import get_dotnet_cache_dir
        from wizards.pgpatcher import _is_dep_installed, _mark_dep_installed

        proton_script, env, prefix_path = self._get_proton_env()

        if prefix_path is None:
            self._set_label(
                "_net10_status",
                "No Proton prefix configured for this game.\n"
                "Configure the prefix in Game Settings, then reopen this wizard.",
                color="#e06c6c",
            )
            return

        if _is_dep_installed(prefix_path, _NET10_DEP_KEY):
            self._set_label("_net10_status", ".NET 10 already installed \u2014 skipping.", color="#6bc76b")
            self._safe_after(500, self._show_step_run)
            return

        if proton_script is None:
            self._set_label(
                "_net10_status",
                "Could not find Proton \u2014 check that the prefix is configured.",
                color="#e06c6c",
            )
            return

        cache_path = get_dotnet_cache_dir() / _NET10_FILENAME

        try:
            if not cache_path.is_file():
                self._set_label("_net10_status", "Downloading .NET 10 runtime\u2026")
                self._log("Pandora Wizard: downloading .NET 10 runtime \u2026")
                urllib.request.urlretrieve(_NET10_URL, cache_path)
                self._log("Pandora Wizard: .NET 10 download complete.")
            else:
                self._log("Pandora Wizard: using cached .NET 10 installer.")

            self._set_label(
                "_net10_status",
                "Installing .NET 10 into game prefix\u2026\n(this may take a few minutes)",
            )
            self._log("Pandora Wizard: launching .NET 10 installer in game prefix \u2026")

            proc = subprocess.run(
                ["python3", str(proton_script), "run", str(cache_path), "/quiet", "/norestart"],
                env=env,
                cwd=str(cache_path.parent),
            )

            # Exit codes from the .NET desktop runtime installer:
            #   0    = installed successfully
            #   1602 = user cancel
            #   1638 = another version already installed (success, no-op)
            #   3010 = installed, reboot required (success)
            #   102  = already installed / no-op (success)
            _ok_codes = {0, 102, 1638, 3010}
            if proc.returncode not in _ok_codes:
                raise RuntimeError(f".NET 10 installer exited with code {proc.returncode}.")

            _mark_dep_installed(prefix_path, _NET10_DEP_KEY)
            self._set_label("_net10_status", ".NET 10 installed successfully.", color="#6bc76b")
            self._safe_after(500, self._show_step_run)

        except Exception as exc:
            self._set_label("_net10_status", f"Error: {exc}", color="#e06c6c")
            self._log(f"Pandora Wizard: .NET 10 install error: {exc}")

    # ------------------------------------------------------------------
    # Step 3 — Run Pandora
    # ------------------------------------------------------------------

    def _show_step_run(self):
        self._clear_body()

        ctk.CTkLabel(
            self._body, text="Step 3: Run Pandora",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(pady=(0, 12))

        exe = find_pandora_exe(self._game)
        if exe is None:
            ctk.CTkLabel(
                self._body,
                text=(
                    f"'{_EXE_NAME}' was not found in your mod staging folder.\n\n"
                    "Install Pandora Behaviour Engine+ as a mod, then reopen this wizard."
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
            self._body, text="Launching Pandora\u2026",
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

        game_path = self._game.get_game_path()
        if game_path is None:
            self._set_label("_run_status", "Game path not configured.", color="#e06c6c")
            return

        staging = self._game.get_effective_mod_staging_path()

        from gui.plugin_panel import _resolve_compat_data
        compat_data = _resolve_compat_data(_prefix) if _prefix else None

        from Utils.exe_args_builder import _bootstrap_pandora_settings
        _bootstrap_pandora_settings(
            getattr(self._game, "game_id", None),
            game_path,
            staging,
            compat_data,
            self._log,
        )

        pfx = compat_data / "pfx" if compat_data and compat_data.name != "pfx" else compat_data
        game_arg = f'--tesv:{_to_wine_path(game_path, pfx)}'

        # Unset .NET environment variables that can prevent Pandora from launching
        # when the host has a .NET runtime installed (e.g. via Bottles/MO2).
        env.pop("DOTNET_ROOT", None)
        env.pop("DOTNET_BUNDLE_EXTRACT_BASE_DIR", None)

        # WPF rendering over DXVK produces a double title bar / frame glitch
        # in Proton. Forcing the WineD3D GDI renderer bypasses the Vulkan path
        # entirely and gives a single, properly-decorated window.
        # PROTON_USE_WINED3D is required — WINE_D3D_CONFIG only takes effect
        # when WineD3D (not DXVK) is actually handling the d3d calls.
        env["PROTON_USE_WINED3D"] = "1"
        env["WINE_D3D_CONFIG"] = "renderer=gdi"

        cmd = ["python3", str(proton_script), "run", str(exe), game_arg]
        self._log(f"Pandora Wizard: launching {exe} via Proton")
        self._log(f"  cmd: {' '.join(cmd)}")
        self._log(
            "  env: "
            f"PROTON_USE_WINED3D={env.get('PROTON_USE_WINED3D', '<unset>')} "
            f"WINE_D3D_CONFIG={env.get('WINE_D3D_CONFIG', '<unset>')} "
            f"STEAM_COMPAT_DATA_PATH={env.get('STEAM_COMPAT_DATA_PATH', '<unset>')}"
        )
        try:
            proc = subprocess.Popen(
                cmd,
                env=env,
                cwd=str(exe.parent),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            self._set_label(
                "_run_status",
                "Pandora is running.\nClose it when you are done, then click Done.",
                color="#6bc76b",
            )
            self.after(0, lambda: self._done_btn.configure(state="normal"))
            _stdout, stderr_bytes = proc.communicate()
            rc = proc.returncode
            self._log(f"Pandora Wizard: Pandora exited (code {rc}).")
            if stderr_bytes:
                for line in stderr_bytes.decode(errors="replace").splitlines():
                    self._log(f"  Pandora stderr: {line}")
            if rc != 0:
                self._set_label(
                    "_run_status",
                    f"Pandora exited with error (code {rc}).\nSee the log for details. Click Done to close.",
                    color="#e06c6c",
                )
            else:
                self._set_label("_run_status", "Pandora finished. Click Done to close.", color="#6bc76b")
        except Exception as exc:
            self._set_label("_run_status", f"Launch error: {exc}", color="#e06c6c")
            self._log(f"Pandora Wizard: launch error: {exc}")
