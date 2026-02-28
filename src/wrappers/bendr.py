"""
bendr.py
Linux wrapper for BENDr — runs the Windows BENDr normal-map pipeline on Linux
via Wine/Proton.

All steps (BSA extract, loose copy, exclusions, filter, parallax prep,
alpha handling, BENDing, BC7, output QC) run through the original BENDr
.exe tools under Wine. Paths and work directory are provided by the mod
manager; no registry or PowerShell dialogs.

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

from Utils.steam_finder import find_any_installed_proton
from wrappers.vramr import _ensure_compressonator, _optimise_one_texture


# ── Wine helpers ───────────────────────────────────────────────────────────

def _linux_to_wine(path: str | Path) -> str:
    r"""Convert a Linux absolute path to a Wine Z:\ drive path."""
    return "Z:" + str(path).replace("/", "\\")


def _find_wine() -> str:
    """Locate a wine64 binary from a Proton installation."""
    proton_script = find_any_installed_proton()
    if proton_script is None:
        raise RuntimeError("No Proton/Wine installation found. Install Proton via Steam.")
    wine = proton_script.parent / "files" / "bin" / "wine64"
    if not wine.is_file():
        raise RuntimeError(f"wine64 not found at expected path: {wine}")
    return str(wine)


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

    We patch system.reg directly — the ACP and OEMCP values under
    HKLM\\System\\CurrentControlSet\\Control\\Nls\\CodePage — so that the
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


def _run_native_bc7(
    output_dir: Path,
    comp_cli: Path,
    log_fn: Callable[[str], None],
    progress_fn: Callable[[int], None],
    progress_start: int = 82,
    progress_end: int = 95,
) -> None:
    """Native replacement for BC7.exe — compresses all DDS files to BC7.

    BENDr's BC7.exe just recompresses every DDS in the output directory to
    BC7 format.  No resizing is needed; PrepParallax already handled that.
    """
    from concurrent.futures import ProcessPoolExecutor, as_completed

    dds_files = list(output_dir.rglob("*.dds"))
    total = len(dds_files)
    if total == 0:
        log_fn("  No DDS files to compress.")
        return

    log_fn(f"  Compressing {total} DDS files to BC7 (native)...")

    tasks = [
        (str(f), 0, 0, "BC7", str(comp_cli), False)
        for f in dds_files
    ]

    workers = min(2, total)
    done = 0
    errors = 0

    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_optimise_one_texture, task): task for task in tasks}
        for future in as_completed(futures):
            done += 1
            result_msg = future.result()
            if result_msg.startswith("FAIL") or result_msg.startswith("ERROR"):
                errors += 1
            log_fn(f"  [{done}/{total}] {result_msg}")
            pct = progress_start + int((progress_end - progress_start) * done / total)
            progress_fn(pct)

    log_fn(f"  BC7 compression complete: {done - errors}/{total} succeeded, {errors} failed.")


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
    wine = _find_wine()
    prefix = os.path.expanduser("~/bendr_wine_prefix")
    _log(f"  Wine: {wine}")
    _ensure_utf8_prefix(wine, prefix)
    comp_cli = _ensure_compressonator(_log)
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
        f.write(f"Platform       : Linux (all steps via Wine)\n\n")

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

    # ── Step 1: BSA extraction (normals + parallax only)
    _file_log("Extracting BSA Archives...")
    _wine_run(wine, prefix, _tool("ExtractBSA.exe"), [
        "--source", w_game + "\\*.bsa",
        "--dest", w_output,
        "--logfile", w_logfiles,
        "--filter", "*_n.dds", "*_p.dds",
    ], log_fn=_log, label="Step 1/8: BSA Extraction")
    _progress(20)

    # ── Step 2: Loose file copy (normals + parallax only)
    _file_log("Copying Loose Normal/Parallax Textures...")
    _wine_run(wine, prefix, _tool("LooseCopy.exe"), [
        "--source", w_game + "\\textures",
        "--dest", w_output + "\\textures",
        "--logfile", w_logfiles,
        "--filter", "*_n.dds", "*_p.dds",
    ], log_fn=_log, label="Step 2/8: Loose File Copy")
    _progress(30)

    # ── Step 3: Exclusions
    _file_log("Processing Exclusions...")
    _wine_run(wine, prefix, _tool("Exclusions.exe"), [
        "--Exclude", w_exclusions,
        "--Dest", w_output,
        "--Logfile", w_logfiles,
    ], log_fn=_log, label="Step 3/8: Applying Exclusions")
    _progress(38)

    # ── Step 4: Filter pairs (keeps only matched normal+parallax pairs)
    _file_log("Filtering Pairs...")
    _wine_run(wine, prefix, _tool("BENDrFilter.exe"), [
        "--source", w_output,
        "--logfiles", w_logfiles,
    ], log_fn=_log, label="Step 4/8: Filtering Pairs")
    _progress(45)

    # ── Step 5: Prepare parallax height maps (downscale to 1024)
    _file_log("Preparing Parallax Height Maps...")
    _wine_run(wine, prefix, _tool("PrepParallax.exe"), [
        "--downscale", "1024",
        "--source", w_output,
        "--logfiles", w_logfiles,
    ], log_fn=_log, label="Step 5/8: Prep Parallax")
    _progress(54)

    # ── Step 6a: Build alpha-normal SQL database (remove alpha channel)
    _file_log("Building Normal Map Alpha DB...")
    _wine_run(wine, prefix, _tool("AlphaNormalSQL.exe"), [
        "--remove",
        "--downscale", "1024",
        "--source", w_output,
        "--logfile", w_logfiles,
    ], log_fn=_log, label="Step 6/8: Alpha Normal SQL (remove)")
    _progress(62)

    # ── Step 7: BENDr — bend the normal maps
    _file_log("BENDing Normal Maps...")
    _wine_run(wine, prefix, _tool("BENDr.exe"), [
        "--source", w_output,
        "--logfile", w_logfiles,
    ], log_fn=_log, label="Step 7/8: BENDr")
    _progress(75)

    # ── Step 6b: Restore alpha transparency
    _file_log("Recovering Alpha Transparency...")
    _wine_run(wine, prefix, _tool("AlphaNormalSQL.exe"), [
        "--restore",
        "--source", w_output,
        "--logfile", w_logfiles,
    ], log_fn=_log, label="Step 7b/8: Alpha Normal SQL (restore)")
    _progress(82)

    # ── Step 8: BC7 compression (native — Pillow + CompressonatorCLI)
    _file_log("Finalising DDS Library (BC7, native)...")
    _log("── Step 8/8: BC7 Compression (native) ──")
    _run_native_bc7(
        output_dir=work_output,
        comp_cli=comp_cli,
        log_fn=_log,
        progress_fn=_progress,
        progress_start=82,
        progress_end=95,
    )
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

    # Remove the Wine prefix — it's only needed during the run
    shutil.rmtree(prefix, ignore_errors=True)

    _file_log("BENDr Complete")
    _log("BENDr: Complete! Output is ready as a mod.")
    _progress(100)
