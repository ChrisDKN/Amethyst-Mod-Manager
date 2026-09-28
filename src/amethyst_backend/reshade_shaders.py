from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
import uuid
import zipfile
from pathlib import Path

from Utils.operations import OperationCancelled

from .jobs import JobCancelled, JobError
from .reshade_acquisition import _checkpoint
from .tool_install import _selected_game


def _preset_path(value):
    path = Path(value).expanduser()
    if not path.is_absolute() or path.suffix.lower() not in (".ini", ".txt"):
        raise JobError("invalid_reshade_preset", "Choose an absolute ReShade preset .ini or .txt path.")
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > 2 * 1024 * 1024:
            raise JobError("invalid_reshade_preset", "The preset must be a regular file under 2 MiB.")
        return path.resolve(strict=True)
    except OSError as exc:
        raise JobError("invalid_reshade_preset", "The preset file is unavailable.",
                       {"reason": str(exc)}) from exc


def _digest(path, control):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            _checkpoint(control)
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _bundle(source, bundle, control):
    files = sorted(path for path in source.rglob("*") if path.is_file())
    if not files:
        raise JobError("empty_reshade_shaders", "The selected shader packs contain no files.")
    shader_count = sum(path.suffix.lower() == ".fx" for path in files)
    with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_DEFLATED,
                         compresslevel=6, allowZip64=True) as output:
        for path in files:
            _checkpoint(control)
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode):
                raise JobError("unsafe_reshade_shaders", "A shader output changed into a link or special file.")
            relative = path.relative_to(source).as_posix()
            with path.open("rb") as input_stream, output.open(relative, "w") as output_stream:
                while True:
                    _checkpoint(control)
                    chunk = input_stream.read(65536)
                    if not chunk:
                        break
                    output_stream.write(chunk)
    _checkpoint(control)
    return len(files), shader_count


def _publish(cache, bundle, metadata, control):
    digest = _digest(bundle, control)
    metadata = {**metadata, "sha256": digest, "size_bytes": bundle.stat().st_size}
    for _ in range(100):
        _checkpoint(control)
        path = cache / f"ReShade_Shaders_{uuid.uuid4().hex}.zip"
        sidecar = path.with_suffix(".zip.reshade.json")
        try:
            os.link(bundle, path, follow_symlinks=False)
        except FileExistsError:
            continue
        temporary = None
        try:
            fd, raw = tempfile.mkstemp(prefix=".reshade-meta-", dir=cache)
            temporary = Path(raw)
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                json.dump(metadata, output, sort_keys=True)
            os.link(temporary, sidecar, follow_symlinks=False)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return path, metadata
    raise JobError("reshade_bundle_failed", "Could not reserve a shader bundle cache filename.")


def acquire_runner(game, profile, pack_indices, preset_value, control):
    def run(log, progress):
        from Utils.config_paths import get_download_cache_dir_for_game
        from Utils.operations import acquire_resource_operation
        from Utils.wizards.reshade import (
            OPTIONAL_SHADER_PACKS, SHADER_BASE_URL,
            download_and_extract_shaders, parse_preset_effect_files,
            prune_shaders_to_preset,
        )

        _checkpoint(control)
        selected = _selected_game(game, profile)
        if not selected.is_configured():
            raise JobError("game_not_configured", "Configure the game before acquiring ReShade shaders.")
        game_name = str(selected.name)
        if (not game_name or game_name in (".", "..") or "/" in game_name
                or "\\" in game_name or any(ord(char) < 32 or ord(char) == 127
                                             for char in game_name)):
            raise JobError("reshade_cache_unsafe", "The game's cache folder name is unsafe.")
        preset = _preset_path(preset_value) if preset_value is not None else None
        try:
            preset_hash = _digest(preset, control) if preset else None
            wanted = parse_preset_effect_files(preset) if preset else set()
            if preset and _digest(preset, control) != preset_hash:
                raise JobError("reshade_preset_changed", "The preset changed while being read.")
        except OSError as exc:
            raise JobError("invalid_reshade_preset", "The preset file could not be read.",
                           {"reason": str(exc)}) from exc
        if len(wanted) > 2048 or any(len(name) > 256 for name in wanted):
            raise JobError("invalid_reshade_preset", "The preset lists too many or overly long effect names.")
        effective_indices = (list(range(len(OPTIONAL_SHADER_PACKS))) if wanted
                             else list(pack_indices))
        packs = [OPTIONAL_SHADER_PACKS[index] for index in effective_indices]

        try:
            cache = get_download_cache_dir_for_game(game_name) / "ReShade"
            with acquire_resource_operation(f"reshade-shaders:{cache.resolve()}",
                                            "ReShade shader download", cancel=control):
                if (cache.parent.is_symlink() or cache.is_symlink()
                        or (cache.exists() and not cache.is_dir())):
                    raise JobError("reshade_cache_unsafe", "The ReShade cache folder is unsafe.")
                cache.mkdir(parents=True, exist_ok=True)
                pack_cache = cache / "packs"
                if pack_cache.is_symlink() or (pack_cache.exists() and not pack_cache.is_dir()):
                    raise JobError("reshade_cache_unsafe", "The shader pack cache is unsafe.")
                with tempfile.TemporaryDirectory(prefix=".reshade-shaders-", dir=cache) as raw:
                    work = Path(raw)
                    shaders = download_and_extract_shaders(
                        work, packs, cache_dir=pack_cache,
                        checkpoint=lambda: _checkpoint(control),
                        progress=lambda done, total: progress(done, total, "extract_reshade_shaders"),
                    )
                    missing = (prune_shaders_to_preset(
                        shaders, wanted, checkpoint=lambda: _checkpoint(control))
                        if wanted else set())
                    if preset:
                        try:
                            current_hash = _digest(preset, control)
                        except OSError as exc:
                            raise JobError("reshade_preset_changed", "The preset disappeared during shader acquisition.") from exc
                        if current_hash != preset_hash:
                            raise JobError("reshade_preset_changed", "The preset changed during shader acquisition.")
                    if wanted:
                        shutil.copy2(preset, work / "preset.ini")
                        if _digest(work / "preset.ini", control) != preset_hash:
                            raise JobError("reshade_preset_changed", "The copied preset differs from the selected file.")
                    bundle = work / "bundle.zip"
                    progress(0, 1, "package_reshade_shaders")
                    count, shader_count = _bundle(work, bundle, control)
                    metadata = {
                        "version": 1, "kind": "reshade_shaders",
                        "base_url": SHADER_BASE_URL,
                        "requested_pack_indices": list(pack_indices),
                        "pack_indices": effective_indices,
                        "packs": [
                            {"index": index, "label": OPTIONAL_SHADER_PACKS[index][0],
                             "url": OPTIONAL_SHADER_PACKS[index][1],
                             "subfolder": OPTIONAL_SHADER_PACKS[index][2]}
                            for index in effective_indices
                        ],
                        "preset_name": preset.name if wanted else None,
                        "preset_sha256": preset_hash if wanted else None,
                        "preset_ignored": preset is not None and not wanted,
                        "preset_effects": sorted(wanted),
                        "missing_effects": sorted(missing),
                        "file_count": count, "shader_count": shader_count,
                    }
                    path, metadata = _publish(cache, bundle, metadata, control)
                    progress(1, 1, "package_reshade_shaders")
            progress(1, 1, "acquire_reshade_shaders")
            log(f"ReShade: prepared {shader_count} shaders in {path}")
            return {"ok": True, "path": str(path), **metadata}
        except OperationCancelled as exc:
            raise JobCancelled("ReShade shader download cancelled.") from exc
        except (JobError, JobCancelled):
            raise
        except (OSError, ValueError, RuntimeError, zipfile.BadZipFile) as exc:
            raise JobError("reshade_shaders_failed", "Could not prepare the selected ReShade shaders.",
                           {"reason": str(exc)}) from exc

    return run
