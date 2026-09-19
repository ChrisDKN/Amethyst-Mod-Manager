from __future__ import annotations

import os
import threading
import time
from collections import deque
from pathlib import Path

from Utils.archives.budget import _get_available_memory_bytes
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import ExitStack, contextmanager, nullcontext
from contextvars import ContextVar
from functools import wraps

from Utils.archives.budget import ExtractionMemoryBudget, ExtractionSpaceBudget

_current_work = ContextVar("install_work", default=None)
_current_resources = ContextVar("install_resources", default=None)
_SMALL_ARCHIVE_BYTES = 100 * 1024 ** 2
_MEMORY_PRESSURE_SAMPLES = 3
_MEMORY_BACKOFF_SECONDS = 5.0


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
                 on_system_stats=None, trace=None):
        self.workers = workers
        self.network_snapshot = network_snapshot
        self.blocked = blocked or (lambda: False)
        self.on_event = on_event
        self.on_state = on_state
        self.on_system_stats = on_system_stats
        self.trace = trace if getattr(trace, "enabled", False) else None
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
        self._waiting = 0
        self._jobs = {}
        self._target = 1
        self._downloads_done = False
        self._backpressure = False
        self._queued_bytes = 0
        self._costs = dict(costs)
        self._progress = {}
        self._completed_work = 0.0
        self._completed_items = 0
        self._finished_rows = set()
        self._network_peak = 0.0
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
        self._download_trial = None
        self._download_retry_at = 0.0
        self._parallel_limit = 1
        self._parallel_trial = None
        self._parallel_retry_at = 0.0
        self._parallel_baseline = deque(maxlen=8)
        self._last_sample_at = time.monotonic()
        self._decision = "starting"
        self._storage_overload_streak = 0
        self._memory_pressure_streak = 0
        self._memory_reduce_at = 0.0
        self._pressure_blocked = False
        self._observations = deque(maxlen=8)
        self.emit("install.resources.configured", cpu_limit=self.cpu_limit,
             shared_rotational=self.shared_rotational, shared_storage=self.shared_storage,
             initial_workers=1, small_archive_bytes=_SMALL_ARCHIVE_BYTES,
             objective="installation-throughput", trial_seconds_min=4, trial_seconds_max=16,
             memory_pressure_samples=_MEMORY_PRESSURE_SAMPLES,
             memory_backoff_seconds=_MEMORY_BACKOFF_SECONDS)
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
        started = time.monotonic() if self.trace is not None else 0.0
        small = self._small_archive(work_bytes)
        with self._cv:
            self._waiting += 1
            if small:
                self._waiting_small += 1
            try:
                while self._active >= self._admission_limit(small):
                    if self._closed.is_set() or stop is not None and stop.is_set():
                        if self.trace is not None:
                            self.trace.wait("extract_admission", time.monotonic() - started)
                        return False
                    self._cv.wait(0.2)
                if self._closed.is_set() or stop is not None and stop.is_set():
                    if self.trace is not None:
                        self.trace.wait("extract_admission", time.monotonic() - started)
                    return False
                self._active += 1
                if small:
                    self._active_small += 1
                state = self._state_locked()
            finally:
                self._waiting -= 1
                if small:
                    self._waiting_small -= 1
        self._publish_state(state)
        if self.trace is not None:
            self.trace.wait("extract_admission", time.monotonic() - started)
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

    def waiting_changed(self, work_bytes, delta):
        with self._cv:
            self._waiting = max(0, self._waiting + int(delta))
            if self._small_archive(work_bytes):
                self._waiting_small = max(0, self._waiting_small + int(delta))
            self._cv.notify_all()

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
            self._progress[row] = after

    def completed(self, row):
        self.progress(row, 1, 1)
        with self._cv:
            if row not in self._finished_rows:
                self._finished_rows.add(row)
                self._completed_items += 1

    def downloads_complete(self):
        with self._cv:
            self._downloads_done = True
            self._target = max(1, self._target)
            if not self._io_pause:
                self._reason = "Downloads complete"
            self._cv.notify_all()
            state = self._state_locked()
        self._publish_state(state)

    @staticmethod
    def _window_metrics(observations):
        if not observations:
            return {}
        result = {}
        for key in observations[0][1]:
            values = [(seconds, row[key]) for seconds, row in observations
                      if row[key] is not None]
            weight = sum(seconds for seconds, _ in values)
            result[key] = (sum(seconds * value for seconds, value in values) / weight
                           if weight else None)
        return result

    @staticmethod
    def _parallel_outcome(before, after, before_seconds, after_seconds):
        for categories in (("small", "medium", "large", "unknown"),
                           ("extracting", "installing", "waiting", "other")):
            keys = ["workload_" + category for category in categories]
            if not any(key in before or key in after for key in keys):
                continue
            old_total = sum(before.get(key, 0) for key in keys)
            new_total = sum(after.get(key, 0) for key in keys)
            if not old_total or not new_total:
                return "inconclusive"
            if sum(abs(before.get(key, 0) / old_total - after.get(key, 0) / new_total)
                   for key in keys) > 0.7:
                return "workload-changed"
        ratios = []
        for key, minimum, evidence in (("installation_work_per_second", 256 * 1024, 1024 ** 2),
                                       ("installation_items_per_second", 0.1, 2)):
            old, new = before.get(key, 0) or 0, after.get(key, 0) or 0
            if (new * after_seconds >= evidence
                    and (old == 0 or old * before_seconds >= evidence)):
                ratios.append(new / max(minimum, old))
        if not ratios:
            return "inconclusive"
        if max(ratios) >= 1.05:
            return "helped"
        if max(ratios) < 0.85:
            return "slower"
        return "unchanged"

    def _parallel_target(self, observation, elapsed, ceiling, unsafe, now, events, *,
                         memory_busy=False, memory_sustained=False):
        def finish(outcome, after):
            trial = self._parallel_trial
            events.append(("install.resources.intervention.completed", {
                "action": "parallel-install-workers", "outcome": outcome,
                "before": trial["before"], "after": after,
                "workers_from": trial["from"], "workers_to": trial["target"],
                "admissions": trial["admissions"],
                "measured_seconds": sum(dt for dt, _ in trial["samples"])}))
            self._parallel_trial = None
            self._parallel_baseline.clear()

        if unsafe:
            if self._parallel_trial is not None:
                finish("pressure-worsened", observation)
            self._parallel_limit = 1
            self._parallel_baseline.clear()
            self._pressure_blocked = True
            self._decision = "resource-pressure"
            return 1
        if memory_busy:
            self._parallel_baseline.clear()
            target = max(1, min(self._target, ceiling))
            self._decision = "observing-memory-pressure"
            if memory_sustained:
                self._pressure_blocked = True
                self._decision = ("memory-pressure-draining" if self._active > target
                                  else "memory-pressure-hold")
                if now >= self._memory_reduce_at and self._active <= target and target > 1:
                    if self._parallel_trial is not None:
                        finish("memory-pressure", observation)
                    self._parallel_limit = target - 1
                    self._memory_reduce_at = now + _MEMORY_BACKOFF_SECONDS
                    events.append(("install.resources.intervention.completed", {
                        "action": "reduce-install-workers", "outcome": "sustained-memory-pressure",
                        "workers_from": target, "workers_to": target - 1,
                        "pressure_samples": self._memory_pressure_streak}))
                    self._decision = "memory-pressure-step-down"
                    return target - 1
            return target
        if self._pressure_blocked:
            self._pressure_blocked = False
            self._parallel_retry_at = now + 3
        self._parallel_limit = min(self._parallel_limit, ceiling)
        trial = self._parallel_trial
        if trial is not None:
            if ceiling < trial["target"]:
                finish("capacity-changed", observation)
            elif trial["ready_at"] is None:
                if not self._waiting or now - trial["started"] >= 8:
                    finish("not-exercised", {})
                    self._parallel_retry_at = now + 2
                else:
                    self._decision = "trial-awaiting-admission"
                    return trial["target"]
            elif not self._waiting and self._active < trial["target"]:
                self._parallel_limit = trial["target"]
                finish("work-drained", self._window_metrics(trial["samples"]))
                self._parallel_retry_at = now + 2
            else:
                # The first sample can include time before the extra job started.
                if now - elapsed >= trial["ready_at"]:
                    trial["samples"].append((elapsed, dict(observation)))
                measured = sum(dt for dt, _ in trial["samples"])
                after = self._window_metrics(trial["samples"])
                outcome = self._parallel_outcome(
                    trial["before"], after, trial["baseline_seconds"], measured)
                if measured >= 4 and (outcome != "inconclusive" or measured >= 16):
                    if outcome in {"helped", "inconclusive", "workload-changed"}:
                        self._parallel_limit = trial["target"]
                    self._parallel_retry_at = now + (
                        2 if outcome in {"helped", "inconclusive", "workload-changed"} else 10)
                    finish(outcome, after)
                    self._decision = "trial-" + outcome
                else:
                    self._decision = "trial-measuring"
                    return trial["target"]

        target = max(1, self._parallel_limit)
        if self._active > target:
            self._parallel_baseline.clear()
            self._decision = "waiting-for-active-jobs-to-drain"
        elif now < self._parallel_retry_at:
            self._decision = "retry-cooldown"
        elif not self._waiting or self._active < target:
            self._parallel_baseline.clear()
            self._decision = "waiting-for-work"
        elif target >= ceiling:
            self._decision = "worker-ceiling"
        else:
            self._parallel_baseline.append((elapsed, dict(observation)))
            if sum(dt for dt, _ in self._parallel_baseline) >= 2:
                before = self._window_metrics(self._parallel_baseline)
                self._parallel_trial = {
                    "from": target, "target": target + 1, "before": before,
                    "baseline_seconds": sum(dt for dt, _ in self._parallel_baseline),
                    "started": now, "ready_at": None, "samples": [], "admissions": 0}
                events.append(("install.resources.intervention.started", {
                    "action": "parallel-install-workers", "before": before,
                    "workers_from": target, "workers_to": target + 1}))
                self._parallel_baseline.clear()
                self._decision = "trial-awaiting-admission"
                return target + 1
            self._decision = "measuring-baseline"
        return target

    def _workload_mix(self):
        mix = dict.fromkeys(("small", "medium", "large", "unknown", "extracting",
                             "installing", "waiting", "other"), 0.0)
        for work in self._jobs.values():
            size = self._costs.get(work.row, 0)
            band = ("unknown" if size <= 1 else "small" if size < _SMALL_ARCHIVE_BYTES
                    else "medium" if size < 1024 ** 3 else "large")
            phase = work.current_phase
            kind = ("extracting" if phase in {"extraction", "reconstruct_extracting"}
                    else "waiting" if phase.endswith("_wait")
                    else "installing" if phase in {
                        "staging", "indexing", "reconstruct_installing", "reconstruct_patching",
                        "reconstruct_textures", "reconstruct_finalising", "archive_build"}
                    else "other")
            mix[band] += 1
            mix[kind] += 1
        count = max(1, len(self._jobs))
        return {"workload_" + key: value / count for key, value in mix.items()}

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
            self._memory_pressure_streak = self._memory_pressure_streak + 1 if memory_busy else 0
            memory_sustained = self._memory_pressure_streak >= _MEMORY_PRESSURE_SAMPLES
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
                **self._workload_mix(),
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
            downloading = network_active and not self._downloads_done
            overload_now = pressure("io_full") >= 75 or pressure("io_some") >= 90
            self._storage_overload_streak = (
                self._storage_overload_streak + 1 if overload_now else 0)
            storage_overloaded = self._storage_overload_streak >= 2
            unsafe = memory_low or storage_overloaded
            ceiling = min(self.workers.limit,
                          self.cpu_count if self._active_small or self._waiting_small
                          else self.cpu_limit)
            elapsed = max(0.001, now - self._last_sample_at)
            self._last_sample_at = now
            target = self._parallel_target(
                self._observations[-1], elapsed, ceiling, unsafe, now, events,
                memory_busy=memory_busy, memory_sustained=memory_sustained)

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
            elif (self._download_trial is None and downloading
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
            elif (self._download_trial is None
                  and self._download_rate and self._write_healthy_streak >= 3
                  and now - self._download_change >= 4):
                rate = max(self._download_rate * 1.5, self._download_rate + 1024 ** 2)
                if rate >= max(16 * 1024 ** 2, self._download_recovery_rate):
                    rate = 0.0
                self._set_download_rate(rate, now)

            reason = ("Low memory" if memory_low else "Memory pressure" if memory_busy
                      else "Storage pressure" if storage_overloaded
                      else "Downloads complete" if self._downloads_done and not self._waiting
                      else "Clearing install backlog")
            changed = target != self._target
            self._target = target
            self._reason = reason
            if changed:
                self._cv.notify_all()
            state = {"workers": self._limit(), "active": self._active,
                     "active_small": self._active_small,
                     "waiting_small": self._waiting_small,
                     "queued_bytes": self._queued_bytes, "backpressure": self._backpressure,
                     "waiting": self._waiting,
                     "decision": self._decision,
                     "parallel_limit": self._parallel_limit,
                     "parallel_trial": self._parallel_trial is not None,
                     "parallel_retry_seconds": round(max(0, self._parallel_retry_at - now), 2),
                     "parallel_trial_target": (
                         self._parallel_trial["target"] if self._parallel_trial else None),
                     "parallel_trial_admissions": (
                         self._parallel_trial["admissions"] if self._parallel_trial else 0),
                     "parallel_trial_seconds": (
                         round(now - self._parallel_trial["ready_at"], 2)
                         if self._parallel_trial and self._parallel_trial["ready_at"] is not None
                         else 0),
                     "active_jobs": [work.snapshot(now) for work in self._jobs.values()],
                     "storage_overload_streak": self._storage_overload_streak,
                     "memory_pressure_streak": self._memory_pressure_streak,
                     "memory_backoff_seconds": round(max(0, self._memory_reduce_at - now), 2),
                     "download_limit_bytes_per_second": round(self._download_rate),
                     "extractor_duty_cycle": 0.5 if self._io_pause else 1.0}
            display_state = self._state_locked()
        for event, fields in events:
            self.emit(event, **fields)
        self.emit("install.resources.changed" if changed else "install.resources.sample",
             reason=reason, **state, **sample, network_bytes_per_second=round(network_rate),
             installation_work_per_second=round(work_rate),
             installation_items_per_second=round(item_rate, 3))
        if self.trace is not None:
            self.trace.resources(
                reason=reason, **state, **sample, network_active=network_active,
                cpu_count=self.cpu_count,
                network_bytes_per_second=network_rate,
                installation_work_per_second=work_rate,
                installation_items_per_second=item_rate)
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
            install_faster = any(
                after[key] >= max(minimum, before[key] * 1.15)
                for key, minimum in (("installation_work_per_second", 1024 ** 2),
                                     ("installation_items_per_second", 0.25)))
            writes_faster = (old is not None and new is not None and old >= 0.05
                            and new < old * 0.7
                            and after["network_bytes_per_second"] >=
                            before["network_bytes_per_second"] * 0.6)
            return backlog_not_worse and (install_faster or writes_faster)
        return False

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
        identity = threading.get_ident()
        with self._cv:
            self._jobs[identity] = work
            trial = self._parallel_trial
            if trial is not None and len(self._jobs) > trial["from"]:
                trial["admissions"] += 1
                if trial["ready_at"] is None:
                    trial["ready_at"] = time.monotonic()
        try:
            with self.scope(), work.phase("total"):
                yield work
        finally:
            with self._cv:
                self._jobs.pop(identity, None)
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
            self._last_sample_at = previous_time
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
                        self._parallel_trial = self._download_trial = None
                        self._parallel_baseline.clear()
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
        self.started = time.monotonic()
        self.current_phase = "starting"
        self.phase_started = self.started
        self.phase_details = {}
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
        with self.resources._cv:
            previous = self.current_phase, self.phase_started, self.phase_details
            self.current_phase, self.phase_started = phase, started
            self.phase_details = fields
        success = False
        try:
            trace = self.resources.trace
            with trace.activity("phase." + phase) if trace is not None else nullcontext():
                yield
            success = True
        finally:
            elapsed = time.monotonic() - started
            self.timings[phase] = self.timings.get(phase, 0.0) + elapsed
            with self.resources._cv:
                self.current_phase, self.phase_started, self.phase_details = previous
            if phase == "total":
                self.resources.emit("install.work.completed", row=self.row, name=self.name,
                                    seconds={key: round(value, 4) for key, value in self.timings.items()},
                                    success=success, **fields)

    def snapshot(self, now):
        return {"row": self.row, "name": self.name, "phase": self.current_phase,
                "age_seconds": round(now - self.started, 2),
                "phase_seconds": round(now - self.phase_started, 2),
                **self.phase_details}

    @contextmanager
    def extraction(self, probe, directory):
        started = time.monotonic()
        memory = self.resources.memory
        cost = probe.memory_bytes
        large = max(probe.compressed_size, probe.uncompressed_size) >= 1024 ** 3
        with self.phase("memory_wait"):
            memory.acquire(cost, cancel=self.stop, large=large, on_wait=self.on_wait)
        try:
            with ExitStack() as reservations:
                with self.phase("space_wait"):
                    reservations.enter_context(self.resources.space.reserve(
                        directory, probe.uncompressed_size, self.stop, on_wait=self.on_wait))
                self.resources.emit("install.work.admitted", row=self.row, name=self.name,
                                    wait_seconds=round(time.monotonic() - started, 4),
                                    memory_bytes=cost, expanded_bytes=probe.uncompressed_size)
                if self.resources.trace is not None:
                    self.resources.trace.wait(
                        "extract_memory_space", time.monotonic() - started)
                with self.phase("extraction", compressed_bytes=probe.compressed_size,
                                expanded_bytes=probe.uncompressed_size,
                                cpu_threads=self.resources.cpu_threads):
                    yield
        finally:
            memory.release(cost, large=large)
