"""
parallaxr.py
Linux wrapper for ParallaxR — runs the Windows ParallaxR parallax-texture
pipeline on Linux via Wine/Proton.

Steps: BSA extract → loose copy → exclusions → filter pairs → height maps →
output QC.  All steps use the original ParallaxR .exe tools under Wine.
Paths and work directory are provided by the mod manager; no registry or
PowerShell dialogs.

Public entry point:  run_parallaxr(...)
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Callable

from Utils.steam_finder import find_any_installed_proton
from wrappers.bendr import _linux_to_wine, _ensure_utf8_prefix

import re
import pty
import select
import subprocess

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\x1b\[[?][0-9;]*[A-Za-z]|\x1b[A-Za-z]|\r")


def _find_wine() -> tuple[str, Path]:
    """Locate a wine64 binary and the Proton root from any installed Proton.

    Returns (wine64_path_str, proton_files_dir).
    """
    proton_script = find_any_installed_proton()
    if proton_script is None:
        raise RuntimeError("No Proton/Wine installation found. Install Proton via Steam.")
    files_dir = proton_script.parent / "files"
    wine = files_dir / "bin" / "wine64"
    if not wine.is_file():
        raise RuntimeError(f"wine64 not found at expected path: {wine}")
    return str(wine), files_dir


def _build_wine_overlay(proton_files_dir: Path, patched_ucrtbase: Path) -> Path:
    """Create a temporary Wine installation overlay with patched ucrtbase.dll.

    Wine's built-in ucrtbase.dll is missing some UCRT functions (e.g. crealf).
    We ship a patched copy that implements them.  Wine resolves its DLL search
    path relative to the wine64 binary's real location, so we must *copy*
    (not symlink) wine64, wine64-preloader, wineserver, and ntdll.so into a
    temp directory, then symlink everything else from the real Proton install.
    Our patched ucrtbase.dll goes into x86_64-windows/ in this overlay so Wine
    loads it instead of its stub.

    Returns the overlay root directory (caller must clean up).
    """
    overlay = Path(os.path.expanduser("~/parallaxr_wine_overlay"))
    if overlay.exists():
        shutil.rmtree(overlay)

    bin_dir = overlay / "bin"
    wine_win = overlay / "lib" / "wine" / "x86_64-windows"
    wine_unix = overlay / "lib" / "wine" / "x86_64-unix"
    bin_dir.mkdir(parents=True)
    wine_win.mkdir(parents=True)
    wine_unix.mkdir(parents=True)

    proton_bin = proton_files_dir / "bin"
    proton_wine_win = proton_files_dir / "lib" / "wine" / "x86_64-windows"
    proton_wine_unix = proton_files_dir / "lib" / "wine" / "x86_64-unix"

    # Copy binaries (must be real files so Wine resolves paths to our overlay)
    for name in ("wine64", "wine64-preloader", "wineserver"):
        src = proton_bin / name
        if src.is_file():
            shutil.copy2(str(src), str(bin_dir / name))
            (bin_dir / name).chmod(0o755)

    # Copy ntdll.so (must be a real file — same reason as above)
    ntdll_src = proton_wine_unix / "ntdll.so"
    if ntdll_src.is_file():
        shutil.copy2(str(ntdll_src), str(wine_unix / "ntdll.so"))

    # Symlink all other x86_64-unix .so files
    for f in proton_wine_unix.iterdir():
        if f.name == "ntdll.so":
            continue
        dst = wine_unix / f.name
        if not dst.exists():
            dst.symlink_to(f)

    # Symlink all x86_64-windows DLLs except ucrtbase.dll
    for f in proton_wine_win.iterdir():
        if f.name == "ucrtbase.dll":
            continue
        dst = wine_win / f.name
        if not dst.exists():
            dst.symlink_to(f)

    # Place our patched ucrtbase.dll
    shutil.copy2(str(patched_ucrtbase), str(wine_win / "ucrtbase.dll"))

    # Symlink remaining arch dirs (i386-windows, i386-unix, etc.)
    proton_wine = proton_files_dir / "lib" / "wine"
    for d in proton_wine.iterdir():
        if d.is_dir() and d.name not in ("x86_64-windows", "x86_64-unix"):
            dst = overlay / "lib" / "wine" / d.name
            if not dst.exists():
                dst.symlink_to(d)

    # Symlink other lib subdirectories (vkd3d, etc.)
    for item in (proton_files_dir / "lib").iterdir():
        if item.name == "wine":
            continue
        dst = overlay / "lib" / item.name
        if not dst.exists():
            dst.symlink_to(item)

    # Symlink lib64 if present
    lib64 = proton_files_dir / "lib64"
    if lib64.is_dir():
        (overlay / "lib64").symlink_to(lib64)

    # Symlink share (NLS files, default_pfx, etc.)
    share = proton_files_dir / "share"
    if share.is_dir():
        (overlay / "share").symlink_to(share)

    return overlay


def _wine_run_overlay(
    wine: str,
    prefix: str,
    exe: str,
    args: list[str],
    log_fn: Callable[[str], None],
    label: str = "",
) -> int:
    """Run a Windows .exe through Wine and stream output to log_fn.

    Identical to bendr._wine_run but uses the overlay wine binary path.
    """
    env = os.environ.copy()
    env["WINEPREFIX"] = prefix
    env["WINEDEBUG"] = "-all"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

    # Set WINEDLLPATH so Wine can find vkd3d DLLs alongside the wine/ builtins.
    # The overlay root mirrors Proton's layout: lib/wine/ and lib/vkd3d/.
    overlay_lib = str(Path(wine).parent.parent / "lib")
    env["WINEDLLPATH"] = f"{overlay_lib}/vkd3d:{overlay_lib}/wine"

    display = label or Path(exe).name
    log_fn(f"── {display} ──")

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


def _timestamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ── Main pipeline ──────────────────────────────────────────────────────────

def run_parallaxr(
    bat_dir: Path,
    game_data_dir: Path,
    output_dir: Path,
    log_fn: Callable[[str], None] | None = None,
    progress_fn: Callable[[int], None] | None = None,
) -> None:
    """
    Run the ParallaxR texture pipeline.

    Parameters
    ----------
    bat_dir : Path
        Directory containing ParallaxR.bat and the tools/ subfolder.
    game_data_dir : Path
        The game's Data directory (where .bsa files and textures/ live).
    output_dir : Path
        Where ParallaxR should write its output (becomes a mod in the staging area).
    log_fn : callable
        Receives log lines; defaults to print().
    progress_fn : callable
        Receives integer 0-100 progress updates.
    """
    _log = log_fn or print
    _progress = progress_fn or (lambda _: None)

    tools_dir = bat_dir / "tools"
    if not tools_dir.is_dir():
        raise FileNotFoundError(f"ParallaxR tools/ directory not found: {tools_dir}")

    def _tool(name: str) -> str:
        path = tools_dir / name
        if not path.is_file():
            raise FileNotFoundError(f"Required ParallaxR tool not found: {path}")
        return str(path)

    if not game_data_dir.is_dir():
        raise FileNotFoundError(f"Game Data directory not found: {game_data_dir}")

    # Discover Wine
    _log("ParallaxR: Locating Proton/Wine...")
    _, proton_files_dir = _find_wine()
    prefix = os.path.expanduser("~/parallaxr_wine_prefix")

    # Build Wine overlay with patched ucrtbase.dll (shipped alongside this script)
    patched_ucrtbase = Path(__file__).with_name("ucrtbase.dll")
    if not patched_ucrtbase.is_file():
        raise FileNotFoundError(
            f"Patched ucrtbase.dll not found at {patched_ucrtbase}. "
            "This file is required for HeightMap.exe to run under Wine."
        )
    _log("ParallaxR: Building Wine overlay...")
    overlay = _build_wine_overlay(proton_files_dir, patched_ucrtbase)
    wine = str(overlay / "bin" / "wine64")
    _log(f"  Wine: {wine}")

    # Set WINEDLLPATH before prefix init so the wineserver starts with it.
    # wineboot spawns a persistent wineserver whose DLL state is inherited
    # by all later Wine processes in the same prefix.
    overlay_lib = str(overlay / "lib")
    os.environ["WINEDLLPATH"] = f"{overlay_lib}/vkd3d:{overlay_lib}/wine"

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
    log_file = work_logfiles / "ParallaxR.log"
    with open(log_file, "w") as f:
        f.write(f"ParallaxR Started  : {_timestamp()}\n")
        f.write(f"GameDir            : {game_data_dir}\n")
        f.write(f"Platform           : Linux (all steps via Wine)\n\n")

    def _file_log(msg: str):
        with open(log_file, "a") as f:
            f.write(f"{_timestamp()} {msg}\n")

    _log(f"ParallaxR: Game Data = {game_data_dir}")
    _log(f"ParallaxR: Output    = {output_dir}")

    # Wine path conversions
    w_game       = _linux_to_wine(game_data_dir)
    w_output     = _linux_to_wine(work_output)
    w_logfiles   = _linux_to_wine(work_logfiles)
    w_exclusions = _linux_to_wine(tools_dir / "Exclusions.mod")

    # ── Step 1: BSA extraction (normals + parallax only)
    _file_log("Extracting BSA Archives...")
    _wine_run_overlay(wine, prefix, _tool("ExtractBSA.exe"), [
        "--source", w_game + "\\*.bsa",
        "--dest", w_output,
        "--logfile", w_logfiles,
        "--filter", "*_n.dds", "*_p.dds",
    ], log_fn=_log, label="Step 1/6: BSA Extraction")
    _progress(20)

    # ── Step 2: Loose file copy (normals + parallax only)
    _file_log("Copying Loose Normal/Parallax Textures...")
    _wine_run_overlay(wine, prefix, _tool("LooseCopy.exe"), [
        "--source", w_game + "\\textures",
        "--dest", w_output + "\\textures",
        "--logfile", w_logfiles,
        "--filter", "*_n.dds", "*_p.dds",
    ], log_fn=_log, label="Step 2/6: Loose File Copy")
    _progress(32)

    # ── Step 3: Exclusions
    _file_log("Processing Exclusions...")
    _wine_run_overlay(wine, prefix, _tool("Exclusions.exe"), [
        "--Exclude", w_exclusions,
        "--Dest", w_output,
        "--Logfile", w_logfiles,
    ], log_fn=_log, label="Step 3/6: Applying Exclusions")
    _progress(42)

    # ── Step 4: Filter pairs (keeps only matched normal+parallax pairs)
    _file_log("Filtering Pairs...")
    _wine_run_overlay(wine, prefix, _tool("ParallaxRFilter.exe"), [
        "--source", w_output,
        "--logfiles", w_logfiles,
    ], log_fn=_log, label="Step 4/6: Filtering Pairs")
    _progress(55)

    # ── Step 5: Height map generation
    _file_log("Preparing Parallax Height Maps...")
    _wine_run_overlay(wine, prefix, _tool("HeightMap.exe"), [
        "--Source", w_output,
        "--Logfile", w_logfiles,
    ], log_fn=_log, label="Step 5/6: Height Maps")
    _progress(75)

    # ── Step 6: Output QC
    _file_log("Running Output QC...")
    _wine_run_overlay(wine, prefix, _tool("OutputQC.exe"), [
        "--source", w_output,
        "--logfile", w_logfiles,
    ], log_fn=_log, label="Step 6/6: Output QC")
    _progress(88)

    # ── Tidy up
    _file_log("Cleaning up...")
    _log("ParallaxR: Cleaning up...")

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

    # Remove the Wine prefix and overlay — only needed during the run
    shutil.rmtree(prefix, ignore_errors=True)
    shutil.rmtree(str(overlay), ignore_errors=True)

    # Clean up env override
    os.environ.pop("WINEDLLPATH", None)

    _file_log("ParallaxR Complete")
    _log("ParallaxR: Complete! Output is ready as a mod.")
    _progress(100)
