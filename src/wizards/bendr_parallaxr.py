"""
bendr_parallaxr.py
Wizards for running BENDr and ParallaxR texture processing pipelines.

Both tools must be downloaded manually from Nexus Mods and are run via the
Linux wrappers (wrappers.bendr / wrappers.parallaxr).

Workflow
--------
1. Prompt the user to download from Nexus Mods.
2. Auto-detect and extract the archive to Profiles/<game>/Applications/<tool>/.
3. Deploy the modlist.
4. Run the pipeline (opens the log panel automatically).
"""

from __future__ import annotations

import shutil
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



def _get_applications_dir(game: "BaseGame", app_dir: str) -> Path:
    return game.get_mod_staging_path().parent / "Applications" / app_dir


def _is_installed(game: "BaseGame", app_dir: str) -> bool:
    return (_get_applications_dir(game, app_dir) / "tools").is_dir()


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


def _flatten_subdirs(dest: Path) -> None:
    """Collapse single-subdir wrappers until the tools/ dir is at the top level."""
    while True:
        all_entries = [e for e in dest.iterdir() if e.name != "__MACOSX"]
        subdirs = [e for e in all_entries if e.is_dir()]
        if len(subdirs) == 1 and not (dest / "tools").is_dir():
            wrapper = subdirs[0]
            tmp = dest.parent / (dest.name + "_flatten_tmp")
            wrapper.rename(tmp)
            for item in tmp.iterdir():
                shutil.move(str(item), str(dest / item.name))
            tmp.rmdir()
        else:
            break


# ---------------------------------------------------------------------------
# Base wizard
# ---------------------------------------------------------------------------

class _TextureToolWizard(ctk.CTkFrame):

    _wizard_title = ""       # e.g. "BENDr"
    _nexus_url    = ""
    _app_dir      = ""       # folder name under Applications/
    _archive_kw   = ""       # keyword to find archive in Downloads
    _output_dir   = ""       # mod name under staging, e.g. "BENDr"
    _run_desc     = ""       # one-line description shown on run step

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
            text=f"Run {self._wizard_title} \u2014 {game.name}",
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
    # Step 1 — Download (skipped if already installed)
    # ------------------------------------------------------------------

    def _show_step_download(self):
        if _is_installed(self._game, self._app_dir):
            self._show_step_deploy()
            return

        self._clear_body()

        ctk.CTkLabel(
            self._body, text=f"Step 1: Download {self._wizard_title}",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(pady=(0, 12))

        ctk.CTkLabel(
            self._body,
            text=(
                f"Click the button below to open the {self._wizard_title} page on Nexus Mods.\n\n"
                "Download the archive manually (do NOT use the Mod Manager\n"
                "download button), then click Next."
            ),
            font=FONT_NORMAL, text_color=TEXT_DIM, justify="center",
        ).pack(pady=(0, 16))

        ctk.CTkButton(
            self._body, text="Open Download Page", width=220, height=36,
            font=FONT_BOLD,
            fg_color="#da8e35", hover_color="#e5a04a", text_color="white",
            command=lambda: open_url(self._nexus_url),
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
                    f"{self._wizard_title} archive not found in Downloads.\n"
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

        pick_file(
            f"Select the {self._wizard_title} archive",
            lambda p: self.after(0, lambda: _on_picked(p)),
        )

    # ------------------------------------------------------------------
    # Step 3 — Extract archive
    # ------------------------------------------------------------------

    def _show_step_extract(self):
        self._clear_body()

        ctk.CTkLabel(
            self._body, text=f"Step 3: Extract {self._wizard_title}",
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
            self._log(f"{self._wizard_title} Wizard: extracting {archive.name} \u2192 {dest}")

            paths = _extract_archive(archive, dest)
            file_count = len([p for p in paths if p.is_file()])
            self._log(f"{self._wizard_title} Wizard: extracted {file_count} file(s).")

            _flatten_subdirs(dest)

            if not _is_installed(self._game, self._app_dir):
                raise RuntimeError(
                    f"{self._wizard_title} tools/ directory not found after extraction.\n"
                    "Check that the archive is the correct download."
                )

            self._set_label("_extract_status", f"Extracted {file_count} file(s).", color="#6bc76b")
            self.after(0, self._show_step_deploy)

        except Exception as exc:
            self._set_label("_extract_status", f"Error: {exc}", color="#e06c6c")
            self._log(f"{self._wizard_title} Wizard: extract error: {exc}")

    # ------------------------------------------------------------------
    # Step 4 — Deploy
    # ------------------------------------------------------------------

    def _show_step_deploy(self):
        self._clear_body()

        ctk.CTkLabel(
            self._body, text="Step 4: Deploy Modlist",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(pady=(0, 12))

        ctk.CTkLabel(
            self._body,
            text=(
                f"{self._wizard_title} reads directly from the deployed game Data folder.\n\n"
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
    # Step 5 — Run
    # ------------------------------------------------------------------

    def _show_step_run(self):
        self._clear_body()

        ctk.CTkLabel(
            self._body, text=f"Step 5: Run {self._wizard_title}",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(pady=(0, 8))

        ctk.CTkLabel(
            self._body, text=self._run_desc,
            font=FONT_SMALL, text_color=TEXT_DIM, justify="center", wraplength=460,
        ).pack(pady=(0, 16))

        staging = self._game.get_effective_mod_staging_path()
        output_dir = staging / self._output_dir
        ctk.CTkLabel(
            self._body, text=f"Output: {output_dir}",
            font=FONT_SMALL, text_color=TEXT_DIM, wraplength=460,
        ).pack(pady=(0, 12))

        self._run_status = ctk.CTkLabel(
            self._body, text="",
            font=FONT_NORMAL, text_color=TEXT_DIM, justify="center", wraplength=460,
        )
        self._run_status.pack(pady=(0, 8))

        self._run_btn = ctk.CTkButton(
            self._body, text=f"\u25b6  Run {self._wizard_title}", width=180, height=36,
            font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
            command=self._start_run,
        )
        self._run_btn.pack(pady=(0, 8))

        ctk.CTkButton(
            self._body, text="Close", width=100, height=36,
            font=FONT_BOLD,
            fg_color=BG_HEADER, hover_color="#3d3d3d", text_color=TEXT_DIM,
            command=self._on_close_cb,
        ).pack(side="bottom")

    def _start_run(self):
        if not _is_installed(self._game, self._app_dir):
            self._set_label("_run_status", f"{self._wizard_title} not found. Please restart the wizard.", color="#e06c6c")
            return

        game_data_dir = self._game.get_mod_data_path()
        if game_data_dir is None or not game_data_dir.is_dir():
            self._set_label("_run_status", "Game Data folder not found. Deploy first.", color="#e06c6c")
            return

        self._run_btn.configure(state="disabled")
        bat_dir = _get_applications_dir(self._game, self._app_dir)
        staging = self._game.get_effective_mod_staging_path()
        output_dir = staging / self._output_dir
        log_fn = self._log

        self._set_label("_run_status", f"Running {self._wizard_title}\u2026 This may take a while.")
        self._log(f"{self._wizard_title} Wizard: starting pipeline\u2026")

        # Open the log panel so the user can see progress
        app = self.winfo_toplevel()
        if hasattr(app, "_status"):
            app._status.show_log()

        def _log_safe(msg: str):
            try:
                if hasattr(app, "call_threadsafe"):
                    app.call_threadsafe(lambda m=msg: log_fn(m))
                else:
                    log_fn(msg)
            except Exception:
                pass

        def _worker():
            try:
                self._run_pipeline(bat_dir, game_data_dir, output_dir, _log_safe)
                self._set_label("_run_status", f"{self._wizard_title} complete! Output is ready as a mod.", color="#6bc76b")
                self.after(0, self._on_done)
            except Exception as exc:
                self._set_label("_run_status", f"Error: {exc}", color="#e06c6c")
                _log_safe(f"{self._wizard_title} Wizard: error: {exc}")
                self.after(0, lambda: self._run_btn.configure(state="normal"))

        threading.Thread(target=_worker, daemon=True).start()

    def _run_pipeline(self, bat_dir: Path, game_data_dir: Path, output_dir: Path, log_fn):
        """Override in subclasses to call the appropriate wrapper."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Concrete wizards
# ---------------------------------------------------------------------------

class BENDrWizard(_TextureToolWizard):
    _wizard_title = "BENDr"
    _nexus_url    = "https://www.nexusmods.com/skyrimspecialedition/mods/121578?tab=files"
    _app_dir      = "BENDr"
    _archive_kw   = "bendr"
    _output_dir   = "BENDr"
    _run_desc     = "Processes normal maps: BSA extract \u2192 filter \u2192 parallax prep \u2192 bend normals \u2192 BC7 compress"

    def _run_pipeline(self, bat_dir, game_data_dir, output_dir, log_fn):
        from wrappers.bendr import run_bendr
        run_bendr(bat_dir=bat_dir, game_data_dir=game_data_dir, output_dir=output_dir, log_fn=log_fn)


class ParallaxRWizard(_TextureToolWizard):
    _wizard_title = "ParallaxR"
    _nexus_url    = "https://www.nexusmods.com/skyrimspecialedition/mods/124711?tab=files"
    _app_dir      = "ParallaxR"
    _archive_kw   = "parallaxr"
    _output_dir   = "ParallaxR"
    _run_desc     = "Processes parallax textures: BSA extract \u2192 filter pairs \u2192 height maps \u2192 output QC"

    def _run_pipeline(self, bat_dir, game_data_dir, output_dir, log_fn):
        from wrappers.parallaxr import run_parallaxr
        run_parallaxr(bat_dir=bat_dir, game_data_dir=game_data_dir, output_dir=output_dir, log_fn=log_fn)
