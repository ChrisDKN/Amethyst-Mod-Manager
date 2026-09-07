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
    on_phase: Callable[[str, int, int, str], None] = _noop


@dataclass
class InstallControl:
    cancel: threading.Event = field(default_factory=threading.Event)
    pause: threading.Event = field(default_factory=threading.Event)
    stop: threading.Event = field(default_factory=threading.Event)  # set by BOTH pause & cancel
    # manual mode - user actions from the overlay: a str path (Select File…)
    # or None (Skip, honored for optional mods only). Mirrors Tk's
    # _manual_file_queue.
    manual_queue: _queue.Queue = field(default_factory=_queue.Queue)


class ManualDownloadRequired(Exception):
    pass


def consume_pipeline(items, acquire, install, control, *, download_workers=4,
                     install_workers=2, manual_items=(), on_ready=None,
                     on_discard=None, on_error=None, prefetch=None, manual_acquire=None):
    from Utils.downloads.scheduler import order_by_size, run_pipelined
    items, manual_items = tuple(items), tuple(manual_items)
    download_workers, install_workers = max(1, download_workers), max(1, install_workers)
    ready = _queue.Queue(maxsize=max(download_workers + install_workers + 8, 32))
    errors = []
    lock = threading.Lock()
    pending_manual = _queue.Queue(maxsize=max(1, len(items) + len(manual_items)))
    for item in manual_items:
        pending_manual.put((item, ""))
    automatic_done = threading.Event()

    def failed(item, exc):
        with lock:
            errors.append((item, exc))
        notify(on_error, item, exc)

    def notify(callback, *args):
        if callback:
            try:
                callback(*args)
            except Exception:
                pass

    def producer(item, prefetched, *, manual=False, reason=""):
        if control.stop.is_set():
            return
        handed_off = False
        queued = False
        try:
            if manual and manual_acquire:
                result = manual_acquire(item, reason)
            else:
                result = acquire(item, prefetched) if prefetch else acquire(item)
            if control.stop.is_set():
                return
            notify(on_ready, item)
            queued = True
            while not control.stop.is_set():
                try:
                    ready.put((item, result), timeout=0.2)
                    handed_off = True
                    return
                except _queue.Full:
                    pass
        except ManualDownloadRequired as exc:
            if manual_acquire and not manual and not control.stop.is_set():
                pending_manual.put((item, str(exc)))
            elif not control.stop.is_set():
                failed(item, exc)
        except Exception as exc:
            failed(item, exc)
        finally:
            if queued and not handed_off:
                notify(on_discard, item)

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
                else:
                    notify(on_discard, item)
            finally:
                ready.task_done()

    with ThreadPoolExecutor(max_workers=install_workers, thread_name_prefix="install") as pool:
        workers = [pool.submit(consumer) for _ in range(install_workers)]
        try:
            def automatic():
                try:
                    run_pipelined(order_by_size(items, lambda a: a.size), prefetch or (lambda _: None),
                                  producer, download_workers, stop=control.stop,
                                  link_workers=max(4, download_workers),
                                  large_workers=min(2, download_workers - 1))
                finally:
                    automatic_done.set()
            def manual():
                while not control.stop.is_set():
                    try:
                        item, reason = pending_manual.get(timeout=0.2)
                    except _queue.Empty:
                        if automatic_done.is_set() and pending_manual.empty():
                            return
                        continue
                    producer(item, None, manual=True, reason=reason)
            with ThreadPoolExecutor(max_workers=2, thread_name_prefix="acquire") as producers:
                futures = [producers.submit(automatic)]
                if not pending_manual.empty() or manual_acquire:
                    futures.append(producers.submit(manual))
                for future in futures:
                    future.result()
        finally:
            for _ in workers:
                ready.put(None)
            for worker in workers:
                worker.result()
    return errors
