from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
import threading
import time

from Thunderstore.ror2mm_handler import Ror2mmLink
from Thunderstore.thunderstore_download import download_package, fetch_version_info
from Utils.config_paths import get_download_cache_dir_for_game
from Utils.downloads.install import InstallCallbacks, InstallControl, consume_pipeline
from Utils.downloads.resources import InstallResources
from Utils.downloads.speed import RollingDownloadSpeed
from Utils.mods.copy import resolve_target_staging
from Utils.mods.install import install_collection_archive, FOMOD_DEFERRED, BAIN_DEFERRED
from Utils.ui.config import load_collection_settings, _MAX_EXTRACT_WORKERS_CEILING


@dataclass
class _Package:
    index: int
    entry: dict
    link: Ror2mmLink
    size: int

    @property
    def row(self):
        return -self.index - 1

    @property
    def name(self):
        return self.link.full_name


def install_entries(entries, game, profile_dir, *, stamp_meta,
                    control=None, callbacks=None, ownership=None):
    cb = callbacks or InstallCallbacks()
    ctl = control or InstallControl()
    if not entries or ctl.stop.is_set():
        return [], 0, []
    profile_dir = Path(profile_dir)
    staging = Path(resolve_target_staging(game, profile_dir))
    dest = get_download_cache_dir_for_game(game.name)
    dest.mkdir(parents=True, exist_ok=True)
    settings = load_collection_settings()
    ctl.extract_workers.set_default(settings["max_extract_workers"])
    jobs, failures, installed, deferred = [], {}, {}, []
    for index, entry in enumerate(entries):
        try:
            link = Ror2mmLink(namespace=entry["namespace"], name=entry["package"],
                              version=entry["version"], community=entry.get("community", ""))
            jobs.append(_Package(index, entry, link, max(0, int(entry.get("file_size") or 0))))
        except (KeyError, TypeError, ValueError) as exc:
            failures[index] = (entry.get("full_name", "?"), str(exc))
    if not jobs:
        return [], 0, list(failures.values())

    lock = threading.Lock()
    package_locks = {job.name: threading.Lock() for job in jobs}
    acquired = {}
    progress = {job.row: [0, job.size, 0.0] for job in jobs}
    active_downloads, downloaded = set(), set()
    speed = RollingDownloadSpeed()

    def status():
        cb.on_status(f"Thunderstore: {len(downloaded)}/{len(entries)} downloaded; "
                     f"{len(installed)}/{len(entries)} installed; {len(failures)} failed")

    def network_snapshot():
        with lock:
            return speed.total_bytes, bool(active_downloads)

    def update(job, done, total, *, network=True, force=False):
        with lock:
            slot = progress[job.row]
            if network:
                speed.add(max(0, done - slot[0]))
            slot[0], slot[1] = done, max(total, done, slot[1])
            now = time.monotonic()
            if not force and now - slot[2] < 0.1:
                return
            slot[2] = now
            cb.on_dl_mod_update(job.row, slot[0], slot[1])
            cb.on_agg_download(sum(p[0] for p in progress.values()),
                               sum(p[1] for p in progress.values()), speed.rate() / 1024 ** 2)

    resources = InstallResources(
        ctl.extract_workers, dest, staging, network_snapshot,
        sum(job.size for job in jobs), {job.row: max(1, job.size) for job in jobs},
        on_state=cb.on_extract_state, on_system_stats=cb.on_system_stats)

    def acquire(job):
        if ctl.stop.is_set():
            return None
        cb.on_dl_mod_start(job.row, job.name, job.size)
        try:
            with package_locks[job.name]:
                if ctl.stop.is_set():
                    return None
                cached = acquired.get(job.name)
                if cached is None:
                    with lock:
                        active_downloads.add(job.row)
                    try:
                        with resources.scope():
                            result = download_package(
                                job.link, dest_dir=dest, expected_size=job.size,
                                progress_cb=lambda done, total: update(job, done, total),
                                cancel=ctl.stop)
                    finally:
                        with lock:
                            active_downloads.discard(job.row)
                    if result.cancelled or ctl.stop.is_set():
                        return None
                    if not result.success or not result.file_path:
                        raise RuntimeError(result.error or "Download failed")
                    info = job.entry.get("version_info")
                    if not isinstance(info, dict):
                        try:
                            info = fetch_version_info(job.link)
                        except Exception as exc:
                            cb.on_log(f"[thunderstore] Metadata unavailable for {job.name}: {exc}")
                    if not isinstance(info, dict):
                        info = {"full_version_name": job.name,
                                "download_url": job.link.download_url, "size": job.size}
                    cached = (result.file_path, info)
                    acquired[job.name] = cached
                path, info = cached
                size = Path(path).stat().st_size
                update(job, size, size, network=False, force=True)
                with lock:
                    downloaded.add(job.row)
                    status()
                return path, info
        finally:
            cb.on_dl_mod_finish(job.row)

    def install(job, result, *, defer=True):
        if ctl.stop.is_set() or result is None:
            cb.on_extract_remove(job.row)
            return
        path, info = result
        cb.on_extract_add(job.row, job.name)

        def extraction_progress(done, total, phase=None):
            if phase == "Extracting":
                resources.progress(job.row, int(done), int(total))
            cb.on_extract_update(job.row, int(done), int(total))

        try:
            with resources.work(job.row, ctl.stop, name=job.name):
                folder = install_collection_archive(
                    str(path), game, profile_dir, log_fn=cb.on_log,
                    progress_fn=extraction_progress,
                    preferred_name=job.entry.get("logical") or job.entry.get("name") or "",
                    prebuilt_meta=None, skip_index_update=False, cancel=ctl.stop,
                    defer_interactive_fomod=defer, defer_interactive_bain=defer,
                    resolve_fomod=cb.resolve_fomod, resolve_bain=cb.resolve_bain)
            if folder in (FOMOD_DEFERRED, BAIN_DEFERRED):
                with lock:
                    deferred.append((job, result))
                return
            if not folder:
                if ctl.stop.is_set():
                    return
                raise RuntimeError("Install failed")
            stamp_meta(job.link, info, folder, staging_root=staging)
            if ownership is not None:
                ownership.track(folder)
            with lock:
                installed[job.index] = (int(job.entry.get("array_index", job.index)), folder)
                status()
            resources.completed(job.row)
            cb.on_row_installed(job.row)
        finally:
            cb.on_extract_remove(job.row)

    def failed(job, exc):
        if not ctl.stop.is_set():
            with lock:
                failures[job.index] = (job.name, str(exc))
                status()
            cb.on_log(f"[thunderstore] {job.name}: {exc}")
        cb.on_extract_remove(job.row)

    cb.on_mod_plan([(job.row, job.size) for job in jobs])
    cb.on_agg_download(0, sum(job.size for job in jobs), 0.0)
    status()
    with resources:
        consume_pipeline(
            jobs, acquire, install, ctl,
            download_workers=settings["max_concurrent"],
            install_workers=min(len(jobs), _MAX_EXTRACT_WORKERS_CEILING),
            download_order=settings["download_order"],
            worker_limit=resources, defer_large=False,
            on_ready=lambda job: cb.on_extract_queue(job.row, job.name),
            on_discard=lambda job: cb.on_extract_remove(job.row),
            on_error=failed, on_downloads_complete=resources.downloads_complete,
            ready_budget_bytes=min(4 * 1024 ** 3, max(256 * 1024 ** 2,
                                   shutil.disk_usage(dest).free // 16)),
            on_queue_changed=resources.queue_changed)
        for job, result in sorted(deferred, key=lambda item: item[0].index):
            if not resources.acquire(ctl.stop, work_bytes=job.size):
                break
            try:
                install(job, result, defer=False)
            except Exception as exc:
                failed(job, exc)
            finally:
                resources.release(work_bytes=job.size)
    return ([installed[index] for index in sorted(installed)], len(installed),
            [failures[index] for index in sorted(failures)])
