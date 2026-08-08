"""
bendr.py
Linux wrapper for BENDr - runs the Windows BENDr normal-map pipeline on Linux
via Wine/Proton.

All steps (BSA extract, loose copy, exclusions, filter, BENDing +
texconv/BC7 compression) run through the original BENDr .exe tools under
Wine. Paths and work directory are provided by the mod manager; no
registry or PowerShell dialogs.

Targets BENDr v3.0331+ (March 2026), whose simplified workflow folds the
old PrepParallax / AlphaNormalSQL / separate BC7 steps into BENDr.exe
itself (BENDr.exe takes a --tool texconv.exe argument and compresses).

Public entry point:  run_bendr(...)
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
import pty
import select
from pathlib import Path
from typing import Callable

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\x1b\[[?][0-9;]*[A-Za-z]|\x1b[A-Za-z]|\r")

from Utils.config_paths import get_wine_prefixes_dir
from Utils.steam_finder import find_wine


# ── Wine helpers ───────────────────────────────────────────────────────────

def _linux_to_wine(path: str | Path) -> str:
    r"""Convert a Linux absolute path to a Wine Z:\ drive path."""
    return "Z:" + str(path).replace("/", "\\")


def _wine_run(
    wine: str,
    prefix: str,
    exe: str,
    args: list[str],
    log_fn: Callable[[str], None],
    label: str = "",
) -> int:
    """Run a Windows .exe through Wine and stream output to log_fn."""
    env = os.environ.copy()
    env["WINEPREFIX"] = prefix
    env["WINEDEBUG"] = "-all"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

    display = label or Path(exe).name
    log_fn(f"── {display} ──")

    # Use a PTY so the frozen Python exes see a real console, not a pipe.
    # When stdout is a pipe, PyInstaller's frozen Python initialises
    # sys.stdout with Wine's GetACP() fallback (cp1252), causing alive_progress
    # to crash on its Unicode spinner chars.  A PTY makes isatty() return True,
    # which causes Python to use the locale encoding (UTF-8) instead.
    master_fd, slave_fd = pty.openpty()
    try:
        proc = subprocess.Popen(
            [wine, exe] + args,
            stdout=slave_fd,
            stderr=slave_fd,
            env=env,
        )
        os.close(slave_fd)
        slave_fd = -1

        buf = b""
        while True:
            try:
                r, _, _ = select.select([master_fd], [], [], 0.1)
            except (ValueError, OSError):
                break
            if r:
                try:
                    chunk = os.read(master_fd, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line_bytes, buf = buf.split(b"\n", 1)
                    line = line_bytes.decode("utf-8", errors="replace")
                    stripped = _ANSI_RE.sub("", line).rstrip("\r")
                    if stripped.strip():
                        log_fn(f"  {stripped}")
            elif proc.poll() is not None:
                break

        # Flush remaining buffer
        try:
            while True:
                chunk = os.read(master_fd, 4096)
                if not chunk:
                    break
                buf += chunk
        except OSError:
            pass
        if buf:
            line = buf.decode("utf-8", errors="replace")
            stripped = _ANSI_RE.sub("", line).rstrip("\r\n")
            if stripped.strip():
                log_fn(f"  {stripped}")

        proc.wait()
    finally:
        try:
            os.close(master_fd)
        except OSError:
            pass
        if slave_fd != -1:
            try:
                os.close(slave_fd)
            except OSError:
                pass

    if proc.returncode != 0:
        log_fn(f"  WARNING: {display} exited with code {proc.returncode}")
    return proc.returncode



def _ensure_utf8_prefix(wine: str, prefix: str) -> None:
    """Ensure the Wine prefix exists and has its NLS code pages set to UTF-8.

    The BENDr tools are PyInstaller-frozen Python exes using alive_progress.
    alive_progress writes Unicode spinner chars to sys.stdout, whose encoding
    is determined at Python startup from the Windows system code page.  Wine
    defaults to cp1252, causing crashes.

    We patch system.reg directly - the ACP and OEMCP values under
    HKLM\\System\\CurrentControlSet\\Control\\Nls\\CodePage - so that the
    code page is 65001 (UTF-8) before any process reads it.
    """
    prefix_path = Path(prefix)

    # Create the prefix if it doesn't exist (wineboot initialises it)
    if not (prefix_path / "system.reg").is_file():
        env = os.environ.copy()
        env["WINEPREFIX"] = prefix
        env["WINEDEBUG"] = "-all"
        subprocess.run(
            [wine, "wineboot", "--init"],
            env=env, capture_output=True, timeout=60,
        )

    reg_file = prefix_path / "system.reg"
    if not reg_file.is_file():
        return  # can't proceed without the file

    content = reg_file.read_text(errors="replace")

    # Check if already patched
    if '"ACP"="65001"' in content:
        return

    # Find the CodePage key section and replace the ACP/OEMCP values
    import re as _re
    content = _re.sub(
        r'"ACP"="[^"]*"',
        '"ACP"="65001"',
        content,
    )
    content = _re.sub(
        r'"OEMCP"="[^"]*"',
        '"OEMCP"="65001"',
        content,
    )
    reg_file.write_text(content)


def _timestamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ── Main pipeline ──────────────────────────────────────────────────────────

def run_bendr(
    bat_dir: Path,
    game_data_dir: Path,
    output_dir: Path,
    log_fn: Callable[[str], None] | None = None,
    progress_fn: Callable[[int], None] | None = None,
) -> None:
    """
    Run the BENDr normal-map pipeline.

    Parameters
    ----------
    bat_dir : Path
        Directory containing BENDr.bat and the tools/ subfolder.
    game_data_dir : Path
        The game's Data directory (where .bsa files and textures/ live).
    output_dir : Path
        Where BENDr should write its output (becomes a mod in the staging area).
    log_fn : callable
        Receives log lines; defaults to print().
    progress_fn : callable
        Receives integer 0-100 progress updates.
    """
    _log = log_fn or print
    _progress = progress_fn or (lambda _: None)

    tools_dir = bat_dir / "tools"
    if not tools_dir.is_dir():
        raise FileNotFoundError(f"BENDr tools/ directory not found: {tools_dir}")

    def _tool(name: str) -> str:
        path = tools_dir / name
        if not path.is_file():
            raise FileNotFoundError(f"Required BENDr tool not found: {path}")
        return str(path)

    if not game_data_dir.is_dir():
        raise FileNotFoundError(f"Game Data directory not found: {game_data_dir}")

    # Discover Wine
    _log("BENDr: Locating Proton/Wine...")
    wine, _ = find_wine()
    prefix = str(get_wine_prefixes_dir() / "bendr")
    Path(prefix).mkdir(parents=True, exist_ok=True)
    _log(f"  Wine: {wine}")
    _ensure_utf8_prefix(wine, prefix)
    _progress(5)

    # Prepare output directory
    if output_dir.exists():
        shutil.rmtree(output_dir)
    work_output = output_dir / "Output"
    work_logfiles = output_dir / "Logfiles"
    work_output.mkdir(parents=True, exist_ok=True)
    work_logfiles.mkdir(parents=True, exist_ok=True)

    # Write log header
    log_file = work_logfiles / "BENDr.log"
    with open(log_file, "w") as f:
        f.write(f"BENDr Started  : {_timestamp()}\n")
        f.write(f"GameDir        : {game_data_dir}\n")
        f.write("Platform       : Linux (all steps via Wine)\n\n")

    def _file_log(msg: str):
        with open(log_file, "a") as f:
            f.write(f"{_timestamp()} {msg}\n")

    _log(f"BENDr: Game Data = {game_data_dir}")
    _log(f"BENDr: Output    = {output_dir}")

    # Wine path conversions
    w_game      = _linux_to_wine(game_data_dir)
    w_output    = _linux_to_wine(work_output)
    w_logfiles  = _linux_to_wine(work_logfiles)
    w_exclusions = _linux_to_wine(tools_dir / "Exclusions.mod")
    w_texconv    = _linux_to_wine(tools_dir / "texconv.exe")

    # BENDr v3.0331+ simplified workflow (see BENDr.bat). PrepParallax,
    # AlphaNormalSQL and the separate BC7 pass are gone - BENDr.exe now bends
    # the normals and compresses via the supplied --tool texconv.exe.

    # ── Step 1: BSA extraction (normals + parallax only)
    _file_log("Extracting BSA Archives...")
    _wine_run(wine, prefix, _tool("ExtractBSA.exe"), [
        "--source", w_game + "\\*.bsa",
        "--dest", w_output,
        "--logfile", w_logfiles,
        "--filter", "*_n.dds", "*_p.dds",
    ], log_fn=_log, label="Step 1/5: BSA Extraction")
    _progress(25)

    # ── Step 2: Loose file copy (normals + parallax only)
    _file_log("Copying Loose Normal/Parallax Textures...")
    _wine_run(wine, prefix, _tool("LooseCopy.exe"), [
        "--source", w_game + "\\textures",
        "--dest", w_output + "\\textures",
        "--logfile", w_logfiles,
        "--filter", "*_n.dds", "*_p.dds",
    ], log_fn=_log, label="Step 2/5: Loose File Copy")
    _progress(38)

    # ── Step 3: Exclusions
    _file_log("Processing Exclusions...")
    _wine_run(wine, prefix, _tool("Exclusions.exe"), [
        "--Exclude", w_exclusions,
        "--Dest", w_output,
        "--Logfile", w_logfiles,
    ], log_fn=_log, label="Step 3/5: Applying Exclusions")
    _progress(45)

    # ── Step 4: Filter pairs (keeps only matched normal+parallax pairs)
    _file_log("Filtering Pairs...")
    _wine_run(wine, prefix, _tool("BENDrFilter.exe"), [
        "--source", w_output,
        "--logfiles", w_logfiles,
    ], log_fn=_log, label="Step 4/5: Filtering Pairs")
    _progress(55)

    # ── Step 5: BENDr - bend the normal maps and compress via texconv
    _file_log("BENDing Normal Maps...")
    _wine_run(wine, prefix, _tool("BENDr.exe"), [
        "--source", w_output,
        "--logfile", w_logfiles,
        "--tool", w_texconv,
    ], log_fn=_log, label="Step 5/5: BENDr (BEND + BC7)")
    _progress(95)

    # ── Tidy up
    _file_log("Cleaning up...")
    _log("BENDr: Cleaning up...")

    # Remove empty subdirectories inside Output
    for root, dirs, _files in os.walk(str(work_output), topdown=False):
        for d in dirs:
            dp = os.path.join(root, d)
            try:
                os.rmdir(dp)
            except OSError:
                pass

    # Clean up loose .png and .db files left by tools
    for ext in ("*.png", "*.db"):
        for f in work_output.rglob(ext):
            try:
                f.unlink()
            except OSError:
                pass

    # Flatten: move Output/* up into the mod folder root
    for child in list(work_output.iterdir()):
        dest = output_dir / child.name
        if dest.exists():
            if dest.is_dir():
                shutil.rmtree(dest)
            else:
                dest.unlink()
        child.rename(dest)

    if work_output.exists():
        shutil.rmtree(work_output, ignore_errors=True)

    _file_log("BENDr Complete")
    _log("BENDr: Complete! Output is ready as a mod.")
    _progress(100)
