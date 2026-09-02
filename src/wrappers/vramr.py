"""Linux wrapper for VRAMr's upstream v16 texture pipeline.

All six stages run through an isolated Proton prefix.  The final upstream
``Optimise.exe`` drives the bundled Microsoft ``texconv.exe`` BC encoder,
selects a GPU adapter, detects available VRAM, and scales its worker count.
Running through Proton (rather than its bare Wine binary) activates DXVK so
texconv's DirectCompute path is available; texconv falls back to its CPU codec
on systems where DirectCompute is unavailable.

Public entry point: ``run_vramr(...)``.
"""

from __future__ import annotations

import os
import shutil
import threading
import time
from pathlib import Path
from typing import Callable

from Utils.wizards.textures import TextureToolCancelled
from wrappers._proton_texture import (
    linux_to_wine,
    prepare_proton_texture_runner,
    run_proton_texture_tool,
)


PRESETS: dict[str, dict] = {
    "hq":          {"diffuse": 2048, "normal": 2048, "parallax": 1024, "material": 1024, "label": "High Quality"},
    "quality":     {"diffuse": 2048, "normal": 1024, "parallax": 1024, "material": 1024, "label": "Quality"},
    "optimum":     {"diffuse": 2048, "normal": 1024, "parallax": 512,  "material": 512,  "label": "Optimum"},
    "performance": {"diffuse": 2048, "normal": 512,  "parallax": 512,  "material": 512,  "label": "Performance"},
    "vanilla":     {"diffuse": 512,  "normal": 512,  "parallax": 512,  "material": 512,  "label": "Vanilla"},
}


def _timestamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def run_vramr(
    bat_dir: Path,
    game_data_dir: Path,
    output_dir: Path,
    preset: str = "optimum",
    alt_gpu: bool = False,
    log_fn: Callable[[str], None] | None = None,
    progress_fn: Callable[[int], None] | None = None,
    *,
    prefer_discrete_gpu: bool | None = None,
    cancel_evt: "threading.Event | None" = None,
) -> None:
    """Run the VRAMr texture optimisation pipeline.

    ``alt_gpu`` is retained for API compatibility.  New callers should use
    ``prefer_discrete_gpu``; when omitted it inherits ``alt_gpu``.

    Setting *cancel_evt* stops the run: the current step's process group is
    killed and ``TextureToolCancelled`` is raised, leaving the half-written
    output for the caller to clean up.
    """
    _log = log_fn or print
    _progress = progress_fn or (lambda _: None)
    prefer_discrete = (
        alt_gpu if prefer_discrete_gpu is None else prefer_discrete_gpu
    )

    if preset not in PRESETS:
        raise ValueError(
            f"Unknown preset '{preset}'. Choose from: {', '.join(PRESETS)}"
        )

    p = PRESETS[preset]
    diffuse = p["diffuse"]
    normal = p["normal"]
    parallax = p["parallax"]
    material = p["material"]

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

    # Resolve every required v16 tool before deleting/rebuilding an existing
    # output. An old VRAMr install then fails early with a useful filename.
    optimiser = _tool("Optimise.exe")
    texconv = _tool("texconv.exe")

    _log("VRAMr: Preparing Proton/DXVK...")
    runner = prepare_proton_texture_runner(
        Path(optimiser),
        "vramr",
        _log,
        prefer_discrete_gpu=prefer_discrete,
    )
    _progress(5)

    def _run_required(exe: str, args: list[str], label: str) -> None:
        rc = run_proton_texture_tool(
            runner, exe, args, _log, label=label, cancel_evt=cancel_evt,
        )
        if rc != 0:
            raise RuntimeError(f"{label} failed with exit code {rc}")

    if output_dir.exists():
        shutil.rmtree(output_dir)
    work_output = output_dir / "Output"
    work_logfiles = output_dir / "Logfiles"
    work_output.mkdir(parents=True, exist_ok=True)
    work_logfiles.mkdir(parents=True, exist_ok=True)
    db_path = work_output / "VRAMr.DB"

    log_file = work_logfiles / "VRAMr.txt"
    with open(log_file, "w") as f:
        f.write(f"VRAMr Started       : {_timestamp()}\n")
        f.write(f"GameDir             : {game_data_dir}\n")
        f.write(f"Preset              : {p['label']}\n")
        f.write(
            f"Resolutions         : D={diffuse} N={normal} "
            f"P={parallax} M={material}\n"
        )
        f.write("Platform            : Linux (upstream texconv via Proton/DXVK)\n\n")

    def _file_log(msg: str) -> None:
        with open(log_file, "a") as f:
            f.write(f"{_timestamp()} {msg}\n")

    _log(
        f"VRAMr: {p['label']} preset "
        f"(D={diffuse} N={normal} P={parallax} M={material})"
    )
    _log(f"VRAMr: Game Data = {game_data_dir}")
    _log(f"VRAMr: Output    = {output_dir}")

    w_game = linux_to_wine(game_data_dir)
    w_output = linux_to_wine(work_output)
    w_logfiles = linux_to_wine(work_logfiles)
    w_db = linux_to_wine(db_path)
    w_exclusions = linux_to_wine(tools_dir / "Exclusions.mod")
    w_texconv = linux_to_wine(texconv)

    pbr_exists = any(
        child.is_dir() and child.name.lower() == "pbr"
        for textures in game_data_dir.iterdir()
        if textures.is_dir() and textures.name.lower() == "textures"
        for child in textures.iterdir()
    )
    _file_log(f"PBR Textures: {'Detected' if pbr_exists else 'Not Detected'}")
    _progress(10)

    _file_log("Indexing BSA Archives...")
    _run_required(_tool("BSA.exe"), [
        "--source", w_game + "\\*.bsa",
        "--dest", w_output,
        "--logfile", w_logfiles,
        "--filter", "*.dds",
        "--db", w_db,
    ], "Step 1/6: BSA Extraction")
    _progress(25)

    _file_log("Layering Loose Textures...")
    _run_required(_tool("Loose.exe"), [
        "--source", w_game + "\\textures",
        "--dest", w_output + "\\textures",
        "--logfile", w_logfiles,
        "--db", w_db,
    ], "Step 2/6: Loose Texture Indexing")
    _progress(40)

    _file_log("Processing Exclusions...")
    _run_required(_tool("Exclude.exe"), [
        "--Exclude", w_exclusions,
        "--Dest", w_output,
        "--Logfile", w_logfiles,
    ], "Step 3/6: Applying Exclusions")
    _progress(55)

    _file_log("Filtering textures...")
    _run_required(_tool("Filter.exe"), [
        "--dnpm", str(diffuse), str(normal), str(parallax), str(material),
        "--Source", w_db,
        "--Logfiles", w_logfiles,
    ], "Step 4/6: Filtering Textures")
    _progress(62)

    _file_log("Extracting files for optimization...")
    _run_required(_tool("Extract.exe"), [
        "--Source", w_output,
        "--Logfile", w_logfiles,
        "--Output", w_output,
    ], "Step 5/6: Extracting Files")
    _progress(70)

    _file_log("Optimising Textures (upstream texconv GPU pipeline)...")
    _run_required(optimiser, [
        "--dnpm", str(diffuse), str(normal), str(parallax), str(material),
        "--Source", w_db,
        "--Logfiles", w_logfiles,
        # The selected host GPU is filtered to DXVK adapter zero. In automatic
        # mode this remains texconv's normal default adapter.
        "--GPU", "0",
        "--Tool", w_texconv,
    ], "Step 6/6: Optimising Textures (texconv)")
    _progress(93)

    # Last chance to bail before the move-into-place phase - once files start
    # being renamed into output_dir a stop would leave a half-built mod.
    if cancel_evt is not None and cancel_evt.is_set():
        raise TextureToolCancelled("VRAMr cancelled")

    _file_log("Cleaning up...")
    _log("VRAMr: Cleaning up...")

    for root, dirs, _files in os.walk(str(work_output), topdown=False):
        for directory in dirs:
            try:
                os.rmdir(os.path.join(root, directory))
            except OSError:
                pass

    if db_path.is_file():
        db_path.unlink()

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

    _file_log("VRAMr Complete")
    _log("VRAMr: Complete! Output is ready as a mod.")
    _progress(100)
