from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from Utils.archives.budget import _get_available_memory_bytes
from .diagnostics import emit, emit_exception
from .paths import existing_parent


def _storage(path):
    device = existing_parent(path).stat().st_dev
    node = Path(f"/sys/dev/block/{os.major(device)}:{os.minor(device)}").resolve()
    if (node / "partition").exists():
        node = node.parent
    try:
        rotational = (node / "queue/rotational").read_text().strip() == "1"
    except OSError:
        rotational = None
    return str(node), rotational


def _pressure():
    result = {}
    for resource in ("cpu", "io", "memory"):
        try:
            lines = (Path("/proc/pressure") / resource).read_text().splitlines()
            for line in lines:
                kind, *fields = line.split()
                values = dict(field.split("=", 1) for field in fields)
                result[f"{resource}_{kind}"] = float(values["avg10"])
        except (OSError, ValueError, KeyError):
            pass
    result["available_memory"] = _get_available_memory_bytes()
    return result


class InstallResources:
    def __init__(self, workers, request, acquisition, priorities, *, log=None):
        self.workers = workers
        self.acquisition = acquisition
        self.log = log
        cpu_count = getattr(os, "process_cpu_count", os.cpu_count)() or 1
        self.cpu_limit = max(1, cpu_count // 2)
        source, _ = _storage(request.downloads)
        target, rotational = _storage(request.directory)
        self.shared_rotational = source == target and rotational is True
        self._cv = threading.Condition()
        self._closed = threading.Event()
        self._thread = None
        self._active = 0
        self._target = 1
        self._downloads_done = False
        self._backpressure = False
        self._queued_bytes = 0
        self._costs = {acquisition.ids[key]: -priority[1] for key, priority in priorities.items()}
        self._progress = {}
        self._completed_work = 0.0
        self._network_peak = 0.0
        self._download_total = acquisition.report.download_bytes
        self._last_change = time.monotonic()
        emit(log, "install.resources.configured", cpu_limit=self.cpu_limit,
             shared_rotational=self.shared_rotational, initial_workers=1)

    def _limit(self):
        budget = self.acquisition.budget
        floor = int(self._backpressure or self._downloads_done or budget is not None and budget.waiting)
        return min(self.workers.limit, self.cpu_limit, max(floor, self._target))

    @property
    def limit(self):
        with self._cv:
            return self._limit()

    def acquire(self, stop=None):
        with self._cv:
            while self._active >= self._limit():
                if self._closed.is_set() or stop is not None and stop.is_set():
                    return False
                self._cv.wait(0.2)
            if self._closed.is_set() or stop is not None and stop.is_set():
                return False
            self._active += 1
            return True

    def release(self):
        with self._cv:
            self._active -= 1
            self._cv.notify_all()

    def queue_changed(self, queued_bytes, waiters):
        with self._cv:
            self._queued_bytes = queued_bytes
            self._backpressure = waiters > 0
            self._cv.notify_all()

    def progress(self, row, current, total):
        if total <= 0 or row not in self._costs:
            return
        with self._cv:
            before = self._progress.get(row, 0.0)
            after = max(before, min(1.0, current / total))
            self._completed_work += (after - before) * self._costs[row]
            self._progress[row] = after

    def downloads_complete(self):
        with self._cv:
            self._downloads_done = True
            self._target = min(self.cpu_limit, self.workers.limit)
            self._cv.notify_all()

    def _adjust(self, sample, network_rate, work_rate, network_active, now):
        with self._cv:
            self._network_peak = max(network_rate, self._network_peak * 0.98)
            memory_low = sample["available_memory"] < 1536 * 1024 ** 2 or sample.get("memory_full", 0) >= 1
            io_busy = sample.get("io_some", 0) >= 15
            cpu_busy = sample.get("cpu_some", 0) >= 20
            network_slow = self._network_peak > 0 and network_rate < self._network_peak * 0.65
            target = min(self._target, self.workers.limit, self.cpu_limit)
            reason = "steady"
            if "io_some" not in sample or "cpu_some" not in sample:
                target, reason = 1, "pressure metrics unavailable"
            elif memory_low:
                target, reason = 1, "memory pressure"
            elif now - self._last_change >= 10:
                if io_busy or cpu_busy:
                    overlap_useful = False
                    if network_rate > 0 and work_rate > 0:
                        downloaded = self.acquisition.resource_snapshot()[0]
                        remaining_downloads = max(0, self._download_total - downloaded)
                        remaining_work = max(0, sum(self._costs.values()) - self._completed_work)
                        overlapped = max(remaining_downloads / network_rate, remaining_work / work_rate)
                        sequential = remaining_downloads / max(1, self._network_peak) + remaining_work / work_rate
                        overlap_useful = overlapped <= sequential
                    floor = 0 if io_busy and network_slow and network_active and not self._downloads_done and not overlap_useful else 1
                    target, reason = max(floor, target - 1), "storage pressure" if io_busy else "CPU pressure"
                elif sample.get("io_some", 0) < 5 and sample.get("cpu_some", 0) < 10:
                    ceiling = min(self.cpu_limit, self.workers.limit)
                    if self.shared_rotational and not self._downloads_done and network_active:
                        ceiling = 1
                    target, reason = min(ceiling, target + 1), "available capacity"
            if not network_active or self._downloads_done or self._backpressure:
                target = max(1, target)
            changed = target != self._target
            self._target = target
            if changed:
                self._last_change = now
                self._cv.notify_all()
            state = {"workers": self._limit(), "active": self._active,
                     "queued_bytes": self._queued_bytes, "backpressure": self._backpressure}
        emit(self.log, "install.resources.changed" if changed else "install.resources.sample",
             reason=reason, **state, **sample, network_bytes_per_second=round(network_rate),
             reconstruction_work_per_second=round(work_rate))

    def __enter__(self):
        def monitor():
            previous_time = time.monotonic()
            previous_bytes = self.acquisition.resource_snapshot()[0]
            previous_work = 0.0
            while not self._closed.wait(2):
                try:
                    now = time.monotonic()
                    current_bytes, network_active = self.acquisition.resource_snapshot()
                    with self._cv:
                        current_work = self._completed_work
                    elapsed = max(0.001, now - previous_time)
                    self._adjust(_pressure(), max(0, current_bytes - previous_bytes) / elapsed,
                                 max(0, current_work - previous_work) / elapsed, network_active, now)
                    previous_time, previous_bytes, previous_work = now, current_bytes, current_work
                except Exception as exc:
                    with self._cv:
                        self._target = 1
                        self._cv.notify_all()
                    emit_exception(self.log, "install.resources.unavailable", exc)
        self._thread = threading.Thread(target=monitor, name="wabbajack-resources", daemon=True)
        self._thread.start()
        return self

    def close(self):
        self._closed.set()
        with self._cv:
            self._cv.notify_all()
        if self._thread is not None:
            self._thread.join()

    def __exit__(self, *_):
        self.close()
