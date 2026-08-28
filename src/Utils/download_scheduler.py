"""Double-ended download dispatch for collection installs (toolkit-neutral).

Problem: with N equal download workers all pulling from one size-sorted list,
many tiny archives finish faster than the Nexus API can hand out the next CDN
link, so the queue stutters and bandwidth sits idle until a big mod happens to
land in a slot.

Fix: dedicate ONE worker to the largest-remaining mods (it stays busy on long
transfers that keep the pipe full) and let the other workers burn through the
smallest-remaining mods from the other end. They converge in the middle, so
every mod is dispatched exactly once and the link-fetch latency of the small
mods is hidden behind the big worker's ongoing transfer.

This module only owns the *dispatch order*; the actual per-mod work (link
fetch, download, hand-off to the install queue) is the caller's `work` fn.
"""

from __future__ import annotations

import queue as _queue
import threading
from typing import Any, Callable, Iterable


def order_by_size(mods: Iterable, size_key: Callable[[object], int] | None = None
                  ) -> list:
    """Return *mods* sorted smallest→largest by size.

    Mods that don't report a size (``size_bytes`` 0/missing - some Nexus files
    omit it) are sorted to the END, not the front: their real size is unknown and
    could be large, so we download the known-small mods first and leave the
    unknowns for last (rather than letting a big unknown-size mod jump the queue
    and hog a slot while everything small waits behind it)."""
    if size_key is None:
        def size_key(m):
            return getattr(m, "size_bytes", 0) or 0
    # (0 = unknown → sort last) via a (is_unknown, size) key.
    return sorted(mods, key=lambda m: (size_key(m) <= 0, size_key(m)))


def run_double_ended(mods: list, work: Callable[[object], None], workers: int,
                     *, stop: "threading.Event | None" = None,
                     spawn: Callable[[Callable, str], object] | None = None
                     ) -> None:
    """Dispatch *mods* to *work* using the double-ended policy and block until
    every mod has been processed (or *stop* is set).

    *mods*    - the download units, PRE-SORTED smallest→largest
                (see :func:`order_by_size`).
    *work*    - called once per mod on a worker thread: ``work(mod)``.
    *workers* - total worker threads (>=1). One is the "large" worker pulling
                from the tail; the rest pull from the head.
    *stop*    - optional cancel event; when set, workers drain without calling
                *work* on the remainder (the caller's *work* still runs for
                already-claimed items and is expected to short-circuit on stop).
    *spawn*   - optional ``spawn(target, name) -> thread-like`` with ``.start``/
                ``.join`` (defaults to daemon ``threading.Thread``); lets a test
                inject deterministic threading.

    A single shared [lo, hi] cursor over the sorted list guarantees each mod is
    claimed once. The large worker takes ``mods[hi]`` then hi-=1; the small
    workers take ``mods[lo]`` then lo+=1. When the ranges cross, everyone stops.
    """
    n = len(mods)
    if n == 0:
        return
    workers = max(1, int(workers))

    lock = threading.Lock()
    cursor = {"lo": 0, "hi": n - 1}

    def _claim(from_tail: bool):
        """Claim the next mod for this worker, or None when exhausted."""
        with lock:
            if cursor["lo"] > cursor["hi"]:
                return None
            if from_tail:
                m = mods[cursor["hi"]]
                cursor["hi"] -= 1
            else:
                m = mods[cursor["lo"]]
                cursor["lo"] += 1
            return m

    def _worker(from_tail: bool):
        while True:
            if stop is not None and stop.is_set():
                # Drain the rest without doing work so the caller's join
                # returns promptly; already-claimed items are handled by work.
                _drain_remaining(work, cursor, lock, mods, stop)
                return
            mod = _claim(from_tail)
            if mod is None:
                return
            work(mod)

    if spawn is None:
        def spawn(target, name):
            return threading.Thread(target=target, name=name, daemon=True)

    threads = []
    # Exactly one large-end worker (unless there's only a single worker, in
    # which case it must still cover the whole list - it pulls from the head so
    # small-first behaviour is preserved when workers==1).
    for i in range(workers):
        from_tail = (workers > 1 and i == 0)
        t = spawn(lambda ft=from_tail: _worker(ft),
                  f"col-dl-{'big' if from_tail else 'small'}-{i}")
        threads.append(t)
    for t in threads:
        t.start()
    for t in threads:
        t.join()


def run_pipelined(mods: list, fetch: Callable[[object], Any],
                  download: Callable[[object, Any], None],
                  dl_workers: int, *, link_workers: int = 2,
                  large_workers: int = 1,
                  stop: "threading.Event | None" = None,
                  worker_done: "Callable[[], None] | None" = None,
                  spawn: Callable[[Callable, str], object] | None = None
                  ) -> None:
    """Two-stage double-ended dispatch with prefetched signed CDN links.

    Root problem this solves: with a single fetch-link→download loop per worker,
    every worker blocks on a ``get_download_links`` round-trip between mods. For
    tiny archives that latency is comparable to the download itself and all the
    workers hit it in lockstep, so the pipe stutters - downloads arrive at the
    installer in bursts of *dl_workers* with idle gaps between. Pipelining the
    link fetch hides that latency behind other in-flight downloads. Reserving a
    lane for the largest remaining archives also overlaps that small-file churn
    with a long transfer that can keep the connection busy.

    *mods*        - PRE-SORTED smallest→largest (see :func:`order_by_size`).
    *fetch*       - ``fetch(mod) -> links``, called on a link-worker thread;
                    whatever it returns is passed straight to *download* as-is
                    (return None/[] to let *download* fetch links itself). May
                    raise - the mod still flows to *download* with ``links=None``
                    so the caller's per-mod bookkeeping (counters, install-queue
                    sentinels) still fires exactly once.
    *download*    - ``download(mod, links)``, called on a download-worker thread
                    once per mod, exactly once. May raise - the exception is
                    swallowed so the worker (and the pipeline) keeps flowing.
    *dl_workers*  - number of download-worker threads (>=1).
    *link_workers*- number of link-fetch threads (>=1) available across both
                    lanes.
    *large_workers* - download lanes reserved for the largest remaining mods.
    *stop*        - optional cancel event; when set, both stages drain the
                    remainder (feeding *download* with ``links=None``) so every
                    mod is still handed off once and the caller short-circuits.
    *worker_done* - optional cleanup called once on each download-worker thread.
    *spawn*       - optional ``spawn(target, name) -> thread-like`` for tests.

    Each lane has a bounded ready queue so links are only minted a little ahead
    of consumption. The small and large fetchers share one double-ended cursor,
    guaranteeing that each mod is claimed exactly once.
    """
    n = len(mods)
    if n == 0:
        return
    dl_workers = max(1, int(dl_workers))
    link_workers = max(1, int(link_workers))
    large_workers = max(0, int(large_workers))
    if dl_workers < 2:
        large_workers = 0
    else:
        large_workers = min(large_workers, dl_workers - 1)
    small_workers = dl_workers - large_workers

    lock = threading.Lock()
    cursor = {"lo": 0, "hi": n - 1}
    _READY_DONE = object()

    def _claim(from_tail: bool):
        with lock:
            if cursor["lo"] > cursor["hi"]:
                return None, True
            if from_tail:
                mod = mods[cursor["hi"]]
                cursor["hi"] -= 1
            else:
                mod = mods[cursor["lo"]]
                cursor["lo"] += 1
            return mod, False

    def _fetcher(ready, from_tail: bool, claim_gate=None):
        while True:
            if claim_gate is not None:
                claim_gate.acquire()
            mod, exhausted = _claim(from_tail)
            if exhausted:
                if claim_gate is not None:
                    claim_gate.release()
                return
            links = None
            if stop is None or not stop.is_set():
                try:
                    links = fetch(mod)
                except Exception:
                    links = None
            # Enqueue even when stopping so the downloader still hands the mod
            # off once (caller bookkeeping) - the download fn short-circuits.
            ready.put((mod, links))

    def _downloader(ready, claim_gate=None):
        try:
            while True:
                item = ready.get()
                if item is _READY_DONE:
                    return
                mod, links = item
                try:
                    download(mod, links)
                except Exception:
                    # A dead worker wedges the pipeline because fetchers can
                    # block on a full ready queue. Drop this mod and keep going.
                    pass
                finally:
                    if claim_gate is not None:
                        claim_gate.release()
        finally:
            if worker_done is not None:
                try:
                    worker_done()
                except Exception:
                    pass

    if spawn is None:
        def spawn(target, name):
            return threading.Thread(target=target, name=name, daemon=True)

    large_link_workers = (min(large_workers, max(1, link_workers - 1))
                          if large_workers else 0)
    small_link_workers = max(1, link_workers - large_link_workers)
    lanes = []
    if large_workers:
        # Do not let the tail prefetcher reserve several large files serially.
        lanes.append(("large", True, large_workers, large_link_workers,
                      threading.Semaphore(large_workers)))
    lanes.append(("small", False, small_workers, small_link_workers, None))

    running = []
    for name, from_tail, worker_count, fetch_count, claim_gate in lanes:
        ready = _queue.Queue(maxsize=worker_count + fetch_count)
        fetchers = [
            spawn(lambda q=ready, tail=from_tail, gate=claim_gate:
                  _fetcher(q, tail, gate),
                  f"col-link-{name}-{i}")
            for i in range(fetch_count)
        ]
        downloaders = [
            spawn(lambda q=ready, gate=claim_gate: _downloader(q, gate),
                  f"col-dl-{name}-{i}")
            for i in range(worker_count)
        ]
        running.append((ready, fetchers, downloaders))

    for _ready, _fetchers, downloaders in running:
        for thread in downloaders:
            thread.start()
    for _ready, fetchers, _downloaders in running:
        for thread in fetchers:
            thread.start()

    for ready, fetchers, downloaders in running:
        for thread in fetchers:
            thread.join()
        for _ in downloaders:
            ready.put(_READY_DONE)
    for _ready, _fetchers, downloaders in running:
        for thread in downloaders:
            thread.join()


def _drain_remaining(work, cursor, lock, mods, stop):
    """After a cancel, feed every remaining mod to *work* so the caller's
    per-mod bookkeeping (counters, install-queue sentinels) still fires - work
    is expected to no-op the actual download when *stop* is set."""
    while True:
        with lock:
            if cursor["lo"] > cursor["hi"]:
                return
            m = mods[cursor["lo"]]
            cursor["lo"] += 1
        work(m)
