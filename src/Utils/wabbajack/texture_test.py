from __future__ import annotations

import tempfile
import threading
import time
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path

from Utils.downloads.install import InstallCallbacks, InstallControl, consume_pipeline

from .acquire import Acquisition
from .diagnostics import bind, emit, emit_exception
from .hosts import download_priority
from .manifest import archive_path, excluded_directives, required_directives
from .paths import WabbajackError, existing_parent


@dataclass(frozen=True)
class TextureTestResult:
    status: str
    archives: int
    textures: int
    formats: tuple[str, ...]
    substitutes: int
    message: str


def texture_test_plan(request):
    from .adapters import adapter_for
    adapter = adapter_for(
        request.package, request.game,
        store=request.setup_options.get("store", ""))
    planned = required_directives(
        request.package, (), excluded_directives(request, adapter))
    directives = [d for d in request.package.directives
                  if d.kind == "TransformedTexture" and d.path in planned]
    keys = {archive_path(d.data)[0] for d in directives}
    archives = [a for a in request.package.archives.values() if a.key in keys]
    return directives, archives


def texture_test_substitutes(request, report, archives):
    from .games import token
    from .paths import source_path
    substitutes = {}
    missing = []
    available = set(report.game_files) | set(report.prepared_game_files)
    for archive in archives:
        if archive.kind != "GameFileSource" or archive.key in available:
            continue
        name = str(archive.state.get(
            "Game", archive.state.get("GameName", request.package.game)))
        game_root = next((path for game, path in request.game_roots.items()
                          if token(game) == token(name)), None)
        relative = archive.state.get("GameFile", archive.name)
        found = None
        if game_root:
            for candidate in (relative, "Data/" + relative):
                try:
                    path = source_path(game_root, candidate)
                    if path.is_file():
                        found = path
                        break
                except (OSError, WabbajackError):
                    pass
        if found is None:
            missing.append(archive)
        else:
            substitutes[archive.key] = found
    return substitutes, missing


def run_texture_test(request, report, *, callbacks=None, control=None):
    supplied = callbacks or InstallCallbacks()
    cb = replace(supplied, on_log=bind(supplied.on_log, request.diagnostic_id))
    ctl = control or InstallControl()
    started = time.monotonic()
    directives, archives = texture_test_plan(request)
    if not directives:
        raise WabbajackError("The selected profiles do not require texture conversion")

    from .textures import probe_texture_tool, texture_parameters
    formats = tuple(sorted({texture_parameters(d.data["ImageState"])[3]
                            for d in directives}))
    emit(cb.on_log, "texture.test.started", archives=len(archives),
         textures=len(directives), formats=formats)
    cb.on_status("Testing the selected texture converter…")
    probe_texture_tool(request, ctl.stop, formats, log=cb.on_log)

    keys = {archive.key for archive in archives}
    cached = {key: path for key, path in report.cached.items() if key in keys}
    game_files = {key: path for key, path in report.game_files.items() if key in keys}
    prepared = {key: value for key, value in report.prepared_game_files.items()
                if key in keys}
    substitutes, missing_sources = texture_test_substitutes(
        request, report, archives)
    if missing_sources:
        raise WabbajackError(
            "Texture conversion sources are missing and cannot be downloaded: " +
            ", ".join(archive.name for archive in missing_sources))
    download_bytes = sum(a.size for a in archives
                         if a.key not in cached and a.key not in game_files
                         and a.key not in prepared and a.kind != "GameFileSource")
    test_report = replace(
        report, checks=[], cached=cached, game_files=game_files,
        prepared_game_files=prepared, download_bytes=download_bytes,
        install_bytes=sum(d.output_size for d in directives),
        required_archives=sorted(keys), archive_budget_bytes=0)
    cb.on_display_total(test_report.install_bytes)

    from .install import _install_lock
    if not _install_lock.acquire(blocking=False):
        raise WabbajackError("Another Wabbajack operation is running")
    completed = 0
    try:
        parent = existing_parent(request.directory)
        with tempfile.TemporaryDirectory(prefix=".texture-test-", dir=parent) as tmp:
            root = Path(tmp) / "profile"
            directory = root / ".wabbajack" / "test"
            test_request = replace(request, directory=directory, clear_archives=False)
            from .store import Store
            store = Store(directory, root, log=cb.on_log)
            try:
                from .reconstruct import Reconstruction
                reconstruction = Reconstruction(test_request, store, cb, ctl)
                reconstruction.by_archive = {}
                for directive in directives:
                    key, _ = archive_path(directive.data)
                    reconstruction.by_archive.setdefault(key, []).append(directive)
                from Utils.archives.budget import ExtractionMemoryBudget
                from Utils.ui.config import (
                    load_collection_settings, _MAX_EXTRACT_WORKERS_CEILING)
                settings = load_collection_settings()
                ctl.extract_workers.set_default(settings["max_extract_workers"])
                reconstruction.extraction_memory = ExtractionMemoryBudget(
                    max_workers=_MAX_EXTRACT_WORKERS_CEILING, max_large_workers=2)
                counts = Counter()
                count_lock = threading.Lock()

                with Acquisition(test_request, test_report, cb, ctl,
                                 archives=archives) as acquire:
                    automatic = [a for a in archives if acquire.automatic(a)]
                    manual = [a for a in archives if not acquire.automatic(a)]
                    download_plan = [a for a in archives
                                     if a.key not in cached and a.key not in game_files
                                     and a.key not in prepared
                                     and a.kind != "GameFileSource"]
                    cb.on_mod_plan([(acquire.ids[a.key], a.size) for a in download_plan])
                    for archive in archives:
                        cb.on_extract_queue(acquire.ids[archive.key], archive.name)

                    def ready(_archive):
                        with count_lock:
                            counts["ready"] += 1
                            cb.on_status(
                                f"Texture source archives ready: {counts['ready']:,}/{len(archives):,}")

                    def acquire_source(archive, prefetched=None):
                        substitute = substitutes.get(archive.key)
                        if substitute is None:
                            return acquire(archive, prefetched)
                        emit(cb.on_log, "texture.test.game_file_substitute",
                             archive=archive.name, path=substitute,
                             expected_hash=archive.key,
                             game_version=archive.state.get("GameVersion", ""))
                        return substitute

                    def convert(archive, path):
                        nonlocal completed
                        reconstruction.install_archive(archive, path)
                        for directive in reconstruction.by_archive.get(archive.key, ()):
                            result = reconstruction.results.get(directive.path)
                            if result:
                                Path(result["source"]).unlink(missing_ok=True)
                        with count_lock:
                            completed += len(reconstruction.by_archive.get(archive.key, ()))
                            cb.on_phase(
                                "Converting list textures", completed, len(directives),
                                f"Verified {completed:,}/{len(directives):,} textures")
                        cb.on_row_installed(acquire.ids[archive.key])

                    def failed(archive, exc):
                        cb.on_log(f"{archive.name}: {exc}")
                        emit_exception(cb.on_log, "texture.test.archive_failed", exc,
                                       archive=archive.name, archive_hash=archive.key)

                    errors = consume_pipeline(
                        automatic, acquire_source, convert, ctl,
                        manual_items=manual,
                        download_workers=settings["max_concurrent"],
                        install_workers=_MAX_EXTRACT_WORKERS_CEILING,
                        on_ready=ready,
                        on_discard=lambda a: cb.on_extract_remove(acquire.ids[a.key]),
                        on_error=failed, prefetch=acquire.prefetch,
                        manual_acquire=acquire.manual,
                        worker_limit=ctl.extract_workers, defer_large=False,
                        download_group=download_priority)
                    acquire.finish_progress()
                if ctl.stop.is_set():
                    message = ("Texture test paused; verified source downloads were retained."
                               if ctl.pause.is_set() else
                               "Texture test cancelled; verified source downloads were retained.")
                    return TextureTestResult(
                        "paused" if ctl.pause.is_set() else "cancelled",
                        len(archives), completed, formats, len(substitutes), message)
                if errors:
                    raise WabbajackError(
                        "Texture test failed:\n" +
                        "\n".join(f"{archive.name}: {exc}"
                                  for archive, exc in errors[:20]))
                mode = request.setup_options.get("texture", {}).get("mode", "auto")
                converter = ("native Compressonator" if mode == "compressonator"
                             else "Texconv (CPU)" if mode == "cpu"
                             else "Texconv")
                archive_label = "source archive" if len(archives) == 1 else "source archives"
                message = (f"Verified {len(directives):,} real list textures from "
                           f"{len(archives):,} {archive_label} using {converter}. "
                           "Any downloaded archives were retained for reuse.")
                if substitutes:
                    source_label = ("source was" if len(substitutes) == 1
                                    else "sources were")
                    requirement = ("it does not" if len(substitutes) == 1
                                   else "they do not")
                    message += (f" {len(substitutes):,} hash-mismatched local game "
                                f"{source_label} used only for developer conversion testing; "
                                f"{requirement} satisfy the normal installation requirement.")
                emit(cb.on_log, "texture.test.completed", archives=len(archives),
                     textures=len(directives), formats=formats,
                     elapsed_seconds=round(time.monotonic() - started, 3))
                return TextureTestResult(
                    "complete", len(archives), len(directives), formats,
                    len(substitutes), message)
            finally:
                store.close()
    except InterruptedError:
        message = ("Texture test paused; verified source downloads were retained."
                   if ctl.pause.is_set() else
                   "Texture test cancelled; verified source downloads were retained.")
        emit(cb.on_log, "texture.test.stopped", archives=len(archives),
             textures=completed, paused=ctl.pause.is_set())
        return TextureTestResult(
            "paused" if ctl.pause.is_set() else "cancelled",
            len(archives), completed, formats, len(substitutes), message)
    finally:
        _install_lock.release()
