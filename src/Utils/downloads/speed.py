from __future__ import annotations

import threading
import time
from collections import deque


class RollingDownloadSpeed:
    def __init__(self, *, window=10.0, idle_timeout=3.0,
                 warmup=1.0, clock=time.monotonic):
        self.window = max(float(window), 0.1)
        self.idle_timeout = max(float(idle_timeout), 0.1)
        self.warmup = max(float(warmup), 0.0)
        self._clock = clock
        self._lock = threading.Lock()
        self._samples = deque()
        self._bytes = 0
        self._last_transfer = None

    @property
    def total_bytes(self):
        with self._lock:
            return self._bytes

    def add(self, count, *, now=None):
        count = max(0, int(count or 0))
        if not count:
            return
        with self._lock:
            now = self._clock() if now is None else float(now)
            if (self._last_transfer is None
                    or now - self._last_transfer >= self.idle_timeout):
                self._samples.clear()
                self._samples.append((now, self._bytes))
            self._bytes += count
            self._last_transfer = now

    def rate(self, *, now=None):
        with self._lock:
            now = self._clock() if now is None else float(now)
            if (self._last_transfer is None
                    or now - self._last_transfer >= self.idle_timeout):
                self._samples.clear()
                self._samples.append((now, self._bytes))
                return 0.0
            self._samples.append((now, self._bytes))
            cutoff = now - self.window
            while len(self._samples) > 1 and self._samples[1][0] <= cutoff:
                self._samples.popleft()
            started, initial = self._samples[0]
            elapsed = now - started
            if elapsed < self.warmup:
                return 0.0
            return max(0, self._bytes - initial) / max(elapsed, 0.1)
