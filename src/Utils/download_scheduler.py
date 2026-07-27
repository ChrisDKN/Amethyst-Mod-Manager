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

    Mods that don't report a size (``size_bytes`` 0/missing — some Nexus files
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

    *mods*    — the download units, PRE-SORTED smallest→largest
                (see :func:`order_by_size`).
    *work*    — called once per mod on a worker thread: ``work(mod)``.
    *workers* — total worker threads (>=1). One is the "large" worker pulling
                from the tail; the rest pull from the head.
    *stop*    — optional cancel event; when set, workers drain without calling
                *work* on the remainder (the caller's *work* still runs for
                already-claimed items and is expected to short-circuit on stop).
    *spawn*   — optional ``spawn(target, name) -> thread-like`` with ``.start``/
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
    # which case it must still cover the whole list — it pulls from the head so
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
                  stop: "threading.Event | None" = None,
                  spawn: Callable[[Callable, str], object] | None = None
                  ) -> None:
    """Two-stage smallest-first dispatch: fetch each mod's signed CDN link on a
    small dedicated pool AHEAD of the download workers, so a download worker
    that just finished a tiny archive finds the next link already waiting and
    starts transferring bytes with ZERO link-fetch latency.

    Root problem this solves: with a single fetch-link→download loop per worker,
    every worker blocks on a ``get_download_links`` round-trip between mods. For
    tiny archives that latency is comparable to the download itself and all the
    workers hit it in lockstep, so the pipe stutters — downloads arrive at the
    installer in bursts of *dl_workers* with idle gaps between. Pipelining the
    link fetch hides that latency behind other in-flight downloads.

    *mods*        — PRE-SORTED smallest→largest (see :func:`order_by_size`);
                    both stages honour that order (fetchers claim from the head).
    *fetch*       — ``fetch(mod) -> links``, called on a link-worker thread;
                    whatever it returns is passed straight to *download* as-is
                    (return None/[] to let *download* fetch links itself). May
                    raise — the mod still flows to *download* with ``links=None``
                    so the caller's per-mod bookkeeping (counters, install-queue
                    sentinels) still fires exactly once.
    *download*    — ``download(mod, links)``, called on a download-worker thread
                    once per mod, exactly once, in the same smallest-first order
                    the fetchers claimed.
    *dl_workers*  — number of download-worker threads (>=1).
    *link_workers*— number of link-fetch threads (>=1). Small (2–3) is enough to
                    stay a step ahead; keeping it low bounds how far ahead links
                    are minted so signed CDN URLs never go stale before use.
    *stop*        — optional cancel event; when set, both stages drain the
                    remainder (feeding *download* with ``links=None``) so every
                    mod is still handed off once and the caller short-circuits.
    *spawn*       — optional ``spawn(target, name) -> thread-like`` for tests.

    The ready queue is bounded to ``dl_workers + link_workers`` so the fetchers
    stay only a step ahead of consumption — enough to keep every download slot
    fed, not so far ahead that links expire.
    """
    n = len(mods)
    if n == 0:
        return
    dl_workers = max(1, int(dl_workers))
    link_workers = max(1, int(link_workers))

    lock = threading.Lock()
    cursor = {"lo": 0}
    ready: _queue.Queue = _queue.Queue(maxsize=dl_workers + link_workers)
    _READY_DONE = object()

    def _claim():
        with lock:
            if cursor["lo"] >= n:
                return None, True
            mod = mods[cursor["lo"]]
            cursor["lo"] += 1
            return mod, False

    def _fetcher():
        while True:
            mod, exhausted = _claim()
            if exhausted:
                return
            links = None
            if stop is None or not stop.is_set():
                try:
                    links = fetch(mod)
                except Exception:
                    links = None
            # Enqueue even when stopping so the downloader still hands the mod
            # off once (caller bookkeeping) — the download fn short-circuits.
            ready.put((mod, links))

    def _downloader():
        while True:
            item = ready.get()
            if item is _READY_DONE:
                return
            mod, links = item
            download(mod, links)

    if spawn is None:
        def spawn(target, name):
            return threading.Thread(target=target, name=name, daemon=True)

    fetchers = [spawn(_fetcher, f"col-link-{i}") for i in range(link_workers)]
    downloaders = [spawn(_downloader, f"col-dl-{i}") for i in range(dl_workers)]
    for t in downloaders:
        t.start()
    for t in fetchers:
        t.start()

    # Once every fetcher has drained the mod list, no more real items will be
    # enqueued — feed one sentinel per download worker so they exit cleanly.
    def _closer():
        for t in fetchers:
            t.join()
        for _ in downloaders:
            ready.put(_READY_DONE)

    closer = spawn(_closer, "col-link-close")
    closer.start()
    closer.join()
    for t in downloaders:
        t.join()


def _drain_remaining(work, cursor, lock, mods, stop):
    """After a cancel, feed every remaining mod to *work* so the caller's
    per-mod bookkeeping (counters, install-queue sentinels) still fires — work
    is expected to no-op the actual download when *stop* is set."""
    while True:
        with lock:
            if cursor["lo"] > cursor["hi"]:
                return
            m = mods[cursor["lo"]]
            cursor["lo"] += 1
        work(m)
