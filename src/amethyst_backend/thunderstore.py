from __future__ import annotations

import json
import re
import zipfile
from dataclasses import asdict
from pathlib import Path

from .jobs import JobCancelled, JobError, _validated_archive_path


def package_link(namespace, package, version, community):
    from Thunderstore.ror2mm_handler import Ror2mmLink

    try:
        if not isinstance(community, str) or re.fullmatch(r"[a-z0-9_-]+", community) is None:
            raise ValueError("Invalid community")
        link = Ror2mmLink.parse(f"ror2mm://v1/install/thunderstore.io/{namespace}/{package}/{version}/")
        if (link.namespace, link.name, link.version) != (namespace, package, version):
            raise ValueError("Invalid package identity")
        link.community = community
        return link
    except (TypeError, ValueError) as exc:
        raise JobError("thunderstore_identity_invalid", "A Thunderstore package needs valid namespace, package, and numeric version identifiers.") from exc


def version_catalog(context):
    from Nexus.nexus_update_checker import _parse_version
    from Thunderstore.thunderstore_api import fetch_versions
    from Thunderstore.thunderstore_download import resolve_communities

    meta = context["thunderstore_meta"]
    link = package_link(meta.namespace, meta.name, "0.0.0", context["community"])
    if getattr(meta, "community", "") != link.community and link.community not in resolve_communities(link):
        raise JobError("thunderstore_community_mismatch", "The package is not listed for the selected game's community.")
    from .batch import cached_read
    raw = cached_read(("thunderstore.versions", meta.namespace, meta.name),
                      lambda: fetch_versions(meta.namespace, meta.name))
    if not isinstance(raw, list) or not raw:
        raise JobError("mod_version_catalog_unavailable", "Could not read the Thunderstore version catalogue.")
    rows, seen = [], set()
    for item in raw:
        if not isinstance(item, dict):
            raise JobError("mod_version_catalog_unavailable", "The Thunderstore version catalogue is invalid.")
        version = str(item.get("version_number") or "")
        selected = package_link(meta.namespace, meta.name, version, link.community)
        if version in seen:
            raise JobError("mod_version_catalog_unavailable", "The Thunderstore version catalogue has duplicate versions.")
        seen.add(version)
        rows.append({"version": version, "full_name": selected.full_name,
                     "installed": version == meta.version, "recommended": False,
                     "uploaded_at": str(item.get("datetime_created") or ""),
                     "download_count": int(item.get("download_count") or 0),
                     "dependency_count": int(item.get("dependency_count") or 0)})
    rows.sort(key=lambda item: _parse_version(item["version"]) or (), reverse=True)
    latest = rows[0]["version"]
    rows[0]["recommended"] = latest != meta.version
    return {"versions": rows, "recommended_version": latest if latest != meta.version else None}


def version_metadata(link):
    from Thunderstore.thunderstore_meta import build_meta_from_link
    from Thunderstore.thunderstore_requirements import _fetch_version

    info = _fetch_version(link.namespace, link.name, link.version)
    if not isinstance(info, dict) or str(info.get("version_number") or "") != link.version:
        raise JobError("mod_version_not_found", "The selected Thunderstore version is unavailable.")
    if not isinstance(info.get("dependencies"), list):
        raise JobError("thunderstore_dependencies_unavailable", "Could not read the selected version's dependencies.")
    size = int(info.get("file_size") or info.get("size") or 0)
    if size <= 0:
        raise JobError("thunderstore_size_unavailable", "Could not verify the selected version's archive size.")
    metadata = build_meta_from_link(link, {**info, "size": size})
    if metadata.full_name != link.full_name:
        raise JobError("mod_version_source_mismatch", "The Thunderstore response identifies another package version.")
    return metadata


def validate_archive(path, link, size):
    if size > 0 and path.stat().st_size != size:
        raise JobError("mod_version_archive_mismatch", "The archive size does not match the selected Thunderstore version.")
    try:
        with zipfile.ZipFile(path) as archive:
            manifests = [item for item in archive.infolist() if item.filename.lower() == "manifest.json"]
            if len(manifests) != 1 or manifests[0].file_size > 1048576:
                raise ValueError("Missing or oversized manifest")
            manifest = json.loads(archive.read(manifests[0]))
        if not isinstance(manifest, dict) or (manifest.get("name"), manifest.get("version_number")) != (link.name, link.version):
            raise ValueError("Wrong package manifest")
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, RuntimeError) as exc:
        raise JobError("mod_version_archive_mismatch", "The archive manifest does not match the selected Thunderstore package and version.") from exc


def retained_archive(game, profile, link, size, archive_id=None, downloaded=None):
    from Utils.downloads.inventory import build_inventory
    from .download_archives import _token

    inventory = build_inventory(game, profile)
    candidates = []
    for item in inventory["archives"]:
        identity = item.get("thunderstore")
        match = (identity is not None and (identity.get("namespace"), identity.get("package"), identity.get("package_version"))
                 == (link.namespace, link.name, link.version))
        if match or (identity is None and item["name"] == f"{link.full_name}.zip"):
            candidates.append(item)
    if archive_id is not None:
        item = next((value for value in inventory["archives"] if value["id"] == archive_id), None)
        if item is None:
            raise JobError("archive_not_found", "The selected archive is unavailable.")
        if item not in candidates:
            raise JobError("mod_version_archive_mismatch", "The archive does not identify the selected Thunderstore version.")
    elif downloaded is not None:
        item = next((value for value in candidates if Path(value["path"]) == downloaded), None)
        if item is None:
            raise JobError("mod_version_archive_mismatch", "The downloaded archive has no matching Thunderstore identity.")
    elif len(candidates) > 1:
        raise JobError("mod_version_archive_ambiguous", "Several retained archives identify this version; select one.",
                       {"archives": [{"id": value["id"], "name": value["name"]} for value in candidates]})
    else:
        item = candidates[0] if candidates else None
    if item is None:
        return None
    path = _validated_archive_path(inventory, item)
    paths = [path, path.with_suffix(path.suffix + ".thunderstore.json")]
    tokens = [_token(value, required=index == 0) for index, value in enumerate(paths)]
    if tokens[1] is not None:
        try:
            identity = json.loads(paths[1].read_text())
            if (not isinstance(identity, dict) or identity.get("version") != 1
                    or (identity.get("namespace"), identity.get("package"), identity.get("package_version"))
                    != (link.namespace, link.name, link.version)):
                raise ValueError("Conflicting identity")
        except (OSError, ValueError) as exc:
            raise JobError("mod_version_archive_mismatch", "The Thunderstore archive sidecar does not identify the selected version.") from exc
    validate_archive(path, link, size)
    if tokens != [_token(value, required=index == 0) for index, value in enumerate(paths)]:
        raise JobError("mod_version_archive_changed", "The Thunderstore archive changed while it was verified.")
    return item, paths, tokens


def download(game, link, metadata, log, progress, control=None):
    from Thunderstore.thunderstore_download import download_package
    from Utils.atomic_write import write_atomic_text
    from Utils.config_paths import get_download_cache_dir_for_game
    from Utils.operations import acquire_resource_operation

    if control is not None and control.is_set():
        raise JobCancelled("Download cancelled.")
    destination = Path(get_download_cache_dir_for_game(game.name))
    final = destination / f"{link.full_name}.zip"
    with acquire_resource_operation(f"thunderstore:{final.resolve()}", "Thunderstore download", cancel=control):
        if final.is_symlink() or final.with_suffix(".zip.part").is_symlink():
            raise JobError("unsafe_archive", "The Thunderstore download path is a symbolic link.")
        result = download_package(link, destination, expected_size=metadata.file_size,
                                  progress_cb=lambda done, total: progress(done, total, "Thunderstore download"),
                                  cancel=control, operation_locked=True,
                                  verify_archive=lambda path: validate_archive(path, link, metadata.file_size))
        if result.cancelled:
            raise JobCancelled("Download cancelled.")
        if not result.success or not result.file_path:
            raise JobError("thunderstore_download_failed", result.error or "The package download failed.")
        if Path(result.file_path).resolve() != final.resolve():
            raise JobError("unsafe_archive", "The downloader returned an unexpected archive path.")
        validate_archive(final, link, metadata.file_size)
        sidecar = final.with_suffix(final.suffix + ".thunderstore.json")
        identity = {"version": 1, "namespace": link.namespace, "package": link.name,
                    "package_version": link.version, "community": link.community,
                    "metadata": asdict(metadata)}
        write_atomic_text(sidecar, json.dumps(identity, ensure_ascii=True))
        if json.loads(sidecar.read_text()) != identity:
            raise JobError("thunderstore_metadata_failed", "Could not verify the downloaded package's identity.")
    log(f"Downloaded {link.full_name}")
    return final


def missing_dependencies(game, profile, metadata):
    from Nexus.nexus_update_checker import _parse_version
    from Thunderstore.thunderstore_meta import read_meta, parse_full_name
    from Utils.mods.modlist import read_modlist

    available = {}
    staging = Path(game.get_effective_mod_staging_path())
    for entry in read_modlist(profile / "modlist.txt"):
        if not entry.enabled or entry.is_separator or entry.is_group_header:
            continue
        meta = read_meta(staging / entry.name / "meta.ini")
        if meta.package_id:
            available.setdefault(meta.package_id, []).append(meta.version)
    missing = []
    for dependency in metadata.dependency_list():
        namespace, package, version = parse_full_name(dependency)
        package_link(namespace, package, version, metadata.community)
        if f"{namespace}-{package}" == metadata.package_id and (_parse_version(metadata.version) or ()) >= (_parse_version(version) or ()):
            continue
        if not any((_parse_version(value) or ()) >= (_parse_version(version) or ())
                   for value in available.get(f"{namespace}-{package}", ())):
            missing.append({"namespace": namespace, "package": package, "version": version})
    return missing


def dependency_report(game, context, metadata):
    import copy
    from Utils.mods.modlist import read_modlist

    missing = []
    for affected in context["properties"]["affected_profiles"]:
        profile = context["target_profile"].parent / affected["profile"]
        if not any(entry.enabled and entry.name in affected["mods"] for entry in read_modlist(profile / "modlist.txt")):
            continue
        selected = copy.copy(game)
        selected.set_active_profile_dir(profile)
        selected.load_paths(read_only=True)
        missing.extend({"profile": profile.name, **item} for item in missing_dependencies(selected, profile, metadata))
    return missing


def downloaded_metadata(path, game):
    from Nexus.nexus_meta import NexusModMeta
    from Thunderstore.thunderstore_meta import ThunderstoreModMeta
    from .download_archives import _token

    sidecar = path.with_suffix(path.suffix + ".thunderstore.json")
    if not sidecar.exists() and not sidecar.is_symlink():
        return None
    tokens = [_token(value, required=True) for value in (path, sidecar)]
    try:
        raw = json.loads(sidecar.read_text())
        if not isinstance(raw, dict) or raw.get("version") != 1:
            raise ValueError("Unknown sidecar version")
        link = package_link(raw["namespace"], raw["package"], raw["package_version"], raw["community"])
        if link.community != getattr(game, "thunderstore_community", ""):
            raise JobError("thunderstore_community_mismatch", "The archive belongs to another game's Thunderstore community.")
        metadata = ThunderstoreModMeta(**raw["metadata"])
        if type(metadata.file_size) is not int or metadata.file_size <= 0:
            raise ValueError("Invalid archive size")
        if (metadata.namespace, metadata.name, metadata.version, metadata.community, metadata.full_name) != (link.namespace, link.name, link.version, link.community, link.full_name):
            raise ValueError("Conflicting metadata")
        validate_archive(path, link, metadata.file_size)
    except (KeyError, TypeError, ValueError) as exc:
        raise JobError("thunderstore_metadata_failed", "The retained Thunderstore metadata is invalid.") from exc
    general = NexusModMeta(mod_name=link.name, version=link.version, installation_file=path.name)
    return general, metadata, sidecar, tokens


def versions_runner(game, link):
    def run(_log, progress):
        from types import SimpleNamespace
        from Utils.operations import acquire_game_operation

        with acquire_game_operation(game, "thunderstore.packages.versions"):
            progress(0, 1, "Reading Thunderstore versions")
            meta = SimpleNamespace(namespace=link.namespace, name=link.name, version="", community="")
            catalog = version_catalog({"thunderstore_meta": meta, "community": link.community})
            progress(1, 1, "Thunderstore versions ready")
            return {"version_catalog": {"source": "thunderstore", "community": link.community,
                                        "namespace": link.namespace, "package": link.name, **catalog}}
    return run


def download_runner(game, link, control):
    def run(log, progress):
        from Thunderstore.thunderstore_download import resolve_communities
        from Utils.operations import OperationCancelled, acquire_game_operation

        try:
            with acquire_game_operation(game, "thunderstore.download"):
                if control.is_set():
                    raise JobCancelled("Download cancelled.")
                progress(0, 1, "Reading Thunderstore package metadata")
                if link.community not in resolve_communities(link):
                    raise JobError("thunderstore_community_mismatch", "The package is not listed for the selected game's community.")
                metadata = version_metadata(link)
                path = download(game, link, metadata, log, progress, control)
                return {"archive": {"path": str(path), "name": path.name, "size_bytes": path.stat().st_size},
                        "thunderstore": {"namespace": link.namespace, "package": link.name,
                                         "version": link.version, "community": link.community,
                                         "dependencies": metadata.dependency_list()}}
        except OperationCancelled as exc:
            raise JobCancelled("Download cancelled.") from exc
    return run
