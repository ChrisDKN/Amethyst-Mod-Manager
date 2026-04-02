"""
dyndolod.py
Wizards for running TexGen and DynDOLOD with Skyrim Special Edition.

Both tools come in the same DynDOLOD archive and use the same flags:
  -d:<game>/Data   (input data path)
  -o:<staging>/TexGen_Output  or  -o:<staging>/DynDOLOD_Output

Workflow
--------
1. Prompt the user to download DynDOLOD from Nexus Mods (manual download only).
2. Auto-detect and extract the archive to Profiles/<game>/Applications/DynDOLOD/.
3. Prompt the user to delete any previous output, then deploy the modlist.
4. Run TexGenx64.exe or DynDOLODx64.exe via Proton with -d: and -o: flags.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import TYPE_CHECKING

import customtkinter as ctk

from Utils.xdg import open_url
from Utils.portal_filechooser import pick_file

if TYPE_CHECKING:
    from Games.base_game import BaseGame

# ---------------------------------------------------------------------------
# Theme
# ---------------------------------------------------------------------------
BG_DEEP    = "#1a1a1a"
BG_PANEL   = "#252526"
BG_HEADER  = "#2a2a2b"
ACCENT     = "#0078d4"
ACCENT_HOV = "#1084d8"
TEXT_MAIN  = "#d4d4d4"
TEXT_DIM   = "#858585"

FONT_NORMAL = ("Segoe UI", 14)
FONT_BOLD   = ("Segoe UI", 14, "bold")

_NEXUS_URL          = "https://www.nexusmods.com/skyrimspecialedition/mods/68518?tab=files"
_XLODGEN_GITHUB_API = "https://api.github.com/repos/sheson/xLODGen/releases/latest"
_APP_DIR          = "DynDOLOD"
_XLODGEN_APP_DIR  = "xLODGen"
_TEXGEN_EXE       = "TexGenx64.exe"
_DYNDOLOD_EXE     = "DynDOLODx64.exe"
_XLODGEN_EXE      = "xLODGenx64.exe"
_TEXGEN_OUT_DIR   = "TexGen_Output"
_DYNDOLOD_OUT_DIR = "DynDOLOD_Output"
_XLODGEN_OUT_DIR  = "xLODGen_Output"


def _get_applications_dir(game: "BaseGame", app_dir: str = _APP_DIR) -> Path:
    return game.get_mod_staging_path().parent / "Applications" / app_dir


def _tool_exe_path(game: "BaseGame", exe_name: str, app_dir: str = _APP_DIR) -> Path | None:
    p = _get_applications_dir(game, app_dir) / exe_name
    return p if p.is_file() else None


def _find_archive(downloads_dir: Path, keyword: str) -> Path | None:
    if not downloads_dir.is_dir():
        return None
    candidates = [
        p for p in downloads_dir.iterdir()
        if p.is_file()
        and p.suffix.lower() in {".zip", ".7z", ".rar"}
        and keyword in p.name.lower()
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _flatten_subdirs(dest: Path, exe_name: str) -> None:
    """Collapse single-subdir wrappers until exe_name is at the top level."""
    while True:
        all_entries = [e for e in dest.iterdir() if e.name != "__MACOSX"]
        subdirs = [e for e in all_entries if e.is_dir()]
        if len(subdirs) == 1 and not (dest / exe_name).is_file():
            wrapper = subdirs[0]
            tmp = dest.parent / (dest.name + "_flatten_tmp")
            wrapper.rename(tmp)
            for item in tmp.iterdir():
                shutil.move(str(item), str(dest / item.name))
            tmp.rmdir()
        else:
            break


def _to_wine_path(p: Path) -> str:
    return "Z:" + str(p).replace("/", "\\")


# ---------------------------------------------------------------------------
# Base wizard — shared download/extract/deploy/run logic
# ---------------------------------------------------------------------------

class _DynDOLODBaseWizard(ctk.CTkFrame):

    _wizard_title  = ""          # overridden by subclasses
    _exe_name      = ""          # overridden by subclasses
    _output_dir    = ""          # overridden by subclasses
    _delete_prompt = ""          # overridden by subclasses
    _app_dir       = _APP_DIR    # overridden by subclasses
    _download_url  = _NEXUS_URL  # overridden by subclasses (unused by xLODGen)
    _archive_kw    = "dyndolod"  # keyword to find the archive in Downloads

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
        self._on_close_cb  = on_close or (lambda: None)
        self._game         = game
        self._log          = log_fn or (lambda msg: None)
        self._archive_path: Path | None = None

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

        self._show_step_download()

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
    # Step 1 — Download (skipped if already extracted)
    # ------------------------------------------------------------------

    def _show_step_download(self):
        if _tool_exe_path(self._game, self._exe_name, self._app_dir) is not None:
            self._show_step_deploy()
            return

        self._clear_body()

        ctk.CTkLabel(
            self._body, text="Step 1: Download DynDOLOD",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(pady=(0, 12))

        ctk.CTkLabel(
            self._body,
            text=(
                "Click the button below to open the DynDOLOD page on Nexus Mods.\n\n"
                "Download the archive manually (do NOT use the Mod Manager\n"
                "download button), then click Next."
            ),
            font=FONT_NORMAL, text_color=TEXT_DIM, justify="center",
        ).pack(pady=(0, 16))

        ctk.CTkButton(
            self._body, text="Open Download Page", width=220, height=36,
            font=FONT_BOLD,
            fg_color="#da8e35", hover_color="#e5a04a", text_color="white",
            command=lambda: open_url(self._download_url),
        ).pack(pady=(0, 20))

        ctk.CTkButton(
            self._body, text="Next \u2192", width=120, height=36,
            font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
            command=self._show_step_locate,
        ).pack(side="bottom")

    # ------------------------------------------------------------------
    # Step 2 — Locate archive
    # ------------------------------------------------------------------

    def _show_step_locate(self):
        self._clear_body()

        ctk.CTkLabel(
            self._body, text="Step 2: Locate the Archive",
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
        found = _find_archive(Path.home() / "Downloads", self._archive_kw)
        if found:
            self._archive_path = found
            self._locate_status.configure(text=f"Found: {found.name}", text_color="#6bc76b")
            self.after(300, self._show_step_extract)
        else:
            self._archive_path = None
            self._locate_status.configure(
                text=(
                    "DynDOLOD archive not found in Downloads.\n"
                    "Make sure you downloaded it, then press Try Again,\n"
                    "or use Browse to select it manually."
                ),
                text_color="#e06c6c",
            )

    def _browse_archive(self):
        def _on_picked(path: Path | None) -> None:
            if path and path.is_file():
                self._archive_path = path
                self._locate_status.configure(text=f"Selected: {path.name}", text_color="#6bc76b")
                self.after(300, self._show_step_extract)

        pick_file("Select the DynDOLOD archive", lambda p: self.after(0, lambda: _on_picked(p)))

    # ------------------------------------------------------------------
    # Step 3 — Extract archive
    # ------------------------------------------------------------------

    def _show_step_extract(self):
        self._clear_body()

        ctk.CTkLabel(
            self._body, text="Step 3: Extract DynDOLOD",
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

            dest = _get_applications_dir(self._game, self._app_dir)
            dest.mkdir(parents=True, exist_ok=True)

            self._set_label("_extract_status", f"Extracting {archive.name}\u2026")
            self._log(f"DynDOLOD Wizard: extracting {archive.name} \u2192 {dest}")

            paths = _extract_archive(archive, dest)
            file_count = len([p for p in paths if p.is_file()])
            self._log(f"DynDOLOD Wizard: extracted {file_count} file(s).")

            _flatten_subdirs(dest, self._exe_name)

            exe = dest / self._exe_name
            if not exe.is_file():
                raise RuntimeError(
                    f"{self._exe_name} not found after extraction.\n"
                    f"Check that the archive contains {self._exe_name}."
                )

            self._set_label("_extract_status", f"Extracted {file_count} file(s).", color="#6bc76b")
            self.after(0, self._show_step_deploy)

        except Exception as exc:
            self._set_label("_extract_status", f"Error: {exc}", color="#e06c6c")
            self._log(f"DynDOLOD Wizard: extract error: {exc}")

    # ------------------------------------------------------------------
    # Step 4 — Delete previous output, then deploy
    # ------------------------------------------------------------------

    def _show_step_deploy(self):
        self._clear_body()

        ctk.CTkLabel(
            self._body, text="Step 4: Deploy Modlist",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(pady=(0, 12))

        ctk.CTkLabel(
            self._body,
            text=self._delete_prompt,
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
            self._log(f"DynDOLOD Wizard: deploy error: {exc}")

    # ------------------------------------------------------------------
    # Step 5 — Run tool
    # ------------------------------------------------------------------

    def _show_step_run(self):
        self._clear_body()

        ctk.CTkLabel(
            self._body, text=f"Step 5: Run {self._wizard_title}",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(pady=(0, 12))

        exe = _tool_exe_path(self._game, self._exe_name, self._app_dir)
        if exe is None:
            ctk.CTkLabel(
                self._body,
                text=(
                    f"{self._exe_name} was not found.\n"
                    "Please restart the wizard and install DynDOLOD first."
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

        game_path = self._game.get_game_path()
        if game_path is None:
            self._set_label("_run_status", "Game path not configured.", color="#e06c6c")
            return

        staging   = self._game.get_effective_mod_staging_path()
        output    = staging / self._output_dir
        output.mkdir(parents=True, exist_ok=True)

        data_arg   = f'-d:{_to_wine_path(game_path / "Data")}'
        output_arg = f'-o:{_to_wine_path(output)}'

        self._log(f"DynDOLOD Wizard: launching {exe} via Proton")
        self._log(f"  args: {data_arg}  {output_arg}  -sse")
        try:
            proc = subprocess.Popen(
                ["python3", str(proton_script), "run", str(exe), data_arg, output_arg, "-sse"],
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
            self._log(f"DynDOLOD Wizard: {self._exe_name} closed.")
            self._set_label("_run_status", f"{self._wizard_title} finished.", color="#6bc76b")
            self.after(0, self._on_done)
        except Exception as exc:
            self._set_label("_run_status", f"Launch error: {exc}", color="#e06c6c")
            self._log(f"DynDOLOD Wizard: launch error: {exc}")


# ---------------------------------------------------------------------------
# Concrete wizards
# ---------------------------------------------------------------------------

class TexGenWizard(_DynDOLODBaseWizard):
    _wizard_title  = "Run TexGen"
    _exe_name      = _TEXGEN_EXE
    _output_dir    = _TEXGEN_OUT_DIR
    _delete_prompt = (
        "Before deploying, please delete any output from a previous\n"
        "TexGen run (the 'TexGen_Output' mod in your mod list).\n\n"
        "Once you have done this, click Deploy."
    )


class DynDOLODWizard(_DynDOLODBaseWizard):
    _wizard_title  = "Run DynDOLOD"
    _exe_name      = _DYNDOLOD_EXE
    _output_dir    = _DYNDOLOD_OUT_DIR
    _delete_prompt = (
        "Before deploying, please delete any output from a previous\n"
        "DynDOLOD run (the 'DynDOLOD_Output' mod in your mod list).\n\n"
        "Once you have done this, click Deploy."
    )


class xLODGenWizard(_DynDOLODBaseWizard):
    _wizard_title  = "Run xLODGen"
    _exe_name      = _XLODGEN_EXE
    _output_dir    = _XLODGEN_OUT_DIR
    _app_dir       = _XLODGEN_APP_DIR
    _archive_kw    = "xlodgen"
    _delete_prompt = (
        "Before deploying, please delete any output from a previous\n"
        "xLODGen run (the 'xLODGen_Output' mod in your mod list).\n\n"
        "Once you have done this, click Deploy."
    )

    # Override the manual download/locate/extract steps with auto-download from GitHub.

    def _show_step_download(self):
        if _tool_exe_path(self._game, self._exe_name, self._app_dir) is not None:
            self._show_step_deploy()
            return

        self._clear_body()

        ctk.CTkLabel(
            self._body, text="Step 1: Download xLODGen",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(pady=(0, 12))

        self._dl_status = ctk.CTkLabel(
            self._body, text="Fetching latest release from GitHub\u2026",
            font=FONT_NORMAL, text_color=TEXT_DIM, justify="center", wraplength=460,
        )
        self._dl_status.pack(pady=(0, 12))

        threading.Thread(target=self._do_auto_download, daemon=True).start()

    def _do_auto_download(self):
        import urllib.request as _urlreq
        import tempfile
        from wizards.script_extender import _fetch_latest_github_asset, _extract_archive

        try:
            self._set_label("_dl_status", "Fetching latest release from GitHub\u2026")
            tag, dl_url = _fetch_latest_github_asset(
                _XLODGEN_GITHUB_API, ["xlodgen"]
            )
            self._set_label("_dl_status", f"Downloading {tag}\u2026")
            self._log(f"xLODGen Wizard: downloading {tag} from {dl_url}")

            suffix = Path(dl_url).suffix or ".7z"
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                tmp_path = Path(tmp.name)

            _urlreq.urlretrieve(dl_url, tmp_path)
            self._log(f"xLODGen Wizard: download complete, extracting\u2026")
            self._set_label("_dl_status", "Extracting\u2026")

            dest = _get_applications_dir(self._game, self._app_dir)
            dest.mkdir(parents=True, exist_ok=True)
            paths = _extract_archive(tmp_path, dest)
            tmp_path.unlink(missing_ok=True)

            file_count = len([p for p in paths if p.is_file()])
            _flatten_subdirs(dest, self._exe_name)

            if not (dest / self._exe_name).is_file():
                raise RuntimeError(f"{self._exe_name} not found after extraction.")

            self._log(f"xLODGen Wizard: extracted {file_count} file(s).")
            self._set_label("_dl_status", f"Downloaded and extracted {tag}.", color="#6bc76b")
            self.after(500, self._show_step_deploy)

        except Exception as exc:
            self._set_label("_dl_status", f"Error: {exc}", color="#e06c6c")
            self._log(f"xLODGen Wizard: download error: {exc}")
