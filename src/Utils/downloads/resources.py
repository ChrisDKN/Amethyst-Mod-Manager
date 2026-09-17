from __future__ import annotations

import os
import threading
import time
from collections import deque
from pathlib import Path

from Utils.archives.budget import _get_available_memory_bytes
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps

from Utils.archives.budget import ExtractionMemoryBudget, ExtractionSpaceBudget

_current_work = ContextVar("install_work", default=None)
_current_resources = ContextVar("install_resources", default=None)
_SMALL_ARCHIVE_BYTES = 100 * 1024 ** 2


def current_resources():
    return _current_resources.get()


def wait_for_io(stop=None):
    resources = current_resources()
    if resources is None:
        return
    while resources.pause_extractor():
        if stop is not None and stop.is_set():
            raise InterruptedError("Installation stopped")
        resources._closed.wait(0.05)
    if stop is not None and stop.is_set():
        raise InterruptedError("Installation stopped")


def current_work():
    return _current_work.get()



def time_phase(name):
    def decorate(function):
        @wraps(function)
        def timed(*args, **kwargs):
            work = current_work()
            if work is None:
                return function(*args, **kwargs)
            with work.phase(name):
                return function(*args, **kwargs)
        return timed
    return decorate


def existing_parent(path):
    path = Path(path)
    while not path.exists() and path != path.parent:
        path = path.parent
    return path


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


def _cpu_times():
    try:
        fields = Path("/proc/stat").read_text().splitlines()[0].split()[1:]
        values = [int(value) for value in fields]
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        return sum(values[:8]), idle
    except (OSError, ValueError, IndexError):
        return None


def _disk_bytes(nodes):
    read_sectors = write_sectors = 0
    found = False
    for node in nodes:
        try:
            fields = (Path(node) / "stat").read_text().split()
            read_sectors += int(fields[2])
            write_sectors += int(fields[6])
            found = True
        except (OSError, ValueError, IndexError):
            pass
    if not found:
        return None
    return read_sectors * 512, write_sectors * 512


def _memory(stats=None):
    total = available = 0
    extra = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                total = int(line.split()[1]) * 1024
            elif line.startswith("MemAvailable:"):
                available = int(line.split()[1]) * 1024
            elif line.startswith(("Dirty:", "Writeback:", "SwapTotal:", "SwapFree:")):
                key, value, *_ = line.split()
                extra[key[:-1]] = int(value) * 1024
    except (OSError, ValueError, IndexError):
        pass
    if stats is not None:
        for key, name in (("Dirty", "dirty_bytes"), ("Writeback", "writeback_bytes")):
            if key in extra:
                stats[name] = extra[key]
        if "SwapTotal" in extra and "SwapFree" in extra:
            stats["swap_used_bytes"] = max(0, extra["SwapTotal"] - extra["SwapFree"])
    return total, available or _get_available_memory_bytes()


def _pressure():
    result = {}
    for resource in ("cpu", "io", "memory"):
        try:
            lines = (Path("/proc/pressure") / resource).read_text().splitlines()
            for line in lines:
                kind, *fields = line.split()
                values = dict(field.split("=", 1) for field in fields)
                result[f"{resource}_{kind}"] = float(values["avg10"])
                result[f"{resource}_{kind}_total"] = int(values["total"])
        except (OSError, ValueError, KeyError):
            pass
    result["total_memory"], result["available_memory"] = _memory(result)
    return result


class InstallResources:
    def __init__(self, workers, downloads, staging, network_snapshot, download_bytes,
                 costs, *, blocked=None, on_event=None, on_state=None,
                 on_system_stats=None):
        self.workers = workers
        self.network_snapshot = network_snapshot
        self.blocked = blocked or (lambda: False)
        self.on_event = on_event
        self.on_state = on_state
        self.on_system_stats = on_system_stats
        self.cpu_count = getattr(os, "process_cpu_count", os.cpu_count)() or 1
        self.memory = ExtractionMemoryBudget(max_workers=max(1, os.cpu_count() or 1), max_large_workers=2)
        self.space = ExtractionSpaceBudget()
        self._file_pool = None
        self.cpu_limit = max(1, self.cpu_count // 2)
        source, _ = _storage(downloads)
        target, rotational = _storage(staging)
        self._storage_nodes = tuple(dict.fromkeys((source, target)))
        self.shared_storage = source == target
        self.shared_rotational = source == target and rotational is True
        self._cv = threading.Condition()
        self._closed = threading.Event()
        self._thread = None
        self._active = 0
        self._active_small = 0
        self._waiting_small = 0
        self._target = 1
        self._downloads_done = False
        self._backpressure = False
        self._queued_bytes = 0
        self._costs = dict(costs)
        self._progress = {}
        self._completed_work = 0.0
        self._completed_items = 0
        self._network_peak = 0.0
        self._download_total = download_bytes
        self._last_change = time.monotonic()
        self._pressure_streak = 0
        self._capacity_streak = 0
        self._reason = "Starting"
        self._io_pause = False
        self._io_recovery_streak = 0
        self._download_rate = 0.0
        self._download_tokens = 0.0
        self._download_updated = time.monotonic()
        self._download_recovery_rate = 0.0
        self._download_change = 0.0
        self._write_bytes = 0
        self._write_seconds = 0.0
        self._write_max_seconds = 0.0
        self._write_healthy_streak = 0
        self._write_slow_streak = 0
        self._hold_extractions = False
        self._overlap_trial = None
        self._overlap_retry_at = 0.0
        self._download_trial = None
        self._download_retry_at = 0.0
        self._small_limit = 1
        self._small_trial = None
        self._small_retry_at = 0.0
        self._storage_overload_streak = 0
        self._small_pressure_blocked = False
        self._observations = deque(maxlen=8)
        self.emit("install.resources.configured", cpu_limit=self.cpu_limit,
             shared_rotational=self.shared_rotational, shared_storage=self.shared_storage,
             initial_workers=1, small_archive_bytes=_SMALL_ARCHIVE_BYTES)
        self._publish_state()

    def _limit(self):
        floor = int(self._backpressure or self._downloads_done or self.blocked())
        return min(self.workers.limit, max(floor, self._target))

    def _admission_limit(self, small):
        limit = self._limit()
        return limit if small else min(limit, self.cpu_limit)

    @property
    def limit(self):
        with self._cv:
            return self._limit()

    @property
    def cpu_threads(self):
        with self._cv:
            parallel = max(1, self._limit())
        return max(1, min(4, self.cpu_count // parallel))

    @contextmanager
    def scope(self):
        token = _current_resources.set(self)
        try:
            yield
        finally:
            _current_resources.reset(token)

    def pause_extractor(self):
        with self._cv:
            # Reserve emergency recovery time under severe memory pressure.
            return (not self._closed.is_set() and self._io_pause
                    and time.monotonic() % 1.0 < 0.5)

    def throttle_download(self, count, stop=None):
        with self._cv:
            while count > 0 and self._download_rate > 0 and not self._closed.is_set():
                if stop is not None and stop.is_set():
                    return
                now = time.monotonic()
                self._download_tokens = min(
                    max(256 * 1024, self._download_rate * 0.25),
                    self._download_tokens + (now - self._download_updated) * self._download_rate)
                self._download_updated = now
                taken = min(count, max(0, self._download_tokens))
                count -= taken
                self._download_tokens -= taken
                if count:
                    self._cv.wait(min(0.1, count / self._download_rate))

    def write_download(self, stream, data):
        started = time.monotonic()
        written = stream.write(data)
        elapsed = time.monotonic() - started
        with self._cv:
            self._write_bytes += written
            self._write_seconds += elapsed
            self._write_max_seconds = max(self._write_max_seconds, elapsed)
        return written

    def _state_locked(self):
        return self._limit(), self._active, self.workers.limit, self._reason

    def _publish_state(self, state=None):
        if self.on_state is None:
            return
        if state is None:
            with self._cv:
                state = self._state_locked()
        try:
            self.on_state(*state)
        except Exception:
            pass

    def _publish_system_stats(self, sample):
        if self.on_system_stats is None:
            return
        try:
            self.on_system_stats(dict(sample))
        except Exception:
            pass

    @staticmethod
    def _small_archive(work_bytes):
        return work_bytes is not None and 0 < work_bytes < _SMALL_ARCHIVE_BYTES

    def acquire(self, stop=None, *, work_bytes=None):
        small = self._small_archive(work_bytes)
        with self._cv:
            if small:
                self._waiting_small += 1
            try:
                while self._active >= self._admission_limit(small):
                    if self._closed.is_set() or stop is not None and stop.is_set():
                        return False
                    self._cv.wait(0.2)
                if self._closed.is_set() or stop is not None and stop.is_set():
                    return False
                self._active += 1
                if small:
                    self._active_small += 1
                state = self._state_locked()
            finally:
                if small:
                    self._waiting_small -= 1
        self._publish_state(state)
        return True

    def try_acquire(self, stop=None, *, work_bytes=None):
        small = self._small_archive(work_bytes)
        with self._cv:
            if (self._closed.is_set() or stop is not None and stop.is_set()
                    or self._active >= self._admission_limit(small)):
                return False
            self._active += 1
            if small:
                self._active_small += 1
            state = self._state_locked()
        self._publish_state(state)
        return True

    def release(self, *, work_bytes=None):
        with self._cv:
            self._active -= 1
            if self._small_archive(work_bytes):
                self._active_small = max(0, self._active_small - 1)
            self._cv.notify_all()
            state = self._state_locked()
        self._publish_state(state)

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
            if before < 1.0 <= after:
                self._completed_items += 1
            self._progress[row] = after

    def downloads_complete(self):
        with self._cv:
            self._downloads_done = True
            self._target = max(1, self._target)
            if not self._io_pause:
                self._reason = "Downloads complete"
            self._pressure_streak = 0
            self._capacity_streak = 0
            self._cv.notify_all()
            state = self._state_locked()
        self._publish_state(state)

    def _adjust(self, sample, network_rate, work_rate, network_active, now,
                item_rate=0.0):
        events = []
        with self._cv:
            self._network_peak = max(network_rate, self._network_peak * 0.98)
            available_memory = sample["available_memory"]
            memory_low = available_memory < 1536 * 1024 ** 2
            def pressure(name):
                return sample.get(name + "_recent", sample.get(name, 0))
            memory_busy = pressure("memory_full") >= 5 or pressure("memory_some") >= 10
            storage_busy = pressure("io_full") >= 10 or pressure("io_some") >= 40
            dirty = sample.get("dirty_bytes", 0) + sample.get("writeback_bytes", 0)
            dirty_limit = max(64 * 1024 ** 2, min(256 * 1024 ** 2, available_memory // 16))
            writes_slow = self._write_max_seconds >= 0.1
            writes_fast = self._write_bytes > 0 and self._write_max_seconds < 0.05
            self._write_healthy_streak = self._write_healthy_streak + 1 if writes_fast else 0
            self._write_slow_streak = self._write_slow_streak + 1 if writes_slow else 0
            previous_dirty = self._observations[0]["dirty_bytes"] if self._observations else dirty
            sample["writeback_growth_bytes"] = dirty - previous_dirty
            self._observations.append({
                "network_bytes_per_second": network_rate,
                "installation_work_per_second": work_rate,
                "installation_items_per_second": item_rate,
                "io_full": pressure("io_full"),
                "io_some": pressure("io_some"),
                "memory_full": pressure("memory_full"),
                "dirty_bytes": dirty,
                "write_delay_seconds": self._write_max_seconds if self._write_bytes else None,
            })
            metrics = {}
            for key in self._observations[-1]:
                values = [item[key] for item in self._observations if item[key] is not None]
                metrics[key] = sum(values) / len(values) if values else None
            sample.update(download_write_bytes=self._write_bytes,
                          download_write_seconds=round(self._write_seconds, 4),
                          download_write_max_seconds=round(self._write_max_seconds, 4),
                          writeback_limit_bytes=dirty_limit)
            self._write_bytes = 0
            self._write_seconds = self._write_max_seconds = 0.0
            severe = (available_memory < 512 * 1024 ** 2 or
                      (available_memory < 1536 * 1024 ** 2 and pressure("memory_full") >= 20))
            if severe:
                self._io_pause = True
                self._io_recovery_streak = 0
            else:
                self._io_recovery_streak += 1
                if self._io_recovery_streak >= 3:
                    self._io_pause = False
            target = min(self._target, self.workers.limit, self.cpu_limit)
            ceiling = min(self.cpu_limit, self.workers.limit)
            install_backlog = self._backpressure or self.blocked()
            downloading = network_active and not self._downloads_done
            self._pressure_streak = self._pressure_streak + 1 if storage_busy or memory_busy else 0
            spare = ("io_some" in sample and pressure("io_some") < 8
                     and pressure("memory_some") < 2 and sample.get("cpu_some", 100) < 15)
            self._capacity_streak = self._capacity_streak + 1 if spare else 0
            small_ceiling = min(
                self.workers.limit, self.cpu_count,
                self._active_small + self._waiting_small)
            small_ready = (small_ceiling >= 2 and self._active_small > 0
                           and self._waiting_small > 0)
            overload_now = (pressure("io_full") >= 75
                            or pressure("io_some") >= 90)
            self._storage_overload_streak = (
                self._storage_overload_streak + 1 if overload_now else 0)
            storage_overloaded = self._storage_overload_streak >= 2
            small_eligible = small_ready and (not downloading or install_backlog)
            small_unsafe = memory_low or memory_busy or storage_overloaded

            if small_unsafe:
                if self._small_trial is not None:
                    events.append(("install.resources.intervention.completed", {
                        "action": "parallel-small-archives",
                        "outcome": "pressure-worsened", "after": metrics,
                        "workers_from": self._small_trial["from"],
                        "workers_to": self._small_trial["target"]}))
                self._small_trial = None
                self._small_limit = 1
                self._small_pressure_blocked = True
            elif self._small_pressure_blocked:
                self._small_pressure_blocked = False
                self._small_retry_at = max(self._small_retry_at, now + 10)

            if self._small_trial is not None:
                trial = self._small_trial
                if not small_eligible:
                    events.append(("install.resources.intervention.completed", {
                        "action": "parallel-small-archives",
                        "outcome": "small-work-drained" if not small_ready else "downloads-active",
                        "after": metrics, "workers_from": trial["from"],
                        "workers_to": trial["target"]}))
                    self._small_limit = trial["from"]
                    self._small_trial = None
                elif now - trial["started"] >= trial["duration"]:
                    helped = self._small_parallel_helped(trial["before"], metrics)
                    events.append(("install.resources.intervention.completed", {
                        "action": "parallel-small-archives",
                        "outcome": "helped" if helped else "no-improvement",
                        "before": trial["before"], "after": metrics,
                        "workers_from": trial["from"],
                        "workers_to": trial["target"]}))
                    self._small_limit = trial["target"] if helped else trial["from"]
                    self._small_trial = None
                    self._small_retry_at = now + (
                        1 if helped and not downloading else 15 if helped else 30)

            if (small_eligible and not small_unsafe and self._small_trial is None
                    and self._small_limit < small_ceiling
                    and now >= self._small_retry_at):
                trial_target = min(small_ceiling, self._small_limit + 1)
                duration = 4 if not downloading else 8
                self._small_trial = {
                    "before": metrics, "started": now, "duration": duration,
                    "from": self._small_limit, "target": trial_target}
                events.append(("install.resources.intervention.started", {
                    "action": "parallel-small-archives", "before": metrics,
                    "archive_limit_bytes": _SMALL_ARCHIVE_BYTES,
                    "workers_from": self._small_limit, "workers_to": trial_target,
                    "duration_seconds": duration,
                    "downloads_active": downloading}))

            small_target = min(self._small_limit, max(1, small_ceiling))
            if self._small_trial is not None:
                small_target = self._small_trial["target"]

            if self._hold_extractions:
                if not downloading or install_backlog:
                    events.append(("install.resources.intervention.completed", {
                        "action": "reduce-overlap", "outcome": "queue-needs-draining" if install_backlog
                        else "downloads-inactive", "after": metrics}))
                    self._hold_extractions = False
                    self._overlap_trial = None
                    self._overlap_retry_at = now + 30
                elif self._overlap_trial is not None:
                    trial = self._overlap_trial
                    if self._active == 0:
                        if trial["ready_at"] is None:
                            trial["ready_at"] = now
                        elif now - trial["ready_at"] >= 8:
                            improved = self._intervention_helped(trial["before"], metrics, "reduce-overlap")
                            events.append(("install.resources.intervention.completed", {
                                "action": "reduce-overlap", "outcome": "helped" if improved else "no-improvement",
                                "before": trial["before"], "after": metrics}))
                            self._hold_extractions = improved
                            self._overlap_trial = None
                            self._overlap_retry_at = now + 30
                    else:
                        trial["ready_at"] = None
                elif self._capacity_streak >= 6 and now >= self._overlap_retry_at:
                    self._hold_extractions = False
                    self._overlap_retry_at = now + 15
                    events.append(("install.resources.overlap.resumed", {"reason": "capacity-recovered"}))

            if (not self._hold_extractions and self.shared_storage and downloading
                    and not install_backlog and self._active > 0 and self._pressure_streak >= 3
                    and now >= self._overlap_retry_at and self._download_trial is None):
                self._hold_extractions = True
                self._overlap_trial = {"before": metrics, "ready_at": None}
                events.append(("install.resources.intervention.started", {
                    "action": "reduce-overlap", "before": metrics}))

            if self._download_trial is not None and now - self._download_trial["ready_at"] >= 8:
                trial = self._download_trial
                improved = self._intervention_helped(trial["before"], metrics, "limit-downloads")
                events.append(("install.resources.intervention.completed", {
                    "action": "limit-downloads", "outcome": "helped" if improved else "no-improvement",
                    "before": trial["before"], "after": metrics}))
                if not improved:
                    self._set_download_rate(trial["previous_rate"], now)
                self._download_trial = None
                self._download_retry_at = now + (15 if improved else 30)
            elif (self._download_trial is None and self._overlap_trial is None and downloading
                  and self._write_slow_streak >= 3 and now >= self._download_retry_at):
                rate = max(1024 ** 2, (self._download_rate or metrics["network_bytes_per_second"]) * 0.75)
                if not self._download_rate or rate < self._download_rate:
                    self._download_trial = {"before": metrics, "ready_at": now,
                                            "previous_rate": self._download_rate}
                    self._download_recovery_rate = max(self._download_recovery_rate, self._network_peak)
                    self._set_download_rate(rate, now)
                    events.append(("install.resources.intervention.started", {
                        "action": "limit-downloads", "before": metrics,
                        "download_limit_bytes_per_second": round(rate)}))
            elif (self._download_trial is None and self._overlap_trial is None
                  and self._download_rate and self._write_healthy_streak >= 3
                  and now - self._download_change >= 4):
                rate = max(self._download_rate * 1.5, self._download_rate + 1024 ** 2)
                if rate >= max(16 * 1024 ** 2, self._download_recovery_rate):
                    rate = 0.0
                self._set_download_rate(rate, now)

            if self._hold_extractions:
                target = 0
                reason = "Protecting downloads"
            elif memory_low or memory_busy or storage_overloaded:
                target = 1
                reason = ("Low memory" if memory_low else "Memory pressure" if memory_busy
                          else "Storage pressure")
                self._capacity_streak = 0
            elif small_eligible:
                target = small_target
                reason = "Clearing install backlog"
            elif not downloading:
                target = ceiling
                reason = "Downloads complete" if self._downloads_done else "Clearing install backlog"
            elif install_backlog and (storage_busy or memory_busy):
                target = 1
                reason = "Storage pressure"
                self._capacity_streak = 0
            elif storage_busy:
                target = 1
                reason = "Storage pressure"
                self._capacity_streak = 0
            else:
                if install_backlog:
                    reason = "Clearing install backlog"
                elif self._downloads_done:
                    reason = "Downloads complete"
                elif "io_some" not in sample or "cpu_some" not in sample:
                    reason = "Monitoring unavailable"
                else:
                    reason = "Balancing downloads"

                can_change = now - self._last_change >= 6 and self._download_trial is None
                if can_change and self._capacity_streak >= 3:
                    if self.shared_rotational and network_active:
                        ceiling = 1
                    target = min(ceiling, target + 1)
                    self._capacity_streak = 0
            if not self._hold_extractions:
                target = max(1, target)
            changed = target != self._target
            self._target = target
            self._reason = reason
            if changed:
                self._last_change = now
                self._cv.notify_all()
            state = {"workers": self._limit(), "active": self._active,
                     "active_small": self._active_small,
                     "waiting_small": self._waiting_small,
                     "queued_bytes": self._queued_bytes, "backpressure": self._backpressure,
                     "holding_extractions": self._hold_extractions,
                     "parallel_small_archives": self._small_limit > 1,
                     "parallel_small_limit": self._small_limit,
                     "parallel_small_trial": self._small_trial is not None,
                     "parallel_small_trial_target": (
                         self._small_trial["target"] if self._small_trial is not None else None),
                     "storage_overload_streak": self._storage_overload_streak,
                     "download_limit_bytes_per_second": round(self._download_rate),
                     "extractor_duty_cycle": 0.5 if self._io_pause else 1.0}
            display_state = self._state_locked()
        for event, fields in events:
            self.emit(event, **fields)
        self.emit("install.resources.changed" if changed else "install.resources.sample",
             reason=reason, **state, **sample, network_bytes_per_second=round(network_rate),
             installation_work_per_second=round(work_rate),
             installation_items_per_second=round(item_rate, 3))
        self._publish_state(display_state)

    def _set_download_rate(self, rate, now):
        self._download_rate = rate
        self._download_tokens = 0.0
        self._download_updated = now
        self._download_change = now
        self._cv.notify_all()

    @staticmethod
    def _intervention_helped(before, after, action):
        backlog_not_worse = after["dirty_bytes"] <= before["dirty_bytes"] + max(
            64 * 1024 ** 2, before["dirty_bytes"] * 0.1)
        if action == "limit-downloads":
            old, new = before["write_delay_seconds"], after["write_delay_seconds"]
            return (old is not None and new is not None and old >= 0.05 and new < old * 0.7
                    and backlog_not_worse
                    and after["network_bytes_per_second"] >= before["network_bytes_per_second"] * 0.6)
        throughput = after["network_bytes_per_second"]
        old_throughput = before["network_bytes_per_second"]
        faster = throughput > max(old_throughput * 1.15, old_throughput + 1024 ** 2)
        fewer_stalls = any(before[key] >= 5 and after[key] < before[key] * 0.75
                          for key in ("io_full", "memory_full"))
        return (throughput > 0 and backlog_not_worse
                and (faster or (fewer_stalls and throughput >= old_throughput * 0.9)))

    @staticmethod
    def _small_parallel_helped(before, after):
        old_rate = before["installation_work_per_second"] or 0
        new_rate = after["installation_work_per_second"] or 0
        old_items = before["installation_items_per_second"] or 0
        new_items = after["installation_items_per_second"] or 0
        faster = (new_rate >= max(1024 ** 2, old_rate * 1.05,
                                  old_rate + 256 * 1024)
                  or new_items >= max(0.25, old_items * 1.05, old_items + 0.1))
        pressure_safe = all(after[key] <= max(before[key] + 10, before[key] * 1.25)
                            for key in ("io_full", "io_some"))
        old_delay = before["write_delay_seconds"]
        new_delay = after["write_delay_seconds"]
        writes_safe = (new_delay is None or new_delay <= 0.1
                       or old_delay is not None and new_delay <= old_delay * 1.5)
        return faster and pressure_safe and writes_safe

    def emit(self, event, **fields):
        if self.on_event is not None:
            try:
                self.on_event(event, **fields)
            except Exception:
                pass

    @contextmanager
    def work(self, row, stop, *, name="", on_wait=None):
        work = InstallWork(self, row, stop, name, on_wait)
        token = _current_work.set(work)
        try:
            with self.scope(), work.phase("total"):
                yield work
        finally:
            _current_work.reset(token)

    def map_files(self, operation, entries, stop=None):
        def run(item):
            with self.scope():
                wait_for_io(stop)
                return operation(item)

        with self._cv:
            if self._file_pool is None:
                self._file_pool = ThreadPoolExecutor(
                    max_workers=min(4, self.cpu_limit * 2), thread_name_prefix="install-files")
            pool = self._file_pool
        iterator = iter(entries)
        pending = set()
        try:
            while True:
                if stop is not None and stop.is_set():
                    raise InterruptedError("File staging stopped")
                while len(pending) < self.cpu_threads:
                    item = next(iterator, None)
                    if item is None:
                        break
                    pending.add(pool.submit(run, item))
                if not pending:
                    return
                done, pending = wait(pending, timeout=0.2, return_when=FIRST_COMPLETED)
                for future in done:
                    future.result()
        finally:
            for future in pending:
                future.cancel()
            if pending:
                wait(pending)

    def __enter__(self):
        def monitor():
            previous_time = time.monotonic()
            previous_bytes = self.network_snapshot()[0]
            previous_work = 0.0
            previous_items = 0
            previous_cpu = _cpu_times()
            previous_disk = _disk_bytes(self._storage_nodes)
            previous_pressure = _pressure()
            while not self._closed.wait(1):
                try:
                    now = time.monotonic()
                    current_bytes, network_active = self.network_snapshot()
                    with self._cv:
                        current_work = self._completed_work
                        current_items = self._completed_items
                    elapsed = max(0.001, now - previous_time)
                    sample = _pressure()
                    for key, value in list(sample.items()):
                        if key.endswith("_total") and key in previous_pressure:
                            sample[key[:-6] + "_recent"] = min(100.0, max(
                                0.0, (value - previous_pressure[key]) / (elapsed * 10000)))
                    previous_pressure = sample
                    current_cpu = _cpu_times()
                    if previous_cpu is not None and current_cpu is not None:
                        total_delta = current_cpu[0] - previous_cpu[0]
                        idle_delta = current_cpu[1] - previous_cpu[1]
                        if total_delta > 0:
                            sample["cpu_percent"] = max(
                                0.0, min(100.0, 100.0 * (total_delta - idle_delta) / total_delta))
                    current_disk = _disk_bytes(self._storage_nodes)
                    if previous_disk is not None and current_disk is not None:
                        sample["disk_read_bytes_per_second"] = max(
                            0.0, current_disk[0] - previous_disk[0]) / elapsed
                        sample["disk_write_bytes_per_second"] = max(
                            0.0, current_disk[1] - previous_disk[1]) / elapsed
                    self._adjust(
                        sample, max(0, current_bytes - previous_bytes) / elapsed,
                        max(0, current_work - previous_work) / elapsed,
                        network_active, now,
                        max(0, current_items - previous_items) / elapsed)
                    self._publish_system_stats(sample)
                    previous_time, previous_bytes, previous_work = now, current_bytes, current_work
                    previous_items = current_items
                    previous_cpu, previous_disk = current_cpu, current_disk
                except Exception as exc:
                    with self._cv:
                        self._io_pause = False
                        self._download_rate = 0.0
                        self._hold_extractions = False
                        self._overlap_trial = self._download_trial = None
                        self._target = max(1, self._target)
                        self._reason = "Monitoring unavailable"
                        self._cv.notify_all()
                        state = self._state_locked()
                    self.emit("install.resources.unavailable", error=str(exc))
                    self._publish_state(state)
        self._thread = threading.Thread(target=monitor, name="install-resources", daemon=True)
        self._thread.start()
        self._scope_token = _current_resources.set(self)
        return self

    def close(self):
        self._closed.set()
        with self._cv:
            self._cv.notify_all()
        if self._thread is not None:
            self._thread.join()
        if self._file_pool is not None:
            self._file_pool.shutdown(wait=True, cancel_futures=True)

    def __exit__(self, *_):
        try:
            self.close()
        finally:
            _current_resources.reset(self._scope_token)


class InstallWork:
    def __init__(self, resources, row, stop, name, on_wait):
        self.resources, self.row, self.stop = resources, row, stop
        self.name, self.on_wait = name, on_wait
        self.timings = {}
        self.inventories = {}

    @staticmethod
    def directory_stamp(path):
        info = path.stat()
        return info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns

    def files(self, root):
        for base, (files, directories) in self.inventories.items():
            if root.is_relative_to(base):
                try:
                    if any(self.directory_stamp(path) != stamp for path, stamp in directories):
                        return None
                except OSError:
                    return None
                return [path for path in files if path.is_relative_to(root)]
        return None

    @contextmanager
    def phase(self, phase, **fields):
        started = time.monotonic()
        success = False
        try:
            yield
            success = True
        finally:
            self.timings[phase] = self.timings.get(phase, 0.0) + time.monotonic() - started
            if phase == "total":
                self.resources.emit("install.work.completed", row=self.row, name=self.name,
                                    seconds={key: round(value, 4) for key, value in self.timings.items()},
                                    success=success, **fields)

    @contextmanager
    def extraction(self, probe, directory):
        started = time.monotonic()
        memory = self.resources.memory
        cost = probe.memory_bytes
        large = max(probe.compressed_size, probe.uncompressed_size) >= 1024 ** 3
        memory.acquire(cost, cancel=self.stop, large=large, on_wait=self.on_wait)
        try:
            with self.resources.space.reserve(directory, probe.uncompressed_size,
                                              self.stop, on_wait=self.on_wait):
                self.resources.emit("install.work.admitted", row=self.row, name=self.name,
                                    wait_seconds=round(time.monotonic() - started, 4),
                                    memory_bytes=cost, expanded_bytes=probe.uncompressed_size)
                with self.phase("extraction", compressed_bytes=probe.compressed_size,
                                expanded_bytes=probe.uncompressed_size):
                    yield
        finally:
            memory.release(cost, large=large)
