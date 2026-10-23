from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .jobs import JobCancelled, JobError


_CARD_FIELDS = (
    "id", "name", "summary", "description", "user_name", "author",
    "tile_image_url", "image_url", "thumbnail_url", "total_downloads",
    "endorsements", "adult",
)


def _validate_manual_archives(
    mods: list[Any], manifest: dict[str, Any], paths: dict[int, Path],
) -> None:
    from Nexus.nexus_download import _md5_matches, _zip_is_intact

    required_ids = {
        int(mod.file_id) for mod in mods
        if getattr(mod, "source_type", "nexus") == "nexus" and mod.file_id
    }
    unknown_ids = sorted(set(paths) - required_ids)
    missing_ids = sorted(required_ids - set(paths))
    if unknown_ids or missing_ids:
        raise JobError(
            "collection_manual_archives_invalid",
            "Manual archives do not match the collection's required file IDs.",
            {"unknown_file_ids": unknown_ids, "missing_file_ids": missing_ids},
        )
    sources = {}
    for entry in manifest.get("mods", []):
        if not isinstance(entry, dict):
            continue
        source = entry.get("source")
        if not isinstance(source, dict):
            continue
        try:
            file_id = int(source.get("fileId") or 0)
        except (TypeError, ValueError):
            continue
        if file_id > 0:
            sources[file_id] = source
    for mod in mods:
        if getattr(mod, "source_type", "nexus") != "nexus" or not mod.file_id:
            continue
        path = paths[int(mod.file_id)]
        if path.is_symlink() or not path.is_file():
            raise JobError(
                "collection_manual_archive_unavailable",
                "A supplied manual archive is unavailable.",
                {"file_id": mod.file_id, "path": str(path)},
            )
        source = sources.get(int(mod.file_id), {})
        expected_size = int(source.get("fileSize") or mod.size_bytes or 0)
        if (expected_size > 0 and abs(path.stat().st_size - expected_size)
                > expected_size * 0.01):
            raise JobError(
                "collection_manual_archive_mismatch",
                "A supplied manual archive has the wrong size.",
                {"file_id": mod.file_id, "path": str(path)},
            )
        expected_md5 = str(source.get("md5") or mod.md5 or "").strip().lower()
        if not _md5_matches(path, expected_md5) or not _zip_is_intact(path):
            raise JobError(
                "collection_manual_archive_mismatch",
                "A supplied manual archive does not match its manifest entry.",
                {"file_id": mod.file_id, "path": str(path)},
            )


def _state_revision(record: dict[str, Any]) -> str:
    from Utils.collections.installed import collection_record_revision
    return collection_record_revision(record)


def _scalar(value: Any) -> Any:
    return value if value is None or isinstance(value, (str, int, float, bool)) else None


def _record_summary(record: dict[str, Any], profile_dir: Path, locked: bool) -> dict[str, Any]:
    from Utils.collections.installed import collection_title
    from Utils.collections.manifest import is_collection_manifest

    manifest = record.get("manifest") if isinstance(record.get("manifest"), dict) else {}
    info = manifest.get("info") if isinstance(manifest.get("info"), dict) else {}
    card = record.get("card") if isinstance(record.get("card"), dict) else {}
    members = record.get("members") if isinstance(record.get("members"), dict) else {}
    mods = manifest.get("mods") if isinstance(manifest.get("mods"), list) else []
    plugins = manifest.get("plugins") if isinstance(manifest.get("plugins"), list) else []
    previous = record.get("previous") if isinstance(record.get("previous"), dict) else {}
    kind = str(record.get("kind") or "appended")
    install_id = record.get("install_id")
    if not isinstance(install_id, str) or not install_id or len(install_id) > 128:
        install_id = None
    slug = str(record.get("slug") or "")
    local_import = (record.get("source") == "local"
                    or (kind == "standalone" and slug.startswith("import_")))
    domain = str(record.get("domain") or info.get("domainName") or "") or None
    status = str(record.get("status") or "unknown")
    return {
        "id": install_id,
        "kind": kind,
        "profile": profile_dir.name,
        "title": collection_title(record, profile_dir.name),
        "slug": slug,
        "source": "local" if local_import else "nexus",
        "domain": domain,
        "revision": _scalar(record.get("revision")),
        "previous_revision": _scalar(previous.get("revision")),
        "status": status,
        "saved_at": _scalar(record.get("saved")),
        "state_revision": _state_revision(record),
        "locked": bool(locked),
        "mods_enabled": (record.get("mods_enabled")
                         if isinstance(record.get("mods_enabled"), bool) else None),
        "member_count": len(members),
        "manifest": {
            "available": is_collection_manifest(manifest),
            "mod_count": len(mods),
            "plugin_count": len(plugins),
        },
        "card": {
            key: value for key in _CARD_FIELDS
            if (value := _scalar(card.get(key))) is not None
        },
        "author": str(card.get("user_name") or card.get("author")
                      or info.get("author") or "") or None,
        "can_toggle": kind == "appended" and install_id is not None and not locked,
        "can_resume": (not local_import and kind == "standalone"
                       and install_id is not None
                       and status != "complete" and not locked
                       and not previous),
        "can_update": (not local_import and kind == "standalone"
                       and install_id is not None
                       and not locked and (status == "complete" or bool(previous))),
        "can_remove": ((kind == "appended" and install_id is not None)
                       or kind == "standalone") and not locked,
        "can_reset_load_order": install_id is not None and not locked,
        "remove_method": ("collections.remove.start" if kind == "appended" and install_id
                          else "profiles.remove.start" if kind == "standalone"
                          else None),
        "resume_method": (
            "collections.resume.start" if not local_import
            and kind == "standalone" and install_id
            and status != "complete" and not previous else None),
        "update_method": (
            "collections.update.start" if not local_import
            and kind == "standalone" and install_id
            and (status == "complete" or previous) else None),
        "reset_load_order_method": (
            "collections.reset_load_order.start" if install_id else None),
    }


def installed_inventory(
    game: Any,
    game_summary: dict[str, str],
    profile_dir: Path | None = None,
    log_fn: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    from Utils.collections.installed import list_appended_collections, primary_collection
    from Utils.profiles.groups import profile_is_locked

    log = log_fn or (lambda _message: None)
    if profile_dir is not None:
        profiles = [Path(profile_dir)]
    else:
        root = Path(game.get_profile_root()) / "profiles"
        profiles = [] if not root.is_dir() else [
            path for path in sorted(root.iterdir(), key=lambda value: value.name.casefold())
            if path.is_dir() and not path.is_symlink()
        ]

    installations = []
    warnings = []
    for profile in profiles:
        messages = []
        try:
            locked = profile_is_locked(profile)
            primary = primary_collection(profile)
            if primary is not None:
                installations.append(_record_summary(primary, profile, locked))
            for record in list_appended_collections(profile, messages.append):
                installations.append(_record_summary(record, profile, locked))
        except (OSError, TypeError, ValueError) as exc:
            messages.append(f"Could not scan collections in {profile.name}: {exc}")
        for message in messages:
            log(message)
            warnings.append({"profile": profile.name, "message": message})

    installations.sort(key=lambda value: (
        value["profile"].casefold(), value["title"].casefold(),
        value["kind"], str(value["revision"]),
    ))
    return {
        "game": game_summary,
        "profile": profile_dir.name if profile_dir is not None else None,
        "installations": installations,
        "counts": {
            "installations": len(installations),
            "standalone": sum(item["kind"] == "standalone" for item in installations),
            "appended": sum(item["kind"] == "appended" for item in installations),
            "incomplete": sum(item["status"] != "complete" for item in installations),
        },
        "warnings": warnings,
    }


def _appended_record(
    profile_dir: Path,
    collection_id: str,
    expected_revision: str | None = None,
) -> dict[str, Any]:
    from Utils.collections.installed import list_appended_collections

    records = [
        record for record in list_appended_collections(profile_dir)
        if record.get("install_id") == collection_id
    ]
    if not records:
        raise JobError(
            "collection_not_found",
            "The appended collection is no longer installed in this profile.",
            {"collection_id": collection_id, "profile": profile_dir.name},
        )
    if len(records) > 1:
        raise JobError(
            "collection_identity_ambiguous",
            "More than one appended collection has the requested identity.",
            {"collection_id": collection_id, "profile": profile_dir.name},
        )
    record = records[0]
    actual_revision = _state_revision(record)
    if expected_revision is not None and actual_revision != expected_revision:
        raise JobError(
            "collection_state_changed",
            "The installed collection changed; list it again before retrying.",
            {"collection_id": collection_id, "expected": expected_revision,
             "actual": actual_revision},
        )
    return record


def _installed_record(
    profile_dir: Path,
    collection_id: str,
    expected_revision: str | None = None,
) -> dict[str, Any]:
    from Utils.collections.installed import list_appended_collections, primary_collection

    candidates = list_appended_collections(profile_dir)
    primary = primary_collection(profile_dir)
    if primary is not None:
        candidates.append(primary)
    records = [
        record for record in candidates
        if record.get("install_id") == collection_id
    ]
    if not records:
        raise JobError(
            "collection_not_found",
            "The collection is no longer installed in this profile.",
            {"collection_id": collection_id, "profile": profile_dir.name},
        )
    if len(records) > 1:
        raise JobError(
            "collection_identity_ambiguous",
            "More than one installed collection has the requested identity.",
            {"collection_id": collection_id, "profile": profile_dir.name},
        )
    record = records[0]
    actual_revision = _state_revision(record)
    if expected_revision is not None and actual_revision != expected_revision:
        raise JobError(
            "collection_state_changed",
            "The installed collection changed; list it again before retrying.",
            {"collection_id": collection_id, "expected": expected_revision,
             "actual": actual_revision},
        )
    return record


def set_enabled_runner(
    game: Any,
    profile_dir: Path,
    collection_id: str,
    state_revision: str,
    enabled: bool,
    owner: str,
) -> Callable[[Callable[..., None], Callable[..., None]], dict[str, Any]]:
    def run(log: Callable[..., None], progress: Callable[..., None]) -> dict[str, Any]:
        from Utils.collections.installed import (
            CollectionStateChanged, toggle_appended_collection_mods,
        )
        from Utils.operations import OperationBusy
        from Utils.profiles.groups import profile_is_locked

        progress(0, 2, "Validating collection state")
        record = _appended_record(profile_dir, collection_id, state_revision)
        progress(1, 2, "Updating collection mods")
        try:
            actual, changed, skipped = toggle_appended_collection_mods(
                game, profile_dir, record, enabled=enabled, log_fn=log,
                operation_owner=owner,
                expected_state_revision=state_revision,
            )
        except CollectionStateChanged as exc:
            raise JobError(
                "collection_state_changed",
                "The installed collection changed; list it again before retrying.",
                {"collection_id": collection_id, "expected": state_revision,
                 "actual": exc.actual_revision},
            ) from exc
        except JobError:
            raise
        except OperationBusy:
            raise
        except ValueError as exc:
            raise JobError(
                "collection_change_rejected", str(exc),
                {"collection_id": collection_id, "profile": profile_dir.name},
            ) from exc
        except Exception as exc:
            raise JobError(
                "collection_state_sync_failed",
                "The collection mod state could not be changed.",
                {"collection_id": collection_id, "profile": profile_dir.name,
                 "reason": str(exc)},
            ) from exc
        updated = _appended_record(profile_dir, collection_id)
        progress(2, 2, "Collection mods updated")
        return {
            "ok": True,
            "enabled": actual,
            "changed": changed,
            "skipped": skipped,
            "installation": _record_summary(
                updated, profile_dir, profile_is_locked(profile_dir)),
        }

    return run


def removal_plan_runner(
    game: Any,
    profile_dir: Path,
    collection_id: str,
    state_revision: str,
) -> Callable[[Callable[..., None], Callable[..., None]], dict[str, Any]]:
    def run(log: Callable[..., None], progress: Callable[..., None]) -> dict[str, Any]:
        from Utils.collections.installed import (
            collection_removal_plan_revision, plan_collection_removal,
        )
        from Utils.profiles.groups import profile_is_locked

        progress(0, 2, "Validating collection state")
        record = _appended_record(profile_dir, collection_id, state_revision)
        progress(1, 2, "Planning collection removal")
        try:
            plan = plan_collection_removal(game, profile_dir, record)
            _appended_record(profile_dir, collection_id, state_revision)
        except JobError:
            raise
        except (OSError, TypeError, ValueError) as exc:
            raise JobError(
                "collection_removal_plan_failed",
                "The collection removal plan could not be prepared.",
                {"collection_id": collection_id, "profile": profile_dir.name,
                 "reason": str(exc)},
            ) from exc
        progress(2, 2, "Collection removal plan ready")
        return {
            "installation": _record_summary(
                record, profile_dir, profile_is_locked(profile_dir)),
            "plan": {
                "revision": collection_removal_plan_revision(plan),
                "delete": plan.delete,
                "retain": plan.retain,
                "delete_count": len(plan.delete),
                "retain_count": len(plan.retain),
            },
        }

    return run


def remove_runner(
    game: Any,
    profile_dir: Path,
    collection_id: str,
    state_revision: str,
    plan_revision: str,
    owner: str,
) -> Callable[[Callable[..., None], Callable[..., None]], dict[str, Any]]:
    def run(log: Callable[..., None], progress: Callable[..., None]) -> dict[str, Any]:
        from Utils.collections.installed import (
            CollectionRemovalPlanChanged, CollectionStateChanged,
            remove_appended_collection,
        )
        from Utils.operations import OperationBusy

        progress(0, 2, "Validating collection removal")
        record = _appended_record(profile_dir, collection_id, state_revision)
        progress(1, 2, "Removing appended collection")
        try:
            plan = remove_appended_collection(
                game, profile_dir, record, log_fn=log, operation_owner=owner,
                expected_state_revision=state_revision,
                expected_plan_revision=plan_revision,
            )
        except CollectionStateChanged as exc:
            raise JobError(
                "collection_state_changed",
                "The installed collection changed; plan its removal again.",
                {"collection_id": collection_id, "expected": state_revision,
                 "actual": exc.actual_revision},
            ) from exc
        except CollectionRemovalPlanChanged as exc:
            raise JobError(
                "collection_removal_plan_changed",
                "The collection removal plan changed; prepare a new plan before retrying.",
                {"collection_id": collection_id, "expected": plan_revision,
                 "actual": exc.actual_revision},
            ) from exc
        except JobError:
            raise
        except OperationBusy:
            raise
        except ValueError as exc:
            raise JobError(
                "collection_removal_rejected", str(exc),
                {"collection_id": collection_id, "profile": profile_dir.name},
            ) from exc
        except Exception as exc:
            raise JobError(
                "collection_removal_failed",
                "The appended collection could not be removed.",
                {"collection_id": collection_id, "profile": profile_dir.name,
                 "reason": str(exc)},
            ) from exc
        progress(2, 2, "Appended collection removed")
        return {
            "ok": True,
            "collection_id": collection_id,
            "profile": profile_dir.name,
            "deleted": plan.delete,
            "retained": plan.retain,
            "deleted_count": len(plan.delete),
            "retained_count": len(plan.retain),
        }

    return run


def reset_load_order_runner(
    game: Any,
    profile_dir: Path,
    group_dir: Path | None,
    collection_id: str,
    state_revision: str,
    nexus_session: Any,
    owner: str,
) -> Callable[[Callable[..., None], Callable[..., None]], dict[str, Any]]:
    def run(log: Callable[..., None], progress: Callable[..., None]) -> dict[str, Any]:
        from Utils.collections.installed import CollectionStateChanged
        from Utils.collections.install import load_amethyst_reset_data
        from Utils.collections.manifest import (
            is_collection_manifest, load_collection_manifest,
        )
        from Utils.operations import OperationBusy
        from Utils.profiles.groups import profile_is_locked

        progress(0, 3, "Validating collection state")
        record = _installed_record(profile_dir, collection_id, state_revision)
        manifest = record.get("manifest") or {}
        if not is_collection_manifest(manifest):
            slug = str(record.get("slug") or "").strip()
            if not slug:
                raise JobError(
                    "collection_manifest_unavailable",
                    "The installed collection has no saved manifest or collection identity.",
                    {"collection_id": collection_id, "profile": profile_dir.name},
                )
            progress(1, 3, "Fetching collection manifest")
            try:
                api = nexus_session.require_api()
                domain = str(
                    record.get("domain")
                    or (record.get("card") or {}).get("game_domain")
                    or getattr(game, "nexus_game_domain", "") or "")
                revision = record.get("revision")
                detail = api.get_collection_detail(slug, domain, revision)
                manifest = load_collection_manifest(
                    api, str(getattr(game, "name", "") or ""), slug,
                    revision, detail[4], log_fn=log)
            except Exception as exc:
                raise JobError(
                    "collection_manifest_unavailable",
                    "The collection manifest could not be loaded.",
                    {"collection_id": collection_id, "profile": profile_dir.name,
                     "reason": str(exc)},
                ) from exc
            if not is_collection_manifest(manifest):
                raise JobError(
                    "collection_manifest_unavailable",
                    "The downloaded collection manifest is invalid.",
                    {"collection_id": collection_id, "profile": profile_dir.name},
                )

        record = _installed_record(profile_dir, collection_id, state_revision)
        progress(2, 3, "Restoring collection load order")
        kind = str(record.get("kind") or "appended")
        domain = str(
            record.get("domain")
            or (record.get("card") or {}).get("game_domain")
            or (manifest.get("info") or {}).get("domainName")
            or getattr(game, "nexus_game_domain", "") or "")
        try:
            amethyst_state = load_amethyst_reset_data(
                game, str(record.get("slug") or ""),
                profile_dir=profile_dir if kind == "standalone" else None,
                revision_hint=record.get("revision"), domain=domain,
                api_provider=nexus_session.require_api, log=log)
        except Exception as exc:
            log(f"Collection reset: authored order data unavailable ({exc})")
            amethyst_state = None
        try:
            if group_dir is not None:
                from Utils.collections.group_reset import reset_group_collection_load_order
                result = reset_group_collection_load_order(
                    game, group_dir, profile_dir.name, manifest,
                    amethyst_state=amethyst_state, log_fn=log,
                    collection_record=record, operation_owner=owner,
                    expected_state_revision=state_revision)
            else:
                from Utils.collections.group_reset import reset_profile_collection_load_order
                result = reset_profile_collection_load_order(
                    game, profile_dir, manifest, amethyst_state=amethyst_state,
                    log_fn=log, collection_record=record,
                    operation_owner=owner,
                    expected_state_revision=state_revision)
        except CollectionStateChanged as exc:
            raise JobError(
                "collection_state_changed",
                "The installed collection changed; list it again before retrying.",
                {"collection_id": collection_id, "expected": state_revision,
                 "actual": exc.actual_revision},
            ) from exc
        except JobError:
            raise
        except OperationBusy:
            raise
        except ValueError as exc:
            raise JobError(
                "collection_reset_rejected", str(exc),
                {"collection_id": collection_id, "profile": profile_dir.name,
                 "group": group_dir.name if group_dir is not None else None},
            ) from exc
        except Exception as exc:
            raise JobError(
                "collection_reset_failed",
                "The collection load order could not be reset.",
                {"collection_id": collection_id, "profile": profile_dir.name,
                 "group": group_dir.name if group_dir is not None else None,
                 "reason": str(exc)},
            ) from exc
        progress(3, 3, "Collection load order restored")
        return {
            "ok": True,
            "collection_id": collection_id,
            "profile": profile_dir.name,
            "group": group_dir.name if group_dir is not None else None,
            "authored_snapshot": amethyst_state is not None,
            **result,
            "installation": _record_summary(
                record, profile_dir, profile_is_locked(profile_dir)),
        }

    return run


def group_runner(
    game: Any,
    profile_dir: Path,
    target_dir: Path,
    target_is_group: bool,
    group_name: str,
    collection_id: str,
    state_revision: str,
    ini_source: str | None,
    overwrite_excluded: list[str],
) -> Callable[[Callable[..., None], Callable[..., None]], dict[str, Any]]:
    def run(log: Callable[..., None], progress: Callable[..., None]) -> dict[str, Any]:
        from Utils.collections.grouping import finalize_group
        from Utils.collections.options import CollectionInstallOptions
        from Utils.operations import OperationBusy
        from Utils.profiles.groups import get_members, is_group, profile_is_locked

        options = CollectionInstallOptions(
            mode="group", target=target_dir.name, group_name=group_name,
            reuse_profile=profile_dir.name, target_is_group=target_is_group)
        record_box: dict[str, Any] = {}

        def validate() -> None:
            record = _installed_record(
                profile_dir, collection_id, state_revision)
            if str(record.get("kind") or "") != "standalone":
                raise JobError(
                    "collection_group_unsupported",
                    "Only standalone collection profiles can be grouped.",
                    {"collection_id": collection_id,
                     "profile": profile_dir.name},
                )
            if profile_is_locked(profile_dir):
                raise JobError(
                    "profile_locked",
                    "Unlock the collection profile before grouping it.",
                    {"profile": profile_dir.name},
                )
            record_box["record"] = record
            result_path = (target_dir if target_is_group else
                           target_dir.parent / group_name)
            record_box["group_existed"] = result_path.exists()
            record_box["members"] = (
                get_members(result_path) if is_group(result_path) else [])

        progress(0, 2, "Validating collection group")
        try:
            result = finalize_group(
                game, profile_dir, options, ini_source=ini_source,
                overwrite_excluded=overwrite_excluded, log_fn=log,
                validate_fn=validate)
        except (JobError, OperationBusy):
            raise
        except ValueError as exc:
            raise JobError(
                "collection_group_rejected", str(exc),
                {"collection_id": collection_id, "profile": profile_dir.name,
                 "target": target_dir.name},
            ) from exc
        except Exception as exc:
            raise JobError(
                "collection_group_failed",
                "The collection profile could not be added to the profile group.",
                {"collection_id": collection_id, "profile": profile_dir.name,
                 "target": target_dir.name, "reason": str(exc)},
            ) from exc

        progress(2, 2, "Collection profile grouped")
        record = record_box.get("record") or _installed_record(
            profile_dir, collection_id)
        members = get_members(result)
        before = record_box.get("members") or []
        return {
            "ok": True,
            "profile": profile_dir.name,
            "group": result.name,
            "created": not record_box.get("group_existed", False),
            "added": profile_dir.name not in before,
            "members": members,
            "installation": _record_summary(
                record, profile_dir, profile_is_locked(profile_dir)),
        }

    return run


def _stamp_collection_profile(
    profile_dir: Path,
    domain: str,
    slug: str,
    revision: int | None,
    skipped: set[int],
) -> None:
    from Utils.games.registry import save_collection_url_to_profile
    from Utils.profiles.state import (
        merge_profile_settings, write_collection_optional_skipped,
        write_collection_revision,
    )

    url = f"https://www.nexusmods.com/games/{domain}/collections/{slug}"
    if revision is not None:
        url += f"/revisions/{revision}"
    save_collection_url_to_profile(profile_dir, url)
    merge_profile_settings(profile_dir, {"collection_identity": {
        "domain": domain, "slug": slug, "revision": revision,
    }})
    if revision is not None:
        write_collection_revision(profile_dir, revision)
    write_collection_optional_skipped(profile_dir, skipped)


def install_new_runner(
    game: Any,
    profile_name: str,
    domain: str,
    slug: str,
    revision: int | None,
    skipped_optional_file_ids: set[int],
    accept_game_version_mismatch: bool,
    nexus_session: Any,
    control: Any,
    owner: str,
    *,
    resume_profile_dir: Path | None = None,
    expected_collection_id: str | None = None,
    expected_state_revision: str | None = None,
    update_target: bool = False,
    append_target: bool = False,
    append_overwrite_existing: bool = False,
    append_skip_existing: bool = False,
    manual_archive_paths: dict[int, Path] | None = None,
    manual_session: Any | None = None,
    installer_requests: Any | None = None,
) -> Callable[[Callable[..., None], Callable[..., None]], dict[str, Any]]:
    def run(log: Callable[..., None], progress: Callable[..., None]) -> dict[str, Any]:
        from .nexus import _call

        def operation() -> dict[str, Any]:
            from Utils.collections.manifest import (
                extract_offsite_mods, is_collection_manifest,
                load_collection_manifest,
            )

            progress(0, 4, "Fetching collection detail")
            api = nexus_session.require_api()
            (name, total_size, _mod_count, all_mods, download_link,
             revisions, card) = api.get_collection_detail(
                 slug, domain, revision_number=revision)
            if not name and not revisions and not card:
                raise JobError(
                    "collection_not_found",
                    "The Nexus collection or revision could not be loaded.",
                    {"slug": slug, "domain": domain, "revision": revision},
                )
            if card.get("unsupported_collection_schema"):
                raise JobError(
                    "collection_schema_unsupported",
                    "This collection revision uses an unsupported schema.",
                    {"slug": slug, "domain": domain, "revision": revision,
                     "schema_id": card.get("collection_schema_id")},
                )
            resolved_revision = revision
            if resolved_revision is None:
                published = []
                for value in revisions:
                    try:
                        if str(value.get("revisionStatus") or "").casefold() == "published":
                            published.append(int(value.get("revisionNumber") or 0))
                    except (AttributeError, TypeError, ValueError):
                        continue
                resolved_revision = max(published) if published else None
            if not download_link:
                raise JobError(
                    "collection_manifest_unavailable",
                    "This collection revision has no downloadable manifest.",
                    {"slug": slug, "domain": domain,
                     "revision": resolved_revision},
                )
            progress(1, 4, "Fetching collection manifest")
            manifest = load_collection_manifest(
                api, str(getattr(game, "name", "") or ""), slug,
                resolved_revision, download_link, log_fn=log)
            if not is_collection_manifest(manifest):
                raise JobError(
                    "collection_manifest_unavailable",
                    "The collection manifest could not be loaded.",
                    {"slug": slug, "domain": domain,
                     "revision": resolved_revision},
                )
            from Utils.profiles import export as profile_export
            thunderstore = profile_export.thunderstore_entries(manifest)
            thunderstore_count = sum(
                1 for mod in manifest.get("mods", [])
                if isinstance(mod, dict)
                and str(((mod.get("source") or {}).get("type") or ""))
                .strip().casefold() == "thunderstore")
            if len(thunderstore) != thunderstore_count:
                raise JobError(
                    "collection_manifest_invalid",
                    "A Thunderstore entry has an incomplete package identity.",
                    {"source": "thunderstore"},
                )
            optional_ids = {
                int(mod.file_id) for mod in all_mods
                if mod.optional and int(mod.file_id or 0) > 0
            }
            invalid_skips = sorted(skipped_optional_file_ids - optional_ids)
            if invalid_skips:
                raise JobError(
                    "collection_optional_invalid",
                    "One or more skipped file IDs are not optional collection mods.",
                    {"file_ids": invalid_skips, "optional_file_ids": sorted(optional_ids)},
                )
            mods = [
                mod for mod in all_mods
                if int(mod.file_id or 0) not in skipped_optional_file_ids
            ]
            skipped_mods = [
                mod for mod in all_mods
                if int(mod.file_id or 0) in skipped_optional_file_ids
            ]
            if manual_archive_paths is not None:
                _validate_manual_archives(mods, manifest, manual_archive_paths)
            elif mods and manual_session is None:
                progress(2, 4, "Checking Nexus account")
                user = api.validate()
                if not bool(getattr(user, "is_premium", False)):
                    raise JobError(
                        "nexus_premium_required",
                        "Automatic headless collection installation requires Nexus Premium.",
                        {"manual_install_supported": True},
                    )
            from Utils.collections.export import collection_game_version_mismatch
            mismatch = collection_game_version_mismatch(
                game, card.get("game_versions") or ())
            if mismatch is not None and not accept_game_version_mismatch:
                installed, expected = mismatch
                raise JobError(
                    "collection_game_version_mismatch",
                    "The collection targets a different game version.",
                    {"installed": installed, "expected": list(expected),
                     "retry_with": {"accept_game_version_mismatch": True}},
                )
            if control.stop.is_set():
                raise JobCancelled("Collection installation cancelled.")

            from Utils.deployment.locking import coordinated_game_mutation
            from Utils.profiles.manage import ProfileError, create_profile
            mutation = coordinated_game_mutation(
                game, "collections.install", owner=owner)
            mutation.__enter__()
            try:
                progress(3, 4, "Preparing collection profile")
                existing = resume_profile_dir is not None
                updating = existing and update_target
                appending = existing and append_target
                resuming = existing and not updating and not appending
                profile_dir = (Path(resume_profile_dir) if existing else
                               Path(game.get_profile_root()) / "profiles" / profile_name)
                from Utils.profiles.state import merge_profile_settings
                from Utils.collections.installed import begin_collection, save_record
                from Utils.mods.copy import resolve_target_staging
                card_info = {"name": name or slug, "slug": slug,
                             "game_domain": domain, **card}
                update_context = None
                update_summary = None
                if existing:
                    expected_path = (Path(game.get_profile_root()) / "profiles"
                                     / profile_name)
                    if (profile_dir.is_symlink() or not profile_dir.is_dir()
                            or profile_dir.resolve() != expected_path.resolve()):
                        raise JobError(
                            "unsafe_profile_path",
                            "The collection profile is no longer available.",
                            {"profile": profile_name},
                        )
                    record = None
                    same_revision = False
                    if not appending:
                        record = _installed_record(
                            profile_dir, str(expected_collection_id or ""),
                            expected_state_revision)
                        from Nexus.nexus_meta import normalise_game_domain
                        actual_domain = normalise_game_domain(
                            record.get("domain") or domain)
                        same_identity = (
                            record.get("kind") == "standalone"
                            and str(record.get("slug") or "") == slug
                            and actual_domain == normalise_game_domain(domain))
                        same_revision = (
                            str(record.get("revision")) == str(resolved_revision))
                        if (not same_identity or (resuming and not same_revision)
                                or (updating and record.get("previous")
                                    and not same_revision)):
                            raise JobError(
                                "collection_state_changed",
                                "The installed collection identity changed; list it again before retrying.",
                                {"collection_id": expected_collection_id,
                                 "profile": profile_name},
                            )
                        if resuming and record.get("previous"):
                            raise JobError(
                                "collection_update_pending",
                                "This profile has an unfinished collection update.",
                                {"profile": profile_name,
                                 "collection_id": expected_collection_id},
                            )
                        if resuming and record.get("status") == "complete":
                            raise JobError(
                                "collection_already_complete",
                                "This collection installation is already complete.",
                                {"profile": profile_name,
                                 "collection_id": expected_collection_id},
                            )
                    from Utils.profiles.groups import (
                        is_group, member_of_groups, profile_is_locked,
                    )
                    if is_group(profile_dir) or profile_is_locked(profile_dir):
                        raise JobError(
                            "profile_locked",
                            "Unlock the collection profile before changing it.",
                            {"profile": profile_name},
                        )
                    groups = member_of_groups(game, profile_name)
                    if groups:
                        raise JobError(
                            ("collection_group_update_unsupported" if updating else
                             "collection_group_append_unsupported" if appending else
                             "collection_group_resume_unsupported"),
                            "Collection profiles in profile groups cannot yet be changed headlessly.",
                            {"profile": profile_name, "groups": groups},
                        )
                    from Utils.profiles.manage import _reject_deployed_use
                    _reject_deployed_use(game, profile_dir)
                    from Utils.profiles.state import (
                        profile_uses_specific_mods,
                        write_collection_install_paused,
                    )
                    if not profile_uses_specific_mods(profile_dir):
                        raise JobError(
                            "profile_conversion_required",
                            "Convert the collection profile to profile-specific mods before changing it.",
                            {"profile": profile_name,
                             "method": "profiles.convert.start"},
                        )
                    from Utils.mods.modlist import read_modlist
                    snapshot = list(read_modlist(profile_dir / "modlist.txt"))
                    if updating:
                        if same_revision and not record.get("previous"):
                            raise JobError(
                                "collection_already_current",
                                "This collection already uses the requested revision.",
                                {"profile": profile_name,
                                 "collection_id": expected_collection_id,
                                 "revision": resolved_revision},
                            )
                        previous = record.get("previous") or {}
                        old_manifest = previous.get("manifest") or record.get("manifest") or {}
                        if not old_manifest:
                            import json
                            try:
                                old_manifest = json.loads(
                                    (profile_dir / "collection.json").read_text(
                                        encoding="utf-8"))
                            except (OSError, ValueError):
                                old_manifest = {}
                        from Utils.collections.diff import diff_collection
                        staging = resolve_target_staging(game, profile_dir)
                        installed_names = {
                            entry.name.casefold() for entry in snapshot
                            if not entry.is_separator
                        }
                        diff = diff_collection(
                            old_manifest=old_manifest, new_mods=mods,
                            staging_path=staging,
                            installed_names_lower=installed_names,
                            collection_slug=slug,
                            collection_install_id=str(record.get("install_id") or ""),
                            new_manifest=manifest,
                        )
                        states = {
                            entry.name.casefold(): (entry.enabled, entry.locked)
                            for entry in snapshot if not entry.is_separator
                        }
                        replacement_states = {
                            file_id: states[old_name.casefold()]
                            for old_name, file_id in zip(
                                diff.to_update_old, diff.to_update_new_fids)
                            if old_name.casefold() in states
                        }
                        pending = list(record.get("pending_delete") or {})
                        retire = list(dict.fromkeys(diff.removals + pending))
                        from Utils.collections.installed import plan_collection_removal
                        removal = plan_collection_removal(
                            game, profile_dir, record, candidates=retire)
                        update_context = {
                            "snapshot": snapshot,
                            "schema_order": {},
                            "preserve_plugins": True,
                            "retire": retire,
                            "replacement_states": replacement_states,
                        }
                        update_summary = {
                            "from_revision": previous.get(
                                "revision", record.get("revision")),
                            "to_revision": resolved_revision,
                            "remove": list(removal.delete),
                            "retain": list(removal.retain),
                            "replace": [
                                {"old": old_name, "file_id": file_id}
                                for old_name, file_id in zip(
                                    diff.to_update_old,
                                    diff.to_update_new_fids)
                            ],
                            "install_file_ids": list(diff.to_install_fids),
                            "orphans": list(diff.orphans),
                        }
                    elif resuming:
                        update_context = {
                            "snapshot": snapshot,
                            "schema_order": {},
                            "preserve_plugins": True,
                        }
                    if not appending:
                        write_collection_install_paused(profile_dir, False)
                    else:
                        from Utils.collections.installed import list_appended_collections
                        if any(str(item.get("slug") or "") == slug
                               for item in list_appended_collections(profile_dir)):
                            raise JobError(
                                "collection_already_installed",
                                "This collection is already appended to the profile.",
                                {"profile": profile_name, "slug": slug},
                            )
                else:
                    create_profile(
                        game, profile_name, profile_specific_mods=True,
                        log_fn=log, operation_owner=owner,
                        mutation_coordinated=True)
                    _stamp_collection_profile(
                        profile_dir, domain, slug, resolved_revision,
                        skipped_optional_file_ids)
                    merge_profile_settings(profile_dir, {
                        "collection_generated_profile_name": profile_name,
                        "collection_card": card_info,
                    })
                record = begin_collection(
                    profile_dir, slug=slug, revision=resolved_revision,
                    card=card_info, manifest=manifest, appended=appending)
                record["skipped_fids"] = sorted(skipped_optional_file_ids)
                save_record(record)
                from Utils.collections.ownership import CollectionOwnership
                ownership = CollectionOwnership(
                    resolve_target_staging(game, profile_dir), record)
                state: dict[str, Any] = {"phase": "Installing collection"}

                def status(message: str) -> None:
                    state["phase"] = str(message)
                    log(message)

                def fraction(value: Any) -> None:
                    try:
                        amount = max(0.0, min(1.0, float(value)))
                    except (TypeError, ValueError):
                        return
                    progress(round(amount * 1000), 1000, state["phase"])

                from Utils.collections.install import (
                    CollectionInstallCallbacks, run_collection_install,
                )
                callbacks = CollectionInstallCallbacks(
                    resolve_fomod=installer_requests.fomod if installer_requests is not None else None,
                    resolve_bain=installer_requests.bain if installer_requests is not None else None,
                    resolve_layout=installer_requests.layout if installer_requests is not None else None,
                    resolve_choice=installer_requests.choice if installer_requests is not None else None,
                    on_status=status,
                    on_progress=fraction,
                    on_log=log,
                    on_result=lambda report: state.update(
                        report=report.to_dict(), ready=report.ready),
                    on_done=lambda installed, skipped, total, profile:
                        state.update(installed=installed, skipped=skipped,
                                     total=total, profile=profile),
                    on_manual_mod=(manual_session.on_prompt
                                   if manual_session is not None else lambda payload: None),
                    on_manual_resolved=(manual_session.on_resolved
                                        if manual_session is not None else lambda request_id: None),
                    on_manual_rejected=(manual_session.on_rejected
                                        if manual_session is not None else lambda request_id: None),
                )
                from Nexus.nexus_download import NexusDownloader
                from Utils.config_paths import get_download_cache_dir_for_game
                downloader = NexusDownloader(
                    api, download_dir=get_download_cache_dir_for_game(
                        str(getattr(game, "name", "") or "")))
                previous = getattr(game, "_active_profile_dir", None)
                thunderstore_order = []
                thunderstore_installed = 0
                thunderstore_failures = []
                try:
                    if thunderstore:
                        from Utils.collections.thunderstore import install_entries
                        (thunderstore_order, thunderstore_installed,
                         thunderstore_failures) = install_entries(
                            thunderstore, game, profile_dir, control, callbacks,
                            ownership, log_fn=log)
                    run_collection_install(
                        game=game, api=api, downloader=downloader, mods=mods,
                        download_link_path=download_link,
                        profile_dir=profile_dir, old_profile_dir=previous,
                        collection_slug=slug, collection_domain=domain,
                        revision_number=resolved_revision,
                        collection_total_size=int(total_size or 0),
                        collection_schema_cache=manifest,
                        skipped_fids=skipped_optional_file_ids,
                        skipped_mods=skipped_mods,
                        update_context=update_context,
                        overwrite_existing=(
                            append_overwrite_existing if appending else None),
                        skip_existing=(append_skip_existing if appending else False),
                        manual_mode=(manual_archive_paths is not None
                                     or manual_session is not None),
                        manual_archive_paths=manual_archive_paths,
                        append_card_info=card_info,
                        ownership=ownership, mutation_coordinated=True,
                        append_pre_existing=(snapshot if appending else None),
                        preinstalled_order=thunderstore_order,
                        preinstall_failures=(
                            ["Some Thunderstore mods failed to install."]
                            if thunderstore_failures else []),
                        callbacks=callbacks, control=control)
                except Exception:
                    try:
                        ownership.finish("incomplete")
                    except Exception:
                        pass
                    raise
                finally:
                    try:
                        game.set_active_profile_dir(previous)
                        game.load_paths()
                    finally:
                        close = getattr(downloader, "close_worker_session", None)
                        if callable(close):
                            close()
                if control.cancel.is_set() or control.stop.is_set():
                    raise JobCancelled("Collection installation cancelled.")
                report = state.get("report") or {}
                if not state.get("ready", False):
                    raise JobError(
                        "collection_install_incomplete",
                        "The collection installation finished with required work missing.",
                        {"profile": profile_name, "report": report},
                    )
                if updating:
                    _stamp_collection_profile(
                        profile_dir, domain, slug, resolved_revision,
                        skipped_optional_file_ids)
                    merge_profile_settings(profile_dir, {
                        "collection_card": card_info,
                    })
                progress(4, 4, "Collection installed")
                fresh = _installed_record(
                    profile_dir, str(record["install_id"]))
                from Utils.profiles.groups import profile_is_locked
                return {
                    "ok": True,
                    "profile": profile_name,
                    "resumed": resuming,
                    "updated": updating,
                    "appended": appending,
                    "collection_id": record["install_id"],
                    "revision": resolved_revision,
                    "installed": (int(state.get("installed", 0))
                                  + thunderstore_installed),
                    "skipped": (int(state.get("skipped", 0))
                                + len(thunderstore_failures)),
                    "total": (int(state.get("total", len(mods)))
                              + len(thunderstore)),
                    "thunderstore": {
                        "installed": thunderstore_installed,
                        "failed": len(thunderstore_failures),
                    },
                    "optional_skipped": sorted(skipped_optional_file_ids),
                    "offsite": [
                        {"name": mod_name, "url": url}
                        for mod_name, url in extract_offsite_mods(manifest)
                    ],
                    "report": report,
                    "update": update_summary,
                    "installation": _record_summary(
                        fresh, profile_dir, profile_is_locked(profile_dir)),
                }
            except ProfileError as exc:
                raise JobError(exc.name, str(exc), exc.details) from exc
            finally:
                mutation.__exit__(None, None, None)

        try:
            return _call(operation)
        except (JobError, JobCancelled):
            raise
        except Exception as exc:
            from Utils.operations import OperationBusy
            if isinstance(exc, OperationBusy):
                raise
            raise JobError(
                "collection_install_failed",
                str(exc) or "The collection installation failed.",
                {"profile": profile_name,
                 "exception": type(exc).__name__},
            ) from exc
    return run


def import_local_runner(
    game: Any,
    profile_name: str,
    source_path: Path,
    skipped_optional_file_ids: set[int],
    accept_game_version_mismatch: bool,
    nexus_session: Any,
    control: Any,
    owner: str,
    manual_archive_paths: dict[int, Path] | None = None,
    manual_session: Any | None = None,
    installer_requests: Any | None = None,
) -> Callable[[Callable[..., None], Callable[..., None]], dict[str, Any]]:
    def run(log: Callable[..., None], progress: Callable[..., None]) -> dict[str, Any]:
        from .nexus import _call

        def operation() -> dict[str, Any]:
            from Utils.collections.manifest import (
                extract_offsite_mods, is_collection_manifest,
            )
            from Utils.profiles import export as profile_export

            progress(0, 5, "Reading Amethyst profile")
            if (source_path.is_symlink() or not source_path.is_file()):
                raise JobError(
                    "collection_import_unavailable",
                    "The local Amethyst profile is no longer available.",
                    {"path": str(source_path)},
                )
            import zipfile
            bundle_path = str(source_path) if zipfile.is_zipfile(source_path) else ""
            try:
                if source_path.suffix.casefold() in {".r2x", ".txt"}:
                    from Thunderstore.r2_profile import read_profile, prepare_import
                    if control.stop.is_set():
                        raise JobCancelled("Profile import cancelled.")
                    community = str(getattr(game, "thunderstore_community", "") or "")
                    manifest = prepare_import(read_profile(source_path), community)
                else:
                    manifest = profile_export.read_manifest(source_path)
            except JobCancelled:
                raise
            except Exception as exc:
                raise JobError(
                    "collection_import_invalid",
                    "The local profile manifest could not be read.",
                    {"path": str(source_path), "reason": str(exc)},
                ) from exc
            if not is_collection_manifest(manifest):
                raise JobError(
                    "collection_import_invalid",
                    "The file does not contain a valid Amethyst manifest.",
                    {"path": str(source_path)},
                )
            try:
                all_mods, total_size = profile_export.manifest_install_mods(manifest)
            except ValueError as exc:
                raise JobError(
                    "collection_import_invalid", str(exc),
                    {"path": str(source_path)},
                ) from exc
            thunderstore = profile_export.thunderstore_entries(manifest)
            thunderstore_count = sum(
                1 for mod in all_mods
                if getattr(mod, "source_type", "") == "thunderstore")
            if len(thunderstore) != thunderstore_count:
                raise JobError(
                    "collection_import_invalid",
                    "A Thunderstore entry has an incomplete package identity.",
                    {"path": str(source_path)},
                )
            nexus_file_ids = [
                int(mod.file_id or 0) for mod in all_mods
                if getattr(mod, "source_type", "") == "nexus"]
            if (any(value <= 0 for value in nexus_file_ids)
                    or len(nexus_file_ids) != len(set(nexus_file_ids))):
                raise JobError(
                    "collection_import_invalid",
                    "Nexus entries must have distinct positive file IDs.",
                    {"path": str(source_path)},
                )
            optional_ids = {
                int(mod.file_id) for mod in all_mods
                if getattr(mod, "source_type", "") == "nexus"
                and mod.optional and int(mod.file_id or 0) > 0
            }
            invalid_skips = sorted(skipped_optional_file_ids - optional_ids)
            if invalid_skips:
                raise JobError(
                    "collection_optional_invalid",
                    "One or more skipped file IDs are not optional manifest mods.",
                    {"file_ids": invalid_skips,
                     "optional_file_ids": sorted(optional_ids)},
                )
            mods = [
                mod for mod in all_mods
                if getattr(mod, "source_type", "") != "thunderstore"
                and int(mod.file_id or 0) not in skipped_optional_file_ids
            ]
            skipped_mods = [
                mod for mod in all_mods
                if getattr(mod, "source_type", "") != "thunderstore"
                and int(mod.file_id or 0) in skipped_optional_file_ids
            ]
            from Utils.collections.providers import entries as provider_entries
            if not all_mods and not provider_entries(manifest):
                raise JobError(
                    "collection_no_installable_mods",
                    "The manifest has no downloadable or bundled mods.",
                    {"path": str(source_path)},
                )
            if manual_archive_paths is not None:
                _validate_manual_archives(mods, manifest, manual_archive_paths)

            progress(1, 5, "Validating Amethyst profile")
            info = manifest.get("info") or {}
            if not isinstance(info, dict):
                info = {}
            domain = str(info.get("domainName") or "").strip()
            game_domain = str(getattr(game, "nexus_game_domain", "") or "").strip()
            accepts_domain = getattr(game, "accepts_nexus_domain", None)
            if domain and game_domain:
                accepted = (bool(accepts_domain(domain)) if callable(accepts_domain)
                            else domain.casefold() == game_domain.casefold())
                if not accepted:
                    raise JobError(
                        "collection_game_mismatch",
                        "The Amethyst profile targets a different game.",
                        {"manifest_domain": domain,
                         "game_domain": game_domain},
                    )
            from Utils.collections.export import collection_game_version_mismatch
            mismatch = collection_game_version_mismatch(
                game, info.get("gameVersions") or ())
            if mismatch is not None and not accept_game_version_mismatch:
                installed, expected = mismatch
                raise JobError(
                    "collection_game_version_mismatch",
                    "The Amethyst profile targets a different game version.",
                    {"installed": installed, "expected": list(expected),
                     "retry_with": {"accept_game_version_mismatch": True}},
                )

            api = None
            if any(getattr(mod, "source_type", "") == "nexus" for mod in mods):
                progress(2, 5, "Checking Nexus account")
                api = nexus_session.require_api()
                if manual_archive_paths is None and manual_session is None:
                    user = api.validate()
                    if not bool(getattr(user, "is_premium", False)):
                        raise JobError(
                            "nexus_premium_required",
                            "Automatic headless profile import requires Nexus Premium.",
                            {"manual_install_supported": True},
                        )
            if control.stop.is_set():
                raise JobCancelled("Profile import cancelled.")

            from Utils.deployment.locking import coordinated_game_mutation
            from Utils.profiles.manage import ProfileError, create_profile
            mutation = coordinated_game_mutation(
                game, "collections.import", owner=owner)
            mutation.__enter__()
            try:
                progress(3, 5, "Preparing imported profile")
                profile_dir = Path(game.get_profile_root()) / "profiles" / profile_name
                create_profile(
                    game, profile_name, profile_specific_mods=True,
                    log_fn=log, operation_owner=owner,
                    mutation_coordinated=True)
                import re
                safe_stem = re.sub(
                    r"[^\w-]", "_", source_path.stem).strip("_")[:60]
                slug = f"import_{safe_stem or 'profile'}"
                from Utils.profiles.state import (
                    merge_profile_settings, write_collection_optional_skipped,
                )
                card_info = {
                    "id": 0,
                    "name": str(info.get("name") or source_path.stem),
                    "slug": slug,
                    "game_domain": domain or game_domain,
                }
                merge_profile_settings(profile_dir, {
                    "collection_identity": {
                        "domain": domain or game_domain,
                        "slug": slug,
                        "revision": None,
                    },
                    "collection_generated_profile_name": profile_name,
                    "collection_bundle_path": bundle_path,
                    "collection_card": card_info,
                })
                write_collection_optional_skipped(
                    profile_dir, skipped_optional_file_ids)
                from Utils.collections.installed import begin_collection, save_record
                record = begin_collection(
                    profile_dir, slug=slug, revision=None, card=card_info,
                    manifest=manifest, appended=False)
                record["source"] = "local"
                record["skipped_fids"] = sorted(skipped_optional_file_ids)
                save_record(record)
                from Utils.mods.copy import resolve_target_staging
                from Utils.collections.ownership import CollectionOwnership
                staging = Path(resolve_target_staging(game, profile_dir))
                ownership = CollectionOwnership(staging, record)
                state: dict[str, Any] = {"phase": "Importing profile"}

                def status(message: str) -> None:
                    state["phase"] = str(message)
                    log(message)

                def fraction(value: Any) -> None:
                    try:
                        amount = max(0.0, min(1.0, float(value)))
                    except (TypeError, ValueError):
                        return
                    progress(round(amount * 1000), 1000, state["phase"])

                from Utils.collections.install import (
                    CollectionInstallCallbacks, required_bundle_folders,
                    run_collection_install,
                )
                callbacks = CollectionInstallCallbacks(
                    resolve_fomod=installer_requests.fomod if installer_requests is not None else None,
                    resolve_bain=installer_requests.bain if installer_requests is not None else None,
                    resolve_layout=installer_requests.layout if installer_requests is not None else None,
                    resolve_choice=installer_requests.choice if installer_requests is not None else None,
                    on_status=status,
                    on_progress=fraction,
                    on_log=log,
                    on_result=lambda report: state.update(report=report.to_dict()),
                    on_done=lambda installed, skipped, total, profile:
                        state.update(installed=installed, skipped=skipped,
                                     total=total, profile=profile),
                    on_manual_mod=(manual_session.on_prompt
                                   if manual_session is not None else lambda payload: None),
                    on_manual_resolved=(manual_session.on_resolved
                                        if manual_session is not None else lambda request_id: None),
                    on_manual_rejected=(manual_session.on_rejected
                                        if manual_session is not None else lambda request_id: None),
                )
                from Nexus.nexus_download import NexusDownloader
                from Utils.config_paths import get_download_cache_dir_for_game
                downloader = NexusDownloader(
                    api, download_dir=get_download_cache_dir_for_game(
                        str(getattr(game, "name", "") or "")))
                previous = getattr(game, "_active_profile_dir", None)
                thunderstore_order = []
                thunderstore_installed = 0
                thunderstore_failures = []
                try:
                    if thunderstore:
                        from Utils.collections.thunderstore import install_entries
                        (thunderstore_order, thunderstore_installed,
                         thunderstore_failures) = install_entries(
                            thunderstore, game, profile_dir, control, callbacks,
                            ownership, log_fn=log)
                    run_collection_install(
                        game=game, api=api, downloader=downloader, mods=mods,
                        download_link_path="", profile_dir=profile_dir,
                        old_profile_dir=previous, collection_slug=slug,
                        collection_domain=domain or game_domain,
                        revision_number=None,
                        collection_total_size=int(total_size or 0),
                        collection_schema_cache=manifest,
                        skipped_fids=skipped_optional_file_ids,
                        skipped_mods=skipped_mods,
                        manual_mode=(manual_archive_paths is not None
                                     or manual_session is not None),
                        manual_archive_paths=manual_archive_paths,
                        append_card_info=card_info, ownership=ownership,
                        local_bundle_zip=bundle_path,
                        preinstalled_order=thunderstore_order,
                        preinstall_failures=(
                            ["Some Thunderstore mods failed to install."]
                            if thunderstore_failures else []),
                        mutation_coordinated=True,
                        callbacks=callbacks, control=control)
                    if control.cancel.is_set() or control.stop.is_set():
                        raise JobCancelled("Profile import cancelled.")

                    report = state.get("report") or {}
                    staged = []
                    bundle_errors = []
                    if bundle_path:
                        from Utils.profiles.state import read_profile_settings
                        settings = read_profile_settings(profile_dir, None)
                        preserved = {
                            key: value for key, value in settings.items()
                            if key.startswith("collection_")
                            or key in ("profile_specific_mods",
                                      "pending_collection_group")}
                        try:
                            staged = profile_export.install_local_bundle(
                                source_path, profile_dir, staging,
                                profile_dir / "overwrite", log_fn=log,
                                error_sink=bundle_errors,
                                on_staged=ownership.track)
                            found, missing = required_bundle_folders(
                                staging, manifest, slug)
                            report.setdefault("required_folders", []).extend(found)
                            report.setdefault("missing_required", []).extend(missing)
                            if staged:
                                from Utils.filegraph.service import FileGraphService
                                library = FileGraphService.open_library(
                                    game, profile_dir, log_fn=log)
                                library.refresh(profile_dir, mod_names=staged)
                        except Exception as exc:
                            bundle_errors.append(str(exc))
                        finally:
                            merge_profile_settings(profile_dir, preserved)
                        if not bundle_errors:
                            from Utils.collections.options import BUNDLE_PENDING
                            report["failed_stages"] = [
                                item for item in report.get("failed_stages", [])
                                if item != BUNDLE_PENDING]
                        else:
                            report.setdefault("failed_stages", []).extend(bundle_errors)

                    report["required_folders"] = list(dict.fromkeys(
                        report.get("required_folders", [])))
                    report["missing_required"] = list(dict.fromkeys(
                        report.get("missing_required", [])))
                    report["failed_stages"] = list(dict.fromkeys(
                        report.get("failed_stages", [])))
                    from Utils.mods.modlist import read_modlist
                    listed = {
                        entry.name for entry in read_modlist(
                            profile_dir / "modlist.txt")
                        if not entry.is_separator}
                    missing_from_modlist = (
                        "Required mods are missing from the profile modlist")
                    if (any(name not in listed for name in
                            report["required_folders"])
                            and missing_from_modlist not in
                            report["failed_stages"]):
                        report["failed_stages"].append(missing_from_modlist)
                    merge_profile_settings(
                        profile_dir, {"collection_install_report": report})
                    ready = bool(
                        report.get("verified")
                        and not report.get("missing_required")
                        and not report.get("failed_stages"))
                    ownership.finish(
                        "complete" if ready else "incomplete", report=report)
                    if not ready:
                        raise JobError(
                            "collection_import_incomplete",
                            "The Amethyst profile import has required work missing.",
                            {"profile": profile_name, "report": report},
                        )
                except JobCancelled:
                    try:
                        ownership.finish("cancelled")
                    except Exception:
                        pass
                    raise
                except JobError:
                    try:
                        ownership.finish("incomplete")
                    except Exception:
                        pass
                    raise
                except Exception:
                    try:
                        ownership.finish("incomplete")
                    except Exception:
                        pass
                    raise
                finally:
                    try:
                        game.set_active_profile_dir(previous)
                        game.load_paths()
                    finally:
                        close = getattr(downloader, "close_worker_session", None)
                        if callable(close):
                            close()

                progress(5, 5, "Amethyst profile imported")
                fresh = _installed_record(
                    profile_dir, str(record["install_id"]))
                from Utils.profiles.groups import profile_is_locked
                return {
                    "ok": True,
                    "profile": profile_name,
                    "collection_id": record["install_id"],
                    "installed": (int(state.get("installed", 0))
                                  + thunderstore_installed + len(staged)),
                    "skipped": (int(state.get("skipped", 0))
                                + len(thunderstore_failures)),
                    "total": (int(state.get("total", len(mods)))
                              + len(thunderstore)),
                    "thunderstore": {
                        "installed": thunderstore_installed,
                        "failed": len(thunderstore_failures),
                    },
                    "bundled": {"installed": len(staged)},
                    "optional_skipped": sorted(skipped_optional_file_ids),
                    "offsite": [
                        {"name": mod_name, "url": url}
                        for mod_name, url in extract_offsite_mods(manifest)
                    ],
                    "report": report,
                    "installation": _record_summary(
                        fresh, profile_dir, profile_is_locked(profile_dir)),
                }
            except ProfileError as exc:
                raise JobError(exc.name, str(exc), exc.details) from exc
            finally:
                mutation.__exit__(None, None, None)

        try:
            return _call(operation)
        except (JobError, JobCancelled):
            raise
        except Exception as exc:
            from Utils.operations import OperationBusy
            if isinstance(exc, OperationBusy):
                raise
            raise JobError(
                "collection_import_failed",
                str(exc) or "The Amethyst profile could not be imported.",
                {"profile": profile_name, "path": str(source_path),
                 "exception": type(exc).__name__},
            ) from exc

    return run


def resume_runner(
    game: Any,
    profile_dir: Path,
    collection_id: str,
    state_revision: str,
    accept_game_version_mismatch: bool,
    nexus_session: Any,
    control: Any,
    owner: str,
    manual_archive_paths: dict[int, Path] | None = None,
    manual_session: Any | None = None,
    installer_requests: Any | None = None,
) -> Callable[[Callable[..., None], Callable[..., None]], dict[str, Any]]:
    def run(log: Callable[..., None], progress: Callable[..., None]) -> dict[str, Any]:
        record = _installed_record(profile_dir, collection_id, state_revision)
        if record.get("kind") != "standalone":
            raise JobError(
                "collection_resume_unsupported",
                "Appended collection resume is not yet supported headlessly.",
                {"profile": profile_dir.name, "collection_id": collection_id,
                 "kind": record.get("kind")},
            )
        if record.get("previous"):
            raise JobError(
                "collection_update_pending",
                "This profile has an unfinished collection update.",
                {"profile": profile_dir.name, "collection_id": collection_id},
            )
        if record.get("status") == "complete":
            raise JobError(
                "collection_already_complete",
                "This collection installation is already complete.",
                {"profile": profile_dir.name, "collection_id": collection_id},
            )
        slug = str(record.get("slug") or "")
        domain = str(record.get("domain") or
                     getattr(game, "nexus_game_domain", "") or "").strip().lower()
        try:
            revision = int(record.get("revision") or 0)
            skipped = {int(value) for value in (record.get("skipped_fids") or ())}
        except (TypeError, ValueError) as exc:
            raise JobError(
                "collection_record_invalid",
                "The saved collection installation is invalid.",
                {"profile": profile_dir.name,
                 "collection_id": collection_id},
            ) from exc
        if (not slug or not domain or revision <= 0
                or any(value <= 0 for value in skipped)):
            raise JobError(
                "collection_record_invalid",
                "The saved collection installation has no usable Nexus identity.",
                {"profile": profile_dir.name,
                 "collection_id": collection_id},
            )
        return install_new_runner(
            game, profile_dir.name, domain, slug, revision, skipped,
            accept_game_version_mismatch, nexus_session, control, owner,
            resume_profile_dir=profile_dir,
            expected_collection_id=collection_id,
            expected_state_revision=state_revision,
            manual_archive_paths=manual_archive_paths,
            manual_session=manual_session, installer_requests=installer_requests,
        )(log, progress)

    return run


def update_runner(
    game: Any,
    profile_dir: Path,
    collection_id: str,
    state_revision: str,
    target_revision: int,
    skipped_optional_file_ids: set[int] | None,
    accept_game_version_mismatch: bool,
    nexus_session: Any,
    control: Any,
    owner: str,
    manual_archive_paths: dict[int, Path] | None = None,
    manual_session: Any | None = None,
    installer_requests: Any | None = None,
) -> Callable[[Callable[..., None], Callable[..., None]], dict[str, Any]]:
    def run(log: Callable[..., None], progress: Callable[..., None]) -> dict[str, Any]:
        record = _installed_record(profile_dir, collection_id, state_revision)
        if record.get("kind") != "standalone":
            raise JobError(
                "collection_update_unsupported",
                "Appended collection updates are not yet supported headlessly.",
                {"profile": profile_dir.name, "collection_id": collection_id,
                 "kind": record.get("kind")},
            )
        current_revision = record.get("revision")
        if record.get("previous"):
            if str(current_revision) != str(target_revision):
                raise JobError(
                    "collection_update_pending",
                    "Finish the interrupted collection update before choosing another revision.",
                    {"profile": profile_dir.name,
                     "collection_id": collection_id,
                     "pending_revision": current_revision},
                )
        elif str(current_revision) == str(target_revision):
            raise JobError(
                "collection_already_current",
                "This collection already uses the requested revision.",
                {"profile": profile_dir.name, "collection_id": collection_id,
                 "revision": target_revision},
            )
        slug = str(record.get("slug") or "")
        domain = str(record.get("domain") or
                     getattr(game, "nexus_game_domain", "") or "").strip().lower()
        try:
            saved_skips = {
                int(value) for value in (record.get("skipped_fids") or ())
            }
        except (TypeError, ValueError) as exc:
            raise JobError(
                "collection_record_invalid",
                "The saved collection installation is invalid.",
                {"profile": profile_dir.name,
                 "collection_id": collection_id},
            ) from exc
        skipped = (saved_skips if skipped_optional_file_ids is None else
                   set(skipped_optional_file_ids))
        if (not slug or not domain or target_revision <= 0
                or any(value <= 0 for value in skipped)):
            raise JobError(
                "collection_record_invalid",
                "The collection update has no usable Nexus identity.",
                {"profile": profile_dir.name,
                 "collection_id": collection_id},
            )
        return install_new_runner(
            game, profile_dir.name, domain, slug, target_revision, skipped,
            accept_game_version_mismatch, nexus_session, control, owner,
            resume_profile_dir=profile_dir,
            expected_collection_id=collection_id,
            expected_state_revision=state_revision,
            update_target=True,
            manual_archive_paths=manual_archive_paths,
            manual_session=manual_session, installer_requests=installer_requests,
        )(log, progress)

    return run


def append_runner(
    game: Any,
    profile_dir: Path,
    domain: str,
    slug: str,
    revision: int | None,
    skipped_optional_file_ids: set[int],
    overwrite_existing: bool,
    skip_existing: bool,
    accept_game_version_mismatch: bool,
    nexus_session: Any,
    control: Any,
    owner: str,
    manual_archive_paths: dict[int, Path] | None = None,
    manual_session: Any | None = None,
    installer_requests: Any | None = None,
) -> Callable[[Callable[..., None], Callable[..., None]], dict[str, Any]]:
    return install_new_runner(
        game, profile_dir.name, domain, slug, revision,
        skipped_optional_file_ids, accept_game_version_mismatch,
        nexus_session, control, owner,
        resume_profile_dir=profile_dir,
        append_target=True,
        append_overwrite_existing=overwrite_existing,
        append_skip_existing=skip_existing,
        manual_archive_paths=manual_archive_paths,
        manual_session=manual_session, installer_requests=installer_requests,
    )


def download_runner(
    game: Any,
    domain: str,
    slug: str,
    revision: int | None,
    nexus_session: Any,
    control: Any,
) -> Callable[[Callable[..., None], Callable[..., None]], dict[str, Any]]:
    def run(log: Callable[..., None], progress: Callable[..., None]) -> dict[str, Any]:
        from .nexus import _call

        def operation() -> dict[str, Any]:
            from Utils.collections.manifest import (
                extract_offsite_mods, is_collection_manifest,
                load_collection_manifest,
            )

            progress(0, 2, "Fetching collection detail")
            api = nexus_session.require_api()
            (name, total_size, _mod_count, mods, download_link,
             revisions, card) = api.get_collection_detail(
                 slug, domain, revision_number=revision)
            if not name and not revisions and not card:
                raise JobError(
                    "collection_not_found",
                    "The Nexus collection or revision could not be loaded.",
                    {"slug": slug, "domain": domain, "revision": revision},
                )
            if card.get("unsupported_collection_schema"):
                raise JobError(
                    "collection_schema_unsupported",
                    "This collection revision uses an unsupported schema.",
                    {"slug": slug, "domain": domain, "revision": revision,
                     "schema_id": card.get("collection_schema_id")},
                )
            resolved_revision = revision
            if resolved_revision is None:
                published = []
                for value in revisions:
                    try:
                        if str(value.get("revisionStatus") or "").casefold() == "published":
                            published.append(int(value.get("revisionNumber") or 0))
                    except (AttributeError, TypeError, ValueError):
                        continue
                resolved_revision = max(published) if published else None
            if not download_link:
                raise JobError(
                    "collection_manifest_unavailable",
                    "This collection revision has no downloadable manifest.",
                    {"slug": slug, "domain": domain,
                     "revision": resolved_revision},
                )
            progress(1, 2, "Fetching collection manifest")
            manifest = load_collection_manifest(
                api, str(getattr(game, "name", "") or ""), slug,
                resolved_revision, download_link, log_fn=log)
            if not is_collection_manifest(manifest):
                raise JobError(
                    "collection_manifest_unavailable",
                    "The collection manifest could not be loaded.",
                    {"slug": slug, "domain": domain,
                     "revision": resolved_revision},
                )
            from Utils.profiles import export as profile_export
            thunderstore = profile_export.thunderstore_entries(manifest)
            thunderstore_count = sum(
                1 for mod in manifest.get("mods", [])
                if isinstance(mod, dict)
                and str(((mod.get("source") or {}).get("type") or ""))
                .strip().casefold() == "thunderstore")
            if len(thunderstore) != thunderstore_count:
                raise JobError(
                    "collection_manifest_invalid",
                    "A Thunderstore entry has an incomplete package identity.",
                    {"source": "thunderstore"},
                )
            if mods:
                user = api.validate()
                if not bool(getattr(user, "is_premium", False)):
                    raise JobError(
                        "nexus_premium_required",
                        "Automatic headless collection downloads require Nexus Premium.",
                        {"manual_download_supported": False},
                    )
            if control.stop.is_set():
                raise JobCancelled("Collection download cancelled.")

            state: dict[str, Any] = {"phase": "Downloading collection"}

            def status(message: str) -> None:
                state["phase"] = str(message)
                log(message)

            def fraction(value: Any) -> None:
                try:
                    amount = max(0.0, min(1.0, float(value)))
                except (TypeError, ValueError):
                    return
                progress(round(amount * 1000), 1000, state["phase"])

            from Utils.collections.install import (
                CollectionInstallCallbacks, run_collection_install,
            )
            callbacks = CollectionInstallCallbacks(
                on_status=status,
                on_progress=fraction,
                on_log=log,
                on_done=lambda downloaded, skipped, total, _profile:
                    state.update(downloaded=downloaded, skipped=skipped,
                                 total=total),
            )
            from Nexus.nexus_download import NexusDownloader
            from Utils.config_paths import get_download_cache_dir_for_game
            downloader = NexusDownloader(
                api, download_dir=get_download_cache_dir_for_game(
                    str(getattr(game, "name", "") or "")))
            thunderstore_downloaded = 0
            thunderstore_failures = []
            try:
                if thunderstore:
                    from Utils.collections.thunderstore import download_entries
                    (thunderstore_downloaded,
                     thunderstore_failures) = download_entries(
                        thunderstore, game, control, callbacks, log_fn=log)
                run_collection_install(
                    game=game, api=api, downloader=downloader, mods=mods,
                    download_link_path=download_link,
                    profile_dir=None, old_profile_dir=None,
                    collection_slug=slug, collection_domain=domain,
                    revision_number=resolved_revision,
                    collection_total_size=int(total_size or 0),
                    collection_schema_cache=manifest,
                    manual_mode=False, download_only=True,
                    callbacks=callbacks, control=control,
                )
            finally:
                close = getattr(downloader, "close_worker_session", None)
                if callable(close):
                    close()
            if control.cancel.is_set() or control.stop.is_set():
                raise JobCancelled("Collection download cancelled.")
            progress(2, 2, "Collection downloaded")
            return {
                "ok": True,
                "domain": domain,
                "slug": slug,
                "revision": resolved_revision,
                "name": str(name or slug),
                "downloaded": (int(state.get("downloaded", 0))
                               + thunderstore_downloaded),
                "skipped": (int(state.get("skipped", 0))
                             + len(thunderstore_failures)),
                "total": (int(state.get("total", len(mods)))
                          + len(thunderstore)),
                "thunderstore": {
                    "downloaded": thunderstore_downloaded,
                    "failed": len(thunderstore_failures),
                },
                "offsite": [
                    {"name": mod_name, "url": url}
                    for mod_name, url in extract_offsite_mods(manifest)
                ],
            }

        try:
            return _call(operation)
        except (JobError, JobCancelled):
            raise
        except Exception as exc:
            raise JobError(
                "collection_download_failed",
                str(exc) or "The collection download failed.",
                {"slug": slug, "domain": domain,
                 "exception": type(exc).__name__},
            ) from exc

    return run
