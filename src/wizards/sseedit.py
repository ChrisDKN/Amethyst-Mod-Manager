"""
sseedit.py
Wizard for running SSEEdit with Skyrim Special Edition.

Workflow
--------
1. Prompt the user to download SSEEdit from Nexus Mods (manual download only).
2. Auto-detect and extract the archive to Profiles/<game>/Applications/SSEEdit/.
3. Deploy the modlist.
4. Run SSEEdit64.exe via Proton with -d:<game>/Data.
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
from gui.path_utils import _to_wine_path

if TYPE_CHECKING:
    from Games.base_game import BaseGame

from gui.theme import (
    ACCENT, ACCENT_HOV, BG_DEEP, BG_HEADER, BG_PANEL,
    TEXT_DIM, TEXT_MAIN,
    FONT_NORMAL, FONT_BOLD,
)

_NEXUS_URL   = "https://www.nexusmods.com/skyrimspecialedition/mods/164?tab=files&file_id=495506"
_EXE_NAME         = "SSEEdit.exe"
_EXE_NAME_QAC     = "SSEEditQuickAutoClean.exe"
_APP_DIR          = "SSEEdit"


def _get_applications_dir(game: "BaseGame") -> Path:
    return game.get_mod_staging_path().parent / "Applications" / _APP_DIR


def _sseedit_exe_path(game: "BaseGame", exe_name: str = _EXE_NAME) -> Path | None:
    p = _get_applications_dir(game) / exe_name
    return p if p.is_file() else None


def _find_archive(downloads_dir: Path) -> Path | None:
    if not downloads_dir.is_dir():
        return None
    candidates = [
        p for p in downloads_dir.iterdir()
        if p.is_file()
        and p.suffix.lower() in {".zip", ".7z", ".rar"}
        and "sseedit" in p.name.lower()
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


def _set_winxp_compat(prefix_path: Path, exe: Path, log_fn=None) -> None:
    """Set the Wine per-app Windows version for *exe* to Windows XP.

    This writes the same entry that winecfg writes when you select an
    application and change its Windows Version to "Windows XP":
        HKCU\\Software\\Wine\\AppDefaults\\<exe.name>  "Version"="winxp"
    in user.reg.
    """
    import time as _time

    _log = log_fn or (lambda _: None)

    # Accept either pfx/ directly or its compatdata parent
    if not (prefix_path / "user.reg").is_file() and (prefix_path / "pfx" / "user.reg").is_file():
        prefix_path = prefix_path / "pfx"
    user_reg = prefix_path / "user.reg"
    if not user_reg.is_file():
        _log(f"Warning: user.reg not found at {user_reg}; skipping WinXP version flag.")
        return

    try:
        text = user_reg.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        _log(f"Warning: could not read user.reg: {exc}")
        return

    section_header = f"[Software\\\\Wine\\\\AppDefaults\\\\{exe.name}]"
    lines = text.splitlines(keepends=True)

    section_start: int | None = None
    section_end: int | None = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.lower().startswith(section_header.lower()):
            section_start = i
        elif section_start is not None and stripped.startswith("["):
            section_end = i
            break

    _filetime_hex = format(int((_time.time() + 11644473600) * 1e7), "x")
    entry_line = '"Version"="winxp"\n'

    if section_start is None:
        if lines and not lines[-1].endswith("\n"):
            lines.append("\n")
        lines.append("\n")
        lines.append(f"{section_header} {_filetime_hex}\n")
        lines.append(f"#time={_filetime_hex}\n")
        lines.append(entry_line)
        _log(f"SSEEdit: set Windows version to WinXP for {exe.name}.")
    else:
        body_start = section_start + 1
        body_end = section_end if section_end is not None else len(lines)
        key_lines = lines[body_start:body_end]

        lines[section_start] = f"{section_header} {_filetime_hex}\n"
        for j, kline in enumerate(key_lines):
            if kline.lower().startswith("#time="):
                key_lines[j] = f"#time={_filetime_hex}\n"
                break

        found = False
        for j, kline in enumerate(key_lines):
            if kline.lower().startswith('"version"='):
                if kline.strip() != entry_line.strip():
                    key_lines[j] = entry_line
                    _log(f"SSEEdit: updated Windows version to WinXP for {exe.name}.")
                found = True
                break
        if not found:
            key_lines.append(entry_line)
            _log(f"SSEEdit: set Windows version to WinXP for {exe.name}.")

        lines[body_start:body_end] = key_lines

    tmp = user_reg.with_suffix(".reg.tmp")
    try:
        tmp.write_text("".join(lines), encoding="utf-8")
        tmp.replace(user_reg)
    except OSError as exc:
        _log(f"Warning: could not write user.reg: {exc}")
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


class SSEEditWizard(ctk.CTkFrame):
    """Step-by-step wizard to set up and run SSEEdit for Skyrim SE."""

    _wizard_title = "Run SSEEdit"
    _exe_name     = _EXE_NAME

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
    # Step 1 — Download SSEEdit (skipped if already extracted)
    # ------------------------------------------------------------------

    def _show_step_download(self):
        if _sseedit_exe_path(self._game, self._exe_name) is not None:
            self._show_step_deploy()
            return

        self._clear_body()

        ctk.CTkLabel(
            self._body, text="Step 1: Download SSEEdit",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(pady=(0, 12))

        ctk.CTkLabel(
            self._body,
            text=(
                "Click the button below to open the SSEEdit page on Nexus Mods.\n\n"
                "Download the archive manually (do NOT use the Mod Manager\n"
                "download button), then click Next."
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
        found = _find_archive(Path.home() / "Downloads")
        if found:
            self._archive_path = found
            self._locate_status.configure(text=f"Found: {found.name}", text_color="#6bc76b")
            self.after(300, self._show_step_extract)
        else:
            self._archive_path = None
            self._locate_status.configure(
                text=(
                    "SSEEdit archive not found in Downloads.\n"
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

        pick_file("Select the SSEEdit archive", lambda p: self.after(0, lambda: _on_picked(p)))

    # ------------------------------------------------------------------
    # Step 3 — Extract archive
    # ------------------------------------------------------------------

    def _show_step_extract(self):
        self._clear_body()

        ctk.CTkLabel(
            self._body, text="Step 3: Extract SSEEdit",
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
            self._log(f"SSEEdit Wizard: extracting {archive.name} \u2192 {dest}")

            paths = _extract_archive(archive, dest)
            file_count = len([p for p in paths if p.is_file()])
            self._log(f"SSEEdit Wizard: extracted {file_count} file(s).")

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
            self._log(f"SSEEdit Wizard: extract error: {exc}")

    # ------------------------------------------------------------------
    # Step 4 — Deploy modlist
    # ------------------------------------------------------------------

    def _show_step_deploy(self):
        self._clear_body()

        ctk.CTkLabel(
            self._body, text="Step 4: Deploy Modlist",
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
            command=self._show_step_run,
        ).pack(side="bottom")

        from gui.dialogs import confirm_deploy_appdata
        if not confirm_deploy_appdata(self.winfo_toplevel(), self._game):
            self._set_label("_deploy_status", "Deploy cancelled — AppData folder missing.", color="#e06c6c")
            return
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
            self._log(f"SSEEdit Wizard: deploy error: {exc}")

    # ------------------------------------------------------------------
    # Step 5 — Run SSEEdit
    # ------------------------------------------------------------------

    def _show_step_run(self):
        self._clear_body()

        ctk.CTkLabel(
            self._body, text="Step 5: Run SSEEdit",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(pady=(0, 12))

        exe = _sseedit_exe_path(self._game, self._exe_name)
        if exe is None:
            ctk.CTkLabel(
                self._body,
                text=(
                    f"{self._exe_name} was not found.\n"
                    "Please restart the wizard and install SSEEdit first."
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
            self._body, text="Launching SSEEdit\u2026",
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

        prefix_path = self._game.get_prefix_path()
        pfx = (prefix_path / "pfx") if prefix_path is not None else None
        data_arg = f'-d:{_to_wine_path(game_path / "Data", pfx)}'

        if prefix_path is not None:
            _set_winxp_compat(prefix_path, exe, log_fn=self._log)

        self._log(f"SSEEdit Wizard: launching {exe} via Proton with {data_arg}")
        try:
            proc = subprocess.Popen(
                ["python3", str(proton_script), "run", str(exe), data_arg],
                env=env,
                cwd=str(exe.parent),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._set_label(
                "_run_status",
                "SSEEdit is running.\nClose it when you are done, then click Done.",
                color="#6bc76b",
            )
            self.after(0, lambda: self._done_btn.configure(state="normal"))
            proc.wait()
            self._log("SSEEdit Wizard: SSEEdit closed.")
            self._set_label("_run_status", "SSEEdit finished.", color="#6bc76b")
            self.after(0, self._on_done)
        except Exception as exc:
            self._set_label("_run_status", f"Launch error: {exc}", color="#e06c6c")
            self._log(f"SSEEdit Wizard: launch error: {exc}")


class SSEEditQACWizard(SSEEditWizard):
    """Variant of SSEEditWizard that runs SSEEditQuickAutoClean.exe."""

    _wizard_title = "Run SSEEdit QAC"
    _exe_name     = _EXE_NAME_QAC
