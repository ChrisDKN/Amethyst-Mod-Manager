"""run_in_worker - start a daemon thread that computes a result and hands it
back to the UI thread via a Signal.

The standard pattern in gui_qt is: nested ``worker()`` → ``threading.Thread``
→ ``safe_emit(done_sig, result)``. This helper replaces that scaffold for the
common compute-one-result case; workers that stream progress or emit several
different Signals keep their hand-rolled form.

The emit goes through safe_emit, so a result arriving after the owning view
was closed is dropped instead of raising into the worker thread. Exceptions
from *fn* are written to the app log (visible log panel, thread-safe).
"""

from __future__ import annotations

import threading

from gui_qt.safe_emit import safe_emit

#: Pass as ``error_result`` when a failed fn should emit nothing at all.
NO_EMIT = object()


class LatestWorker:
    """Run at most one expensive job at a time and retain only the newest.

    Preview-style UIs can produce work much faster than it can be completed
    (scrubbing through meshes, changing filters, switching texture sources).
    Generation counters stop stale results reaching the UI, but starting one
    thread per request still lets all of the stale I/O and decoding run.  This
    small daemon runner serialises the active job and replaces any queued job
    with the newest submission.
    """

    def __init__(self, name: str = "latest-worker"):
        self.name = name
        self._lock = threading.Lock()
        self._pending = None
        self._running = False

    def submit(self, fn) -> None:
        """Schedule ``fn()``; replace a not-yet-started older function."""
        with self._lock:
            self._pending = fn
            if self._running:
                return
            self._running = True
        threading.Thread(target=self._drain, daemon=True,
                         name=self.name).start()

    def discard_pending(self) -> None:
        """Drop the queued job.  A currently-running function is cooperative."""
        with self._lock:
            self._pending = None

    def _drain(self) -> None:
        while True:
            with self._lock:
                fn = self._pending
                self._pending = None
                if fn is None:
                    self._running = False
                    return
            try:
                fn()
            except Exception as exc:                     # noqa: BLE001
                from Utils.app_log import app_log
                app_log(f"[worker:{self.name}] {exc}")


def run_in_worker(fn, done_sig=None, *, name: str | None = None,
                  error_result=None, unpack: bool = False) -> threading.Thread:
    """Run ``fn()`` on a daemon thread, then ``safe_emit(done_sig, result)``.

    *done_sig* may be None for fire-and-forget work (errors still get
    logged). If *fn* raises, *error_result* is emitted instead - pass
    ``NO_EMIT`` to skip the emit on failure. With ``unpack=True`` a tuple
    result is splatted across a multi-argument Signal.
    """
    def _worker():
        try:
            result = fn()
        except Exception as exc:
            from Utils.app_log import app_log
            app_log(f"[worker:{name or getattr(fn, '__name__', 'fn')}] {exc}")
            result = error_result
        if done_sig is None or result is NO_EMIT:
            return
        if unpack and isinstance(result, tuple):
            safe_emit(done_sig, *result)
        else:
            safe_emit(done_sig, result)

    t = threading.Thread(target=_worker, daemon=True, name=name)
    t.start()
    return t
