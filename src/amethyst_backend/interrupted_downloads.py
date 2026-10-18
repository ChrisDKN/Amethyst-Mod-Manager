from __future__ import annotations

from pathlib import Path
from typing import Any

from .jobs import JobCancelled, JobError


def inventory(game: Any, domains: list[str]) -> dict[str, Any]:
    from Nexus.nexus_download import list_resumable_downloads
    from Utils.downloads.core import get_scan_dirs, section_label_for_dir
    from Utils.downloads.inventory import _location_kind, _opaque_id

    game_name = str(getattr(game, "name", "") or "")
    locations = []
    downloads = []
    for directory in get_scan_dirs(game_name):
        directory = directory.expanduser().absolute()
        location = {
            "id": _opaque_id("location", directory),
            "path": str(directory),
            "kind": _location_kind(directory, game_name),
            "label": section_label_for_dir(directory, game_name),
        }
        locations.append(location)
        for item in list_resumable_downloads([directory]):
            if item.game_domain.casefold() not in domains:
                continue
            downloads.append({
                "id": _opaque_id("interrupted", item.metadata_path),
                "revision": item.revision,
                "name": item.file_name,
                "partial_path": str(item.metadata_path.with_suffix("")),
                "metadata_path": str(item.metadata_path),
                "location_id": location["id"],
                "location_kind": location["kind"],
                "domain": item.game_domain.casefold(),
                "mod_id": item.mod_id,
                "file_id": item.file_id,
                "downloaded_bytes": item.downloaded,
                "total_bytes": item.total,
                "requires_nxm": item.requires_nxm,
            })
    downloads.sort(key=lambda item: (item["name"].casefold(), item["id"]))
    return {"locations": locations, "downloads": downloads}


def _selection(game, domains, download_id, revision):
    item = next((item for item in inventory(game, domains)["downloads"]
                 if item["id"] == download_id), None)
    if item is None:
        raise JobError("download_interrupted_unavailable",
                       "The saved transfer is unavailable; list downloads again.")
    if item["revision"] != revision:
        raise JobError("download_interrupted_changed",
                       "The saved transfer changed; list downloads again.")
    return item


def discard(game, domains, download_id, revision, cancel, log, progress):
    from Nexus.nexus_download import (
        DownloadResumeError, discard_resumable_download, download_resource)
    from Utils.operations import OperationCancelled, acquire_resource_operation

    if cancel.is_set():
        raise JobCancelled("Discard cancelled.")
    item = _selection(game, domains, download_id, revision)
    metadata = Path(item["metadata_path"])
    resource = download_resource(metadata.parent, item["domain"],
                                 item["mod_id"], item["file_id"])
    try:
        with acquire_resource_operation(
                resource, "downloads.interrupted.discard",
                owner="headless:downloads.interrupted.discard", cancel=cancel):
            _selection(game, domains, download_id, revision)
            discard_resumable_download(metadata, expected_revision=revision)
    except OperationCancelled as exc:
        raise JobCancelled("Discard cancelled.") from exc
    except DownloadResumeError as exc:
        raise JobError(exc.name, str(exc)) from exc
    log(f"Discarded interrupted download: {item['name']}")
    progress(1, 1, "discard")
    return {"ok": True, "removed": item}


def resume(game, domains, download_id, revision, session, nxm_link,
           control, log, progress):
    from Nexus.nexus_download import DownloadResumeError, NexusDownloader
    from .nexus import _call

    def operation():
        if control.is_set():
            raise JobCancelled("Download cancelled.")
        item = _selection(game, domains, download_id, revision)
        if nxm_link is not None and (
                nxm_link.game_domain.casefold() != item["domain"]
                or nxm_link.mod_id != item["mod_id"]
                or nxm_link.file_id != item["file_id"]):
            raise JobError("nexus_download_link_mismatch",
                           "The NXM link identifies a different saved transfer.")
        api = session.require_api()
        if (nxm_link is None or not nxm_link.key) and not api.validate().is_premium:
            raise JobError(
                "nexus_premium_required",
                "Request a fresh Download with Manager link to resume this transfer.",
                {"download_url":
                 f"https://www.nexusmods.com/{item['domain']}/mods/{item['mod_id']}"
                 f"?tab=files&file_id={item['file_id']}&nmm=1"})
        _selection(game, domains, download_id, revision)
        metadata = Path(item["metadata_path"])
        downloader = NexusDownloader(api, download_dir=metadata.parent)
        kwargs = {
            "dest_dir": metadata.parent,
            "known_file_name": item["name"],
            "progress_cb": lambda done, total: progress(done, total, "download"),
            "cancel": control,
            "resume_metadata_path": metadata,
            "resume_revision": revision,
        }
        progress(item["downloaded_bytes"], item["total_bytes"], "download")
        try:
            if nxm_link is None:
                result = downloader.download_file(
                    item["domain"], item["mod_id"], item["file_id"], **kwargs)
            else:
                result = downloader.download_from_nxm(nxm_link, **kwargs)
        except DownloadResumeError as exc:
            raise JobError(exc.name, str(exc)) from exc
        finally:
            downloader.close_worker_session()
        if not result.success:
            if control.is_set() or "cancel" in str(result.error).lower():
                raise JobCancelled("Download cancelled.")
            raise JobError("nexus_download_failed",
                           "Nexus could not resume the saved transfer.")
        path = Path(result.file_path).absolute()
        if (path.is_symlink() or not path.is_file()
                or path.resolve().parent != metadata.parent.resolve()
                or path.name != item["name"]):
            raise JobError("nexus_download_unsafe_path",
                           "The archive is outside the selected download location.")
        size = path.stat().st_size
        progress(size, size, "download")
        log(f"Resumed download: {path.name}")
        return {
            "ok": True, "download_id": download_id,
            "source": {"provider": "nexus", "domain": item["domain"],
                       "mod_id": item["mod_id"], "file_id": item["file_id"],
                       "mode": "nxm" if nxm_link is not None else "premium"},
            "archive": {"name": path.name, "path": str(path),
                        "size_bytes": size, "location_id": item["location_id"],
                        "location_kind": item["location_kind"], "from_cache": False},
        }

    return _call(operation)
