from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .jobs import JobError


_CARD_FIELDS = (
    "id", "name", "summary", "description", "user_name", "author",
    "tile_image_url", "image_url", "thumbnail_url", "total_downloads",
    "endorsements", "adult",
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
    domain = str(record.get("domain") or info.get("domainName") or "") or None
    status = str(record.get("status") or "unknown")
    return {
        "id": install_id,
        "kind": kind,
        "profile": profile_dir.name,
        "title": collection_title(record, profile_dir.name),
        "slug": slug,
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
        "can_remove": ((kind == "appended" and install_id is not None)
                       or kind == "standalone") and not locked,
        "can_reset_load_order": install_id is not None and not locked,
        "remove_method": ("collections.remove.start" if kind == "appended" and install_id
                          else "profiles.remove.start" if kind == "standalone"
                          else None),
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
