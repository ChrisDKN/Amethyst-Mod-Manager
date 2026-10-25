from __future__ import annotations

import queue
import sys
import threading
import time
import traceback
import uuid
from copy import deepcopy
from collections import deque
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


_FINAL_STATES = {"succeeded", "failed", "cancelled"}


class JobError(RuntimeError):
    def __init__(self, name: str, message: str,
                 details: dict[str, Any] | None = None):
        super().__init__(message)
        self.name = name
        self.details = details or {}


class JobCancelled(RuntimeError):
    installer_request_cancelled = True
    def __init__(self, message="Job cancelled.", details=None):
        super().__init__(message)
        self.details = details or {}


class JobNotCancellable(RuntimeError):
    pass


class JobNotPausable(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _reinstall_target(
    game: Any,
    requested_profile: Path,
    mod_name: str,
) -> dict[str, Any]:
    from Utils.mods.copy import resolve_target_staging
    from Utils.mods.modlist import read_modlist
    from Utils.profiles.groups import entry_owner_profile, is_group

    try:
        entries = read_modlist(requested_profile / "modlist.txt")
    except OSError as exc:
        raise JobError(
            "resource_unavailable",
            f"Could not read profile '{requested_profile.name}'.",
            {"profile": requested_profile.name, "reason": str(exc)},
        ) from exc
    names = [entry.name for entry in entries if not entry.is_separator]
    if mod_name not in names:
        casing = next(
            (name for name in names if name.casefold() == mod_name.casefold()),
            None,
        )
        raise JobError(
            "mod_not_found",
            f"Mod '{mod_name}' was not found in the profile.",
            {"mod": mod_name, "casing_match": casing},
        )
    if names.count(mod_name) > 1:
        raise JobError(
            "ambiguous_mod",
            f"Mod '{mod_name}' appears more than once in the profile.",
            {"mod": mod_name, "matches": names.count(mod_name)},
        )

    target_profile = requested_profile
    target_name = mod_name
    grouped = is_group(requested_profile)
    if grouped:
        owner = entry_owner_profile(requested_profile, mod_name)
        if owner is None:
            raise JobError(
                "profile_group_owner_missing",
                f"Could not resolve the owner of '{mod_name}' in profile "
                f"group '{requested_profile.name}'.",
                {"profile": requested_profile.name, "mod": mod_name},
            )
        target_profile, target_name = owner

    if (not target_profile.is_dir() or target_profile.is_symlink()
            or target_profile.parent != requested_profile.parent):
        raise JobError(
            "unsafe_profile_path",
            "The reinstall target profile is unavailable or unsafe.",
            {"profile": target_profile.name, "path": str(target_profile)},
        )
    if grouped:
        try:
            owner_entries = read_modlist(target_profile / "modlist.txt")
        except OSError as exc:
            raise JobError(
                "resource_unavailable",
                f"Could not read owner profile '{target_profile.name}'.",
                {"profile": target_profile.name, "reason": str(exc)},
            ) from exc
        if not any(
            not entry.is_separator and entry.name == target_name
            for entry in owner_entries
        ):
            raise JobError(
                "profile_group_owner_stale",
                "The profile-group owner no longer lists the selected mod.",
                {
                    "profile": requested_profile.name,
                    "owner_profile": target_profile.name,
                    "mod": mod_name,
                    "owner_mod": target_name,
                },
            )

    try:
        staging = Path(resolve_target_staging(game, target_profile))
    except (OSError, TypeError, ValueError) as exc:
        raise JobError(
            "resource_unavailable",
            "The reinstall target staging folder is unavailable.",
            {"profile": target_profile.name, "reason": str(exc)},
        ) from exc
    destination = staging / target_name
    if destination.is_symlink():
        raise JobError(
            "unsafe_mod_path",
            f"Mod '{mod_name}' resolves to a symbolic link and cannot be "
            "reinstalled.",
            {"mod": mod_name, "path": str(destination)},
        )
    if not destination.is_dir():
        raise JobError(
            "mod_files_missing",
            f"The staged folder for '{mod_name}' is no longer available.",
            {"mod": mod_name, "path": str(destination)},
        )

    meta_path = destination / "meta.ini"
    from Nexus.nexus_meta import (
        normalise_game_domain,
        read_meta as read_nexus_meta,
    )
    nexus_meta = read_nexus_meta(meta_path)
    thunderstore_meta = None
    try:
        from Thunderstore.thunderstore_meta import read_meta as read_ts_meta
        candidate = read_ts_meta(meta_path)
        if candidate.package_id:
            thunderstore_meta = candidate
    except Exception:
        pass
    modio_meta = None
    if getattr(game, "game_id", "") == "baldurs_gate_3":
        from Utils.bg3.modio_loader import load_modio_module
        candidate = load_modio_module("modio_meta").read_modio_meta(meta_path)
        if candidate.mod_id > 0:
            modio_meta = candidate

    recorded_names: set[str] = set()
    if nexus_meta.installation_file:
        recorded_names.add(nexus_meta.installation_file)
    if thunderstore_meta is not None and thunderstore_meta.full_name:
        recorded_names.add(f"{thunderstore_meta.full_name}.zip")
    if modio_meta is not None and modio_meta.file_id > 0:
        recorded_names.add(f"modio-6715-{modio_meta.mod_id}-{modio_meta.file_id}.zip")
    nexus_identity = None
    if nexus_meta.mod_id > 0 and nexus_meta.file_id > 0:
        domain = normalise_game_domain(nexus_meta.game_domain)
        if not domain:
            raw_domains = getattr(game, "nexus_game_domains", ()) or ()
            if isinstance(raw_domains, str):
                raw_domains = (raw_domains,)
            domains = [
                normalise_game_domain(value) for value in raw_domains
                if normalise_game_domain(value)
            ]
            domain = domains[0] if domains else normalise_game_domain(
                getattr(game, "nexus_game_domain", "") or "")
        nexus_identity = (
            domain,
            int(nexus_meta.mod_id),
            int(nexus_meta.file_id),
        )
    if not recorded_names and nexus_identity is None:
        raise JobError(
            "reinstall_source_unknown",
            f"Mod '{mod_name}' has no recorded installation archive or exact "
            "Nexus file identity.",
            {"mod": mod_name},
        )

    return {
        "requested_profile": requested_profile,
        "target_profile": target_profile,
        "requested_mod": mod_name,
        "target_mod": target_name,
        "staging": staging,
        "destination": destination,
        "nexus_meta": nexus_meta,
        "thunderstore_meta": thunderstore_meta,
        "modio_meta": modio_meta,
        "recorded_names": recorded_names,
        "nexus_identity": nexus_identity,
        "grouped": grouped,
    }


def _select_reinstall_archive(
    inventory: dict[str, Any],
    context: dict[str, Any],
    archive_id: str | None,
) -> dict[str, Any]:
    archives = [
        archive for archive in inventory.get("archives", [])
        if isinstance(archive, dict)
    ]
    recorded_names = context["recorded_names"]
    nexus_identity = context["nexus_identity"]
    from Nexus.nexus_meta import normalise_game_domain

    def identity_conflicts(archive: dict[str, Any]) -> bool:
        thunderstore_meta = context.get("thunderstore_meta")
        identity = archive.get("thunderstore")
        if thunderstore_meta is not None and isinstance(identity, dict):
            if (identity.get("namespace"), identity.get("package"), identity.get("package_version")) != (thunderstore_meta.namespace, thunderstore_meta.name, thunderstore_meta.version):
                return True
        modio_meta = context.get("modio_meta")
        identity = archive.get("modio")
        if modio_meta is not None and isinstance(identity, dict):
            if (identity.get("game_id"), identity.get("mod_id"), identity.get("file_id")) != (6715, modio_meta.mod_id, modio_meta.file_id):
                return True
        if nexus_identity is None:
            return False
        identity = archive.get("nexus")
        if not isinstance(identity, dict):
            return False
        try:
            mod_id = int(identity.get("mod_id") or 0)
            file_id = int(identity.get("file_id") or 0)
        except (TypeError, ValueError):
            return True
        domain = normalise_game_domain(identity.get("domain") or "")
        return bool(
            (mod_id and mod_id != nexus_identity[1])
            or (file_id and file_id != nexus_identity[2])
            or (domain and nexus_identity[0] and domain != nexus_identity[0])
        )

    by_name = [
        archive for archive in archives
        if archive.get("name") in recorded_names
        and not identity_conflicts(archive)
    ]
    by_identity = []
    if nexus_identity is not None:
        for archive in archives:
            identity = archive.get("nexus")
            if not isinstance(identity, dict):
                continue
            try:
                candidate = (
                    normalise_game_domain(identity.get("domain") or ""),
                    int(identity.get("mod_id") or 0),
                    int(identity.get("file_id") or 0),
                )
            except (TypeError, ValueError):
                continue
            if candidate == nexus_identity and not identity_conflicts(archive):
                by_identity.append(archive)
    candidates = by_name or by_identity
    if not candidates and context.get("modio_meta") is not None:
        meta = context["modio_meta"]
        candidates = [archive for archive in archives if (archive.get("modio") or {}).get("game_id") == 6715
                      and archive["modio"].get("mod_id") == meta.mod_id and archive["modio"].get("file_id") == meta.file_id
                      and not identity_conflicts(archive)]
    if not candidates and context.get("thunderstore_meta") is not None:
        meta = context["thunderstore_meta"]
        candidates = [archive for archive in archives if (archive.get("thunderstore") or {}).get("namespace") == meta.namespace
                      and archive["thunderstore"].get("package") == meta.name and archive["thunderstore"].get("package_version") == meta.version
                      and not identity_conflicts(archive)]

    expected_nexus = (
        {
            "domain": nexus_identity[0] or None,
            "mod_id": nexus_identity[1],
            "file_id": nexus_identity[2],
        }
        if nexus_identity is not None else None
    )
    if archive_id is not None:
        selected = next(
            (archive for archive in archives
             if archive.get("id") == archive_id),
            None,
        )
        if selected is None:
            raise JobError(
                "archive_not_found",
                "The selected downloaded archive is no longer available.",
                {"archive_id": archive_id},
            )
        if not any(
            candidate.get("id") == archive_id for candidate in candidates
        ):
            raise JobError(
                "reinstall_archive_mismatch",
                "The selected archive does not match the mod's recorded "
                "installation source.",
                {
                    "archive_id": archive_id,
                    "mod": context["requested_mod"],
                    "recorded_names": sorted(recorded_names),
                    "nexus": expected_nexus,
                },
            )
        return selected

    if not candidates:
        raise JobError(
            "archive_not_found",
            "The mod's recorded installation archive was not found in an "
            "enabled download location.",
            {
                "mod": context["requested_mod"],
                "recorded_names": sorted(recorded_names),
                "nexus": expected_nexus,
            },
        )
    if len(candidates) > 1:
        raise JobError(
            "reinstall_archive_ambiguous",
            "More than one downloaded archive matches the mod; select one by "
            "archive_id.",
            {
                "mod": context["requested_mod"],
                "candidates": [
                    {
                        "archive_id": candidate.get("id"),
                        "name": candidate.get("name"),
                        "location_kind": candidate.get("location_kind"),
                    }
                    for candidate in candidates
                ],
            },
        )
    return candidates[0]


def _validated_archive_path(inventory: dict, archive: dict) -> Path:
    locations = {item["id"]: item for item in inventory["locations"]}
    location = locations.get(archive.get("location_id"))
    candidate = Path(str(archive.get("path") or ""))
    try:
        root = Path(str(location["path"])).resolve(strict=True)
        resolved = candidate.resolve(strict=True)
        safe = (not candidate.is_symlink() and candidate.is_file()
                and resolved.parent == root)
    except (KeyError, OSError, RuntimeError, TypeError, ValueError):
        safe = False
        resolved = candidate
    if not safe:
        raise JobError(
            "unsafe_archive",
            "The selected archive is not a regular file in its download location.",
            {"archive_id": archive.get("id")},
        )
    return resolved


def _installer_choice_details(prepared, installer: str) -> dict:
    details = {
        "installer": installer,
        "mod": prepared.mod_name,
        "supported_policies": ["reject", "defaults"],
    }
    if prepared.is_bain():
        available = {pkg.name for pkg in prepared.bain_subpkgs}
        details["selection_parameter"] = "bain_selected"
        details["subpackages"] = [
            {"name": pkg.name, "label": pkg.display_name,
             "default_selected": pkg.default_selected,
             "file_count": len(pkg.file_keys or ())}
            for pkg in prepared.bain_subpkgs]
        saved = prepared.saved_bain_selections
        if isinstance(saved, dict) and isinstance(saved.get("selected"), list):
            details["saved_selected"] = [
                name for name in saved["selected"]
                if isinstance(name, str) and name in available]
    if prepared.is_fomod():
        from .fomod import describe_choices
        details.update(describe_choices(prepared))
        details["selection_parameter"] = "fomod_selections"
    if prepared.is_bundle():
        from .bundle import describe_choices
        details.update(describe_choices(prepared))
        details["selection_parameter"] = "bundle_selected"
        from Utils.re_engine.bundle import detect_multi_mod
        multi = detect_multi_mod(prepared.bundle_root)
        if multi:
            details["independent_mods"] = [name for name, _path in multi]
    if prepared.is_multi_mod():
        details["mods"] = [name for name, _path in prepared.multi_mods]
    return details


def _multi_mod_targets(prepared, staging_root: Path,
                       archive_path: Path) -> list[dict[str, Any]]:
    from Utils.mods.install import validate_mod_folder_name

    targets = []
    seen = set()
    for raw_name, _source in prepared.multi_mods:
        name = validate_mod_folder_name(raw_name)
        if name.casefold() in seen:
            raise JobError("installer_selection_invalid",
                           "The archive contains duplicate mod folder names.",
                           {"mod": name})
        seen.add(name.casefold())
        destination = staging_root / name
        try:
            inside = archive_path.is_relative_to(
                destination.resolve(strict=False))
        except (OSError, RuntimeError, ValueError):
            inside = True
        if inside or destination.is_symlink() or (
                destination.exists() and not destination.is_dir()):
            raise JobError("unsafe_install_destination",
                           "A multi-mod destination is unsafe.",
                           {"mod": name, "path": str(destination)})
        targets.append({"name": name, "path": str(destination),
                        "exists": destination.is_dir()})
    return targets


def _inspect_download(game: Any, profile_name: str, archive_id: str,
                      preferred_name: str, log: Callable,
                      progress: Callable, cancel: threading.Event, archive_path=None) -> dict:
    from Utils.downloads.inventory import build_inventory
    from Utils.mods.copy import resolve_target_staging
    from Utils.mods.install import (
        UnsafeInstallPath, prepare_archive, validate_mod_folder_name)
    from Utils.operations import acquire_game_operation
    from Utils.profiles.groups import get_members, is_group

    profile = Path(game.get_profile_root()) / "profiles" / profile_name
    with acquire_game_operation(
        game, "download_inspect", owner="headless:downloads.inspect",
    ):
        if cancel.is_set():
            raise JobCancelled("Archive inspection cancelled.")
        if not profile.is_dir() or profile.is_symlink():
            raise JobError("profile_not_found", "The selected profile is unavailable.",
                           {"profile": profile_name})
        if is_group(profile):
            raise JobError("profile_group_requires_member",
                           "Inspect installation for a member profile.",
                           {"profile": profile.name, "members": get_members(profile)})
        from .batch import cached_read
        inventory = cached_read(("downloads.inventory", str(profile)), lambda: build_inventory(game, profile))
        archive = next((item for item in inventory["archives"]
                        if item.get("id") == archive_id), None)
        if archive is None and archive_path is None:
            raise JobError("archive_not_found",
                           "The selected downloaded archive is no longer available.",
                           {"archive_id": archive_id})
        if archive_path is not None:
            from .install_options import local_archive
            archive = local_archive(archive_path)
            archive_id = archive["id"]
        if archive is None:
            raise JobError("archive_not_found", "The selected archive is unavailable.")
        resolved = Path(archive["path"]) if archive_path is not None else _validated_archive_path(inventory, archive)
        progress(0, 1, "inspect_archive")
        try:
            prepared = prepare_archive(
                str(resolved), game, profile, log_fn=log,
                progress_fn=progress, preferred_name=preferred_name,
                cancel=cancel)
        except UnsafeInstallPath as exc:
            raise JobError("unsafe_install_destination",
                           "The archive resolved to an unsafe mod folder name.") from exc
        if prepared is None:
            if cancel.is_set():
                raise JobCancelled("Archive inspection cancelled.")
            raise JobError("install_failed",
                           "The archive could not be prepared for inspection.",
                           {"archive_id": archive_id})
        try:
            if cancel.is_set():
                raise JobCancelled("Archive inspection cancelled.")
            try:
                name = validate_mod_folder_name(prepared.mod_name)
            except UnsafeInstallPath as exc:
                raise JobError("unsafe_install_destination",
                               "The archive resolved to an unsafe mod folder name.") from exc
            target_staging = resolve_target_staging(game, profile)
            if target_staging is None:
                raise JobError("unsafe_install_destination",
                               "The selected profile has no staging folder.")
            staging_root = Path(target_staging)
            if prepared.is_multi_mod():
                mods = _multi_mod_targets(prepared, staging_root, resolved)
                progress(1, 1, "inspect_archive")
                return {
                    "archive": {"id": archive_id, "name": archive.get("name"),
                                "path": str(resolved)},
                    "profile": profile.name,
                    "mod": None,
                    "mods": mods,
                    "installer": {
                        "type": "multi_mod", "supported": True,
                        "choices_required": True,
                        "choices": _installer_choice_details(prepared, "multi_mod"),
                    },
                }
            destination = staging_root / name
            try:
                source_inside_destination = resolved.is_relative_to(
                    destination.resolve(strict=False))
            except (OSError, RuntimeError, ValueError):
                source_inside_destination = True
            if source_inside_destination:
                raise JobError("unsafe_install_destination",
                               "The destination mod folder contains the archive.",
                               {"mod": name})
            if destination.is_symlink() or (
                    destination.exists() and not destination.is_dir()):
                raise JobError("unsafe_install_destination",
                               "The destination is not a safe mod folder.",
                               {"mod": name})
            if prepared.plugin_install is not None:
                installer = prepared.plugin_install.summary()
            elif prepared.is_scripted_fomod():
                installer = {"type": "scripted_fomod", "supported": False,
                             "reason": "This installer requires its desktop installer window.",
                             "fallback_policy": "verbatim"}
            elif prepared.installer_kind != "plain":
                installer = {"type": prepared.installer_kind, "supported": False,
                             "reason": "A compatible installer plugin is required."}
            elif prepared.fomod_parse_failed:
                installer = {"type": "fomod", "supported": False,
                             "reason": "The FOMOD configuration could not be parsed.",
                             "fallback_policy": "verbatim"}
            else:
                kind = ("fomod" if prepared.is_fomod() else
                        "bain" if prepared.is_bain() else
                        "bundle" if prepared.is_bundle() else "plain")
                required = (prepared.fomod_has_steps() if kind == "fomod" else
                            kind in {"bain", "bundle"})
                installer = {"type": kind, "supported": True,
                             "choices_required": required}
                if required:
                    installer["choices"] = _installer_choice_details(
                        prepared, kind)
                if kind == "fomod":
                    from Utils.fomod.installer import check_module_dependencies
                    satisfied, missing = check_module_dependencies(
                        prepared.fomod_config, *prepared.fomod_context)
                    installer["dependencies_satisfied"] = satisfied
                    if not satisfied:
                        installer["missing_dependencies"] = missing
            progress(1, 1, "inspect_archive")
            layout = None
            if installer["type"] == "plain":
                from Utils.mods.install import stage_file_list
                layout = {"choice_required": False}
                def inspect_layout(required, files, mod):
                    layout.update(choice_required=True, required_directories=sorted(required))
                    return ""
                files = stage_file_list(game, str(prepared.extract_dir), mod_name=name,
                                        on_need_prefix=inspect_layout, log_fn=log)
                layout.update(files=[destination for _source, destination, _folder in (files or [])[:100]],
                              truncated=len(files or []) > 100)
            return {
                "archive": {"id": archive_id, "name": archive.get("name"),
                            "path": str(resolved)},
                "profile": profile.name,
                "mod": {"name": name, "path": str(destination),
                        "exists": destination.is_dir()},
                "installer": installer,
                "layout": layout,
            }
        finally:
            prepared.cleanup()


def _install_download(job: "_Job", log: Callable, progress: Callable, *,
                      replacement=None, prebuilt_meta=None, verify_install=None,
                      before_install=None, operation_locked=False, verify_prepared=None) -> dict:
    from Utils.downloads.inventory import build_inventory
    from Utils.operations import acquire_game_operation

    requested_profile = (
        Path(job.game.get_profile_root()) / "profiles" / str(job.profile or ""))
    operation = "reinstall" if job.kind == "mods.reinstall" else "install"
    with (nullcontext() if operation_locked else acquire_game_operation(
        job.game, operation, owner=f"headless:{job.id}",
    )):
        if not requested_profile.is_dir():
            raise JobError(
                "profile_not_found",
                f"Profile '{job.profile}' is no longer available.",
                {"profile": job.profile},
            )
        if requested_profile.is_symlink():
            raise JobError(
                "unsafe_profile_path",
                f"Profile '{requested_profile.name}' is a symbolic link and "
                "cannot be modified.",
                {"profile": requested_profile.name,
                 "path": str(requested_profile)},
            )

        reinstall = replacement
        profile_dir = requested_profile
        archive_id = str(job.options.get("archive_id") or "") or None
        if replacement is not None:
            profile_dir = replacement["target_profile"]
        elif job.kind == "mods.reinstall":
            reinstall = _reinstall_target(
                job.game,
                requested_profile,
                str(job.options.get("mod") or ""),
            )
            profile_dir = reinstall["target_profile"]
        else:
            from Utils.profiles.groups import get_members, is_group
            if is_group(profile_dir):
                raise JobError(
                    "profile_group_requires_member",
                    "Install into a member profile rather than a profile group.",
                    {
                        "profile": profile_dir.name,
                        "members": get_members(profile_dir),
                    },
                )

        progress(0, 1, "locate_archive")
        from .batch import cached_read
        inventory = cached_read(("downloads.inventory", str(requested_profile)),
                                lambda: build_inventory(job.game, requested_profile))
        if reinstall is not None and replacement is None:
            archive = _select_reinstall_archive(
                inventory, reinstall, archive_id)
            archive_id = str(archive.get("id") or "")
        else:
            archive = next(
                (item for item in inventory["archives"]
                 if item.get("id") == archive_id),
                None,
            )
        archive_path = job.options.get("archive_path")
        if archive_path is not None:
            from .install_options import local_archive
            archive = local_archive(archive_path)
            archive_id = archive["id"]
        if archive is None:
            raise JobError(
                "archive_not_found",
                "The selected downloaded archive is no longer available.",
                {"archive_id": archive_id},
            )
        resolved = Path(archive["path"]) if archive_path is not None else _validated_archive_path(inventory, archive)
        from .download_archives import _token
        archive_token = _token(resolved, required=True)
        progress(1, 1, "locate_archive")

        from Utils.mods.install import (
            InstallRollbackError,
            UnsafeInstallPath,
            finish_install,
            prepare_archive,
        )

        prepared = None
        source_download = None
        source_metadata = {}
        try:
            preferred_name = str(job.options.get("name") or "")
            if reinstall is not None:
                preferred_name = reinstall["target_mod"]
                from Nexus.nexus_meta import (
                    has_reinstall_carryover,
                    merge_reinstall_metadata,
                )
                installed_meta = reinstall["nexus_meta"]
                if prebuilt_meta is None and has_reinstall_carryover(installed_meta):
                    prebuilt_meta = merge_reinstall_metadata(
                        None, installed_meta)
            if job.kind in {"downloads.install", "mods.reinstall"} and replacement is None:
                from .thunderstore import downloaded_metadata as thunderstore_metadata
                from .modio_versions import downloaded_metadata as modio_metadata
                for source, reader in (("thunderstore", thunderstore_metadata), ("modio", modio_metadata)):
                    retained = reader(resolved, job.game)
                    if retained is not None:
                        if source_download is not None:
                            raise JobError("install_source_ambiguous", "The archive has conflicting source metadata sidecars.")
                        source_download = retained
                        if reinstall is None:
                            source_metadata = {f"{source}_meta": retained[1]}
                if source_download is not None and prebuilt_meta is None:
                    prebuilt_meta = source_download[0]
            prepared = prepare_archive(
                str(resolved),
                job.game,
                profile_dir,
                log_fn=log,
                progress_fn=progress,
                preferred_name=preferred_name,
                prebuilt_meta=prebuilt_meta,
                cancel=getattr(job, "cancel_event", None),
            )
            if prepared is None:
                if getattr(job, "cancel_event", None) is not None and job.cancel_event.is_set():
                    raise JobCancelled("Archive preparation cancelled.")
                raise JobError(
                    "install_failed",
                    "The archive could not be prepared for installation.",
                    {"archive_id": archive_id},
                )
            expected_installer = job.options.get("installer_revision")
            if verify_prepared is not None:
                verify_prepared(prepared, resolved)
            if expected_installer is not None and (prepared.plugin_install is None
                    or prepared.plugin_install.revision != expected_installer):
                raise JobError("stale_plugin_installer", "The plugin installer plan changed; inspect the archive again.")
            installer_policy = str(
                job.options.get("installer_policy") or "reject")
            if prepared.is_scripted_fomod() and installer_policy == "verbatim":
                prepared.plugin_install = None
                prepared.installer_kind = "plain"
            if prepared.has_interactive_installer() or (prepared.plugin_install is None
                    and prepared.installer_kind != "plain" and not prepared.is_scripted_fomod()):
                raise JobError("installer_unsupported",
                               "Install this package through Amethyst's desktop interface with a compatible plugin.",
                               {"installer": "plugin" if prepared.plugin_install is not None else prepared.installer_kind})
            fallback_type = (
                "scripted_fomod" if prepared.is_scripted_fomod() and prepared.plugin_install is None else
                "fomod" if prepared.fomod_parse_failed else None)
            if fallback_type and installer_policy != "verbatim":
                raise JobError(
                    "installer_unsupported",
                    "FOMOD options are unavailable; choose the verbatim fallback explicitly.",
                    {"installer": fallback_type, "mod": prepared.mod_name,
                     "fallback_policy": "verbatim"},
                )
            if not fallback_type and installer_policy == "verbatim":
                raise JobError(
                    "installer_selection_mismatch",
                    "Verbatim fallback is only available for scripted or unparseable FOMODs.",
                    {"mod": prepared.mod_name},
                )
            if fallback_type:
                log("FOMOD options are unavailable; installing archive files as-is.")
            from .install_options import layout_resolver, select_multi
            select_multi(prepared, job.options, reinstall)
            requests = getattr(job, "installer_requests", None)
            prepared.on_need_prefix = layout_resolver(
                job.options.get("content_prefix"), supplied="content_prefix" in job.options,
                requests=requests)

            bain_selected = job.options.get("bain_selected")
            fomod_selections = job.options.get("fomod_selections")
            bundle_selected = job.options.get("bundle_selected")
            if bain_selected is not None:
                if not prepared.is_bain():
                    raise JobError("installer_selection_mismatch",
                                   "BAIN subpackage choices were supplied for a different installer.",
                                   {"mod": prepared.mod_name})
                available = {pkg.name for pkg in prepared.bain_subpkgs}
                unknown = [name for name in bain_selected if name not in available]
                if unknown:
                    raise JobError("installer_selection_invalid",
                                   "The BAIN subpackage selection is no longer available.",
                                   {"mod": prepared.mod_name, "unknown": unknown,
                                    "available": sorted(available)})
            if fomod_selections is not None and not prepared.is_fomod():
                raise JobError("installer_selection_mismatch",
                               "FOMOD choices were supplied for a different installer.",
                               {"mod": prepared.mod_name})
            if bundle_selected is not None:
                if not prepared.is_bundle():
                    raise JobError("installer_selection_mismatch",
                                   "Bundle choices were supplied for a different installer.",
                                   {"mod": prepared.mod_name})
                from .bundle import validate_choices
                validate_choices(prepared, bundle_selected)

            if fallback_type:
                installer = fallback_type
                choices = False
            elif prepared.plugin_install is not None:
                installer = "plugin"
                choices = False
            elif prepared.is_fomod():
                installer = "fomod"
                if fomod_selections is None and requests is not None and prepared.fomod_has_steps():
                    fomod_selections = requests.fomod(
                        prepared.fomod_config, prepared.fomod_base, prepared.mod_name,
                        *prepared.fomod_context, prepared.saved_fomod_selections)
                choices = prepared.fomod_has_steps() and fomod_selections is None
            elif prepared.is_bain():
                installer = "bain"
                if bain_selected is None and requests is not None:
                    bain_selected = requests.bain(prepared.bain_subpkgs, prepared.bain_root, prepared.mod_name)["selected"]
                choices = bain_selected is None
            elif prepared.is_bundle():
                installer = "bundle"
                choices = bundle_selected is None
            elif prepared.is_multi_mod():
                installer = "multi_mod"
                choices = not prepared._multi_explicit
            else:
                installer = "plain"
                choices = False
            if prepared.is_fomod():
                from Utils.fomod.installer import check_module_dependencies
                satisfied, missing = check_module_dependencies(
                    prepared.fomod_config, *prepared.fomod_context)
                if not satisfied:
                    raise JobError(
                        "installer_dependencies_missing",
                        "The FOMOD module requirements are not met.",
                        {"mod": prepared.mod_name, "requirements": missing},
                    )
                if fomod_selections is not None:
                    from .fomod import validate_choices
                    validate_choices(prepared, fomod_selections)
            if choices and installer_policy != "defaults":
                details = _installer_choice_details(prepared, installer)
                raise JobError(
                    "installer_choices_required",
                    "The archive needs installer choices.",
                    details,
                )
            from Utils.mods.copy import resolve_target_staging
            staging_root = Path(resolve_target_staging(job.game, profile_dir))
            if_existing = str(job.options.get("if_exists") or "fail")
            targets = []
            if prepared.is_multi_mod():
                targets = _multi_mod_targets(prepared, staging_root, resolved)
                conflicts = [target["name"] for target in targets
                             if target["exists"] and getattr(prepared, "_multi_policies", {}).get(target["name"], if_existing) != "replace"]
                existing = any(target["exists"] for target in targets)
                if conflicts:
                    raise JobError(
                        "mod_already_exists",
                        "One or more mods from this archive already exist.",
                        {"mods": conflicts},
                    )
            else:
                destination = staging_root / prepared.mod_name
                try:
                    source_inside_destination = resolved.is_relative_to(
                        destination.resolve(strict=False))
                except (OSError, RuntimeError, ValueError):
                    source_inside_destination = True
                if source_inside_destination:
                    raise JobError(
                        "unsafe_install_destination",
                        "The destination mod folder contains the source archive.",
                        {"mod": prepared.mod_name},
                    )
                if destination.is_symlink():
                    raise JobError(
                        "unsafe_install_destination",
                        "The destination mod folder is a symbolic link.",
                        {"mod": prepared.mod_name},
                    )
                existing = destination.exists()
                if existing and not destination.is_dir():
                    raise JobError(
                        "unsafe_install_destination",
                        "The destination exists but is not a mod folder.",
                        {"mod": prepared.mod_name},
                    )
                if existing and if_existing != "replace":
                    raise JobError(
                        "mod_already_exists",
                        f"Mod '{prepared.mod_name}' already exists.",
                        {"mod": prepared.mod_name},
                    )

            late_conflict = {"seen": False}

            def reject_existing(_name, _conflict, _prepared):
                late_conflict["seen"] = True
                return "cancel"

            if before_install is not None:
                before_install()
            if _token(resolved, required=True) != archive_token:
                raise JobError("installer_archive_changed", "The archive changed during preparation; inspect it again.")
            cancel = getattr(job, "cancel_event", None)
            if cancel is not None and cancel.is_set():
                raise JobCancelled("Installation cancelled before staging.")
            installation_verify = verify_install
            reinstall_sources = reinstall if reinstall is not None and replacement is None else {}
            if source_download is not None or reinstall_sources:
                from .download_archives import _token
                from .mod_versions import _stamp_sources
                if source_download is not None:
                    _, _, sidecar, tokens = source_download
                    if [_token(path, required=True) for path in (resolved, sidecar)] != tokens:
                        raise JobError("mod_version_archive_changed", "The downloaded archive or its identity changed during preparation.")
                for source in ("thunderstore", "modio"):
                    if reinstall_sources.get(f"{source}_meta") is not None:
                        reinstall_sources[f"{source}_meta"].installed = datetime.now().isoformat(timespec="seconds")
                def installation_verify(name):
                    try:
                        for installed_name in prepared.installed_mod_names or [name]:
                            _stamp_sources(reinstall_sources, staging_root / installed_name / "meta.ini", **source_metadata)
                    except Exception as exc:
                        raise JobError("reinstall_metadata_failed" if reinstall_sources else "install_source_metadata_failed",
                                       "The installed source metadata could not be verified.",
                                       {"mod": name, "disk_changed": True}) from exc
                    if verify_install is not None:
                        verify_install(name)
            from Utils.ui.hooks import choice_handler
            with choice_handler(requests.choice if requests is not None else None):
                installed = finish_install(
                    prepared,
                    fomod_selections,
                    log_fn=log,
                    progress_fn=progress,
                    on_exists=None if if_existing == "replace" else reject_existing,
                    bain_selections=({"selected": bain_selected}
                                     if bain_selected is not None else None),
                    bundle_selected=bundle_selected,
                    interactive=False,
                    verify_install=installation_verify,
                    cancel=cancel,
                )
            if installed is None:
                if late_conflict["seen"]:
                    raise JobError(
                        "mod_already_exists",
                        "The destination mod folder appeared during installation.",
                        {"mod": prepared.mod_name,
                         "installed_mods": prepared.installed_mod_names,
                         "disk_changed": bool(prepared.installed_mod_names)},
                    )
                raise JobError(
                    "install_failed",
                    "The archive did not produce an installed mod.",
                    {"archive_id": archive_id},
                )

            installed_path = staging_root / installed
            if reinstall is not None and replacement is None:
                if reinstall["grouped"]:
                    try:
                        from Utils.profiles.groups import materialize_group
                        materialize_group(
                            job.game, requested_profile, log_fn=log)
                    except Exception as exc:
                        raise JobError(
                            "profile_group_refresh_failed",
                            "The owner mod was reinstalled, but the profile "
                            "group could not be refreshed.",
                            {
                                "profile": requested_profile.name,
                                "owner_profile": profile_dir.name,
                                "mod": reinstall["requested_mod"],
                                "disk_changed": True,
                                "reason": str(exc),
                            },
                        ) from exc
            installed_names = (prepared.installed_mod_names
                               if prepared.is_multi_mod() else [installed])
            enabled_by_name = {}
            try:
                from Utils.mods.modlist import read_modlist
                enabled_by_name = {
                    item.name: bool(item.enabled)
                    for item in read_modlist(profile_dir / "modlist.txt")
                    if not item.is_separator
                }
            except Exception:
                pass
            mod_results = [
                {"name": name, "path": str(staging_root / name),
                 "enabled": enabled_by_name.get(name)}
                for name in installed_names
            ]
            result = {
                "ok": True,
                "profile": profile_dir.name,
                "archive": {
                    "id": archive_id,
                    "name": archive.get("name"),
                    "path": str(resolved),
                    "location_kind": archive.get("location_kind"),
                    "nexus": archive.get("nexus"),
                    "retained": resolved.is_file(),
                },
                "mod": mod_results[0],
                "installer": {
                    **(prepared.plugin_install.summary() if prepared.plugin_install is not None else {}),
                    "type": installer,
                    "policy": ("explicit" if bain_selected is not None
                               or fomod_selections is not None
                               or bundle_selected is not None else installer_policy),
                    "used_defaults": bool(choices),
                },
                "replaced": (bool(prepared.replaced_mod_names)
                             if prepared.is_multi_mod() else existing),
            }
            if prepared.is_multi_mod():
                result["mods"] = [
                    {**item, "replaced": item["name"] in prepared.replaced_mod_names}
                    for item in mod_results
                ]
            if reinstall is not None and replacement is None:
                result["reinstall"] = {
                    "requested_profile": requested_profile.name,
                    "owner_profile": profile_dir.name,
                    "requested_mod": reinstall["requested_mod"],
                    "installed_mod": installed,
                    "source": "recorded_archive",
                }
            return result
        except JobCancelled as exc:
            partial = list(getattr(prepared, "installed_mod_names", ()) or ())
            exc.details.update(installed_mods=partial, disk_changed=bool(partial))
            raise
        except JobError:
            raise
        except InstallRollbackError as exc:
            raise JobError("install_rollback_failed", str(exc),
                           {"backup_path": exc.backup_path, "disk_changed": True}) from exc
        except UnsafeInstallPath as exc:
            partial = list(getattr(prepared, "installed_mod_names", ()) or ())
            replaced = list(getattr(prepared, "replaced_mod_names", ()) or ())
            raise JobError(
                "unsafe_install_destination",
                "The archive resolved to an unsafe mod folder name.",
                {"installed_mods": partial, "replaced_mods": replaced,
                 "disk_changed": bool(partial or replaced)},
            ) from exc
        except Exception as exc:
            partial = list(getattr(prepared, "installed_mod_names", ()) or ())
            replaced = list(getattr(prepared, "replaced_mod_names", ()) or ())
            if isinstance(exc, InterruptedError) and getattr(job, "cancel_event", None) is not None and job.cancel_event.is_set():
                raise JobCancelled("Installation cancelled; the current mod was rolled back.",
                                   {"installed_mods": partial, "disk_changed": bool(partial)}) from exc
            raise JobError(
                "install_failed",
                str(exc) if isinstance(exc, ValueError) and str(exc) else "The archive could not be installed.",
                {"exception": type(exc).__name__,
                 "installed_mods": partial, "replaced_mods": replaced,
                 "disk_changed": bool(partial or replaced)},
            ) from exc
        finally:
            if prepared is not None:
                prepared.cleanup()


@dataclass
class _Job:
    id: str
    kind: str
    game: Any = field(repr=False)
    game_info: dict[str, str] | None
    profile: str | None
    options: dict[str, Any]
    cancel_event: Any = field(default=None, repr=False)
    installer_requests: Any = field(default=None, repr=False)
    runner: Callable[[Callable[..., None], Callable[..., None]],
                     dict[str, Any]] | None = field(default=None, repr=False)
    cancel_callback: Callable[[], None] | None = field(
        default=None, repr=False)
    pause_callback: Callable[[], None] | None = field(
        default=None, repr=False)
    resume_callback: Callable[[], None] | None = field(
        default=None, repr=False)
    cancellable: bool = False
    pausable: bool = False
    cancel_requested: bool = False
    paused: bool = False
    requires_client: bool = False
    waiting_for: dict[str, Any] | None = None
    state: str = "queued"
    last_event_history_at: float = 0.0
    created_at: str = field(default_factory=_utc_now)
    started_at: str | None = None
    finished_at: str | None = None
    progress: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    sequence: int = 0
    history_persisted: bool = False
    events: deque[dict[str, Any]] = field(default_factory=deque)
    last_progress_event_at: float = field(default=0.0, repr=False)
    last_progress_phase: str | None = field(default=None, repr=False)


class JobManager:
    def __init__(self, *, max_jobs: int = 100, max_events: int = 2048, history=None):
        self._history = history
        self._max_jobs = max_jobs
        self._max_events = max_events
        self._jobs: dict[str, _Job] = {}
        self._condition = threading.Condition()
        self._queue: queue.Queue[_Job | None] = queue.Queue()
        self._closed = False
        self._preparation = threading.local()
        self._execution = threading.local()
        self._worker = threading.Thread(
            target=self._work, name="amethyst-headless-jobs", daemon=True)
        self._worker.start()

    def submit(
        self,
        kind: str,
        game: Any,
        game_info: dict[str, str] | None,
        profile: str | None,
        options: dict[str, Any] | None = None,
        *,
        runner: Callable[[Callable[..., None], Callable[..., None]],
                         dict[str, Any]] | None = None,
        cancel: Callable[[], None] | None = None,
        pause: Callable[[], None] | None = None,
        resume: Callable[[], None] | None = None,
        installer_requests: Any = None,
        requires_client: bool = False,
    ) -> dict[str, Any]:
        if (pause is None) != (resume is None):
            raise ValueError("Pause and resume callbacks must be supplied together.")
        cancel_event = threading.Event() if kind in {"downloads.install", "mods.reinstall"} else None
        if cancel_event is not None and cancel is None:
            cancel = cancel_event.set
        if installer_requests is not None and cancel_event is not None:
            installer_requests.stop = cancel_event
        job = _Job(
            id=uuid.uuid4().hex,
            kind=kind,
            game=game,
            game_info=dict(game_info) if game_info is not None else None,
            profile=profile,
            options=dict(options or {}),
            cancel_event=cancel_event,
            installer_requests=installer_requests,
            runner=runner,
            cancel_callback=cancel,
            pause_callback=pause,
            resume_callback=resume,
            cancellable=cancel is not None,
            pausable=pause is not None,
            requires_client=bool(requires_client or installer_requests is not None
                                 or (options or {}).get("manual_interactive")),
            events=deque(maxlen=self._max_events),
        )
        prepared = getattr(self._preparation, "jobs", None)
        if prepared is not None:
            prepared.append(job)
            return self._snapshot_locked(job)
        with self._condition:
            if self._closed:
                raise RuntimeError("The job manager is closed.")
            self._prune_locked()
            self._jobs[job.id] = job
            if installer_requests is not None:
                installer_requests.bind(self, job.id)
            self._event_locked(job, "state", state="queued")
            snapshot = self._snapshot_locked(job)
            self._queue.put(job)
        return snapshot

    @contextmanager
    def prepare(self):
        if getattr(self._preparation, "jobs", None) is not None:
            raise RuntimeError("Nested job preparation is unsupported.")
        jobs = self._preparation.jobs = []
        try:
            yield jobs
        finally:
            del self._preparation.jobs

    def get(self, job_id: str) -> dict[str, Any]:
        with self._condition:
            return self._snapshot_locked(self._require_locked(job_id))

    def emit(self, job_id: str, event_type: str, **data: Any) -> None:
        with self._condition:
            job = self._require_locked(job_id)
            if job.state not in _FINAL_STATES:
                self._event_locked(job, event_type, **data)

    def installer_requests(self, job_id):
        with self._condition:
            requests = self._require_locked(job_id).installer_requests
            if requests is None:
                raise JobError("installer_requests_unavailable", "Start the installation with interactive:true to answer installer questions.")
            return requests

    def set_waiting(self, job_id, request):
        with self._condition:
            job = self._require_locked(job_id)
            if job.state in _FINAL_STATES:
                return
            job.waiting_for = deepcopy(request)
            if request is not None:
                job.requires_client = True
            if job.state != "queued":
                job.state = "waiting_for_input" if request is not None else "running"
            self._event_locked(job, "state", state=job.state,
                               waiting_for=deepcopy(job.waiting_for), requires_client=job.requires_client)

    @contextmanager
    def input_wait(self, request):
        job = getattr(self._execution, "job", None)
        if job is None:
            raise RuntimeError("Input waits must run inside a job worker.")
        previous = deepcopy(job.waiting_for)
        self.set_waiting(job.id, request)
        try:
            yield
        finally:
            self.set_waiting(job.id, previous)

    def cancel_client_jobs(self):
        with self._condition:
            pending = [(job.id, job.cancellable, job.installer_requests) for job in self._jobs.values()
                       if job.requires_client and job.state not in _FINAL_STATES]
        for job_id, cancellable, requests in pending:
            if cancellable:
                self.cancel(job_id)
            if requests is not None:
                requests.stop.set()

    def cancel_installers(self):
        with self._condition:
            controls = [(job.installer_requests.stop, job.cancel_callback) for job in self._jobs.values()
                        if job.installer_requests is not None and job.state not in _FINAL_STATES]
        for control, cancel in controls:
            if cancel is not None:
                cancel()
            control.set()

    def cancel(self, job_id: str) -> dict[str, Any]:
        with self._condition:
            job = self._require_locked(job_id)
            if job.state in _FINAL_STATES:
                return self._snapshot_locked(job)
            if not job.cancellable or job.cancel_callback is None:
                raise JobNotCancellable(job_id)
            callback = job.cancel_callback
            if not job.cancel_requested:
                job.cancel_requested = True
                job.paused = False
                self._event_locked(job, "cancel", requested=True)
            snapshot = self._snapshot_locked(job)
        callback()
        return snapshot

    def pause(self, job_id: str) -> dict[str, Any]:
        with self._condition:
            job = self._require_locked(job_id)
            if job.state in _FINAL_STATES:
                return self._snapshot_locked(job)
            if (self._closed or not job.pausable or job.cancel_requested
                    or job.pause_callback is None):
                raise JobNotPausable(job_id)
            if not job.paused:
                job.pause_callback()
                job.paused = True
                self._event_locked(job, "pause", paused=True)
            return self._snapshot_locked(job)

    def resume(self, job_id: str) -> dict[str, Any]:
        with self._condition:
            job = self._require_locked(job_id)
            if job.state in _FINAL_STATES:
                return self._snapshot_locked(job)
            if not job.pausable or job.resume_callback is None:
                raise JobNotPausable(job_id)
            if job.paused:
                job.resume_callback()
                job.paused = False
                self._event_locked(job, "pause", paused=False)
            return self._snapshot_locked(job)

    def list(self, *, active_only: bool = False) -> list[dict[str, Any]]:
        with self._condition:
            jobs = list(self._jobs.values())
            if active_only:
                jobs = [job for job in jobs if job.state not in _FINAL_STATES]
            jobs.reverse()
            return [self._snapshot_locked(job) for job in jobs]

    def active(self) -> dict[str, Any] | None:
        with self._condition:
            for job in self._jobs.values():
                if job.state not in _FINAL_STATES:
                    return self._snapshot_locked(job)
        return None

    def events(
        self,
        job_id: str,
        *,
        after: int = 0,
        wait_ms: int = 0,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + (wait_ms / 1000)
        with self._condition:
            job = self._require_locked(job_id)
            while (job.sequence <= after
                   and job.state not in _FINAL_STATES
                   and wait_ms > 0):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(remaining)
                job = self._require_locked(job_id)

            oldest = job.events[0]["sequence"] if job.events else job.sequence + 1
            events = [event for event in job.events
                      if event["sequence"] > after]
            return {
                "job_id": job.id,
                "events": [dict(event) for event in events],
                "next_sequence": (
                    events[-1]["sequence"] if events else after),
                "oldest_sequence": oldest,
                "truncated": bool(job.events and after < oldest - 1),
                "complete": job.state in _FINAL_STATES,
            }

    def close(self, *, wait: bool = True) -> None:
        with self._condition:
            for job in self._jobs.values():
                if job.paused and job.resume_callback is not None:
                    job.resume_callback()
                    job.paused = False
                    self._event_locked(job, "pause", paused=False,
                                       reason="service_closing")
            if self._closed:
                worker = self._worker
            else:
                self._closed = True
                worker = self._worker
                self._queue.put(None)
        if wait and worker is not threading.current_thread():
            worker.join()

    def _work(self) -> None:
        while True:
            job = self._queue.get()
            try:
                if job is None:
                    return
                self._execution.job = job
                self._run(job)
            finally:
                self._execution.job = None
                self._queue.task_done()

    def _run(self, job: _Job) -> None:
        with self._condition:
            job.state = "waiting_for_input" if job.waiting_for is not None else "running"
            job.started_at = _utc_now()
            self._event_locked(job, "state", state=job.state)

        def log(message: Any) -> None:
            with self._condition:
                self._event_locked(job, "log", message=str(message))

        def progress(done: Any, total: Any, phase: Any = None) -> None:
            try:
                done_value = int(done)
            except (TypeError, ValueError):
                done_value = 0
            try:
                total_value = int(total)
            except (TypeError, ValueError):
                total_value = 0
            value = {
                "done": done_value,
                "total": total_value,
                "phase": str(phase) if phase is not None else None,
            }
            with self._condition:
                job.progress = value
                now = time.monotonic()
                phase_changed = value["phase"] != job.last_progress_phase
                complete = total_value > 0 and done_value >= total_value
                if (phase_changed or complete
                        or now - job.last_progress_event_at >= 0.1):
                    job.last_progress_event_at = now
                    job.last_progress_phase = value["phase"]
                    self._event_locked(job, "progress", **value)

        try:
            result: dict[str, Any]
            if job.cancel_requested:
                raise JobCancelled("Job cancelled.")
            if job.runner is not None:
                result = job.runner(log, progress)
                if not isinstance(result, dict):
                    raise TypeError("A job runner must return a dictionary.")
                ok = True
            elif job.kind in {"downloads.install", "mods.reinstall"}:
                result = _install_download(job, log, progress)
                ok = True
            elif job.kind == "deploy":
                from Utils.deployment.pipeline import run_deploy_pipeline
                ok = run_deploy_pipeline(
                    job.game,
                    job.profile,
                    log_fn=log,
                    progress_fn=progress,
                    root_folder_enabled=bool(
                        job.options.get("root_folder_enabled", True)),
                    do_backup=bool(job.options.get("backup", True)),
                    operation_owner=f"headless:{job.id}",
                    raise_busy=True,
                )
                result = {"ok": True}
            elif job.kind == "restore":
                from Utils.deployment.pipeline import run_restore_pipeline
                ok = run_restore_pipeline(
                    job.game,
                    job.profile,
                    log_fn=log,
                    progress_fn=progress,
                    operation_owner=f"headless:{job.id}",
                    raise_busy=True,
                )
                result = {"ok": True}
            elif job.kind == "mods.set_enabled":
                from Utils.mods.state import set_mods_enabled
                changes = [
                    (str(change["name"]), bool(change["enabled"]))
                    for change in job.options.get("changes", [])
                ]
                result = set_mods_enabled(
                    job.game,
                    job.game.get_profile_root() / "profiles" / job.profile,
                    changes,
                    log_fn=log,
                    progress_fn=progress,
                    operation_owner=f"headless:{job.id}",
                )
                ok = True
            elif job.kind == "mods.bundle.set":
                from .bundle import set_installed_choices
                result = set_installed_choices(
                    job.game,
                    Path(job.game.get_profile_root()) / "profiles" / job.profile,
                    str(job.options["mod"]),
                    list(job.options["bundle_selected"]),
                    str(job.options["revision"]),
                    log_fn=log,
                    progress_fn=progress,
                    operation_owner=f"headless:{job.id}",
                )
                ok = True
            elif job.kind == "mods.move":
                from Utils.mods.state import move_mods
                position = dict(job.options.get("position", {}))
                placement, anchor = next(iter(position.items()))
                result = move_mods(
                    job.game,
                    job.game.get_profile_root() / "profiles" / job.profile,
                    [str(name) for name in job.options.get("mods", [])],
                    anchor=str(anchor),
                    after=placement == "after",
                    log_fn=log,
                    progress_fn=progress,
                    operation_owner=f"headless:{job.id}",
                )
                ok = True
            elif job.kind == "mods.rename":
                from .mod_rename import rename_profile_mod
                result = rename_profile_mod(
                    job.game,
                    Path(job.game.get_profile_root()) / "profiles" / job.profile,
                    str(job.options["mod"]),
                    str(job.options["new_name"]),
                    str(job.options["plan_revision"]),
                    log_fn=log,
                    progress_fn=progress,
                    operation_owner=f"headless:{job.id}",
                )
                ok = True
            elif job.kind in {"separators.create", "separators.rename",
                              "separators.remove"}:
                from Utils.mods.separators import edit_separator
                result = edit_separator(
                    job.game,
                    Path(job.game.get_profile_root()) / "profiles" / job.profile,
                    job.kind.rsplit(".", 1)[1],
                    revision=str(job.options["revision"]),
                    separator=job.options.get("separator"),
                    new_name=job.options.get("new_name"),
                    position=job.options.get("position"),
                    log_fn=log,
                    progress_fn=progress,
                    operation_owner=f"headless:{job.id}",
                )
                ok = True
            elif job.kind == "separators.settings.set":
                from Utils.mods.separators import set_separator_settings
                result = set_separator_settings(
                    job.game,
                    Path(job.game.get_profile_root()) / "profiles" / job.profile,
                    str(job.options["separator"]),
                    revision=str(job.options["revision"]),
                    state_revision=str(job.options["state_revision"]),
                    changes=dict(job.options["changes"]),
                    log_fn=log,
                    progress_fn=progress,
                    operation_owner=f"headless:{job.id}",
                )
                ok = True
            elif job.kind == "mods.remove":
                from Utils.mods.state import remove_profile_mods
                result = remove_profile_mods(
                    job.game,
                    job.game.get_profile_root() / "profiles" / job.profile,
                    [str(name) for name in job.options.get("mods", [])],
                    log_fn=log,
                    progress_fn=progress,
                    operation_owner=f"headless:{job.id}",
                )
                ok = True
            elif job.kind == "profiles.create":
                from Utils.profiles.manage import create_profile
                result = create_profile(
                    job.game,
                    job.profile,
                    profile_specific_mods=bool(
                        job.options.get("profile_specific_mods", False)),
                    copy_from=job.options.get("copy_from"),
                    log_fn=log,
                    progress_fn=progress,
                    operation_owner=f"headless:{job.id}",
                )
                ok = True
            elif job.kind == "profiles.groups.create":
                from Utils.profiles.manage import create_profile_group
                result = create_profile_group(
                    job.game,
                    job.profile,
                    list(job.options["members"]),
                    ini_source=job.options.get("ini_source"),
                    overwrite_excluded=list(
                        job.options.get("overwrite_excluded", [])),
                    log_fn=log,
                    progress_fn=progress,
                    operation_owner=f"headless:{job.id}",
                )
                ok = True
            elif job.kind == "profiles.groups.set_members":
                from Utils.profiles.manage import set_profile_group_members
                result = set_profile_group_members(
                    job.game,
                    job.profile,
                    list(job.options["members"]),
                    expected_members=list(job.options["expected_members"]),
                    log_fn=log,
                    progress_fn=progress,
                    operation_owner=f"headless:{job.id}",
                )
                ok = True
            elif job.kind == "profiles.convert":
                from Utils.profiles.manage import convert_profile
                result = convert_profile(
                    job.game,
                    job.profile,
                    log_fn=log,
                    progress_fn=progress,
                    operation_owner=f"headless:{job.id}",
                )
                ok = True
            elif job.kind == "profiles.rename":
                from Utils.profiles.manage import rename_profile
                result = rename_profile(
                    job.game,
                    job.profile,
                    str(job.options.get("new_name", "")),
                    log_fn=log,
                    progress_fn=progress,
                    operation_owner=f"headless:{job.id}",
                )
                ok = True
            elif job.kind == "profiles.remove":
                from Utils.profiles.manage import remove_profile
                result = remove_profile(
                    job.game,
                    job.profile,
                    log_fn=log,
                    progress_fn=progress,
                    operation_owner=f"headless:{job.id}",
                )
                ok = True
            elif job.kind == "profiles.select":
                from Utils.profiles.manage import select_profile
                result = select_profile(
                    job.game,
                    job.profile,
                    log_fn=log,
                    progress_fn=progress,
                    operation_owner=f"headless:{job.id}",
                )
                ok = True
            elif job.kind == "wabbajack.remove":
                from Utils.wabbajack.installed import (
                    installation_by_id, remove_installed_list,
                )
                from Utils.wabbajack.paths import WabbajackError
                installation_id = str(job.options["installation_id"])
                try:
                    directory = installation_by_id(job.game, installation_id)
                    removed = remove_installed_list(
                        job.game, directory, log=log,
                        progress=lambda phase, current, total, detail="":
                        progress(current, total, phase + (
                            f": {detail}" if detail else "")),
                        expected_id=installation_id,
                    )
                except WabbajackError as exc:
                    raise JobError(
                        "wabbajack_remove_failed", str(exc),
                        {"installation_id": installation_id},
                    ) from exc
                result = {
                    "ok": True,
                    "installation_id": installation_id,
                    "directory": str(removed.directory),
                    "profiles": list(removed.profiles),
                    "groups": list(removed.groups),
                    "restored_profile": removed.restored_profile or None,
                }
                ok = True
            elif job.kind == "wabbajack.reset_load_order":
                from Utils.wabbajack.reset import reset_load_order
                from Utils.wabbajack.paths import WabbajackError
                profile_dir = (Path(job.game.get_profile_root()) / "profiles"
                               / str(job.profile))
                try:
                    result = reset_load_order(
                        job.game, profile_dir, log_fn=log)
                except (WabbajackError, OSError) as exc:
                    raise JobError(
                        "wabbajack_reset_failed", str(exc),
                        {"profile": job.profile},
                    ) from exc
                result = {"ok": True, "profile": job.profile, **result}
                ok = True
            else:
                raise RuntimeError(f"Unsupported job kind: {job.kind}")
        except Exception as exc:
            if isinstance(exc, JobCancelled):
                self._finish(job, "cancelled", error={
                    "name": "job_cancelled",
                    "message": str(exc) or "Job cancelled.",
                    "details": exc.details,
                })
                return
            if isinstance(exc, JobError):
                error = {
                    "name": exc.name,
                    "message": str(exc),
                    "details": exc.details,
                }
            else:
                from Utils.operations import OperationBusy
                if isinstance(exc, OperationBusy):
                    error = {
                        "name": "operation_busy",
                        "message": str(exc),
                        "details": exc.details,
                    }
                else:
                    from Utils.mods.state import ModStateError
                    if isinstance(exc, ModStateError):
                        error = {
                            "name": exc.name,
                            "message": str(exc),
                            "details": exc.details,
                        }
                    else:
                        from Utils.profiles.manage import ProfileError
                        if isinstance(exc, ProfileError):
                            error = {
                                "name": exc.name,
                                "message": str(exc),
                                "details": exc.details,
                            }
                        else:
                            error = {
                                "name": "internal_error",
                                "message": str(exc) or type(exc).__name__,
                                "details": {"exception": type(exc).__name__},
                            }
                            try:
                                print(
                                    f"[headless] {job.kind} job {job.id} failed: "
                                    f"{type(exc).__name__}: {exc}\n"
                                    f"{traceback.format_exc()}",
                                    file=sys.stderr,
                                    flush=True,
                                )
                            except Exception:
                                pass
            self._finish(job, "failed", error=error)
            return

        if ok:
            self._finish(job, "succeeded", result=result)
        else:
            self._finish(job, "failed", error={
                "name": f"{job.kind}_failed",
                "message": f"{job.kind.capitalize()} did not complete.",
            })

    def _finish(
        self,
        job: _Job,
        state: str,
        *,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        with self._condition:
            job.state = state
            job.finished_at = _utc_now()
            job.result = result
            job.error = error
            job.runner = None
            job.cancel_callback = None
            job.pause_callback = None
            job.resume_callback = None
            job.paused = False
            job.waiting_for = None
            data: dict[str, Any] = {"state": state, "waiting_for": None,
                                    "requires_client": job.requires_client}
            if result is not None:
                data["result"] = result
            if error is not None:
                data["error"] = error
            self._event_locked(job, "state", **data)

        from amethyst_plugins.events import publish_event
        game_id = getattr(job.game, "game_id", "")
        publish_event("job.completed", game_id, job.profile or "", job_id=job.id,
                      kind=job.kind, state=state)
        if job.kind in {"downloads.install", "downloads.install.batch", "mods.reinstall",
                        "mods.reinstall.batch", "mods.quick_update", "collections.install",
                        "collections.update", "wabbajack.install"}:
            publish_event("install.completed", game_id, job.profile or "", job_id=job.id,
                          kind=job.kind, state=state, success=state == "succeeded")

    def _event_locked(self, job: _Job, event_type: str, **data: Any) -> None:
        job.sequence += 1
        job.events.append({
            "sequence": job.sequence,
            "timestamp": _utc_now(),
            "type": event_type,
            **data,
        })
        if self._history is not None:
            now = time.monotonic()
            if event_type == "state" or now - job.last_event_history_at >= 1.0:
                self._history.save_events(job.id, job.events)
                job.last_event_history_at = now
            if event_type == "state":
                job.history_persisted = self._history.save(self._snapshot_locked(job))
        self._condition.notify_all()

    def _require_locked(self, job_id: str) -> _Job:
        try:
            return self._jobs[job_id]
        except KeyError as exc:
            raise KeyError(job_id) from exc

    @staticmethod
    def _snapshot_locked(job: _Job) -> dict[str, Any]:
        return {
            "id": job.id,
            "kind": job.kind,
            "state": job.state,
            "game": dict(job.game_info) if job.game_info is not None else None,
            "profile": job.profile,
            "options": dict(job.options),
            "created_at": job.created_at,
            "started_at": job.started_at,
            "finished_at": job.finished_at,
            "progress": dict(job.progress) if job.progress else None,
            "result": dict(job.result) if job.result else None,
            "error": dict(job.error) if job.error else None,
            "cancellable": job.cancellable,
            "cancel_requested": job.cancel_requested,
            "pausable": job.pausable,
            "paused": job.paused,
            "requires_client": job.requires_client,
            "waiting_for": deepcopy(job.waiting_for),
            "event_sequence": job.sequence,
            "history_persisted": job.history_persisted,
        }

    def _prune_locked(self) -> None:
        while len(self._jobs) >= self._max_jobs:
            completed = next(
                (job_id for job_id, job in self._jobs.items()
                 if job.state in _FINAL_STATES),
                None,
            )
            if completed is None:
                return
            del self._jobs[completed]
