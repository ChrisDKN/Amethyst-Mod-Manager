from __future__ import annotations

import threading


class DownloadControl:
    def __init__(self):
        self._cancelled = threading.Event()
        self._paused = False
        self._condition = threading.Condition()

    def is_set(self) -> bool:
        return self._cancelled.is_set()

    def set(self) -> None:
        self.cancel()

    def wait(self, timeout: float | None = None) -> bool:
        return self._cancelled.wait(timeout)

    def cancel(self) -> None:
        self._cancelled.set()
        with self._condition:
            self._condition.notify_all()

    def pause(self) -> None:
        with self._condition:
            if not self._cancelled.is_set():
                self._paused = True

    def resume(self) -> None:
        with self._condition:
            self._paused = False
            self._condition.notify_all()

    def is_paused(self) -> bool:
        with self._condition:
            return self._paused and not self._cancelled.is_set()

    def wait_if_paused(self) -> bool:
        with self._condition:
            while self._paused and not self._cancelled.is_set():
                self._condition.wait()
        return self._cancelled.is_set()


def wait_if_paused(control) -> bool:
    wait = getattr(control, "wait_if_paused", None)
    return bool(wait()) if callable(wait) else bool(
        control is not None and control.is_set())
