from __future__ import annotations

import itertools
import queue as _queue
import threading
from dataclasses import dataclass, field
from typing import Callable
from concurrent.futures import ThreadPoolExecutor


def _noop(*_a, **_k):
    return None


class AdjustableWorkerLimit:
    def __init__(self):
        self._limit: int | None = None
        self._active = 0
        self._cv = threading.Condition()

    def set_default(self, value: int) -> None:
        with self._cv:
            if self._limit is None:
                self._limit = max(1, int(value))
                self._cv.notify_all()

    def set_limit(self, value: int) -> None:
        with self._cv:
            self._limit = max(1, int(value))
            self._cv.notify_all()

    @property
    def limit(self) -> int:
        with self._cv:
            return self._limit or 1

    def acquire(self, stop: "threading.Event | None" = None, *,
                work_bytes=None) -> bool:
        with self._cv:
            while self._active >= (self._limit or 1):
                if stop is not None and stop.is_set():
                    return False
                self._cv.wait(timeout=0.2)
            if stop is not None and stop.is_set():
                return False
            self._active += 1
            return True

    def try_acquire(self, stop: "threading.Event | None" = None, *,
                    work_bytes=None) -> bool:
        with self._cv:
            if ((stop is not None and stop.is_set())
                    or self._active >= (self._limit or 1)):
                return False
            self._active += 1
            return True

    def release(self, *, work_bytes=None) -> None:
        with self._cv:
            self._active = max(0, self._active - 1)
            self._cv.notify_all()


@dataclass
class InstallCallbacks:
    on_status: Callable[[str], None] = _noop            # status line text
    on_progress: Callable[["float | None"], None] = _noop  # 0..1 or None=hide
    on_agg_download: Callable[[int, int, float], None] = _noop  # bytes cur,total,MB/s
    on_display_total: Callable[[int], None] = _noop     # true collection size (bytes)
    on_mod_plan: Callable[[list], None] = _noop         # [(file_id, size), ...]
    # RED - active downloads
    on_dl_mod_wait: Callable[[int, str, int, int], None] = _noop
    on_dl_mod_start: Callable[[int, str, int], None] = _noop   # file_id,name,size
    on_dl_mod_update: Callable[[int, int, int], None] = _noop  # file_id,cur,tot
    on_dl_mod_finish: Callable[[int], None] = _noop            # file_id
    # GREEN - extracting/queued
    on_extract_queue: Callable[[int, str], None] = _noop       # file_id,name
    on_extract_wait: Callable[[int, str], None] = _noop
    on_extract_add: Callable[[int, str], None] = _noop
    on_extract_update: Callable[[int, int, int], None] = _noop  # file_id,cur,tot (tot 0 = busy)
    on_extract_detail: Callable[[int, str, int, int, str], None] = _noop
    on_extract_remove: Callable[[int], None] = _noop
    on_extract_state: Callable[[int, int, int, str], None] = _noop
    on_system_stats: Callable[[dict], None] = _noop
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
    on_result: Callable[[object], None] = _noop


@dataclass
class InstallControl:
    cancel: threading.Event = field(default_factory=threading.Event)
    pause: threading.Event = field(default_factory=threading.Event)
    stop: threading.Event = field(default_factory=threading.Event)  # set by BOTH pause & cancel
    extract_workers: AdjustableWorkerLimit = field(default_factory=AdjustableWorkerLimit)
    # manual mode - user actions from the overlay: a str path (Select File…)
    # or None (Skip, honored for optional mods only). Mirrors Tk's
    # _manual_file_queue.
    manual_queue: _queue.Queue = field(default_factory=_queue.Queue)


class ManualDownloadRequired(Exception):
    pass


def consume_pipeline(items, acquire, install, control, *, download_workers=4,
                     install_workers=2, manual_items=(), on_ready=None,
                     on_discard=None, on_error=None, prefetch=None, manual_acquire=None,
                     worker_limit=None, defer_large=True, max_large_install=2,
                     is_large=None, download_first=False, on_downloads_complete=None,
                     download_group=None, on_download_group=None,
                     interleave_groups=False, install_key=None,
                     ready_budget_bytes=0, on_queue_changed=None):
    from Utils.downloads.scheduler import order_by_size, run_pipelined
    items, manual_items = tuple(items), tuple(manual_items)
    download_workers, install_workers = max(1, download_workers), max(1, install_workers)
    ready = _queue.PriorityQueue(maxsize=max(
        download_workers + install_workers + 8, 32,
        len(items) + len(manual_items) + (install_workers if not defer_large else 0)))
    errors = []
    lock = threading.Lock()
    sequence = itertools.count()
    sequence_lock = threading.Lock()
    pending_manual = _queue.Queue(maxsize=max(1, len(items) + len(manual_items)))
    groups = {}
    for lane, batch in enumerate((items, manual_items)):
        for item in batch:
            group = download_group(item) if download_group and not interleave_groups else 0
            groups.setdefault(group, ([], []))[lane].append(item)
    started_groups = set()
    automatic_done = threading.Event()
    acquisition_done = threading.Event()
    admission = threading.Condition()
    active_large = active_small = 0
    waiting_large = []
    outstanding = 0
    queued_bytes = waiting_producers = 0

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

    def queue_key(item):
        if install_key is not None:
            return install_key(item)
        try:
            size = max(0, int(item.size or 0))
        except (AttributeError, TypeError, ValueError):
            size = 0
        return (1 if size <= 0 else 0, size)

    def next_sequence():
        with sequence_lock:
            return next(sequence)

    def enqueue(item, result):
        nonlocal outstanding, queued_bytes, waiting_producers
        task = (item, result) if defer_large else (
            item, result, bool(is_large(item)) if is_large else False)
        priority = (tuple(queue_key(item)), next_sequence(), task)
        if not defer_large:
            if control.stop.is_set():
                return False
            with admission:
                cost = max(1, int(item.size or 0))
                waiting = False
                try:
                    while (ready_budget_bytes and not download_first and outstanding
                           and queued_bytes + cost > ready_budget_bytes):
                        if control.stop.is_set():
                            return False
                        if not waiting:
                            waiting = True
                            waiting_producers += 1
                            notify(on_queue_changed, queued_bytes, waiting_producers)
                        admission.wait(0.2)
                finally:
                    if waiting:
                        waiting_producers -= 1
                        notify(on_queue_changed, queued_bytes, waiting_producers)
                if control.stop.is_set():
                    return False
                ready.put_nowait(priority)
                outstanding += 1
                queued_bytes += cost
                notify(on_queue_changed, queued_bytes, waiting_producers)
                admission.notify_all()
            return True
        while not control.stop.is_set():
            try:
                ready.put(priority, timeout=0.2)
                return True
            except _queue.Full:
                pass
        return False

    def producer(item, prefetched, *, manual=False, reason=""):
        if control.stop.is_set():
            return
        handed_off = False
        queued = False
        try:
            if interleave_groups and download_group is not None:
                group = download_group(item)
                with lock:
                    first = group not in started_groups
                    started_groups.add(group)
                if first:
                    notify(on_download_group, group)
            if manual and manual_acquire:
                result = manual_acquire(item, reason)
            else:
                result = acquire(item, prefetched) if prefetch else acquire(item)
            if control.stop.is_set():
                return
            notify(on_ready, item)
            queued = True
            handed_off = enqueue(item, result)
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

    def pipelined_consumer():
        nonlocal active_large, active_small, outstanding, queued_bytes
        waiting_bytes = None
        def set_waiting(work_bytes):
            nonlocal waiting_bytes
            if waiting_bytes == work_bytes:
                return
            changed = getattr(worker_limit, "waiting_changed", None)
            if changed is not None:
                if waiting_bytes is not None:
                    changed(waiting_bytes, -1)
                if work_bytes is not None:
                    changed(work_bytes, 1)
            waiting_bytes = work_bytes
        def task_work_bytes(task):
            if task is None:
                return None
            try:
                return max(0, int(task[0].size or 0))
            except (AttributeError, TypeError, ValueError):
                return 0
        def has_small_ready():
            with ready.mutex:
                return any(entry[2] is not None and not entry[2][2]
                           for entry in ready.queue)
        def large_limit():
            capacity = min(install_workers, getattr(worker_limit, "limit", install_workers))
            maximum = min(max(1, max_large_install), max(1, capacity - 1))
            if not acquisition_done.is_set() or active_small or has_small_ready():
                return 1
            return maximum
        def release_waiting():
            for queued in waiting_large:
                ready.put_nowait(queued)
            waiting_large.clear()
        try:
            while True:
                admitted = False
                admitted_bytes = None
                large_slot = small_slot = requeued = False
                while True:
                    with admission:
                        if control.stop.is_set() or active_large < large_limit():
                            release_waiting()
                        if ready.empty():
                            set_waiting(None)
                            admission.wait(timeout=0.2)
                            continue
                        with ready.mutex:
                            candidate = ready.queue[0]
                        candidate_task = candidate[2]
                        work_bytes = task_work_bytes(candidate_task)
                    if not control.stop.is_set() and candidate_task is not None:
                        if worker_limit is None:
                            admitted = True
                        else:
                            attempt = getattr(worker_limit, "try_acquire", None)
                            admitted = (attempt(control.stop, work_bytes=work_bytes)
                                        if attempt is not None else
                                        worker_limit.acquire(
                                            control.stop, work_bytes=work_bytes))
                        if not admitted:
                            set_waiting(work_bytes)
                            with admission:
                                admission.wait(timeout=0.2)
                            continue
                        admitted_bytes = work_bytes
                    set_waiting(None)
                    with admission:
                        if control.stop.is_set() or active_large < large_limit():
                            release_waiting()
                        with ready.mutex:
                            current = ready.queue[0] if ready.queue else None
                        if current is not candidate:
                            no_entry = True
                        else:
                            no_entry = False
                            entry = ready.get_nowait()
                            task = entry[2]
                            if task is not None and not control.stop.is_set():
                                if task[2] and active_large >= large_limit():
                                    waiting_large.append(entry)
                                    ready.task_done()
                                    no_entry = True
                                elif task[2]:
                                    active_large += 1
                                    large_slot = True
                                else:
                                    active_small += 1
                                    small_slot = True
                    if no_entry:
                        if admitted and worker_limit is not None:
                            worker_limit.release(work_bytes=admitted_bytes)
                        admitted = False
                        with admission:
                            admission.wait(timeout=0.2)
                        continue
                    break
                try:
                    if task is None:
                        with admission:
                            if not outstanding:
                                return
                            ready.put_nowait(entry)
                            requeued = True
                    else:
                        item, result, large = task
                        if control.stop.is_set():
                            notify(on_discard, item)
                            continue
                        try:
                            install(item, result)
                        except Exception as exc:
                            failed(item, exc)
                finally:
                    with admission:
                        if large_slot:
                            active_large -= 1
                        elif small_slot:
                            active_small -= 1
                        if control.stop.is_set() or active_large < large_limit():
                            release_waiting()
                        if task is not None and not requeued:
                            outstanding -= 1
                            queued_bytes -= max(1, int(task[0].size or 0))
                            notify(on_queue_changed, queued_bytes, waiting_producers)
                        if large_slot or (task is not None and not requeued):
                            admission.notify_all()
                    if admitted and worker_limit is not None:
                        worker_limit.release(work_bytes=admitted_bytes)
                    ready.task_done()
                if requeued:
                    with admission:
                        admission.wait(timeout=0.2)
        finally:
            set_waiting(None)

    def consumer():
        if not defer_large:
            return pipelined_consumer()
        while True:
            _priority, _seq, task = ready.get()
            try:
                if task is None:
                    return
                item, result = task
                if not control.stop.is_set():
                    work_bytes = max(0, int(getattr(item, "size", 0) or 0))
                    admitted = (worker_limit is None or
                                worker_limit.acquire(
                                    control.stop, work_bytes=work_bytes))
                    if admitted:
                        try:
                            install(item, result)
                        except Exception as exc:
                            failed(item, exc)
                        finally:
                            if worker_limit is not None:
                                worker_limit.release(work_bytes=work_bytes)
                    else:
                        notify(on_discard, item)
                else:
                    notify(on_discard, item)
            finally:
                ready.task_done()

    with ThreadPoolExecutor(max_workers=install_workers, thread_name_prefix="install") as pool:
        workers = ([] if download_first else
                   [pool.submit(consumer) for _ in range(install_workers)])
        try:
            def automatic(batch):
                try:
                    run_pipelined(order_by_size(batch, lambda a: a.size), prefetch or (lambda _: None),
                                  producer, download_workers, stop=control.stop,
                                  link_workers=max(4, download_workers),
                                  large_workers=2, size_key=lambda a: a.size,
                                  group_key=download_group if interleave_groups else None)
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
                for group, (automatic_items, manual_batch) in sorted(groups.items()):
                    if errors or control.stop.is_set():
                        break
                    automatic_done.clear()
                    for item in order_by_size(manual_batch, lambda a: a.size):
                        pending_manual.put((item, ""))
                    if not interleave_groups:
                        notify(on_download_group, group)
                    futures = [producers.submit(automatic, automatic_items)]
                    if not pending_manual.empty() or manual_acquire:
                        futures.append(producers.submit(manual))
                    for future in futures:
                        future.result()
            if download_first and not errors and not control.stop.is_set():
                if on_downloads_complete:
                    on_downloads_complete()
                workers = [pool.submit(consumer) for _ in range(install_workers)]
        finally:
            acquisition_done.set()
            with admission:
                admission.notify_all()
            try:
                if not download_first and on_downloads_complete:
                    on_downloads_complete()
            finally:
                if not workers:
                    while not ready.empty():
                        task = ready.get_nowait()[2]
                        notify(on_discard, task[0])
                        ready.task_done()
                for _ in workers:
                    ready.put(((2,), next_sequence(), None))
                with admission:
                    admission.notify_all()
                for worker in workers:
                    worker.result()
    return errors
