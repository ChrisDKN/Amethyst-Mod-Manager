"""
SMAPI installation wizard for Stardew Valley (hotfix plugin).

Replaces the built-in SMAPI wizard, which failed because the terminal
closed before the installer could run interactively and the cwd was
not set to the extracted folder (installer uses relative paths).

Fixes:
  * Writes a bash wrapper that cd's into the extracted folder before
    running "install on Linux.sh", then pauses with `read` so the user
    can read the installer's output.
  * Detects flatpak and prefers `flatpak-spawn --host` for the terminal
    so konsole/gnome-terminal on the host can be reached from inside
    the sandbox.
  * Chmods the install script, SMAPI.Installer binary, and any fallback
    shell helpers before launching.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import threading
import urllib.request
import zipfile
import json as _json
from pathlib import Path

import customtkinter as ctk

from Utils.portal_filechooser import pick_file
from gui.theme import (
    ACCENT, ACCENT_HOV, BG_DEEP, BG_HEADER, BG_PANEL, BORDER,
    TEXT_DIM, TEXT_MAIN,
    FONT_NORMAL, FONT_BOLD, FONT_SMALL,
)

PLUGIN_INFO = {
    "id":           "sdv_smapi_hotfix",
    "label":        "Install SMAPI (Updated)",
    "description":  "Download and install SMAPI. Use this instead of the older 'Install SMAPI' option.",
    "game_ids":     ["Stardew_Valley"],
    "all_games":    False,
    "dialog_class": "SmapiWizardFixed",
}

_GITHUB_API_URL = "https://api.github.com/repos/Pathoschild/SMAPI/releases/latest"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_flatpak() -> bool:
    return os.path.exists("/.flatpak-info")


def _get_downloads_dir() -> Path:
    xdg = os.environ.get("XDG_DOWNLOAD_DIR")
    if xdg:
        return Path(xdg)
    return Path.home() / "Downloads"


def _fetch_latest_smapi_asset() -> tuple[str, str]:
    """Return (version_tag, download_url) for the latest SMAPI installer zip."""
    req = urllib.request.Request(
        _GITHUB_API_URL,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "ModManager/1.0"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = _json.loads(resp.read().decode())
    tag = data.get("tag_name", "unknown")
    assets = data.get("assets", [])
    for asset in assets:
        nl = asset.get("name", "").lower()
        if nl.endswith(".zip") and "smapi" in nl and "installer" in nl and "double" not in nl:
            return tag, asset["browser_download_url"]
    for asset in assets:
        nl = asset.get("name", "").lower()
        if nl.endswith(".zip") and "smapi" in nl and "double" not in nl:
            return tag, asset["browser_download_url"]
    raise RuntimeError("No SMAPI installer zip found in the latest GitHub release.")


def _extract_zip(archive: Path, dest: Path) -> None:
    name = archive.name.lower()
    if name.endswith(".zip"):
        with zipfile.ZipFile(archive, "r") as zf:
            zf.extractall(dest)
    else:
        raise RuntimeError(f"Unsupported archive format: {archive.name}")


def _chmod_exec(path: Path) -> None:
    try:
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        pass


def _build_wrapper(script: Path, wrapper_dir: Path) -> Path:
    """Create a bash wrapper that cd's to the script's folder, runs the
    installer, then pauses so the user can read its output."""
    wrapper = wrapper_dir / "run_smapi_install.sh"
    # Escape single quotes in paths for bash single-quoted strings
    script_dir = str(script.parent).replace("'", "'\\''")
    script_name = script.name.replace("'", "'\\''")
    wrapper.write_text(
        "#!/usr/bin/env bash\n"
        f"cd '{script_dir}' || {{ echo 'Failed to cd into installer folder'; read -n 1; exit 1; }}\n"
        f"./'{script_name}'\n"
        "rc=$?\n"
        "echo\n"
        "echo '---- SMAPI installer finished (exit code '$rc') ----'\n"
        "echo 'Press any key to close this window...'\n"
        "read -n 1 -s\n"
        "exit $rc\n",
        encoding="utf-8",
    )
    _chmod_exec(wrapper)
    return wrapper


def _terminal_candidates(wrapper: str) -> list[tuple[str, list[str]]]:
    return [
        ("konsole",         ["konsole", "--hold", "-e", "bash", wrapper]),
        ("alacritty",       ["alacritty", "-e", "bash", wrapper]),
        ("gnome-terminal",  ["gnome-terminal", "--wait", "--", "bash", wrapper]),
        ("xfce4-terminal",  ["xfce4-terminal", "--hold", "-e", f"bash {wrapper}"]),
        ("kitty",           ["kitty", "--hold", "bash", wrapper]),
        ("ptyxis",          ["ptyxis", "--new-window", "--", "bash", wrapper]),
        ("xterm",           ["xterm", "-hold", "-e", "bash", wrapper]),
    ]


def _clean_env() -> dict:
    """Copy of os.environ with AppImage / bundle vars removed.

    AppImage exports LD_LIBRARY_PATH, QT_PLUGIN_PATH, PYTHONHOME etc. pointing
    into its bundle. Inherited by konsole, those make it load the wrong Qt
    libs and exit immediately. Strip them so the terminal uses host libraries.
    """
    env = os.environ.copy()
    for var in (
        "LD_LIBRARY_PATH", "LD_PRELOAD",
        "PYTHONHOME", "PYTHONPATH",
        "QT_PLUGIN_PATH", "QML2_IMPORT_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH",
        "GTK_PATH", "GIO_MODULE_DIR", "GSETTINGS_SCHEMA_DIR",
        "GDK_PIXBUF_MODULE_FILE", "GDK_PIXBUF_MODULEDIR",
        "XDG_DATA_DIRS_APPIMAGE", "PERLLIB", "GCONV_PATH",
        "APPDIR", "APPIMAGE", "ARGV0", "OWD",
    ):
        env.pop(var, None)
    env.setdefault("XDG_DATA_DIRS", "/usr/local/share:/usr/share")
    return env


def _spawn_host_prefix() -> list[str]:
    """flatpak-spawn invocation that starts on the host with a safe cwd.

    Inside a flatpak the sandbox cwd (e.g. /app/share/amethyst-mod-manager)
    doesn't exist on the host, and the portal call fails with:
        Portal call failed: Failed to start command: Failed to change to
        directory "/app/share/..." (No such file or directory)
    Passing --directory=$HOME avoids it.
    """
    home = os.environ.get("HOME") or "/tmp"
    return ["flatpak-spawn", "--host", f"--directory={home}"]


def _host_has(exe: str, env: dict) -> bool:
    """Ask the host (via flatpak-spawn) whether *exe* is on its PATH."""
    try:
        r = subprocess.run(
            _spawn_host_prefix() + ["sh", "-c", f"command -v {exe}"],
            env=env, capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0 and bool(r.stdout.strip())


def _find_terminal_cmd(wrapper: str, log_fn=None) -> tuple[list[str], dict] | None:
    """Return (argv, env) for a terminal that actually launches, or None.

    Order of preference:
      1. Host-side terminal that passes `<bin> --version` under a cleaned env
         (catches AppImage library-conflict crashes).
      2. Host terminal via flatpak-spawn --host (for flatpak builds) —
         selected by asking the host `command -v <exe>`, not by probing, because
         `<exe> --version` through flatpak-spawn can spuriously fail on some
         hosts.
      3. x-terminal-emulator / xdg-terminal-exec.
      4. Forced direct launch of a host terminal that's on PATH even if it
         failed the probe.
      5. Forced flatpak-spawn of konsole/alacritty/xterm even without host-check.
    """
    log = log_fn or (lambda m: None)
    env = _clean_env()
    have_spawn = shutil.which("flatpak-spawn") is not None

    def _probe_host(exe: str) -> bool:
        try:
            r = subprocess.run(
                [exe, "--version"], env=env, capture_output=True, text=True, timeout=5,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            log(f"SMAPI Wizard: probe {exe} failed: {exc}")
            return False
        if r.returncode != 0:
            log(f"SMAPI Wizard: probe {exe} rc={r.returncode} "
                f"stderr={r.stderr.strip()[:200]}")
            return False
        return True

    candidates = _terminal_candidates(wrapper)

    # 1. Direct host invocation with a working probe.
    for exe, argv in candidates:
        if shutil.which(exe) and _probe_host(exe):
            log(f"SMAPI Wizard: selected host terminal: {exe}")
            return argv, env

    # 2. flatpak-spawn --host, selecting by `command -v` on the host.
    if have_spawn:
        for exe, argv in candidates:
            if _host_has(exe, env):
                log(f"SMAPI Wizard: selected host terminal via flatpak-spawn: {exe}")
                return _spawn_host_prefix() + argv, env

    # 3. Generic terminal wrappers.
    for generic in ("x-terminal-emulator", "xdg-terminal-exec"):
        if shutil.which(generic):
            log(f"SMAPI Wizard: falling back to {generic}")
            return [generic, "-e", "bash", wrapper], env

    # 4. Host terminal on PATH that failed probe — force it.
    for exe, argv in candidates:
        if shutil.which(exe):
            log(f"SMAPI Wizard: no terminal passed probe, forcing {exe}")
            return argv, env

    # 5. Last-ditch for flatpak: try flatpak-spawn --host konsole anyway.
    #    If it fails we'll see stderr in the log via the caller's capture_output.
    if have_spawn:
        for exe, argv in candidates:
            log(f"SMAPI Wizard: last-ditch flatpak-spawn --host {exe}")
            return _spawn_host_prefix() + argv, env

    return None


# ============================================================================
# Wizard dialog
# ============================================================================

class SmapiWizardFixed(ctk.CTkFrame):

    def __init__(self, parent, game, log_fn=None, *, on_close=None, **_extra):
        super().__init__(parent, fg_color=BG_DEEP, corner_radius=0)
        self._on_close_cb = on_close or (lambda: None)
        self._game = game
        self._log = log_fn or (lambda msg: None)
        self._archive_path: Path | None = None

        title_bar = ctk.CTkFrame(self, fg_color=BG_HEADER, corner_radius=0, height=40)
        title_bar.pack(fill="x")
        title_bar.pack_propagate(False)
        ctk.CTkLabel(
            title_bar, text="Install SMAPI \u2014 Stardew Valley",
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

    def _clear_body(self):
        for w in self._body.winfo_children():
            w.destroy()

    # ------------------------------------------------------------------
    # Step 1 — Fetch & download latest release
    # ------------------------------------------------------------------

    def _show_step_download(self):
        self._clear_body()

        ctk.CTkLabel(
            self._body, text="Step 1: Download SMAPI",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(pady=(0, 12))

        self._dl_status = ctk.CTkLabel(
            self._body, text="Checking for the latest SMAPI release\u2026",
            font=FONT_NORMAL, text_color=TEXT_DIM, justify="center",
            wraplength=480,
        )
        self._dl_status.pack(pady=(0, 16))

        self._dl_progress = ctk.CTkProgressBar(self._body, width=400, mode="indeterminate")
        self._dl_progress.pack(pady=(0, 16))
        self._dl_progress.start()

        ctk.CTkLabel(
            self._body,
            text="A terminal window will open to run the installer.\n"
                 "Follow its prompts, then press a key to close it.",
            font=FONT_SMALL, text_color=TEXT_DIM, justify="center",
        ).pack(pady=(0, 8))

        btn_frame = ctk.CTkFrame(self._body, fg_color="transparent")
        btn_frame.pack(side="bottom", pady=(8, 0))

        self._dl_next_btn = ctk.CTkButton(
            btn_frame, text="Next \u2192", width=120, height=36,
            font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
            command=self._show_step_install, state="disabled",
        )
        self._dl_next_btn.pack(side="right", padx=(8, 0))

        ctk.CTkButton(
            btn_frame, text="Browse\u2026", width=100, height=36,
            font=FONT_BOLD,
            fg_color=BG_HEADER, hover_color="#3d3d3d", text_color=TEXT_MAIN,
            command=self._browse_archive_step1,
        ).pack(side="right")

        threading.Thread(target=self._do_fetch_and_download, daemon=True).start()

    def _do_fetch_and_download(self):
        try:
            self._set_dl_status("Fetching latest SMAPI release from GitHub\u2026")
            tag, url = _fetch_latest_smapi_asset()
            filename = url.split("/")[-1]
            dest = _get_downloads_dir() / filename
            self._set_dl_status(f"Downloading SMAPI {tag}\u2026")
            self._log(f"SMAPI Wizard: downloading {url} \u2192 {dest}")

            def _reporthook(block_num, block_size, total_size):
                if total_size > 0:
                    pct = min(block_num * block_size / total_size, 1.0)
                    try:
                        self.after(0, lambda p=pct: self._dl_progress.configure(
                            mode="determinate"
                        ) or self._dl_progress.set(p))
                    except Exception:
                        pass

            urllib.request.urlretrieve(url, dest, reporthook=_reporthook)
            self._archive_path = dest
            self._log(f"SMAPI Wizard: downloaded {filename}")
            self.after(0, lambda: self._dl_progress.stop())
            self.after(0, lambda: self._dl_progress.configure(mode="determinate"))
            self.after(0, lambda: self._dl_progress.set(1.0))
            self._set_dl_status(f"Downloaded SMAPI {tag}: {filename}", color="#6bc76b")
            self.after(0, lambda: self._dl_next_btn.configure(state="normal"))
        except Exception as exc:
            self._log(f"SMAPI Wizard: download error: {exc}")
            self.after(0, lambda: self._dl_progress.stop())
            self._set_dl_status(
                f"Download failed: {exc}\n\nUse Browse to select a manually downloaded archive.",
                color="#e06c6c",
            )
            self.after(0, lambda: self._dl_next_btn.configure(state="normal"))

    def _set_dl_status(self, text: str, color: str = TEXT_DIM):
        try:
            self.after(0, lambda: self._dl_status.configure(text=text, text_color=color))
        except Exception:
            pass

    def _browse_archive_step1(self):
        def _on_picked(path: Path | None) -> None:
            if path and path.is_file():
                self._archive_path = path
                self._set_dl_status(f"Selected: {path.name}", color="#6bc76b")
                try:
                    self.after(0, lambda: self._dl_next_btn.configure(state="normal"))
                except Exception:
                    pass

        pick_file("Select the SMAPI archive", lambda p: self.after(0, lambda: _on_picked(p)))

    # ------------------------------------------------------------------
    # Step 2 — Extract & run installer
    # ------------------------------------------------------------------

    def _show_step_install(self):
        self._clear_body()

        ctk.CTkLabel(
            self._body, text="Step 2: Install SMAPI",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(pady=(0, 12))

        self._run_status = ctk.CTkLabel(
            self._body, text="Extracting SMAPI archive\u2026",
            font=FONT_NORMAL, text_color=TEXT_DIM, justify="center",
            wraplength=480,
        )
        self._run_status.pack(pady=(0, 16))

        self._done_btn = ctk.CTkButton(
            self._body, text="Done", width=120, height=36,
            font=FONT_BOLD,
            fg_color="#2d7a2d", hover_color="#3a9e3a", text_color="white",
            command=self._finish, state="disabled",
        )
        self._done_btn.pack(side="bottom")

        threading.Thread(target=self._do_install, daemon=True).start()

    def _do_install(self):
        tmp_dir: Path | None = None
        try:
            archive = self._archive_path
            if archive is None or not archive.is_file():
                raise RuntimeError("Archive not found.")

            self._set_status("Extracting SMAPI archive\u2026")
            self._log(f"SMAPI Wizard: extracting {archive.name}")
            tmp_dir = Path(tempfile.mkdtemp(prefix="smapi_install_"))
            _extract_zip(archive, tmp_dir)

            script: Path | None = None
            for candidate in tmp_dir.rglob("install on Linux.sh"):
                script = candidate
                break
            if script is None:
                raise RuntimeError('Could not find "install on Linux.sh" inside the archive.')

            _chmod_exec(script)
            installer_bin = script.parent / "internal" / "linux" / "SMAPI.Installer"
            if installer_bin.is_file():
                _chmod_exec(installer_bin)
            # Also mark any other .sh / binary helpers executable, just in case.
            for p in script.parent.rglob("*"):
                if p.is_file() and (p.suffix == ".sh" or "Installer" in p.name):
                    _chmod_exec(p)

            wrapper = _build_wrapper(script, tmp_dir)

            self._set_status(
                "Launching the SMAPI installer in a terminal.\n\n"
                "Follow the on-screen prompts, then press a key to close the terminal\n"
                "and click Done here.",
                color=TEXT_MAIN,
            )
            self._log("SMAPI Wizard: launching SMAPI installer in terminal")

            result = _find_terminal_cmd(str(wrapper), log_fn=self._log)
            if result is None:
                raise RuntimeError(
                    "No terminal emulator found (tried konsole, alacritty, gnome-terminal, "
                    "xfce4-terminal, kitty, ptyxis, xterm). Please run the installer manually:\n"
                    f"  {wrapper}"
                )
            terminal_cmd, term_env = result

            self._log(f"SMAPI Wizard: terminal cmd: {' '.join(terminal_cmd)}")
            proc = subprocess.run(
                terminal_cmd,
                cwd=str(script.parent),
                env=term_env,
                capture_output=True,
                text=True,
            )
            if proc.returncode != 0:
                self._log(f"SMAPI Wizard: terminal exited with code {proc.returncode}")
                stderr = (proc.stderr or "").strip()
                if stderr:
                    self._log(f"SMAPI Wizard: terminal stderr: {stderr[:500]}")

            self._set_status(
                "SMAPI installer finished.\n\n"
                "If the installer completed successfully, SMAPI is now installed.\n"
                "Click Done to close.",
                color="#6bc76b",
            )
            self._log("SMAPI Wizard: SMAPI installer completed.")

            try:
                archive.unlink()
                self._log(f"SMAPI Wizard: deleted {archive.name} from Downloads.")
            except OSError as exc:
                self._log(f"SMAPI Wizard: could not delete archive: {exc}")

        except Exception as exc:
            self._set_status(f"Error: {exc}", color="#e06c6c")
            self._log(f"SMAPI Wizard error: {exc}")
        finally:
            if tmp_dir is not None:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            self._enable_done()

    # ------------------------------------------------------------------
    # Finish / helpers
    # ------------------------------------------------------------------

    def _finish(self):
        self._log("SMAPI Wizard: installation wizard finished.")
        self._on_close_cb()

    def _set_status(self, text: str, color: str = TEXT_DIM):
        try:
            self.after(0, lambda: self._run_status.configure(text=text, text_color=color))
        except Exception:
            pass

    def _enable_done(self):
        try:
            self.after(0, lambda: self._done_btn.configure(state="normal"))
        except Exception:
            pass
