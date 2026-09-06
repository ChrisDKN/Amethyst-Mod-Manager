from __future__ import annotations

import queue as _queue
import threading
from dataclasses import dataclass, field
from typing import Callable
from concurrent.futures import ThreadPoolExecutor


def _noop(*_a, **_k):
    return None


@dataclass
class InstallCallbacks:
    on_status: Callable[[str], None] = _noop            # status line text
    on_progress: Callable[["float | None"], None] = _noop  # 0..1 or None=hide
    on_agg_download: Callable[[int, int, float], None] = _noop  # bytes cur,total,MB/s
    on_display_total: Callable[[int], None] = _noop     # true collection size (bytes)
    # RED - active downloads
    on_dl_mod_start: Callable[[int, str, int], None] = _noop   # file_id,name,size
    on_dl_mod_update: Callable[[int, int, int], None] = _noop  # file_id,cur,tot
    on_dl_mod_finish: Callable[[int], None] = _noop            # file_id
    # GREEN - extracting/queued
    on_extract_queue: Callable[[int, str], None] = _noop       # file_id,name
    on_extract_add: Callable[[int, str], None] = _noop
    on_extract_update: Callable[[int, int, int], None] = _noop  # file_id,cur,tot (tot 0 = busy)
    on_extract_remove: Callable[[int], None] = _noop
    on_row_installed: Callable[[int], None] = _noop            # file_id landed
    # manual (non-premium) mode - current-mod card payload dict
    on_manual_mod: Callable[[dict], None] = _noop
    # logging / lifecycle
    on_log: Callable[[str], None] = _noop
    on_done: Callable[[int, int, int, str], None] = _noop      # installed,skipped,total,profile
    on_paused: Callable[[int, str], None] = _noop              # installed,profile
    on_cancelled: Callable[[object], None] = _noop             # profile_dir (Path)
    # interactive resolvers (BLOCK the worker; caller marshals a wizard)
    resolve_fomod: "Callable | None" = None   # (config, base, name, inst, act, loose, saved) -> dict|None
    resolve_bain: "Callable | None" = None     # (subpkgs, root, name) -> {"selected":[...]}|None


@dataclass
class InstallControl:
    cancel: threading.Event = field(default_factory=threading.Event)
    pause: threading.Event = field(default_factory=threading.Event)
    stop: threading.Event = field(default_factory=threading.Event)  # set by BOTH pause & cancel
    # manual mode - user actions from the overlay: a str path (Select File…)
    # or None (Skip, honored for optional mods only). Mirrors Tk's
    # _manual_file_queue.
    manual_queue: _queue.Queue = field(default_factory=_queue.Queue)


def consume_pipeline(items, acquire, install, control, *, download_workers=4,
                     install_workers=2, on_error=None):
    from Utils.downloads.scheduler import order_by_size, run_pipelined
    ready = _queue.Queue(maxsize=max(1, install_workers))
    errors = []
    lock = threading.Lock()

    def failed(item, exc):
        with lock:
            errors.append((item, exc))
        if on_error:
            on_error(item, exc)

    def producer(item, prefetched):
        if control.stop.is_set():
            return
        try:
            result = acquire(item)
            while not control.stop.is_set():
                try:
                    ready.put((item, result), timeout=0.2)
                    return
                except _queue.Full:
                    pass
        except Exception as exc:
            failed(item, exc)

    def consumer():
        while True:
            task = ready.get()
            try:
                if task is None:
                    return
                item, result = task
                if not control.stop.is_set():
                    try:
                        install(item, result)
                    except Exception as exc:
                        failed(item, exc)
            finally:
                ready.task_done()

    with ThreadPoolExecutor(max_workers=install_workers, thread_name_prefix="install") as pool:
        workers = [pool.submit(consumer) for _ in range(install_workers)]
        try:
            run_pipelined(order_by_size(items, lambda a: a.size), lambda _: None,
                          producer, download_workers, stop=control.stop)
        finally:
            for _ in workers:
                ready.put(None)
            for worker in workers:
                worker.result()
    return errors
