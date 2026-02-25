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
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import requests

from .nexus_api import NexusAPI, NexusDownloadLink, NexusAPIError
from .nxm_handler import NxmLink
from Utils.app_log import app_log

# Default chunk size for streaming downloads (256 KB)
_CHUNK_SIZE = 256 * 1024

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
    xdg = os.environ.get("XDG_DOWNLOAD_DIR")
    if xdg:
        return Path(xdg)
    return Path.home() / "Downloads"


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

        # Try to get the original filename from the file info endpoint
        file_name = ""
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
    ) -> DownloadResult:
        """
        Download a file directly (premium users only — no key needed).

        Parameters
        ----------
        game_domain : Nexus game domain.
        mod_id      : Nexus mod ID.
        file_id     : Nexus file ID.
        dest_dir    : Override download directory.
        progress_cb : Progress callback.
        cancel      : Cancellation event.

        Returns
        -------
        DownloadResult with file_path on success.
        """
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

        # Get original filename
        file_name = ""
        try:
            file_info = self._api.get_file_info(
                game_domain, mod_id, file_id)
            file_name = file_info.file_name
        except Exception:
            pass

        return self._download_from_links(
            links=links,
            file_name=file_name,
            dest_dir=dest_dir or self._download_dir,
            progress_cb=progress_cb,
            cancel=cancel,
            game_domain=game_domain,
            mod_id=mod_id,
            file_id=file_id,
        )

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

        with requests.get(url, stream=True, timeout=60) as resp:
            resp.raise_for_status()

            # Determine filename: prefer the one we already resolved,
            # else try Content-Disposition, else build from IDs.
            if not file_name:
                cd = resp.headers.get("Content-Disposition", "")
                if "filename=" in cd:
                    file_name = cd.split("filename=")[-1].strip(' "\'')
            if not file_name:
                file_name = f"{game_domain}_{mod_id}_{file_id}.zip"

            total = int(resp.headers.get("Content-Length", 0))
            dest = dest_dir / file_name

            # Don't clobber existing files — add a suffix
            counter = 1
            stem = dest.stem
            suffix = dest.suffix
            while dest.exists():
                dest = dest_dir / f"{stem} ({counter}){suffix}"
                counter += 1

            downloaded = 0
            with open(dest, "wb") as fh:
                for chunk in resp.iter_content(_CHUNK_SIZE):
                    if cancel and cancel.is_set():
                        fh.close()
                        dest.unlink(missing_ok=True)
                        raise DownloadCancelled()

                    fh.write(chunk)
                    downloaded += len(chunk)

                    if progress_cb:
                        progress_cb(downloaded, total)

        app_log(f"Downloaded {file_name} ({downloaded} bytes) → {dest}")

        return DownloadResult(
            success=True,
            file_path=dest,
            file_name=file_name,
            bytes_downloaded=downloaded,
            game_domain=game_domain,
            mod_id=mod_id,
            file_id=file_id,
        )
