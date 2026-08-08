"""Toolkit-neutral watcher for browser ("Slow") downloads of a Nexus mod.

Free (non-premium) accounts can't use the API download endpoints, so the
browser Install flow opens the mod's files page for the site's download
buttons. "Download with Mod Manager" comes back to us via nxm:// links;
"Slow download" just saves through the browser with no notification at all.
This watcher covers the second path, like the manual collection installer:
poll the system Downloads dir + the user's extra download locations until an
archive matching one of the mod's files is complete on disk, then report it
so the caller can hand it to the install queue.

The game CACHE dirs are deliberately NOT watched - the app's own downloaders
(premium API, nxm) write there and install through their own pipelines, so
watching them would install the same archive twice.

Callbacks fire on the watcher THREAD - Qt callers must marshal (safe_emit).
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable

from Utils.download_locations import (
    is_default_downloads_disabled, load_extra_download_locations)
from Nexus.nexus_download import _find_cached_archive, _get_downloads_dir

# In-flight browser download names (exact final name + suffix): Firefox
# .part, Chromium .crdownload, Safari .download. Only used to surface
# progress; detection itself waits for the completed final file.
_PARTIAL_SUFFIXES = (".part", ".crdownload", ".download")

_POLL_S = 2.0
# Give up after this long with NO change in the watched folders. Any change
# (a growing partial included) re-arms it, so a slow multi-GB free download
# never times out mid-transfer.
_IDLE_TIMEOUT_S = 15 * 60.0


def scan_download_dirs() -> "list[Path]":
    """Folders watched for the browser download: the system Downloads dir
    (unless disabled in download locations) + the extra download locations.
    Re-evaluated every poll - cheap, and picks up newly added locations."""
    dirs: list[Path] = []
    seen: set = set()
    if not is_default_downloads_disabled():
        d = _get_downloads_dir()
        try:
            if d.is_dir():
                dirs.append(d)
                seen.add(d.resolve())
        except OSError:
            pass
    for loc in load_extra_download_locations():
        p = Path(loc).expanduser()
        try:
            rp = p.resolve()
            if rp not in seen and p.is_dir():
                dirs.append(p)
                seen.add(rp)
        except OSError:
            continue
    return dirs


def _expected_size(f) -> int:
    """Expected archive bytes for a NexusModFile (same fallback the premium
    download path uses)."""
    return int((getattr(f, "size_in_bytes", 0) or 0)
               or (getattr(f, "size_kb", 0) or 0) * 1024)


def _match_name(f) -> str:
    """Name to match the downloaded archive against. Normally the API
    ``file_name``; for newer Nexus uploads GraphQL returns a CDN UUID *path*
    there (``ed/8d/27/…``) - useless for matching - so fall back to the
    display ``name``."""
    fn = (getattr(f, "file_name", "") or "").strip()
    if fn and "/" not in fn:
        return fn
    return (getattr(f, "name", "") or "").strip()


def find_existing_archive(mod_id: int, files: list) -> "tuple[Path, object] | None":
    """Return ``(path, file)`` for an already-complete cached archive matching
    any of *files*, or ``None`` if nothing is on disk yet.

    Same matching the watcher uses on its first poll - factored out so callers
    can short-circuit the browser-download flow: if the archive is already
    downloaded there is no need to open the Nexus download page at all, just
    install it directly. *files* are NexusModFile-likes (see the watcher)."""
    picks = [f for f in (files or []) if _match_name(f)]
    if not picks:
        return None
    mid = int(mod_id or 0)
    for folder in scan_download_dirs():
        for f in picks:
            found, complete = _find_cached_archive(
                folder, _match_name(f), _expected_size(f), mid,
                int(getattr(f, "file_id", 0) or 0))
            if found is not None and complete:
                return found, f
    return None


def start_manual_install(
    *, api, game_domain: str, mod_id: int, files: list,
    open_url_fn: Callable[[str], None],
    log_fn: Callable[[str], None] = lambda _m: None,
    log_label: str = "",
    mod_info=None,
    mod_info_fallback=None,
    on_archive: Callable[[Path, object, object], None] = lambda p, m, f: None,
    on_progress: Callable[[int, int], None] = lambda d, t: None,
    on_timeout: Callable[[], None] = lambda: None,
) -> "tuple[ManualDownloadWatcher, bool]":
    """Shared non-premium install flow (Nexus browser / Change Version /
    Reinstall - one implementation so new callers stay consistent).

    If a matching archive is already in the download folders the browser is
    NOT opened; otherwise the first file's own download page (file_id
    deep-link) is. Either way a ManualDownloadWatcher is armed - an existing
    archive is accepted on its first poll, so ``on_archive`` fires immediately
    for the already-downloaded case.

    ``on_archive(path, meta, file)`` runs on the WATCHER thread with a
    prebuilt NexusModMeta (or None on failure): ``mod_info`` is used verbatim
    when given (caller already has a NexusModInfo, e.g. a browser card entry);
    otherwise it is fetched via GraphQL with ``mod_info_fallback`` as the
    backstop. ``on_progress(done, total)`` / ``on_timeout()`` also run on the
    watcher thread - marshal to the UI thread in the callbacks.

    ``log_label`` names the download in log lines (defaults to the first
    file's match name). Returns ``(watcher, already_downloaded)`` - the
    watcher is already started; keep it for cancel support."""
    from Nexus.nexus_meta import build_meta_from_download

    already = find_existing_archive(mod_id, files) is not None
    label = log_label or (_match_name(files[0]) if files else "")
    if already:
        log_fn(f"Nexus: archive already downloaded for '{label}'; "
               "installing without opening the browser.")
    else:
        fid = int(getattr(files[0], "file_id", 0) or 0) if files else 0
        open_url_fn(f"https://www.nexusmods.com/{game_domain}/mods/{mod_id}"
                    f"?tab=files&file_id={fid}")
        log_fn("Nexus: premium required for direct download - opened the "
               f"download page for '{label}'. It will install automatically "
               "once the browser download finishes.")

    def _found(path, file):
        info = mod_info
        if info is None:
            if api is not None:
                try:
                    fetched, _ = api.get_mod_and_file_info_graphql(
                        game_domain, mod_id,
                        int(getattr(file, "file_id", 0) or 0))
                    info = fetched if fetched is not None else mod_info_fallback
                except Exception:
                    info = mod_info_fallback
            else:
                info = mod_info_fallback
        try:
            meta = build_meta_from_download(
                game_domain=game_domain, mod_id=mod_id,
                file_id=int(getattr(file, "file_id", 0) or 0),
                archive_name=path.name, mod_info=info, file_info=file)
        except Exception:
            meta = None
        log_fn(f"Nexus: found downloaded archive → {path}")
        on_archive(path, meta, file)

    watcher = ManualDownloadWatcher(
        mod_id=mod_id, files=files, on_found=_found,
        on_progress=on_progress, on_timeout=on_timeout)
    watcher.start()
    return watcher, already


class ManualDownloadWatcher:
    """Poll the download folders for one mod's browser-downloaded archive.

    *files* is the mod's full file list (NexusModFile-likes; only
    ``file_name``/``file_id``/size fields are read). The first file whose
    archive appears complete wins - the user picks the actual file on the
    website, so every listed file is an acceptable match. A matching archive
    that already exists is accepted immediately (manual collection installer
    parity: "already downloaded" is a valid answer).

    Callbacks (all on the watcher thread):
      on_found(path, file)        - a complete archive matched *file*
      on_progress(done, total)    - an in-flight download's bytes (best match)
      on_timeout()                - gave up (idle timeout / nothing to watch)
    """

    def __init__(self, *, mod_id: int, files: list,
                 on_found: Callable[[Path, object], None],
                 on_progress: Callable[[int, int], None] = lambda d, t: None,
                 on_timeout: Callable[[], None] = lambda: None,
                 poll_s: float = _POLL_S,
                 idle_timeout_s: float = _IDLE_TIMEOUT_S):
        self._mod_id = int(mod_id or 0)
        self._files = [f for f in (files or []) if _match_name(f)]
        self._on_found = on_found
        self._on_progress = on_progress
        self._on_timeout = on_timeout
        self._poll_s = poll_s
        self._idle_timeout_s = idle_timeout_s
        self._stop = threading.Event()
        self._thread: "threading.Thread | None" = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, daemon=True,
            name=f"nexus-manual-watch-{self._mod_id}")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    # -- internals ----------------------------------------------------------
    @staticmethod
    def _folder_sig(folder: Path):
        """Cheap change signature: (name, size) of every file in *folder*.
        Sizes are included so a growing download (partial or final-named)
        keeps the signature changing."""
        try:
            return tuple(sorted(
                (e.name, e.stat().st_size) for e in folder.iterdir()
                if e.is_file()))
        except OSError:
            return None

    def _partial_bytes(self, folder: Path) -> int:
        """Bytes of an in-flight browser download for this mod.

        Browsers name the temp file after the *download* name (which, for the
        newer Nexus website naming, contains the mod id as a space-delimited
        token) - NOT after the API ``file_name``.  So match any in-progress
        temp file (``.part``/``.crdownload``/``.download``) whose name carries
        this mod's id.  Cosmetic only (drives the progress card); completion
        detection is done by _find_cached_archive on the finished archive.
        """
        mid = str(self._mod_id)
        best = 0
        try:
            for e in folder.iterdir():
                if not e.is_file():
                    continue
                low = e.name.lower()
                if not any(low.endswith(s) for s in _PARTIAL_SUFFIXES):
                    continue
                if mid and mid not in e.name:
                    continue
                try:
                    best = max(best, e.stat().st_size)
                except OSError:
                    pass
        except OSError:
            pass
        return best

    def _run(self) -> None:
        files = self._files
        if not files:
            self._on_timeout()
            return
        sigs: dict[str, object] = {}
        deadline = time.monotonic() + self._idle_timeout_s
        best_done = 0
        while not self._stop.is_set():
            prog_done = prog_total = 0
            any_change = False
            for folder in scan_download_dirs():
                sig = self._folder_sig(folder)
                key = str(folder)
                if sigs.get(key, ()) == sig:
                    continue        # nothing changed in this folder
                sigs[key] = sig
                any_change = True
                # In-flight browser temp file for this mod (name shape unknown,
                # so matched by mod-id token) → progress card. Per folder, not
                # per file, since we can't tell which file a .part belongs to.
                part_sz = self._partial_bytes(folder)
                for f in files:
                    exp = _expected_size(f)
                    found, complete = _find_cached_archive(
                        folder, _match_name(f), exp, self._mod_id,
                        int(getattr(f, "file_id", 0) or 0))
                    if found is not None:
                        if complete:
                            if not self._stop.is_set():
                                self._on_found(found, f)
                            return
                        try:        # incomplete final-named file → progress
                            sz = found.stat().st_size
                        except OSError:
                            sz = 0
                        if exp > 0 and sz > prog_done:
                            prog_done, prog_total = sz, exp
                    if exp > 0 and part_sz > prog_done:
                        prog_done, prog_total = part_sz, exp
            if any_change:
                deadline = time.monotonic() + self._idle_timeout_s
                if prog_done > best_done:
                    best_done = prog_done
                    self._on_progress(prog_done, prog_total)
            if time.monotonic() >= deadline:
                self._on_timeout()
                return
            self._stop.wait(self._poll_s)
