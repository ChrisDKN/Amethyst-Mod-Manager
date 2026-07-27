"""Global download bandwidth limiter (toolkit-neutral).

A single token-bucket shared by EVERY download thread in the app — the
collection installer's parallel workers, single nxm/Downloads-tab downloads
and the collection-archive fetch all call :func:`throttle` from their
streaming loops, so the configured cap applies to the aggregate transfer
rate, not per-connection.

Throttling works by sleeping between socket reads: requests' ``iter_content``
only pulls from the socket when the loop asks for the next chunk, so pausing
the consumer applies TCP backpressure and genuinely slows the sender.

The limit is persisted in amethyst.ini (see ``Utils.ui_config``) and loaded
lazily on first use; the UI applies changes live via :func:`set_limit_mbps`
(0 = unlimited).
"""

from __future__ import annotations

import threading
import time

# Sleep in short slices so a cancel event and live limit changes are honoured
# quickly even when a large chunk earned a multi-second debt.
_SLEEP_SLICE = 0.2

# Positive tokens (burst credit) are capped at this many seconds' worth of the
# configured rate, so an idle period doesn't bank an unbounded burst.
_BURST_SECONDS = 0.5

_lock = threading.Lock()
_rate = 0.0          # bytes/sec; 0 = unlimited
_tokens = 0.0        # current bucket level; may go negative (debt to sleep off)
_last = 0.0          # monotonic timestamp of the last refill
_initialized = False


def _ensure_init_locked() -> None:
    global _initialized, _rate
    if _initialized:
        return
    _initialized = True
    try:
        from Utils.ui_config import load_download_speed_limit
        _rate = max(0.0, float(load_download_speed_limit())) * 1024 * 1024
    except Exception:
        _rate = 0.0


def set_limit_mbps(mbps: float) -> None:
    """Set the global cap in MB/s. 0 (or negative) = unlimited. Applies
    immediately to in-flight downloads."""
    global _rate, _tokens, _last, _initialized
    with _lock:
        _initialized = True
        _rate = max(0.0, float(mbps or 0)) * 1024 * 1024
        # Reset the bucket so an old debt/credit from a different rate doesn't
        # produce a stall or a burst right after the change.
        _tokens = 0.0
        _last = time.monotonic()


def throttle(nbytes: int, cancel: "threading.Event | None" = None) -> None:
    """Account *nbytes* just transferred and sleep as needed to keep the
    aggregate rate at the configured cap. No-op when unlimited. Returns
    early (without sleeping off the full debt) if *cancel* is set."""
    global _tokens, _last
    if nbytes <= 0:
        return
    while True:
        with _lock:
            _ensure_init_locked()
            rate = _rate
            if rate <= 0:
                return
            now = time.monotonic()
            if _last <= 0:
                _last = now
            _tokens = min(_tokens + (now - _last) * rate, rate * _BURST_SECONDS)
            _last = now
            if nbytes:
                # Consume immediately (tokens may go negative); the debt is
                # slept off below, shared naturally across worker threads.
                _tokens -= nbytes
                nbytes = 0
            deficit = -_tokens
            if deficit <= 0:
                return
            wait = deficit / rate
        if cancel is not None and cancel.is_set():
            return
        time.sleep(min(wait, _SLEEP_SLICE))
        if wait <= _SLEEP_SLICE:
            return
