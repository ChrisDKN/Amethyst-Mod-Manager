"""
nexus_download.py
Download manager for files from Nexus Mods CDN.

Handles the full flow:
  1. Resolve CDN links via the API (or from an nxm:// URL)
  2. Stream-download with progress callbacks
  3. Save to the user's Downloads directory (or a custom target)

Usage
-----
    from Nexus.nexus_api import NexusAPI
    from Nexus.nexus_download import NexusDownloader
    from Nexus.nxm_handler import NxmLink

    api = NexusAPI(api_key="...")
    dl  = NexusDownloader(api)

    # Download from an nxm:// link (free user)
    link = NxmLink.parse("nxm://skyrimspecialedition/mods/2014/files/1234?key=abc&expires=999")
    path = dl.download_from_nxm(link, progress_cb=lambda cur, total: print(f"{cur}/{total}"))

    # Direct download (premium user)
    path = dl.download_file("skyrimspecialedition", 2014, 1234)
"""

from __future__ import annotations

import os
import re
import threading
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import requests

from .nexus_api import NexusAPI, NexusDownloadLink, NexusAPIError
from .nxm_handler import NxmLink
from Utils.downloads import bandwidth
from Utils.app_log import app_log
from Utils.ca_bundle import resolve_ca_bundle
from Utils.environment.xdg import xdg_download_dir

# Default chunk size for streaming downloads (256 KB)
_CHUNK_SIZE = 256 * 1024

# Archive extensions recognised for cache lookups
_ARCHIVE_EXTS = ('.zip', '.7z', '.rar', '.tar.gz', '.tar.bz2', '.tar.xz', '.tar')


def _clean_nexus_stem(stem: str, mod_id_str: str) -> str:
    """Strip the Nexus trailing metadata (``{modId} version timestamp``) from
    an archive stem, returning just the display-name portion.

    Handles both name shapes Nexus produces:
      * manager / CDN, hyphen-joined:
        ``"FDE Ysolda-124787-2-0-1725289331"`` → ``"FDE Ysolda"``
      * newer website "slow download", space-joined (with a random suffix):
        ``"powerofthree's Tweaks 51073 1.16.0 2026-07-12T16-59Z 587RqTEnz"``
        → ``"powerofthree's Tweaks"``

    The mod id is the anchor: everything from the mod-id *token* onward is
    Nexus metadata regardless of the delimiter.  Falls back to the full stem
    if the mod_id isn't present as a delimited token (so an id that merely
    appears inside the name doesn't truncate it).
    """
    if not mod_id_str:
        return stem
    # Match the mod id only as a whole token - bounded by a hyphen, space or
    # underscore (or the string ends), so "51073" inside "Mod510732" won't
    # match.  \b would treat "-" as a boundary but not reliably for digits
    # next to other digits, hence the explicit delimiter class.
    m = re.search(rf'(?:^|[-_ ]){re.escape(mod_id_str)}(?:[-_ ]|$)', stem)
    if m and m.start() > 0:
        # Cut at the delimiter just before the mod id (m.start() points at
        # that delimiter when the id isn't at the very start).
        cut = m.start()
        return stem[:cut].rstrip(' -_')
    return stem


def _fileid_sidecar(archive: Path) -> Path:
    """Return the path of the .fileid sidecar for *archive*."""
    return archive.with_suffix(archive.suffix + ".fileid")


def delete_archive_and_sidecar(archive_path: Path) -> None:
    """Remove an archive and its .fileid sidecar if present.

    Safe to call even if either file is missing.  Also drops any cached md5
    entry for this path.
    """
    try:
        archive_path.unlink(missing_ok=True)
        _fileid_sidecar(archive_path).unlink(missing_ok=True)
        _md5_cache_forget(archive_path)
    except Exception:
        pass


def _read_sidecar_file_id(archive: Path) -> int:
    """Return the file_id stored in the sidecar, or 0 if absent/unreadable."""
    try:
        return int(_fileid_sidecar(archive).read_text(encoding="utf-8").strip())
    except Exception:
        return 0


def _write_sidecar_file_id(archive: Path, file_id: int) -> None:
    """Write *file_id* to the sidecar next to *archive*."""
    try:
        _fileid_sidecar(archive).write_text(str(file_id))
    except Exception:
        pass


def ingest_archive_to_cache(path: Path, game_name: str,
                            file_id: int) -> "Path | None":
    """Copy a browser-downloaded archive into the per-game download cache.

    Free-account redownloads land wherever the browser saves; cache lookups
    (meta filename or .fileid sidecar) only scan the cache dirs, so the file
    is copied in and stamped with its sidecar. The source is left where the
    user put it. Returns the cached path, or None on failure. A source
    already inside the destination folder is only stamped, never copied.
    """
    import shutil
    from Utils.config_paths import get_download_cache_dir_for_game
    src = Path(path)
    try:
        if not src.is_file():
            return None
        dest_dir = get_download_cache_dir_for_game(game_name or "")
        dest = dest_dir / src.name
        if src.parent.resolve() != dest_dir.resolve():
            same = dest.is_file() and dest.stat().st_size == src.stat().st_size
            if not same:
                # Stage under a temp name: other threads scan the cache dirs
                # with size heuristics, so the file must appear complete.
                tmp = dest.with_name(dest.name + ".ingest-tmp")
                shutil.copy2(src, tmp)
                os.replace(tmp, dest)
        if file_id:
            _write_sidecar_file_id(dest, int(file_id))
    except OSError:
        return None
    return dest


# -- md5 cache ---------------------------------------------------------------
# Hashing a multi-GB archive is slow, so we cache results in a single JSON
# file inside the app's download cache directory.  Entries are keyed by the
# archive's absolute path and invalidated when size or mtime changes.  We
# deliberately never write alongside the archive itself - that would pollute
# the user's Downloads folder / any external download locations they've
# configured.

_MD5_CACHE_FILE = "md5_cache.json"
_md5_cache_lock = threading.Lock()


def _md5_cache_path() -> Path:
    from Utils.config_paths import get_download_cache_dir
    return get_download_cache_dir() / _MD5_CACHE_FILE


def _md5_cache_load() -> dict:
    try:
        import json
        return json.loads(_md5_cache_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def _md5_cache_save(data: dict) -> None:
    try:
        import json
        from Utils.atomic_write import write_atomic_text
        write_atomic_text(_md5_cache_path(), json.dumps(data))
    except Exception:
        pass


def _md5_cache_key(archive: Path) -> str:
    try:
        return str(archive.resolve())
    except Exception:
        return str(archive)


def _md5_cache_get(archive: Path) -> str:
    """Return the cached md5 for *archive*, or "" if absent/stale."""
    try:
        st = archive.stat()
    except Exception:
        return ""
    key = _md5_cache_key(archive)
    with _md5_cache_lock:
        entry = _md5_cache_load().get(key)
    if not entry:
        return ""
    if entry.get("size") != st.st_size or entry.get("mtime") != int(st.st_mtime):
        return ""
    return (entry.get("md5") or "").lower()


def _md5_cache_put(archive: Path, md5_hex: str) -> None:
    try:
        st = archive.stat()
    except Exception:
        return
    key = _md5_cache_key(archive)
    with _md5_cache_lock:
        data = _md5_cache_load()
        data[key] = {"size": st.st_size, "mtime": int(st.st_mtime), "md5": md5_hex.lower()}
        _md5_cache_save(data)


def _md5_cache_forget(archive: Path) -> None:
    key = _md5_cache_key(archive)
    with _md5_cache_lock:
        data = _md5_cache_load()
        if data.pop(key, None) is not None:
            _md5_cache_save(data)


def _compute_md5(path: Path) -> str:
    """Return the lowercase hex md5 of *path*, or "" on any error."""
    import hashlib
    try:
        h = hashlib.md5()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return ""


def _md5_matches(archive: Path, expected_md5: str) -> bool:
    """Return True if *archive*'s md5 equals *expected_md5*.

    Uses the shared md5 cache (in the app's download cache dir) to avoid
    re-hashing archives we've already verified.  An empty *expected_md5*
    means "no verification requested" and the function returns True.
    """
    if not expected_md5:
        return True
    expected = expected_md5.strip().lower()
    cached = _md5_cache_get(archive)
    if cached:
        return cached == expected
    actual = _compute_md5(archive)
    if actual:
        _md5_cache_put(archive, actual)
    return actual == expected


def _zip_is_intact(path: Path) -> bool:
    """Return True if *path* is a valid, complete ZIP archive.

    Reads only the end-of-central-directory record (last ~22 bytes) so this
    is effectively instant regardless of archive size.  Returns False for
    non-ZIP files so callers can skip the check for .7z/.rar/.tar.*
    """
    if not path.name.lower().endswith('.zip'):
        return True  # can't cheaply verify; assume OK
    try:
        if not zipfile.is_zipfile(path):
            return False
        with zipfile.ZipFile(path, 'r') as z:
            return len(z.infolist()) > 0
    except Exception:
        return False


@dataclass(frozen=True)
class _ArchiveCandidate:
    path: Path
    size: int
    file_id: int


class ArchiveLookupIndex:
    """Reusable archive metadata for cache-heavy collection downloads."""

    def __init__(self, directories=()):
        self._lock = threading.RLock()
        self._entries: dict[str, list[_ArchiveCandidate]] = {}
        self._by_file_id: dict[str, dict[int, list[_ArchiveCandidate]]] = {}
        for directory in directories:
            self.refresh(Path(directory))

    @staticmethod
    def _dir_key(directory: Path) -> str:
        return os.path.abspath(os.fspath(directory))

    @staticmethod
    def _path_key(path: Path) -> str:
        return os.path.abspath(os.fspath(path))

    @staticmethod
    def _read_directory(directory: Path) -> list[_ArchiveCandidate]:
        entries: list[_ArchiveCandidate] = []
        try:
            paths = directory.iterdir()
        except OSError:
            return entries
        try:
            for path in paths:
                try:
                    if (not path.is_file()
                            or not any(path.name.lower().endswith(ext)
                                       for ext in _ARCHIVE_EXTS)):
                        continue
                    entries.append(_ArchiveCandidate(
                        path, path.stat().st_size, _read_sidecar_file_id(path)))
                except OSError:
                    continue
        except OSError:
            pass
        return entries

    def _replace(self, key: str, entries: list[_ArchiveCandidate]) -> None:
        by_file_id: dict[int, list[_ArchiveCandidate]] = {}
        for entry in entries:
            if entry.file_id > 0:
                by_file_id.setdefault(entry.file_id, []).append(entry)
        self._entries[key] = entries
        self._by_file_id[key] = by_file_id

    def refresh(self, directory: Path) -> None:
        key = self._dir_key(directory)
        entries = self._read_directory(directory)
        with self._lock:
            self._replace(key, entries)

    def candidates(self, directory: Path) -> tuple[_ArchiveCandidate, ...]:
        key = self._dir_key(directory)
        with self._lock:
            entries = self._entries.get(key)
        if entries is None:
            self.refresh(directory)
            with self._lock:
                entries = self._entries.get(key, [])
        return tuple(entries)

    def exact(self, directory: Path, file_id: int) -> tuple[_ArchiveCandidate, ...]:
        key = self._dir_key(directory)
        with self._lock:
            missing = key not in self._entries
        if missing:
            self.refresh(directory)
        with self._lock:
            return tuple(self._by_file_id.get(key, {}).get(file_id, ()))

    def add(self, path: Path, file_id: int = 0) -> None:
        path = Path(path)
        try:
            entry = _ArchiveCandidate(
                path, path.stat().st_size,
                int(file_id or _read_sidecar_file_id(path)))
        except OSError:
            return
        dir_key = self._dir_key(path.parent)
        path_key = self._path_key(path)
        with self._lock:
            entries = [candidate for candidate in self._entries.get(dir_key, [])
                       if self._path_key(candidate.path) != path_key]
            entries.append(entry)
            self._replace(dir_key, entries)

    def discard(self, path: Path) -> None:
        path = Path(path)
        dir_key = self._dir_key(path.parent)
        path_key = self._path_key(path)
        with self._lock:
            if dir_key not in self._entries:
                return
            entries = [candidate for candidate in self._entries[dir_key]
                       if self._path_key(candidate.path) != path_key]
            self._replace(dir_key, entries)


def _find_cached_archive(
    dl_dir: Path,
    display_name: str,
    expected_size_bytes: int,
    mod_id: int = 0,
    file_id: int = 0,
    expected_md5: str = "",
    cache_index: "ArchiveLookupIndex | None" = None,
) -> "tuple[Path | None, bool]":
    """Scan *dl_dir* for an existing archive that matches this mod.

    Matching strategy
    -----------------
    0. If *file_id* > 0: check each archive's ``.fileid`` sidecar for an exact
       match.  This is the most reliable check and short-circuits everything
       else.
    1. If *expected_size_bytes* > 0: find a file whose size is within 1 % of
       the expected value AND whose filename contains the mod ID.  The display
       name is used as an additional hint (substring match) when available.
    2. Fallback (no expected size): find a file whose stem partially matches
       the normalised *display_name*.

    md5 verification
    ----------------
    If *expected_md5* is given, a size/name candidate is only accepted when
    its md5 matches.  Manually-downloaded archives lack the ``.fileid``
    sidecar so this extra check prevents false positives where an unrelated
    archive happens to share the same size and name shape.  The computed
    hash is cached in an ``.md5`` sidecar so repeat lookups don't rehash.

    Partial-download detection
    --------------------------
    If a file whose stem matches the display name exists but its size is
    < 95 % of *expected_size_bytes*, it is treated as an incomplete download
    and returned with ``is_complete=False`` so the caller can delete it.

    Returns
    -------
    (path, is_complete) - both ``None``/``False`` when nothing suitable found.
    """
    _SIZE_TOLERANCE = 0.01   # ±1 % - file is considered complete
    _PARTIAL_CUTOFF  = 0.95  # < 95 % of expected → treat as partial

    norm_name = re.sub(r'[^\w]', '', (display_name or '').lower())
    mod_id_str = str(mod_id) if mod_id > 0 else ""

    if cache_index is not None:
        candidates = cache_index.candidates(dl_dir)
    else:
        candidates = ArchiveLookupIndex._read_directory(dl_dir)

    # Pass 0: exact file_id match via sidecar (written on every download)
    if file_id > 0:
        exact = (cache_index.exact(dl_dir, file_id)
                 if cache_index is not None
                 else (candidate for candidate in candidates
                       if candidate.file_id == file_id))
        for candidate in exact:
            f = candidate.path
            try:
                actual = f.stat().st_size
            except Exception:
                continue
            if expected_size_bytes > 0:
                ratio = actual / expected_size_bytes
                if ratio >= _PARTIAL_CUTOFF:
                    is_complete = ratio >= (1.0 - _SIZE_TOLERANCE) and _zip_is_intact(f)
                    return f, is_complete
                # Sidecar matched but file is clearly truncated - treat as partial
                return f, False
            return f, _zip_is_intact(f)

    best_partial: "Path | None" = None

    for candidate in candidates:
        f = candidate.path
        # Skip files whose sidecar belongs to a different file_id - they are
        # unambiguously a different download and must never be treated as
        # partials of this one.  This prevents cross-contamination when two
        # files from the same mod (e.g. 76460) are being fetched in parallel
        # and one's filename is a prefix of the other.
        if file_id > 0:
            _sid = candidate.file_id
            if _sid > 0 and _sid != file_id:
                continue

        actual = candidate.size

        if expected_size_bytes > 0:
            ratio = actual / expected_size_bytes
            if 1.0 - _SIZE_TOLERANCE <= ratio <= 1.0 + _SIZE_TOLERANCE:
                # Size match - also verify the mod ID appears in the filename
                # to prevent false positives with similarly-sized archives from
                # different mods.
                if mod_id_str and mod_id_str not in f.name:
                    continue
                # Use the display name as a loose hint (substring in either
                # direction) to distinguish two files from the same mod with
                # similar sizes.  We intentionally don't require exact equality
                # here because the GraphQL display name and the CDN filename
                # stem often differ (e.g. spaces vs hyphens, or extra suffixes).
                if mod_id_str and norm_name:
                    clean = re.sub(
                        r'[^\w]', '',
                        _clean_nexus_stem(f.stem, mod_id_str).lower()
                    )
                    if clean and norm_name not in clean and clean not in norm_name:
                        continue
                if cache_index is not None:
                    try:
                        if f.stat().st_size != actual:
                            continue
                    except OSError:
                        continue
                # Size (and optional name hint) match - verify md5 when
                # provided (e.g. from a collection manifest) to rule out
                # unrelated archives that happen to share the same size.
                if expected_md5 and not _md5_matches(f, expected_md5):
                    continue
                return f, _zip_is_intact(f)
            if ratio < _PARTIAL_CUTOFF:
                # Might be a partial download of this file.  Require exact
                # normalized-stem equality (not substring) so that files whose
                # names are prefixes of each other - e.g. "Deathbell" vs
                # "Deathbell ENB-light" under the same mod ID - do not get
                # misidentified as partials of one another.  Strip any trailing
                # ``(N)`` collision counter the downloader may have appended.
                if not mod_id_str or mod_id_str in f.name:
                    raw_stem = re.sub(r'\s*\(\d+\)$', '', f.stem)
                    if mod_id_str and norm_name:
                        clean = re.sub(
                            r'[^\w]', '',
                            _clean_nexus_stem(raw_stem, mod_id_str).lower()
                        )
                        if clean and clean == norm_name:
                            best_partial = f
                    elif norm_name:
                        norm_stem = re.sub(r'[^\w]', '', raw_stem.lower())
                        if norm_stem == norm_name:
                            best_partial = f
        else:
            # No expected size: match by name stem only, verify integrity.
            # Require exact normalized equality for the same prefix-collision
            # reason described above.  Strip trailing ``(N)`` collision counter.
            raw_stem = re.sub(r'\s*\(\d+\)$', '', f.stem)
            # Compare display-portion to display-portion: strip the trailing
            # Nexus ``{modId} version timestamp`` metadata (hyphen- OR space-
            # joined) so the newer website "slow download" names still match.
            display_stem = _clean_nexus_stem(raw_stem, mod_id_str) \
                if mod_id_str else raw_stem
            norm_stem = re.sub(r'[^\w]', '', display_stem.lower())
            if norm_name and norm_stem == norm_name:
                if cache_index is not None and not f.is_file():
                    continue
                if expected_md5 and not _md5_matches(f, expected_md5):
                    continue
                return f, _zip_is_intact(f)

    if best_partial is not None:
        return best_partial, False
    return None, False

# Callback signature: (bytes_downloaded, total_bytes_or_zero)
ProgressCallback = Callable[[int, int], None]


@dataclass
class DownloadResult:
    """Result of a completed (or failed) download."""
    success: bool
    file_path: Path | None = None
    file_name: str = ""
    error: str = ""
    bytes_downloaded: int = 0
    game_domain: str = ""
    mod_id: int = 0
    file_id: int = 0


class DownloadCancelled(Exception):
    """Raised when a download is cancelled via the cancel event."""


def _get_downloads_dir() -> Path:
    """Return the user's Downloads directory."""
    return xdg_download_dir()


class NexusDownloader:
    """
    Manages downloading mod files from Nexus Mods.

    Parameters
    ----------
    api : NexusAPI
        An authenticated API client instance.
    download_dir : Path | None
        Where to save downloaded files. Defaults to ~/Downloads.
    """

    def __init__(self, api: NexusAPI,
                 download_dir: Path | None = None):
        self._api = api
        self._download_dir = download_dir or _get_downloads_dir()
        self._download_dir.mkdir(parents=True, exist_ok=True)
        self._worker_state = threading.local()

    def _worker_session(self) -> requests.Session:
        session = getattr(self._worker_state, "session", None)
        if session is None:
            session = requests.Session()
            session.verify = resolve_ca_bundle() or True
            self._worker_state.session = session
        return session

    def close_worker_session(self) -> None:
        session = getattr(self._worker_state, "session", None)
        if session is not None:
            session.close()
            del self._worker_state.session

    @property
    def download_dir(self) -> Path:
        return self._download_dir

    @download_dir.setter
    def download_dir(self, path: Path) -> None:
        self._download_dir = path
        self._download_dir.mkdir(parents=True, exist_ok=True)

    # -- Public API ---------------------------------------------------------

    def download_from_nxm(
        self,
        link: NxmLink,
        dest_dir: Path | None = None,
        progress_cb: ProgressCallback | None = None,
        cancel: threading.Event | None = None,
        known_file_name: str = "",
    ) -> DownloadResult:
        """
        Download a file using a parsed NXM link.

        This is the primary entry point for free-user downloads triggered
        by clicking "Download with Manager" on the Nexus website.

        Parameters
        ----------
        link        : Parsed nxm:// URL.
        dest_dir    : Override download directory. Defaults to self.download_dir.
        progress_cb : Called periodically with (bytes_so_far, total_bytes).
        cancel      : Set this event to abort the download.

        Returns
        -------
        DownloadResult with file_path on success, or error message on failure.
        """
        if cancel is not None and cancel.is_set():
            return DownloadResult(
                success=False, error="Download cancelled",
                game_domain=link.game_domain,
                mod_id=link.mod_id, file_id=link.file_id,
            )
        try:
            links = self._api.get_download_links(
                game_domain=link.game_domain,
                mod_id=link.mod_id,
                file_id=link.file_id,
                key=link.key or None,
                expires=link.expires or None,
            )
        except NexusAPIError as exc:
            return DownloadResult(
                success=False, error=str(exc),
                game_domain=link.game_domain,
                mod_id=link.mod_id, file_id=link.file_id,
            )

        if not links:
            return DownloadResult(
                success=False, error="API returned no download links",
                game_domain=link.game_domain,
                mod_id=link.mod_id, file_id=link.file_id,
            )

        # Use caller-supplied filename if available; otherwise fall back to
        # a dedicated get_file_info call (costs 1 rate-limited request).
        file_name = known_file_name or ""
        if not file_name:
            try:
                file_info = self._api.get_file_info(
                    link.game_domain, link.mod_id, link.file_id)
                file_name = file_info.file_name
            except Exception:
                pass

        return self._download_from_links(
            links=links,
            file_name=file_name,
            dest_dir=dest_dir or self._download_dir,
            progress_cb=progress_cb,
            cancel=cancel,
            game_domain=link.game_domain,
            mod_id=link.mod_id,
            file_id=link.file_id,
        )

    def download_file(
        self,
        game_domain: str,
        mod_id: int,
        file_id: int,
        dest_dir: Path | None = None,
        progress_cb: ProgressCallback | None = None,
        cancel: threading.Event | None = None,
        known_file_name: str = "",
        expected_size_bytes: int = 0,
        prefetched_links: "list[NexusDownloadLink] | None" = None,
        cache_index: "ArchiveLookupIndex | None" = None,
    ) -> DownloadResult:
        """
        Download a file directly (premium users only - no key needed).

        Parameters
        ----------
        game_domain          : Nexus game domain.
        mod_id               : Nexus mod ID.
        file_id              : Nexus file ID.
        dest_dir             : Override download directory.
        progress_cb          : Progress callback.
        cancel               : Cancellation event.
        known_file_name      : If the caller already has the archive display
                               name (e.g. from a prior get_mod_files call),
                               pass it here to enable the cache check and to
                               skip an extra get_file_info API call.
        expected_size_bytes  : Expected size of the finished archive in bytes
                               (from the API).  Used to validate cached files
                               and detect partial downloads.  Pass 0 if
                               unknown.
        prefetched_links     : Signed CDN links already fetched by the caller
                               (via :meth:`get_download_links`) so the in-line
                               link-fetch round-trip can be skipped - lets a
                               caller pipeline the link fetch AHEAD of the
                               download so a worker starts transferring bytes
                               with zero link latency. Ignored when a complete
                               cached archive is found (no download needed).
        cache_index          : Reusable cache metadata for bulk downloads.

        Returns
        -------
        DownloadResult with file_path on success.
        """
        if cancel is not None and cancel.is_set():
            return DownloadResult(
                success=False, error="Download cancelled",
                game_domain=game_domain, mod_id=mod_id, file_id=file_id,
            )
        # ------------------------------------------------------------------
        # Cache / partial-download check
        # ------------------------------------------------------------------
        # Check for an already-downloaded archive.  The sidecar (.fileid file)
        # gives an exact match on file_id; name+size heuristics are used as
        # fallback.  Partial downloads (size < 95 % of expected) are deleted
        # so the download starts cleanly.
        _dest = dest_dir or self._download_dir
        cached, is_complete = _find_cached_archive(
            _dest, known_file_name, expected_size_bytes, mod_id, file_id,
            cache_index=cache_index,
        )
        if cached is not None:
            if is_complete:
                app_log(
                    f"Skipping download - cached archive found: {cached.name}"
                )
                return DownloadResult(
                    success=True,
                    file_path=cached,
                    file_name=cached.name,
                    bytes_downloaded=cached.stat().st_size,
                    game_domain=game_domain,
                    mod_id=mod_id,
                    file_id=file_id,
                )
            else:
                app_log(
                    f"Removing partial download before retry: {cached.name}"
                )
                try:
                    cached.unlink(missing_ok=True)
                    _fileid_sidecar(cached).unlink(missing_ok=True)
                    if cache_index is not None:
                        cache_index.discard(cached)
                except Exception:
                    pass

        # Use links the caller pre-fetched (pipelined ahead of the download) if
        # provided; otherwise fetch a fresh signed CDN link now (one
        # rate-limited API call per download). Either way it's exactly one
        # get_download_links per downloaded mod.
        links = prefetched_links
        if not links:
            try:
                links = self._api.get_download_links(
                    game_domain=game_domain,
                    mod_id=mod_id,
                    file_id=file_id,
                )
            except NexusAPIError as exc:
                return DownloadResult(
                    success=False, error=str(exc),
                    game_domain=game_domain,
                    mod_id=mod_id, file_id=file_id,
                )

        if not links:
            return DownloadResult(
                success=False, error="API returned no download links",
                game_domain=game_domain,
                mod_id=mod_id, file_id=file_id,
            )

        # Use the caller-supplied filename if available; otherwise fall back to
        # a dedicated get_file_info call (costs 1 rate-limited request).
        file_name = known_file_name or ""
        if not file_name:
            try:
                file_info = self._api.get_file_info(
                    game_domain, mod_id, file_id)
                file_name = file_info.file_name
            except Exception:
                pass

        result = self._download_from_links(
            links=links,
            file_name=file_name,
            dest_dir=dest_dir or self._download_dir,
            progress_cb=progress_cb,
            cancel=cancel,
            game_domain=game_domain,
            mod_id=mod_id,
            file_id=file_id,
        )

        # Prefetched links can be minted a whole pipeline queue ahead of use;
        # if the signed URLs expired before this worker got to them, every
        # mirror fails. Retry once with freshly fetched links before giving
        # the mod up for good.
        if (not result.success and prefetched_links
                and not (cancel is not None and cancel.is_set())):
            app_log(
                f"Prefetched links failed for file {file_id} "
                f"({result.error}); retrying with fresh links."
            )
            try:
                fresh = self._api.get_download_links(
                    game_domain=game_domain,
                    mod_id=mod_id,
                    file_id=file_id,
                )
            except NexusAPIError as exc:
                app_log(f"Fresh link fetch failed for file {file_id}: {exc}")
                fresh = None
            if fresh:
                result = self._download_from_links(
                    links=fresh,
                    file_name=file_name,
                    dest_dir=dest_dir or self._download_dir,
                    progress_cb=progress_cb,
                    cancel=cancel,
                    game_domain=game_domain,
                    mod_id=mod_id,
                    file_id=file_id,
                )
        return result

    # -- Internal -----------------------------------------------------------

    def _download_from_links(
        self,
        links: list[NexusDownloadLink],
        file_name: str,
        dest_dir: Path,
        progress_cb: ProgressCallback | None,
        cancel: threading.Event | None,
        game_domain: str,
        mod_id: int,
        file_id: int,
    ) -> DownloadResult:
        """Try each mirror in order until one succeeds."""

        last_error = ""
        for link in links:
            if cancel is not None and cancel.is_set():
                return DownloadResult(
                    success=False, error="Download cancelled",
                    game_domain=game_domain,
                    mod_id=mod_id, file_id=file_id,
                )
            try:
                result = self._stream_download(
                    url=link.URI,
                    file_name=file_name,
                    dest_dir=dest_dir,
                    progress_cb=progress_cb,
                    cancel=cancel,
                    game_domain=game_domain,
                    mod_id=mod_id,
                    file_id=file_id,
                )
                if result.success:
                    return result
                last_error = result.error
            except DownloadCancelled:
                return DownloadResult(
                    success=False, error="Download cancelled",
                    game_domain=game_domain,
                    mod_id=mod_id, file_id=file_id,
                )
            except Exception as exc:
                last_error = str(exc)
                app_log(f"Mirror {link.name} failed: {exc}")
                continue

        return DownloadResult(
            success=False,
            error=f"All mirrors failed. Last error: {last_error}",
            game_domain=game_domain,
            mod_id=mod_id, file_id=file_id,
        )

    def _stream_download(
        self,
        url: str,
        file_name: str,
        dest_dir: Path,
        progress_cb: ProgressCallback | None,
        cancel: threading.Event | None,
        game_domain: str,
        mod_id: int,
        file_id: int,
    ) -> DownloadResult:
        """Stream-download a single URL to disk."""

        session = self._worker_session()
        with session.get(url, stream=True, timeout=60,
                         verify=session.verify) as resp:
            resp.raise_for_status()

            # Determine filename with the correct extension.
            # The provided file_name may be a GraphQL display name with no
            # extension (e.g. "UI Info Suite 2 v2.3.7").  In that case, derive
            # the real filename from the CDN URL path, then Content-Disposition,
            # with the provided name as a last resort.
            _has_archive_ext = any(file_name.lower().endswith(e) for e in _ARCHIVE_EXTS)
            if not _has_archive_ext:
                # Try the URL path first - CDN URLs always embed the real filename
                try:
                    from urllib.parse import urlparse, unquote
                    _url_path = unquote(urlparse(url).path)
                    _url_basename = os.path.basename(_url_path)
                    if _url_basename and any(_url_basename.lower().endswith(e) for e in _ARCHIVE_EXTS):
                        file_name = _url_basename
                        _has_archive_ext = True
                except Exception:
                    pass
            if not _has_archive_ext:
                cd = resp.headers.get("Content-Disposition", "")
                if "filename=" in cd:
                    # Sanitise: server-controlled value must never carry path
                    # components ("../evil", "C:\evil") or be empty/dot-only.
                    cand = cd.split("filename=")[-1].strip(' "\'')
                    cand = os.path.basename(cand.replace("\\", "/")).strip(' "\'')
                    if cand and cand.strip("."):
                        file_name = cand
            if not file_name:
                file_name = f"{game_domain}_{mod_id}_{file_id}.zip"

            try:
                total = int(resp.headers.get("Content-Length", 0))
            except (TypeError, ValueError):
                total = 0
            dest = dest_dir / file_name

            # Don't clobber existing files - add a suffix
            counter = 1
            stem = dest.stem
            suffix = dest.suffix
            while dest.exists():
                dest = dest_dir / f"{stem} ({counter}){suffix}"
                counter += 1

            # Stamp the sidecar now, before the download starts, so that
            # concurrent _find_cached_archive calls from other threads (e.g.
            # a sibling file from the same mod) can identify this in-flight
            # partial by file_id and skip it, rather than misclassifying it
            # as a partial of their own file and unlinking it.
            if file_id > 0:
                _write_sidecar_file_id(dest, file_id)

            downloaded = 0
            with open(dest, "wb") as fh:
                for chunk in resp.iter_content(_CHUNK_SIZE):
                    if cancel and cancel.is_set():
                        fh.close()
                        delete_archive_and_sidecar(dest)
                        raise DownloadCancelled()

                    fh.write(chunk)
                    downloaded += len(chunk)
                    bandwidth.throttle(len(chunk), cancel)

                    if progress_cb:
                        progress_cb(downloaded, total)

        # Verify against Content-Length - a dropped connection can end the
        # stream early without raising; a short file must not look successful.
        if total and downloaded != total:
            app_log(f"Incomplete download of {file_name}: got {downloaded} "
                    f"of {total} bytes - discarding")
            delete_archive_and_sidecar(dest)
            return DownloadResult(
                success=False,
                error=f"Incomplete download: got {downloaded} of {total} bytes",
                game_domain=game_domain,
                mod_id=mod_id, file_id=file_id,
            )

        app_log(f"Downloaded {file_name} ({downloaded} bytes) → {dest}")
        if file_id > 0:
            _write_sidecar_file_id(dest, file_id)

        return DownloadResult(
            success=True,
            file_path=dest,
            file_name=file_name,
            bytes_downloaded=downloaded,
            game_domain=game_domain,
            mod_id=mod_id,
            file_id=file_id,
        )
