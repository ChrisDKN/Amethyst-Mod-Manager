from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
from pathlib import Path

from Utils.operations import OperationCancelled

from .jobs import JobCancelled, JobError
from .tool_install import _selected_game


def _checkpoint(control):
    if control.wait_if_paused():
        raise JobCancelled("ReShade DLL download cancelled.")


def _publish(cache, source, dll, architecture, control):
    url, version = source
    digest = hashlib.sha256()
    size = 0
    with dll.open("rb") as stream:
        while True:
            _checkpoint(control)
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
    if not size:
        raise JobError("reshade_dll_invalid", "The downloaded ReShade DLL is empty.")

    identity = {
        "version": 1, "kind": "reshade_dll", "reshade_version": version,
        "architecture": architecture, "source_url": url,
        "sha256": digest.hexdigest(), "size_bytes": size,
    }
    for _ in range(100):
        _checkpoint(control)
        path = cache / f"ReShade_{version}_{architecture}_{uuid.uuid4().hex}.dll"
        sidecar = path.with_suffix(".dll.reshade.json")
        try:
            os.link(dll, path, follow_symlinks=False)
        except FileExistsError:
            continue
        temporary = None
        try:
            fd, raw = tempfile.mkstemp(prefix=".reshade-meta-", dir=cache)
            temporary = Path(raw)
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                json.dump(identity, output, sort_keys=True)
            os.link(temporary, sidecar, follow_symlinks=False)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return path, identity
    raise JobError("reshade_download_failed", "Could not reserve a ReShade DLL cache filename.")


def acquire_runner(game, profile, architecture, control):
    def run(log, progress):
        from Utils.config_paths import get_download_cache_dir_for_game
        from Utils.operations import acquire_resource_operation
        from Utils.wizards.reshade import (
            download_and_extract_reshade_dll, fetch_latest_reshade_url,
        )

        _checkpoint(control)
        selected = _selected_game(game, profile)
        if not selected.is_configured():
            raise JobError("game_not_configured", "Configure the game before acquiring ReShade.")
        game_name = str(selected.name)
        if (not game_name or game_name in (".", "..") or "/" in game_name
                or "\\" in game_name or any(ord(char) < 32 or ord(char) == 127
                                             for char in game_name)):
            raise JobError("reshade_cache_unsafe", "The game's cache folder name is unsafe.")
        try:
            progress(0, 1, "resolve_reshade_version")
            source = fetch_latest_reshade_url()
            _checkpoint(control)
            cache = get_download_cache_dir_for_game(game_name) / "ReShade"
            with acquire_resource_operation(f"reshade-download:{cache.resolve()}",
                                            "ReShade DLL download", cancel=control):
                if (cache.parent.is_symlink() or cache.is_symlink()
                        or (cache.exists() and not cache.is_dir())):
                    raise JobError("reshade_cache_unsafe", "The ReShade cache folder is unsafe.")
                cache.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(prefix=".reshade-download-", dir=cache) as raw:
                    dll = download_and_extract_reshade_dll(
                        Path(raw), architecture, source=source,
                        checkpoint=lambda: _checkpoint(control),
                        progress=lambda done, total: progress(done, total, "download_reshade_dll"),
                    )
                    _checkpoint(control)
                    path, identity = _publish(cache, source, dll, architecture, control)
            progress(1, 1, "acquire_reshade_dll")
            log(f"ReShade: acquired {identity['reshade_version']} {architecture}-bit DLL at {path}")
            return {"ok": True, "path": str(path), **identity}
        except OperationCancelled as exc:
            raise JobCancelled("ReShade DLL download cancelled.") from exc
        except (JobError, JobCancelled):
            raise
        except (OSError, ValueError, RuntimeError, TypeError) as exc:
            raise JobError("reshade_download_failed", "Could not acquire the official ReShade DLL.",
                           {"reason": str(exc)}) from exc

    return run
