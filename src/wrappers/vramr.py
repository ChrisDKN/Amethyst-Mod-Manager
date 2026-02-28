"""
vramr.py
Linux wrapper for VRAMr — runs the Windows VRAMr texture optimisation pipeline
on Linux via Proton/Wine.

Steps 1–5 (BSA extraction, loose copy, exclusions, filter, extract) use the
original VRAMr .exe tools through Wine.

Step 6 (Optimise) runs natively: Pillow resizes textures, then AMD
Compressonator CLI compresses them to BC7.  This is ~10x faster than running
texconv.exe through Wine/DXVK on the Steam Deck.

Public entry point:  run_vramr(...)
"""

from __future__ import annotations

import os
import platform
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Callable

from Utils.config_paths import get_config_dir
from Utils.steam_finder import find_any_installed_proton


# ── Presets ────────────────────────────────────────────────────────────────

PRESETS: dict[str, dict] = {
    "hq":          {"diffuse": 2048, "normal": 2048, "parallax": 1024, "material": 1024, "label": "High Quality"},
    "quality":     {"diffuse": 2048, "normal": 1024, "parallax": 1024, "material": 1024, "label": "Quality"},
    "optimum":     {"diffuse": 2048, "normal": 1024, "parallax": 512,  "material": 512,  "label": "Optimum"},
    "performance": {"diffuse": 2048, "normal": 512,  "parallax": 512,  "material": 512,  "label": "Performance"},
    "vanilla":     {"diffuse": 512,  "normal": 512,  "parallax": 512,  "material": 512,  "label": "Vanilla"},
}

# Target format per texture type — BC7 for quality, BC1 for simple cases
_FORMAT_MAP = {
    "diffuse":  "BC7",
    "normal":   "BC7",
    "parallax": "BC7",
    "material": "BC7",
}

_COMPRESSONATOR_URL = (
    "https://github.com/GPUOpen-Tools/compressonator/releases/download/"
    "V4.5.52/compressonatorcli-4.5.52-Linux.tar.gz"
)
_COMPRESSONATOR_DIR_NAME = "compressonatorcli-4.5.52-Linux"


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

    proc = subprocess.Popen(
        [wine, exe] + args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
        text=True,
        errors="replace",
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        stripped = line.rstrip("\n\r")
        if not stripped:
            continue
    proc.wait()

    if proc.returncode != 0:
        log_fn(f"  WARNING: {display} exited with code {proc.returncode}")
    return proc.returncode


def _timestamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ── Compressonator CLI management ─────────────────────────────────────────

def _get_compressonator_dir() -> Path:
    """Return the directory where CompressonatorCLI is stored."""
    return get_config_dir() / "tools" / "compressonator"


def _ensure_compressonator(log_fn: Callable[[str], None]) -> Path:
    """Download CompressonatorCLI if not present. Returns path to the *real*
    binary (compressonatorcli-bin), not the shell wrapper."""
    base = _get_compressonator_dir()
    cli_dir = base / _COMPRESSONATOR_DIR_NAME
    cli_bin = cli_dir / "compressonatorcli-bin"

    if cli_bin.is_file():
        return cli_bin

    log_fn("Downloading AMD CompressonatorCLI (one-time, ~19 MB)...")
    base.mkdir(parents=True, exist_ok=True)

    import urllib.request
    tarball = base / "compressonatorcli.tar.gz"
    try:
        urllib.request.urlretrieve(_COMPRESSONATOR_URL, tarball)
    except Exception as exc:
        raise RuntimeError(
            f"Failed to download CompressonatorCLI: {exc}\n"
            f"You can manually download from:\n  {_COMPRESSONATOR_URL}\n"
            f"and extract to:\n  {base}/"
        ) from exc

    with tarfile.open(tarball, "r:gz") as tf:
        tf.extractall(base)
    tarball.unlink(missing_ok=True)

    if not cli_bin.is_file():
        raise FileNotFoundError(
            f"CompressonatorCLI binary not found after extraction: {cli_bin}"
        )

    cli_bin.chmod(0o755)
    log_fn(f"  CompressonatorCLI installed: {cli_bin}")
    return cli_bin


# ── Native texture optimisation ───────────────────────────────────────────

def _compute_target_size(
    width: int, height: int, target: int,
) -> tuple[int, int] | None:
    """Compute the resized dimensions, maintaining aspect ratio.

    Returns (new_w, new_h) if a resize is needed, or None if already at or
    below the target.
    """
    larger = max(width, height)
    if larger <= target:
        return None
    scale = target / larger
    new_w = max(4, int(width * scale) & ~3)   # BC7 blocks require multiples of 4
    new_h = max(4, int(height * scale) & ~3)
    return (new_w, new_h)


def _optimise_one_texture(args: tuple) -> str:
    """Process a single texture file. Runs in a worker process.

    Returns a human-readable result string.
    """
    dds_path_str, target_w, target_h, target_fmt, comp_cli_str, needs_resize = args
    dds_path = Path(dds_path_str)
    comp_cli = Path(comp_cli_str)

    if not dds_path.is_file():
        return f"SKIP (missing): {dds_path.name}"

    try:
        from PIL import Image

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            # Resize with Pillow if needed
            if needs_resize:
                img = Image.open(dds_path)
                resized = img.resize((target_w, target_h), Image.LANCZOS)
                src_for_compress = tmp_path / "resized.png"
                resized.save(src_for_compress)
            else:
                src_for_compress = dds_path

            # Compress with Compressonator
            out_file = tmp_path / "output.dds"
            cmd = [
                str(comp_cli),
                "-fd", target_fmt,
                "-miplevels", "1",
                str(src_for_compress),
                str(out_file),
            ]

            cli_dir = comp_cli.parent
            env = os.environ.copy()
            env["LD_LIBRARY_PATH"] = ":".join(filter(None, [
                str(cli_dir),
                str(cli_dir / "pkglibs"),
                str(cli_dir / "qt"),
                env.get("LD_LIBRARY_PATH", ""),
            ]))

            result = subprocess.run(
                cmd, env=env,
                capture_output=True, text=True, errors="replace",
                timeout=300,
            )

            if result.returncode != 0 or not out_file.is_file():
                stderr_snippet = (result.stderr or result.stdout or "")[:200]
                return f"FAIL: {dds_path.name} — {stderr_snippet}"

            # Replace the original
            shutil.move(str(out_file), str(dds_path))

        action = f"resized to {target_w}x{target_h} and " if needs_resize else ""
        return f"OK: {dds_path.name} — {action}converted to {target_fmt}"

    except Exception as exc:
        return f"ERROR: {dds_path.name} — {exc}"


def _run_native_optimise(
    db_path: Path,
    output_dir: Path,
    preset_values: dict[str, int],
    comp_cli: Path,
    log_fn: Callable[[str], None],
    progress_fn: Callable[[int], None],
) -> None:
    """Native replacement for Optimise.exe.

    Reads the VRAMr database, determines which textures need resizing and/or
    recompression, then processes them using Pillow + CompressonatorCLI.
    """
    conn = sqlite3.connect(str(db_path))
    rows = conn.execute(
        "SELECT internal_path, width, height, format, type FROM bsa_files"
    ).fetchall()
    conn.close()

    if not rows:
        log_fn("  No textures to optimise.")
        return

    # Build work items
    tasks: list[tuple] = []
    for internal_path, width, height, current_fmt, tex_type in rows:
        target_res = preset_values.get(tex_type)
        if target_res is None:
            continue

        target_fmt = _FORMAT_MAP.get(tex_type, "BC7")

        resize = _compute_target_size(width, height, target_res)
        needs_resize = resize is not None
        target_w = resize[0] if resize else width
        target_h = resize[1] if resize else height

        needs_format_change = current_fmt.upper() != target_fmt.upper()

        if not needs_resize and not needs_format_change:
            continue

        # The internal_path may have Wine-style backslashes or forward slashes
        clean_path = internal_path.lstrip("/\\").replace("\\", "/")
        dds_file = output_dir / clean_path

        tasks.append((
            str(dds_file), target_w, target_h, target_fmt,
            str(comp_cli), needs_resize,
        ))

    total = len(tasks)
    log_fn(f"  Processing {total} textures natively (Pillow + CompressonatorCLI)...")

    # Use 2 parallel workers — Compressonator already uses multiple CPU threads
    # internally, so more workers would cause contention on the Deck's 8 threads.
    workers = min(2, total) if total > 0 else 1
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
            else:
                log_fn(f"  [{done}/{total}] {result_msg}")

            pct = 70 + int(23 * done / total)
            progress_fn(pct)

    log_fn(f"  Optimisation complete: {done - errors}/{total} succeeded, {errors} failed.")


# ── Main pipeline ──────────────────────────────────────────────────────────

def run_vramr(
    bat_dir: Path,
    game_data_dir: Path,
    output_dir: Path,
    preset: str = "optimum",
    alt_gpu: bool = False,
    log_fn: Callable[[str], None] | None = None,
    progress_fn: Callable[[int], None] | None = None,
) -> None:
    """
    Run the VRAMr texture optimisation pipeline.

    Parameters
    ----------
    bat_dir : Path
        Directory containing VRAMr.bat and tools/ subfolder.
    game_data_dir : Path
        The game's Data directory (where .bsa files and textures/ live).
    output_dir : Path
        Where VRAMr should write its output (becomes a mod in the staging area).
    preset : str
        One of the PRESETS keys.
    alt_gpu : bool
        Unused (kept for API compatibility).
    log_fn : callable
        Receives log lines; defaults to print().
    progress_fn : callable
        Receives integer 0-100 progress updates.
    """
    _log = log_fn or print
    _progress = progress_fn or (lambda _: None)

    if preset not in PRESETS:
        raise ValueError(f"Unknown preset '{preset}'. Choose from: {', '.join(PRESETS)}")

    p = PRESETS[preset]
    diffuse, normal, parallax, material = p["diffuse"], p["normal"], p["parallax"], p["material"]

    tools_dir = bat_dir / "tools"
    if not tools_dir.is_dir():
        raise FileNotFoundError(f"VRAMr tools/ directory not found: {tools_dir}")

    def _tool(name: str) -> str:
        path = tools_dir / name
        if not path.is_file():
            raise FileNotFoundError(f"Required VRAMr tool not found: {path}")
        return str(path)

    if not game_data_dir.is_dir():
        raise FileNotFoundError(f"Game Data directory not found: {game_data_dir}")

    # Discover Wine
    _log("Locating Proton/Wine...")
    wine = _find_wine()
    prefix = os.path.expanduser("~/vramr_wine_prefix")
    _log(f"  Wine: {wine}")

    # Ensure native CompressonatorCLI is available
    comp_cli = _ensure_compressonator(_log)
    _progress(5)

    # Prepare output directories
    if output_dir.exists():
        shutil.rmtree(output_dir)
    work_output = output_dir / "Output"
    work_logfiles = output_dir / "Logfiles"
    work_output.mkdir(parents=True, exist_ok=True)
    work_logfiles.mkdir(parents=True, exist_ok=True)
    db_path = work_output / "VRAMr.DB"

    # Write log header
    log_file = work_logfiles / "VRAMr.txt"
    with open(log_file, "w") as f:
        f.write(f"VRAMr Started       : {_timestamp()}\n")
        f.write(f"GameDir             : {game_data_dir}\n")
        f.write(f"Preset              : {p['label']}\n")
        f.write(f"Resolutions         : D={diffuse} N={normal} P={parallax} M={material}\n")
        f.write(f"Platform            : Linux (native optimise via CompressonatorCLI)\n\n")

    def _file_log(msg: str):
        with open(log_file, "a") as f:
            f.write(f"{_timestamp()} {msg}\n")

    _log(f"VRAMr: {p['label']} preset (D={diffuse} N={normal} P={parallax} M={material})")
    _log(f"VRAMr: Game Data = {game_data_dir}")
    _log(f"VRAMr: Output    = {output_dir}")

    # Wine path conversions (for steps 1-5 which still use VRAMr .exe tools)
    w_game     = _linux_to_wine(game_data_dir)
    w_output   = _linux_to_wine(work_output)
    w_logfiles = _linux_to_wine(work_logfiles)
    w_db       = _linux_to_wine(db_path)
    w_exclusions = _linux_to_wine(tools_dir / "Exclusions.mod")

    # PBR detection (case-insensitive — folder may be Textures/PBR on Linux)
    pbr_exists = any(
        p.is_dir()
        for tex in game_data_dir.iterdir() if tex.is_dir() and tex.name.lower() == "textures"
        for p in tex.iterdir() if p.is_dir() and p.name.lower() == "pbr"
    ) if game_data_dir.is_dir() else False
    _file_log(f"PBR Textures: {'Detected' if pbr_exists else 'Not Detected'}")
    _progress(10)

    # ── Step 1: BSA extraction
    _file_log("Indexing BSA Archives...")
    _wine_run(wine, prefix, _tool("BSA.exe"), [
        "--source", w_game + "\\*.bsa",
        "--dest", w_output,
        "--logfile", w_logfiles,
        "--filter", "*.dds",
        "--db", w_db,
    ], log_fn=_log, label="Step 1/6: BSA Extraction")
    _progress(25)

    # ── Step 2: Loose texture indexing
    _file_log("Layering Loose Textures...")
    _wine_run(wine, prefix, _tool("Loose.exe"), [
        "--source", w_game + "\\textures",
        "--dest", w_output + "\\textures",
        "--logfile", w_logfiles,
        "--db", w_db,
    ], log_fn=_log, label="Step 2/6: Loose Texture Indexing")
    _progress(40)

    # ── Step 3: Exclusions
    _file_log("Processing Exclusions...")
    _wine_run(wine, prefix, _tool("Exclude.exe"), [
        "--Exclude", w_exclusions,
        "--Dest", w_output,
        "--Logfile", w_logfiles,
    ], log_fn=_log, label="Step 3/6: Applying Exclusions")
    _progress(55)

    # ── Step 4: Filter
    _file_log("Filtering textures...")
    _wine_run(wine, prefix, _tool("Filter.exe"), [
        "--dnpm", str(diffuse), str(normal), str(parallax), str(material),
        "--Source", w_db,
        "--Logfiles", w_logfiles,
    ], log_fn=_log, label="Step 4/6: Filtering Textures")
    _progress(62)

    # ── Step 5: Extract
    _file_log("Extracting files for optimization...")
    _wine_run(wine, prefix, _tool("Extract.exe"), [
        "--Source", w_output,
        "--Logfile", w_logfiles,
        "--Output", w_output,
    ], log_fn=_log, label="Step 5/6: Extracting Files")
    _progress(70)

    # ── Step 6: Native Optimise (Pillow + CompressonatorCLI)
    _file_log("Optimising Textures (native)...")
    _log("── Step 6/6: Optimising Textures (native) ──")

    preset_values = {
        "diffuse": diffuse, "normal": normal,
        "parallax": parallax, "material": material,
    }
    _run_native_optimise(
        db_path=db_path,
        output_dir=work_output,
        preset_values=preset_values,
        comp_cli=comp_cli,
        log_fn=_log,
        progress_fn=_progress,
    )
    _progress(93)

    # ── Tidy up
    _file_log("Cleaning up...")
    _log("VRAMr: Cleaning up...")

    for root, dirs, _files in os.walk(str(work_output), topdown=False):
        for d in dirs:
            dp = os.path.join(root, d)
            try:
                os.rmdir(dp)
            except OSError:
                pass

    if db_path.is_file():
        db_path.unlink()

    # Flatten: move Output/* up into the mod folder root
    for child in list(work_output.iterdir()):
        dest = output_dir / child.name
        if dest.exists():
            if dest.is_dir():
                shutil.rmtree(dest)
            else:
                dest.unlink()
        child.rename(dest)

    # Remove the now-empty Output dir
    if work_output.exists():
        shutil.rmtree(work_output, ignore_errors=True)

    # Remove the Wine prefix — it's only needed during the run
    shutil.rmtree(prefix, ignore_errors=True)

    _file_log("VRAMr Complete")
    _log("VRAMr: Complete! Output is ready as a mod.")
    _progress(100)
