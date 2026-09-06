from __future__ import annotations

import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
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
        with game_mutation_lock(request.game), store.exclusive():
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
            store.set("pending_authored_profiles", request.package.profiles)
            saved = store.directory / (request.package.identity + ".wabbajack")
            if request.package.path.resolve() != saved.resolve():
                store._copy(request.package.path, saved)
            store.set("package_path", str(saved))
            store.set("pending_package_xxhash", file_hash(saved, ctl.stop))
            request.package.path = saved
            reconstruction = Reconstruction(request, store, cb, ctl)
            needed = reconstruction.needed_archives()
            cb.on_display_total(report.download_bytes)
            with Acquisition(request, report, cb, ctl, archives=needed) as acquire:
                automatic = [a for a in needed if acquire.automatic(a)]
                manual = [a for a in needed if not acquire.automatic(a)]
                with ThreadPoolExecutor(max_workers=2, thread_name_prefix="wabbajack") as pool:
                    futures = [pool.submit(consume_pipeline, items, acquire, reconstruction.install_archive, ctl,
                                          download_workers=4 if auto else 1, install_workers=2 if auto else 1,
                                          on_error=lambda item, exc: cb.on_log(f"{item.name}: {exc}"))
                               for items, auto in ((automatic, True), (manual, False)) if items]
                    errors = [error for f in futures for error in f.result()]
            if ctl.stop.is_set():
                status = "paused" if ctl.pause.is_set() else "cancelled"
                store.set("status", status)
                return InstallResult(status, message="Verified downloads and completed work were retained.")
            if errors:
                raise WabbajackError("Required files failed:\n" + "\n".join(f"{a.name}: {e}" for a, e in errors[:20]))
            desired = reconstruction.finish()
            profiles = prepare_profiles(request, store, reconstruction, desired)
            validate_links(store, profiles)
            current, conflicts = store.preview(desired, repair=request.mode == "repair")
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
            cb.on_status("Preparing required runtime components…")
            ensure_runtime(request, ctl.stop, cb.on_log)
            if ctl.stop.is_set():
                store.set("status", "paused")
                return InstallResult("paused", message="Publication was deferred.")
            if request.game.get_deploy_active():
                raise WabbajackError("Restore the game before publishing the installation")
            cb.on_status("Publishing verified files…")
            store.publish(desired, choices, current, {"version": request.package.version,
                "package_identity": request.package.identity, "gallery_id": request.gallery_id,
                "package_xxhash": store.get("pending_package_xxhash"),
                "authored_profiles": request.package.profiles,
                "fixes": request.fixes, "readme": request.package.metadata.get("Readme", ""),
                "remaining_instructions": [c.detail for c in report.checks if c.name in {"Author instructions", "Linux compatibility"}]}, stop=ctl.stop)
            publish_links(store, profiles)
            all_profiles = referenced_profiles(store.directory, store.profile_root)
            cb.on_status("Refreshing profiles and file catalogs…")
            refresh_profiles(request, all_profiles, cb.on_log)
            store.set("status", "complete")
            store.flush_completed()
            shutil.rmtree(store.work)
            with store.db:
                store.db.execute("DELETE FROM completed")
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
