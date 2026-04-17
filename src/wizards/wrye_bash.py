"""
wrye_bash.py
Wizard for installing and running Wrye Bash.

Auto-downloads the latest Standalone Executable release from GitHub.
Extracts to Profiles/<game>/Applications/Wrye Bash/ and runs via Proton.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
import urllib.request
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

_GITHUB_API = "https://api.github.com/repos/wrye-bash/wrye-bash/releases/latest"
_EXE_NAME   = "Wrye Bash.exe"
_APP_DIR    = "Wrye Bash"


def _get_applications_dir(game: "BaseGame") -> Path:
    return game.get_mod_staging_path().parent / "Applications" / _APP_DIR


def _wrye_bash_exe_path(game: "BaseGame") -> Path | None:
    p = _get_applications_dir(game) / _EXE_NAME
    return p if p.is_file() else None


def _flatten_subdirs(dest: Path, exe_name: str) -> None:
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


class WryeBashWizard(ctk.CTkFrame):
    """Wizard to download, install and run Wrye Bash."""

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
            text=f"Run Wrye Bash \u2014 {game.name}",
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
    # Step 1 — Auto-download from GitHub (skipped if already installed)
    # ------------------------------------------------------------------

    def _show_step_download(self):
        if _wrye_bash_exe_path(self._game) is not None:
            self._show_step_run()
            return

        self._clear_body()

        ctk.CTkLabel(
            self._body, text="Step 1: Download Wrye Bash",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(pady=(0, 12))

        self._dl_status = ctk.CTkLabel(
            self._body, text="Fetching latest release from GitHub\u2026",
            font=FONT_NORMAL, text_color=TEXT_DIM, justify="center", wraplength=460,
        )
        self._dl_status.pack(pady=(0, 12))

        threading.Thread(target=self._do_auto_download, daemon=True).start()

    def _do_auto_download(self):
        from wizards.script_extender import _fetch_latest_github_asset, _extract_archive

        try:
            self._set_label("_dl_status", "Fetching latest release from GitHub\u2026")
            tag, dl_url = _fetch_latest_github_asset(
                _GITHUB_API, ["standalone", "executable"]
            )
            self._set_label("_dl_status", f"Downloading {tag}\u2026")
            self._log(f"Wrye Bash Wizard: downloading {tag} from {dl_url}")

            suffix = Path(dl_url).suffix or ".7z"
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                tmp_path = Path(tmp.name)

            urllib.request.urlretrieve(dl_url, tmp_path)
            self._set_label("_dl_status", "Extracting\u2026")
            self._log("Wrye Bash Wizard: download complete, extracting\u2026")

            dest = _get_applications_dir(self._game)
            dest.mkdir(parents=True, exist_ok=True)
            paths = _extract_archive(tmp_path, dest)
            tmp_path.unlink(missing_ok=True)

            file_count = len([p for p in paths if p.is_file()])
            _flatten_subdirs(dest, _EXE_NAME)

            if not (dest / _EXE_NAME).is_file():
                raise RuntimeError(f"{_EXE_NAME!r} not found after extraction.")

            self._log(f"Wrye Bash Wizard: extracted {file_count} file(s).")
            self._set_label("_dl_status", f"Downloaded and extracted {tag}.", color="#6bc76b")
            self.after(500, self._show_step_run)

        except Exception as exc:
            self._set_label("_dl_status", f"Error: {exc}", color="#e06c6c")
            self._log(f"Wrye Bash Wizard: download error: {exc}")

    # ------------------------------------------------------------------
    # Step 2 — Run Wrye Bash
    # ------------------------------------------------------------------

    def _show_step_run(self):
        self._clear_body()

        ctk.CTkLabel(
            self._body, text="Step 2: Run Wrye Bash",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(pady=(0, 12))

        exe = _wrye_bash_exe_path(self._game)
        if exe is None:
            ctk.CTkLabel(
                self._body,
                text=(
                    f"{_EXE_NAME!r} was not found.\n"
                    "Please restart the wizard to reinstall Wrye Bash."
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
            self._body, text="Launching Wrye Bash\u2026",
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

        game_path = self._game.get_game_path()

        # WB derives its .wbtemp dir from the drive letter of the -o path.
        # Z:\ (Wine's Linux root mapping) is not writable, so we symlink the
        # game folder into drive_c/wb_games/ and pass a C:\ path instead.
        game_arg = []
        if game_path:
            real_game = game_path.resolve()
            from gui.plugin_panel import _resolve_compat_data
            compat_data = _resolve_compat_data(prefix_path)
            c_games = compat_data / "pfx" / "drive_c" / "wb_games"
            c_games.mkdir(parents=True, exist_ok=True)
            link = c_games / real_game.name
            if not link.exists() and not link.is_symlink():
                link.symlink_to(real_game)
            game_arg = ["-o", f"C:\\wb_games\\{real_game.name}"]

        self._log(f"Wrye Bash Wizard: launching {exe} via Proton" + (f" with -o C:\\wb_games\\{game_path.resolve().name}" if game_path else ""))
        try:
            proc = subprocess.Popen(
                ["python3", str(proton_script), "run", str(exe)] + game_arg,
                env=env,
                cwd=str(exe.parent),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._set_label(
                "_run_status",
                "Wrye Bash is running.\nClose it when you are done, then click Done.",
                color="#6bc76b",
            )
            self.after(0, lambda: self._done_btn.configure(state="normal"))
            proc.wait()
            self._log("Wrye Bash Wizard: Wrye Bash closed.")
            self._set_label("_run_status", "Wrye Bash finished.", color="#6bc76b")
            self.after(0, self._on_done)
        except Exception as exc:
            self._set_label("_run_status", f"Launch error: {exc}", color="#e06c6c")
            self._log(f"Wrye Bash Wizard: launch error: {exc}")
