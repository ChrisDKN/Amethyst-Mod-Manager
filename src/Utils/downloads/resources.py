from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from Utils.archives.budget import _get_available_memory_bytes
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps

from Utils.archives.budget import ExtractionMemoryBudget, ExtractionSpaceBudget

_current_work = ContextVar("install_work", default=None)


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


def _memory():
    total = available = 0
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                total = int(line.split()[1]) * 1024
            elif line.startswith("MemAvailable:"):
                available = int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
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
        except (OSError, ValueError, KeyError):
            pass
    result["total_memory"], result["available_memory"] = _memory()
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
        self.shared_rotational = source == target and rotational is True
        self._cv = threading.Condition()
        self._closed = threading.Event()
        self._thread = None
        self._active = 0
        self._target = 1
        self._downloads_done = False
        self._backpressure = False
        self._queued_bytes = 0
        self._costs = dict(costs)
        self._progress = {}
        self._completed_work = 0.0
        self._network_peak = 0.0
        self._download_total = download_bytes
        self._last_change = time.monotonic()
        self._pressure_streak = 0
        self._capacity_streak = 0
        self._reason = "Starting"
        self.emit("install.resources.configured", cpu_limit=self.cpu_limit,
             shared_rotational=self.shared_rotational, initial_workers=1)
        self._publish_state()

    def _limit(self):
        floor = int(self._backpressure or self._downloads_done or self.blocked())
        return min(self.workers.limit, self.cpu_limit, max(floor, self._target))

    @property
    def limit(self):
        with self._cv:
            return self._limit()

    @property
    def cpu_threads(self):
        with self._cv:
            parallel = max(1, self._limit())
        return max(1, min(4, self.cpu_count // parallel))

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

    def acquire(self, stop=None):
        with self._cv:
            while self._active >= self._limit():
                if self._closed.is_set() or stop is not None and stop.is_set():
                    return False
                self._cv.wait(0.2)
            if self._closed.is_set() or stop is not None and stop.is_set():
                return False
            self._active += 1
            state = self._state_locked()
        self._publish_state(state)
        return True

    def try_acquire(self, stop=None):
        with self._cv:
            if (self._closed.is_set() or stop is not None and stop.is_set()
                    or self._active >= self._limit()):
                return False
            self._active += 1
            state = self._state_locked()
        self._publish_state(state)
        return True

    def release(self):
        with self._cv:
            self._active -= 1
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
            self._progress[row] = after

    def downloads_complete(self):
        with self._cv:
            self._downloads_done = True
            self._target = min(self.cpu_limit, self.workers.limit)
            self._reason = "Downloads complete"
            self._pressure_streak = 0
            self._capacity_streak = 0
            self._cv.notify_all()
            state = self._state_locked()
        self._publish_state(state)

    def _adjust(self, sample, network_rate, work_rate, network_active, now):
        with self._cv:
            self._network_peak = max(network_rate, self._network_peak * 0.98)
            available_memory = sample["available_memory"]
            memory_low = available_memory < 1536 * 1024 ** 2
            memory_busy = (available_memory < 2560 * 1024 ** 2
                           and sample.get("memory_full", 0) >= 5)
            io_busy = sample.get("io_some", 0) >= 15
            network_slow = self._network_peak > 0 and network_rate < self._network_peak * 0.65
            target = min(self._target, self.workers.limit, self.cpu_limit)
            ceiling = min(self.cpu_limit, self.workers.limit)
            install_backlog = self._backpressure or self.blocked()
            overlap_useful = False
            if network_rate > 0 and work_rate > 0:
                downloaded = self.network_snapshot()[0]
                remaining_downloads = max(0, self._download_total - downloaded)
                remaining_work = max(0, sum(self._costs.values()) - self._completed_work)
                overlapped = max(remaining_downloads / network_rate,
                                 remaining_work / work_rate)
                sequential = (remaining_downloads / max(1, self._network_peak)
                              + remaining_work / work_rate)
                overlap_useful = overlapped <= sequential
            storage_hurting_downloads = (io_busy and network_active and network_slow
                                         and not install_backlog and not overlap_useful)

            if memory_low:
                target = 1
                reason = "Low memory"
                self._pressure_streak = 0
                self._capacity_streak = 0
            elif self._downloads_done and not memory_busy:
                target = ceiling
                reason = "Downloads complete"
                self._pressure_streak = 0
                self._capacity_streak = 0
            else:
                harmful = memory_busy or storage_hurting_downloads
                spare = (install_backlog or (
                    sample.get("io_some", 100) < 8
                    and sample.get("cpu_some", 100) < 15))
                if harmful:
                    self._pressure_streak += 1
                    self._capacity_streak = 0
                elif spare:
                    self._capacity_streak += 1
                    self._pressure_streak = 0
                else:
                    self._pressure_streak = 0
                    self._capacity_streak = 0

                if memory_busy:
                    reason = "Memory pressure"
                elif storage_hurting_downloads:
                    reason = "Protecting downloads"
                elif install_backlog:
                    reason = "Clearing install backlog"
                elif "io_some" not in sample or "cpu_some" not in sample:
                    reason = "Monitoring unavailable"
                else:
                    reason = "Balancing downloads"

                can_change = now - self._last_change >= 6
                if can_change and self._pressure_streak >= 3:
                    floor = 0 if storage_hurting_downloads else 1
                    target = max(floor, target - 1)
                    self._pressure_streak = 0
                elif can_change and self._capacity_streak >= 3:
                    if self.shared_rotational and network_active:
                        ceiling = 1
                    target = min(ceiling, target + 1)
                    self._capacity_streak = 0
            if not network_active or self._downloads_done or self._backpressure:
                target = max(1, target)
            changed = target != self._target
            self._target = target
            self._reason = reason
            if changed:
                self._last_change = now
                self._cv.notify_all()
            state = {"workers": self._limit(), "active": self._active,
                     "queued_bytes": self._queued_bytes, "backpressure": self._backpressure}
            display_state = self._state_locked()
        self.emit("install.resources.changed" if changed else "install.resources.sample",
             reason=reason, **state, **sample, network_bytes_per_second=round(network_rate),
             installation_work_per_second=round(work_rate))
        self._publish_state(display_state)

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
            with work.phase("total"):
                yield work
        finally:
            _current_work.reset(token)

    def map_files(self, operation, entries, stop=None):
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
                    pending.add(pool.submit(operation, item))
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
            previous_cpu = _cpu_times()
            previous_disk = _disk_bytes(self._storage_nodes)
            while not self._closed.wait(2):
                try:
                    now = time.monotonic()
                    current_bytes, network_active = self.network_snapshot()
                    with self._cv:
                        current_work = self._completed_work
                    elapsed = max(0.001, now - previous_time)
                    sample = _pressure()
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
                        network_active, now)
                    self._publish_system_stats(sample)
                    previous_time, previous_bytes, previous_work = now, current_bytes, current_work
                    previous_cpu, previous_disk = current_cpu, current_disk
                except Exception as exc:
                    with self._cv:
                        self._reason = "Monitoring unavailable"
                        self._cv.notify_all()
                        state = self._state_locked()
                    self.emit("install.resources.unavailable", error=str(exc))
                    self._publish_state(state)
        self._thread = threading.Thread(target=monitor, name="install-resources", daemon=True)
        self._thread.start()
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
        self.close()


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
