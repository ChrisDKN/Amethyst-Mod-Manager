from __future__ import annotations

import hashlib
import json
import re
import time
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from Utils.bg3.modio_loader import load_modio_module

from .jobs import JobCancelled, JobError, _validated_archive_path


GAME_ID = 6715


@contextmanager
def api_client():
    module = load_modio_module("modio_api")
    key, api_path = load_modio_module("modio_key").load_modio_credentials()
    if not key or not api_path:
        raise JobError("modio_credentials_missing", "Save a mod.io API key and API path first.")
    try:
        api = module.ModioAPI(key, api_path)
    except ValueError as exc:
        raise JobError("modio_credentials_invalid", "The saved mod.io credentials are invalid.") from exc
    try:
        yield api
    except module.ModioAPIError as exc:
        raise JobError("modio_request_failed", "The mod.io request failed; verify access and retry.") from exc
    finally:
        api._session.close()


def catalogue(api, mod_id, installed_file_id=0):
    from .batch import cached_read
    files = cached_read(("modio.files", mod_id), lambda: api.get_mod_files(mod_id, use_cache=False))
    summary = cached_read(("modio.latest", mod_id), lambda: api.get_mods_latest_batch([mod_id]).get(mod_id))
    if summary is None or summary.mod_id != mod_id:
        raise JobError("modio_mod_unavailable", "The mod is unavailable for Baldur's Gate 3 on Windows.")
    latest = summary.latest_file_id
    if latest and not any(file.file_id == latest for file in files):
        raise JobError("mod_version_catalog_unavailable", "The mod.io live file is absent from its file catalogue; retry.")
    rows = [{"file_id": file.file_id, "version": file.version, "file_name": file.filename,
             "size_bytes": file.filesize, "size_uncompressed_bytes": file.filesize_uncompressed,
             "md5": file.md5, "uploaded_at": file.date_added, "changelog": file.changelog,
             "installed": file.file_id == installed_file_id,
             "recommended": file.file_id == latest and file.file_id != installed_file_id}
            for file in files]
    return {"files": rows, "recommended_file_id": latest if latest and latest != installed_file_id else None}, files, summary


def file_metadata(api, mod_id, file_id, files, summary):
    if not any(file.file_id == file_id for file in files):
        raise JobError("mod_version_not_found", "The selected file is absent from the current mod.io catalogue.")
    file = api.get_mod_file(mod_id, file_id)
    if ((file.mod_id, file.file_id) != (mod_id, file_id) or file.filesize <= 0
            or re.fullmatch(r"[a-f0-9]{32}", file.md5) is None):
        raise JobError("modio_file_unverifiable", "The selected mod.io file has no matching identity, size, or MD5.")
    metadata = load_modio_module("modio_meta").ModioMeta(
        mod_id=mod_id, file_id=file_id, version=file.version, name=summary.name,
        profile_url=summary.profile_url, latest_file_id=summary.latest_file_id,
        latest_version=summary.latest_version, has_update=bool(summary.latest_file_id and file_id != summary.latest_file_id),
        installed=datetime.now(timezone.utc).isoformat())
    return file, metadata


def validate_archive(path, file, control=None):
    from Utils.downloads.control import wait_if_paused

    if path.stat().st_size != file.filesize:
        raise JobError("mod_version_archive_mismatch", "The archive size does not match the selected mod.io file.")
    digest = hashlib.md5()
    with path.open("rb") as stream:
        while True:
            if wait_if_paused(control):
                raise JobCancelled("Download cancelled.")
            chunk = stream.read(1048576)
            if not chunk:
                break
            digest.update(chunk)
    if digest.hexdigest() != file.md5:
        raise JobError("mod_version_archive_mismatch", "The archive MD5 does not match the selected mod.io file.")


def retained_archive(game, profile, mod_id, file, archive_id=None, downloaded=None):
    from Utils.downloads.inventory import build_inventory
    from .download_archives import _token

    inventory = build_inventory(game, profile)
    candidates = [item for item in inventory["archives"]
                  if (item.get("modio") or {}).get("mod_id") == mod_id
                  and item["modio"].get("file_id") == file.file_id
                  and item["modio"].get("game_id") == GAME_ID]
    if archive_id is not None:
        item = next((value for value in inventory["archives"] if value["id"] == archive_id), None)
        if item is None:
            raise JobError("archive_not_found", "The selected archive is unavailable.")
    elif downloaded is not None:
        item = next((value for value in candidates if Path(value["path"]) == downloaded), None)
        if item is None:
            raise JobError("mod_version_archive_mismatch", "The downloaded archive has no matching mod.io identity.")
    elif len(candidates) > 1:
        raise JobError("mod_version_archive_ambiguous", "Several retained archives identify this file; select one.",
                       {"archives": [{"id": value["id"], "name": value["name"]} for value in candidates]})
    else:
        item = candidates[0] if candidates else None
    if item is None:
        return None
    path = _validated_archive_path(inventory, item)
    paths = [path, path.with_suffix(path.suffix + ".modio.json")]
    tokens = [_token(value, required=index == 0) for index, value in enumerate(paths)]
    if tokens[1] is not None:
        try:
            raw = json.loads(paths[1].read_text())
            if (not isinstance(raw, dict) or raw.get("version") != 1
                    or (raw.get("game_id"), raw.get("mod_id"), raw.get("file_id"), raw.get("md5"), raw.get("size_bytes"))
                    != (GAME_ID, mod_id, file.file_id, file.md5, file.filesize)):
                raise ValueError("Conflicting identity")
        except (OSError, ValueError) as exc:
            raise JobError("mod_version_archive_mismatch", "The archive sidecar does not match the selected mod.io file.") from exc
    validate_archive(path, file)
    if tokens != [_token(value, required=index == 0) for index, value in enumerate(paths)]:
        raise JobError("mod_version_archive_changed", "The mod.io archive changed while it was verified.")
    return item, paths, tokens


def download(game, mod_id, file, metadata, log, progress, control=None):
    import requests
    from Utils.atomic_write import write_atomic_text
    from Utils.ca_bundle import resolve_ca_bundle
    from Utils.config_paths import get_download_cache_dir_for_game
    from Utils.downloads.control import wait_if_paused
    from Utils.operations import acquire_resource_operation

    url = urlparse(file.download_url)
    if (url.scheme != "https" or not url.hostname or url.username or url.password
            or url.fragment or (file.download_expires and file.download_expires <= time.time())):
        raise JobError("modio_download_unavailable", "The mod.io download URL is unavailable or expired; retry.")
    destination = Path(get_download_cache_dir_for_game(game.name))
    final = destination / f"modio-{GAME_ID}-{mod_id}-{file.file_id}.zip"
    part = final.with_suffix(final.suffix + ".part")
    sidecar = final.with_suffix(final.suffix + ".modio.json")
    with acquire_resource_operation(f"modio:{final.resolve()}", "mod.io download", cancel=control):
        if any(path.is_symlink() or (path.exists() and not path.is_file()) for path in (final, part, sidecar)):
            raise JobError("unsafe_archive", "A mod.io download path is not a regular file.")
        if wait_if_paused(control):
            raise JobCancelled("Download cancelled.")
        destination.mkdir(parents=True, exist_ok=True)
        try:
            with requests.Session() as session:
                session.verify = resolve_ca_bundle() or True
                session.headers.update({"User-Agent": "AmethystModManager"})
                with session.get(file.download_url, stream=True, timeout=(15, 60)) as response:
                    if response.status_code != 200:
                        raise JobError("modio_download_failed", "The mod.io download was rejected.",
                                       {"status_code": response.status_code})
                    done = 0
                    progress(0, file.filesize, "mod.io download")
                    with part.open("wb") as stream:
                        for chunk in response.iter_content(chunk_size=262144):
                            if wait_if_paused(control):
                                raise JobCancelled("Download cancelled.")
                            if chunk:
                                stream.write(chunk)
                                done += len(chunk)
                                progress(done, file.filesize, "mod.io download")
            validate_archive(part, file, control)
            if wait_if_paused(control):
                raise JobCancelled("Download cancelled.")
            part.replace(final)
        except requests.RequestException as exc:
            raise JobError("modio_download_failed", "The mod.io archive transfer failed.",
                           {"exception": type(exc).__name__}) from exc
        finally:
            part.unlink(missing_ok=True)
        identity = {"version": 1, "game_id": GAME_ID, "mod_id": mod_id, "file_id": file.file_id,
                    "md5": file.md5, "size_bytes": file.filesize, "metadata": asdict(metadata)}
        write_atomic_text(sidecar, json.dumps(identity, ensure_ascii=True))
        if json.loads(sidecar.read_text()) != identity:
            raise JobError("modio_metadata_failed", "The downloaded mod.io identity could not be verified.")
    log(f"Downloaded mod.io mod {mod_id}, file {file.file_id}")
    return final


def downloaded_metadata(path, game):
    from Nexus.nexus_meta import NexusModMeta
    from .download_archives import _token

    sidecar = path.with_suffix(path.suffix + ".modio.json")
    if not sidecar.exists() and not sidecar.is_symlink():
        return None
    if getattr(game, "game_id", "") != "baldurs_gate_3":
        raise JobError("modio_game_mismatch", "The archive belongs to Baldur's Gate 3 on mod.io.")
    tokens = [_token(value, required=True) for value in (path, sidecar)]
    try:
        raw = json.loads(sidecar.read_text())
        if (not isinstance(raw, dict) or raw.get("version") != 1 or raw.get("game_id") != GAME_ID
                or any(type(raw.get(key)) is not int or raw[key] <= 0 for key in ("mod_id", "file_id", "size_bytes"))
                or not isinstance(raw.get("md5"), str) or re.fullmatch(r"[a-f0-9]{32}", raw["md5"]) is None):
            raise ValueError("Invalid identity")
        metadata = load_modio_module("modio_meta").ModioMeta(**raw["metadata"])
        if (metadata.mod_id, metadata.file_id) != (raw["mod_id"], raw["file_id"]):
            raise ValueError("Conflicting metadata")
        file = load_modio_module("modio_api").ModioFile(filesize=raw["size_bytes"], md5=raw["md5"])
        validate_archive(path, file)
    except (KeyError, TypeError, ValueError) as exc:
        raise JobError("modio_metadata_failed", "The retained mod.io metadata is invalid.") from exc
    general = NexusModMeta(mod_name=metadata.name, version=metadata.version, installation_file=path.name)
    return general, metadata, sidecar, tokens


def files_runner(game, mod_id):
    def run(_log, progress):
        from Utils.operations import acquire_game_operation

        with acquire_game_operation(game, "modio.mods.files"), api_client() as api:
            progress(0, 1, "Reading mod.io files")
            catalog, _, _ = catalogue(api, mod_id)
            progress(1, 1, "mod.io files ready")
            return {"version_catalog": {"source": "modio", "game_id": GAME_ID, "mod_id": mod_id, **catalog}}
    return run


def download_runner(game, mod_id, file_id, control):
    def run(log, progress):
        from Utils.operations import OperationCancelled, acquire_game_operation

        try:
            with acquire_game_operation(game, "modio.download"), api_client() as api:
                if control.is_set():
                    raise JobCancelled("Download cancelled.")
                _, files, summary = catalogue(api, mod_id)
                file, metadata = file_metadata(api, mod_id, file_id, files, summary)
                path = download(game, mod_id, file, metadata, log, progress, control)
                return {"archive": {"path": str(path), "name": path.name, "size_bytes": path.stat().st_size},
                        "modio": {"game_id": GAME_ID, "mod_id": mod_id, "file_id": file_id,
                                  "version": file.version, "md5": file.md5}}
        except OperationCancelled as exc:
            raise JobCancelled("Download cancelled.") from exc
    return run
