from __future__ import annotations

import shutil
import threading
import time
from pathlib import Path

from Utils.downloads.install import InstallCallbacks, InstallControl, consume_pipeline
from Utils.deployment.locking import game_mutation_lock
from .acquire import Acquisition
from .hashes import package_hash, file_hash
from .models import InstallResult
from .paths import WabbajackError
from .preflight import preflight
from .profiles import prepare_profiles, publish_links, validate_links, refresh_profiles, referenced_profiles
from .reconstruct import Reconstruction
from .store import Store

_install_lock = threading.Lock()


def run_install(request, *, callbacks=None, control=None, report=None):
    cb, ctl = callbacks or InstallCallbacks(), control or InstallControl()
    if not _install_lock.acquire(blocking=False):
        raise WabbajackError("Another Wabbajack installation is running")
    store = None
    last_phase, last_emit, phase_started = "", 0.0, 0.0
    def progress(phase, current, total, detail=""):
        nonlocal last_phase, last_emit, phase_started
        now = time.monotonic()
        if phase != last_phase:
            if last_phase:
                cb.on_log(f"{last_phase} finished in {now - phase_started:.2f}s")
            cb.on_log(phase)
            phase_started = now
        if phase != last_phase or now - last_emit >= 0.1 or (total and current == total):
            cb.on_phase(phase, current, total, detail)
            last_phase, last_emit = phase, now
    try:
        if package_hash(request.package.path) != request.package.identity:
            raise WabbajackError("Modlist package changed after inspection")
        if request.game.get_deploy_active():
            raise WabbajackError("Restore the deployed game before installing or updating a modlist")
        cb.on_status("Checking installation requirements…")
        report = report or preflight(request, ctl.stop)
        if not report.ok:
            raise WabbajackError("\n".join(f"{c.name}: {c.detail}" for c in report.checks if c.status == "error"))
        store = Store(request.directory, Path(request.game.get_profile_root()))
        with game_mutation_lock(request.game), store.exclusive(progress=progress):
            if request.game.get_deploy_active():
                raise WabbajackError("Restore the deployed game before modifying this installation")
            store.set("status", "installing")
            store.set("pending_package", request.package.identity)
            store.set("name", request.package.name)
            store.set("gallery_id", request.gallery_id)
            if request.gallery_metadata:
                store.set("gallery_metadata", request.gallery_metadata)
            store.set("game", request.package.game)
            store.set("downloads", str(request.downloads))
            store.set("source_roots", {k: str(v) for k, v in request.game_roots.items()})
            store.set("selected_profiles", request.profiles)
            store.set("setup_options", request.setup_options)
            store.set("pending_authored_profiles", request.package.profiles)
            saved = store.directory / (request.package.identity + ".wabbajack")
            if request.package.path.resolve() != saved.resolve():
                store._copy(request.package.path, saved)
            store.set("package_path", str(saved))
            store.set("pending_package_xxhash", file_hash(saved, ctl.stop))
            request.package.path = saved
            reconstruction = Reconstruction(request, store, cb, ctl)
            progress("Preparing archives", 0, 0, "Verifying reusable installation files")
            needed = reconstruction.needed_archives()
            cb.on_display_total(report.download_bytes)
            with Acquisition(request, report, cb, ctl, archives=needed) as acquire:
                automatic = [a for a in needed if acquire.automatic(a)]
                manual = [a for a in needed if not acquire.automatic(a)]
                from Utils.ui.config import load_collection_settings
                from Utils.archives.budget import ExtractionMemoryBudget, probe_archive
                settings = load_collection_settings()
                memory = ExtractionMemoryBudget(max_workers=settings["max_extract_workers"])
                counts = [0, 0]
                count_lock = threading.Lock()
                def update_status():
                    cb.on_status(f"Archives ready {counts[0]:,}/{len(needed):,} · Installed {counts[1]:,}/{len(needed):,}")
                def ready(archive):
                    cb.on_extract_queue(acquire.ids[archive.key], archive.name)
                    with count_lock:
                        counts[0] += 1
                        update_status()
                        if counts[0] == len(needed):
                            acquire.finish_progress()
                def install(archive, path):
                    reserved = False
                    try:
                        estimate = probe_archive(str(path), compressed_size=archive.size).uncompressed_size
                        memory.acquire(estimate, cancel=ctl.stop)
                        reserved = True
                        reconstruction.install_archive(archive, path)
                    finally:
                        if reserved:
                            memory.release(estimate)
                        cb.on_extract_remove(acquire.ids[archive.key])
                    with count_lock:
                        counts[1] += 1
                        update_status()
                cb.on_agg_download(0, report.download_bytes, 0.0)
                update_status()
                errors = consume_pipeline(automatic, acquire, install, ctl, manual_items=manual,
                    download_workers=settings["max_concurrent"], install_workers=settings["max_extract_workers"],
                    on_ready=ready, on_discard=lambda a: cb.on_extract_remove(acquire.ids[a.key]),
                    on_error=lambda item, exc: cb.on_log(f"{item.name}: {exc}"), prefetch=acquire.prefetch,
                    manual_acquire=acquire.manual)
            if ctl.stop.is_set():
                status = "paused" if ctl.pause.is_set() else "cancelled"
                store.set("status", status)
                return InstallResult(status, message="Verified downloads and completed work were retained.")
            if errors:
                raise WabbajackError("Required files failed:\n" + "\n".join(f"{a.name}: {e}" for a, e in errors[:20]))
            desired = reconstruction.finish(progress=progress)
            from .post_install import prepare_stock, apply_adjustments
            prepare_stock(request, store, desired, ctl.stop, progress)
            from .setup_tasks import run_tasks
            task_records = run_tasks(request, store, desired, ctl.stop, progress)
            from .bsa_setup import run_setup
            generated_mods = run_setup(request, store, desired, ctl.stop, progress)
            progress("Preparing profiles", 0, 0, "Applying authored profiles, INIs and launch settings")
            profiles = prepare_profiles(request, store, reconstruction, desired, generated_mods=generated_mods, progress=progress)
            apply_adjustments(request, store, desired, ctl.stop, progress)
            validate_links(store, profiles)
            current, conflicts = store.preview(desired, repair=request.mode == "repair", stop=ctl.stop, progress=progress)
            choices = {}
            if conflicts:
                if not request.resolve_conflicts:
                    raise WabbajackError(f"{len(conflicts)} local changes require update review")
                choices = request.resolve_conflicts(conflicts)
                if choices is None or ctl.stop.is_set():
                    store.set("status", "paused")
                    return InstallResult("paused", message="Update review was deferred.")
                if any(c.path not in choices or choices[c.path] not in {"keep", "author"} for c in conflicts):
                    raise WabbajackError("Every update conflict must be reviewed")
            from .runtime import ensure_runtime
            progress("Preparing runtime components", 0, 0, "Installing accepted runtime requirements")
            ensure_runtime(request, ctl.stop, cb.on_log)
            if ctl.stop.is_set():
                store.set("status", "paused")
                return InstallResult("paused", message="Publication was deferred.")
            if request.game.get_deploy_active():
                raise WabbajackError("Restore the game before publishing the installation")
            store.publish(desired, choices, current, {"version": request.package.version,
                "package_identity": request.package.identity, "gallery_id": request.gallery_id,
                "package_xxhash": store.get("pending_package_xxhash"),
                "authored_profiles": request.package.profiles,
                "setup_tasks": task_records, "setup_options": request.setup_options,
                "bsa_setup": store.get("pending_bsa_setup", {}) if generated_mods else {},
                "fixes": request.fixes, "readme": request.package.metadata.get("Readme", ""),
                "remaining_instructions": [c.detail for c in report.checks if c.name in {"Author instructions", "Linux compatibility", "Tool output configuration"}]},
                stop=ctl.stop, progress=progress)
            progress("Linking profiles", 0, 0, "Connecting profiles to the shared mods directory")
            publish_links(store, profiles)
            all_profiles = referenced_profiles(store.directory, store.profile_root)
            refresh_profiles(request, all_profiles, cb.on_log, progress=progress)
            store.set("status", "complete")
            store.flush_completed()
            progress("Cleaning temporary files", 0, 0, "Keeping downloaded archives and removing completed temporary work")
            shutil.rmtree(store.work)
            with store.db:
                store.db.execute("DELETE FROM completed")
            progress("Installation complete", 1, 1, "Profiles are ready")
            selected = store.get("profile_names", {}).get(request.package.selected_profile, profiles[0].name)
            if selected not in {p.name for p in profiles}:
                selected = profiles[0].name
            return InstallResult("complete", profiles, selected, len(desired),
                                 "Installation complete. Review the author's remaining instructions.")
    except InterruptedError:
        if not ctl.stop.is_set():
            if store:
                store.set("status", "interrupted")
            raise
        status = "paused" if ctl.pause.is_set() else "cancelled"
        if store:
            store.set("status", status)
        return InstallResult(status, message="Verified downloads and completed work were retained.")
    except BaseException:
        if store and store.get("status") != "committing":
            store.set("status", "interrupted")
        raise
    finally:
        if store:
            store.close()
        _install_lock.release()
