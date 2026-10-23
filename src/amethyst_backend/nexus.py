from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .jobs import JobCancelled, JobError


def _timestamp(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return str(value)


def _rate_limits(api: Any) -> dict[str, Any]:
    limits = api.rate_limits
    return {
        "hourly_remaining": int(limits.hourly_remaining),
        "hourly_limit": int(limits.hourly_limit),
        "daily_remaining": int(limits.daily_remaining),
        "daily_limit": int(limits.daily_limit),
        "last_updated": _timestamp(limits.last_updated),
    }


def _account(user: Any) -> dict[str, Any]:
    return {
        "user_id": int(user.user_id),
        "name": str(user.name),
        "is_premium": bool(user.is_premium),
        "is_supporter": bool(user.is_supporter),
        "profile_url": str(user.profile_url or ""),
    }


def _mod(mod: Any) -> dict[str, Any]:
    return {
        "mod_id": int(mod.mod_id),
        "name": str(mod.name),
        "summary": str(mod.summary or ""),
        "description": str(mod.description or ""),
        "version": str(mod.version or ""),
        "author": str(mod.author or ""),
        "category_id": int(mod.category_id or 0),
        "category_name": str(mod.category_name or ""),
        "game_id": int(mod.game_id or 0),
        "domain": str(mod.domain_name or ""),
        "picture_url": str(mod.picture_url or ""),
        "endorsement_count": int(mod.endorsement_count or 0),
        "downloads_total": int(mod.downloads_total or 0),
        "created_timestamp": int(mod.created_timestamp or 0),
        "updated_timestamp": int(mod.updated_timestamp or 0),
        "created_at": str(mod.created_at or ""),
        "updated_at": str(mod.updated_at or ""),
        "available": bool(mod.available),
        "contains_adult_content": bool(mod.contains_adult_content),
        "status": str(mod.status or ""),
        "uploaded_by": str(mod.uploaded_by or ""),
        "uploader_id": int(mod.uploader_id or 0),
        "file_size_kb": int(mod.file_size_kb or 0),
    }


def _category(category: Any) -> dict[str, Any]:
    parent = category.parent_category
    return {
        "category_id": int(category.category_id),
        "name": str(category.name or ""),
        "parent_category_id": int(parent) if parent is not None else None,
    }


def _tag(tag: Any) -> dict[str, Any]:
    return {
        "tag_id": int(tag.tag_id),
        "name": str(tag.name or ""),
    }


def _collection(value: Any) -> dict[str, Any]:
    domain = str(value.game_domain or "")
    slug = str(value.slug or "")
    return {
        "id": int(value.id or 0),
        "slug": slug,
        "name": str(value.name or ""),
        "summary": str(value.summary or ""),
        "author": str(value.user_name or ""),
        "total_downloads": int(value.total_downloads or 0),
        "endorsements": int(value.endorsements or 0),
        "mod_count": int(value.mod_count or 0),
        "tile_image_url": str(value.tile_image_url or ""),
        "domain": domain,
        "contains_adult_content": bool(value.contains_adult_content),
        "url": (f"https://next.nexusmods.com/{domain}/collections/{slug}"
                if domain and slug else None),
    }


def _collection_mod(value: Any) -> dict[str, Any]:
    return {
        "mod_id": int(value.mod_id or 0),
        "file_id": int(value.file_id or 0),
        "mod_name": str(value.mod_name or ""),
        "mod_author": str(value.mod_author or ""),
        "file_name": str(value.file_name or ""),
        "version": str(value.version or ""),
        "size_bytes": int(value.size_bytes or 0),
        "optional": bool(value.optional),
        "source_type": str(value.source_type or "nexus"),
        "category_id": int(value.category_id or 0),
        "category_name": str(value.category_name or ""),
        "install_type": str(value.install_type or ""),
        "md5": str(value.md5 or ""),
        "domain": str(value.domain_name or ""),
        "update_policy": str(value.update_policy or "exact"),
        "resolved_file_id": int(value.resolved_file_id or 0),
        "resolved_file_name": str(value.resolved_nexus_file_name or ""),
        "resolved_file_category": str(value.resolved_file_category or ""),
    }


def _file(file: Any) -> dict[str, Any]:
    return {
        "file_id": int(file.file_id),
        "name": str(file.name or ""),
        "version": str(file.version or ""),
        "category_id": int(file.category_id or 0),
        "category_name": str(file.category_name or ""),
        "file_name": str(file.file_name or ""),
        "size_in_bytes": (
            int(file.size_in_bytes)
            if file.size_in_bytes is not None else None),
        "size_kb": int(file.size_kb or 0),
        "mod_version": str(file.mod_version or ""),
        "description": str(file.description or ""),
        "uploaded_timestamp": int(file.uploaded_timestamp or 0),
        "is_primary": bool(file.is_primary),
        "changelog_html": str(file.changelog_html or ""),
        "external_virus_scan_url": str(
            file.external_virus_scan_url or ""),
    }


def _available_update(update: Any) -> dict[str, Any]:
    return {
        "mod": str(update.mod_name),
        "domain": str(update.game_domain),
        "mod_id": int(update.mod_id),
        "installed_file_id": int(update.installed_file_id or 0),
        "installed_version": str(update.installed_version or ""),
        "latest_file_id": int(update.latest_file_id or 0),
        "latest_version": str(update.latest_version or ""),
        "latest_file_name": str(update.latest_file_name or ""),
        "nexus_url": str(update.nexus_url or ""),
    }


def _missing_requirements(value: Any) -> dict[str, Any]:
    return {
        "mod": str(value.mod_name),
        "mod_id": int(value.mod_id),
        "missing": [
            {
                "domain": str(requirement.game_domain or ""),
                "mod_id": int(requirement.mod_id),
                "name": str(requirement.mod_name or ""),
                "url": str(requirement.url or ""),
                "notes": str(requirement.notes or ""),
            }
            for requirement in value.missing
        ],
    }


def _thunderstore_update(value: Any) -> dict[str, Any]:
    return {
        "mod": str(value.mod_name),
        "namespace": str(value.namespace or ""),
        "package": str(value.name or ""),
        "package_id": str(value.package_id),
        "installed_version": str(value.installed_version or ""),
        "latest_version": str(value.latest_version or ""),
        "has_update": bool(value.has_update),
        "deprecated": bool(value.is_deprecated),
        "unknown": bool(value.unknown),
    }


def _modio_update(value: Any) -> dict[str, Any]:
    return {
        "mod": str(value.mod_name),
        "mod_id": int(value.mod_id or 0),
        "installed_file_id": int(value.installed_file_id or 0),
        "installed_version": str(value.installed_version or ""),
        "latest_file_id": int(value.latest_file_id or 0),
        "latest_version": str(value.latest_version or ""),
        "has_update": bool(value.has_update),
        "unknown": bool(value.unknown),
    }


def _update_value(update: dict[str, Any], snake: str, camel: str) -> Any:
    return update.get(snake, update.get(camel))


def _file_update(update: dict[str, Any]) -> dict[str, Any]:
    def number(snake: str, camel: str) -> int:
        try:
            return int(_update_value(update, snake, camel) or 0)
        except (TypeError, ValueError):
            return 0

    return {
        "old_file_id": number("old_file_id", "oldFileId"),
        "new_file_id": number("new_file_id", "newFileId"),
        "old_file_name": str(
            _update_value(update, "old_file_name", "oldFileName") or ""),
        "new_file_name": str(
            _update_value(update, "new_file_name", "newFileName") or ""),
        "uploaded_timestamp": number(
            "uploaded_timestamp", "uploadedTimestamp"),
    }


def _call(operation: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    try:
        return operation()
    except Exception as exc:
        from Nexus.session import NexusSessionError

        if isinstance(exc, NexusSessionError):
            raise JobError(exc.name, str(exc), exc.details) from exc

        from Nexus.nexus_api import NexusAPIError, RateLimitError
        from Nexus.nexus_oauth import OAuthRefreshError

        if isinstance(exc, OAuthRefreshError):
            name = ("nexus_auth_expired" if exc.token_revoked
                    else "nexus_auth_unavailable")
            message = ("The Nexus Mods login expired; log in again."
                       if exc.token_revoked else
                       "The Nexus Mods login could not be refreshed.")
            raise JobError(name, message) from exc
        if isinstance(exc, RateLimitError):
            raise JobError(
                "nexus_rate_limited",
                "Nexus Mods rate limit exceeded; retry later.",
                {"status_code": 429},
            ) from exc
        if isinstance(exc, NexusAPIError):
            if exc.status_code == 401:
                raise JobError(
                    "nexus_auth_expired",
                    "The Nexus Mods login was rejected; log in again.",
                    {"status_code": 401},
                ) from exc
            raise JobError(
                "nexus_request_failed",
                str(exc) or "The Nexus Mods request failed.",
                {"status_code": int(exc.status_code or 0)},
            ) from exc

        try:
            import requests
            is_request_error = isinstance(exc, requests.RequestException)
            response = getattr(exc, "response", None)
            status_code = int(getattr(response, "status_code", 0) or 0)
        except Exception:
            is_request_error = False
            status_code = 0
        if is_request_error:
            if status_code == 401:
                raise JobError(
                    "nexus_auth_expired",
                    "The Nexus Mods login was rejected; log in again.",
                    {"status_code": status_code},
                ) from exc
            raise JobError(
                "nexus_request_failed",
                "Could not complete the Nexus Mods request.",
                {"status_code": status_code},
            ) from exc
        raise


def account_runner(session: Any, *, refresh: bool = False) -> Callable:
    def run(_log: Callable, progress: Callable) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            progress(0, 1, "validate_account")
            api = session.require_api()
            user = api.validate(bypass_cache=refresh)
            progress(1, 1, "validate_account")
            return {"account": _account(user), "rate_limits": _rate_limits(api)}

        return _call(operation)
    return run


def logout_runner(session: Any, login: Any) -> Callable:
    def run(_log: Callable, progress: Callable) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            progress(0, 1, "clear_credentials")
            login.cancel()
            session.clear_credentials()
            progress(1, 1, "clear_credentials")
            return {"ok": True, "authenticated": False}

        return _call(operation)
    return run


def filters_runner(session: Any, domain: str) -> Callable:
    def run(_log: Callable, progress: Callable) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            progress(0, 2, "fetch_categories")
            api = session.require_api()
            categories = [_category(item)
                          for item in api.get_game_categories(domain)]
            progress(1, 2, "fetch_tags")
            tags = [_tag(item) for item in api.get_game_tags(domain)]
            progress(2, 2, "fetch_tags")
            return {
                "domain": domain,
                "categories": categories,
                "tags": tags,
                "counts": {
                    "categories": len(categories),
                    "tags": len(tags),
                },
                "rate_limits": _rate_limits(api),
            }

        return _call(operation)
    return run


def search_runner(
    session: Any,
    domain: str,
    *,
    mode: str,
    query: str | None,
    uploader_id: int | None,
    limit: int,
    offset: int,
    categories: list[str],
    sort_key: str,
    created_since_days: int | None,
    filters: Any,
    options: dict[str, Any],
) -> Callable:
    def run(_log: Callable, progress: Callable) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            progress(0, 1, "search_catalogue")
            api = session.require_api()
            common = {
                "count": limit,
                "offset": offset,
                "category_names": categories or None,
                "filters": filters,
            }
            if mode == "browse":
                mods = api.get_top_mods(
                    domain,
                    created_since_days=created_since_days,
                    sort_key=sort_key,
                    **common,
                )
            elif mode == "name":
                mods = api.search_mods(
                    domain, query or "", sort_key=sort_key, **common)
            elif mode == "author":
                mods = api.search_mods_by_author(
                    domain, query or "", sort_key=sort_key, **common)
            elif mode == "uploader_id":
                mods = api.search_mods_by_uploader_id(
                    domain, int(uploader_id or 0),
                    sort_key=sort_key, **common)
            elif mode == "trending":
                mods = api.get_trending_mods_graphql(domain, **common)
            else:
                raise ValueError(f"Unsupported Nexus query mode: {mode}")

            result = [_mod(item) for item in mods]
            progress(1, 1, "search_catalogue")
            return {
                "domain": domain,
                "query": dict(options),
                "mods": result,
                "page": {
                    "offset": offset,
                    "limit": limit,
                    "returned": len(result),
                    "next_offset": (
                        offset + limit if len(result) == limit else None),
                },
                "rate_limits": _rate_limits(api),
            }

        return _call(operation)
    return run


def collection_search_runner(
    session: Any,
    domain: str,
    *,
    query: str | None,
    limit: int,
    offset: int,
    sort: str,
    include_adult: bool,
) -> Callable:
    def run(_log: Callable, progress: Callable) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            progress(0, 1, "search_collections")
            api = session.require_api()
            if query:
                values = api.search_collections(
                    domain, query, count=limit, offset=offset, sort=sort)
            else:
                values = api.get_collections(
                    domain, count=limit, offset=offset, sort=sort)
            returned = len(values)
            hidden = sum(
                bool(value.contains_adult_content) for value in values)
            if not include_adult:
                values = [
                    value for value in values
                    if not value.contains_adult_content
                ]
            progress(1, 1, "search_collections")
            return {
                "domain": domain,
                "query": {
                    "text": query,
                    "sort": sort,
                    "include_adult": include_adult,
                    "limit": limit,
                    "offset": offset,
                },
                "collections": [_collection(value) for value in values],
                "page": {
                    "offset": offset,
                    "limit": limit,
                    "returned": returned,
                    "visible": len(values),
                    "adult_hidden": hidden if not include_adult else 0,
                    "next_offset": offset + limit if returned == limit else None,
                },
                "rate_limits": _rate_limits(api),
            }

        return _call(operation)
    return run


def collection_detail_runner(
    session: Any,
    game_name: str,
    domain: str,
    slug: str,
    revision: int | None,
    include_manifest: bool,
) -> Callable:
    def run(log: Callable, progress: Callable) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            progress(0, 2 if include_manifest else 1, "fetch_collection")
            api = session.require_api()
            (name, total_size, mod_count, mods, download_link,
             revisions, card) = api.get_collection_detail(
                 slug, domain, revision_number=revision)
            if not name and not revisions and not card:
                raise JobError(
                    "collection_not_found",
                    "The Nexus collection or revision could not be loaded.",
                    {"slug": slug, "domain": domain, "revision": revision},
                )
            normalized_revisions = []
            for value in revisions:
                try:
                    number = int(value.get("revisionNumber") or 0)
                except (AttributeError, TypeError, ValueError):
                    number = 0
                if not number:
                    continue
                status = str(value.get("revisionStatus") or "")
                normalized_revisions.append({
                    "revision": number,
                    "status": status,
                    "published": status.casefold() == "published",
                })
            normalized_revisions.sort(
                key=lambda value: value["revision"], reverse=True)
            resolved_revision = revision
            if resolved_revision is None:
                published = [
                    value["revision"] for value in normalized_revisions
                    if value["published"]
                ]
                resolved_revision = max(published) if published else None
            supported = not bool(card.get("unsupported_collection_schema"))
            manifest = None
            if include_manifest:
                if not supported:
                    raise JobError(
                        "collection_schema_unsupported",
                        "This collection revision uses an unsupported schema.",
                        {"slug": slug, "domain": domain,
                         "revision": resolved_revision,
                         "schema_id": card.get("collection_schema_id")},
                    )
                if not download_link:
                    raise JobError(
                        "collection_manifest_unavailable",
                        "This collection revision has no downloadable manifest.",
                        {"slug": slug, "domain": domain,
                         "revision": resolved_revision},
                    )
                progress(1, 2, "fetch_collection_manifest")
                from Utils.collections.manifest import (
                    is_collection_manifest, load_collection_manifest,
                )
                manifest = load_collection_manifest(
                    api, game_name, slug, resolved_revision, download_link,
                    log_fn=log)
                if not is_collection_manifest(manifest):
                    raise JobError(
                        "collection_manifest_unavailable",
                        "The collection manifest could not be loaded.",
                        {"slug": slug, "domain": domain,
                         "revision": resolved_revision},
                    )
            progress(
                2 if include_manifest else 1,
                2 if include_manifest else 1,
                "fetch_collection_manifest" if include_manifest
                else "fetch_collection")
            safe_card = {
                key: value for key, value in card.items()
                if value is None or isinstance(value, (str, int, float, bool))
                or (key == "game_versions" and isinstance(value, list)
                    and all(isinstance(item, str) for item in value))
            }
            return {
                "domain": domain,
                "slug": slug,
                "revision": resolved_revision,
                "name": str(name or slug),
                "supported": supported,
                "total_size_bytes": int(total_size or 0),
                "mod_count": int(mod_count or 0),
                "mods": [_collection_mod(value) for value in mods],
                "revisions": normalized_revisions,
                "card": safe_card,
                "manifest_available": bool(download_link),
                "manifest": manifest,
                "url": f"https://next.nexusmods.com/{domain}/collections/{slug}",
                "rate_limits": _rate_limits(api),
            }

        return _call(operation)
    return run


def mod_runner(session: Any, domain: str, mod_id: int) -> Callable:
    def run(_log: Callable, progress: Callable) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            progress(0, 1, "fetch_mod")
            api = session.require_api()
            from .batch import cached_read
            mod = cached_read(("nexus.mod", domain, mod_id), lambda: api.get_mod(domain, mod_id))
            progress(1, 1, "fetch_mod")
            return {
                "domain": domain,
                "mod": _mod(mod),
                "rate_limits": _rate_limits(api),
            }

        return _call(operation)
    return run


def files_runner(session: Any, domain: str, mod_id: int) -> Callable:
    def run(_log: Callable, progress: Callable) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            progress(0, 1, "fetch_files")
            api = session.require_api()
            from .batch import cached_read
            listing = cached_read(("nexus.files", domain, mod_id), lambda: api.get_mod_files(domain, mod_id))
            files = [_file(item) for item in listing.files]
            updates = [
                _file_update(item) for item in listing.file_updates
                if isinstance(item, dict)
            ]
            progress(1, 1, "fetch_files")
            return {
                "domain": domain,
                "mod_id": mod_id,
                "files": files,
                "file_updates": updates,
                "counts": {"files": len(files), "updates": len(updates)},
                "rate_limits": _rate_limits(api),
            }

        return _call(operation)
    return run


def file_runner(
    session: Any,
    domain: str,
    mod_id: int,
    file_id: int,
) -> Callable:
    def run(_log: Callable, progress: Callable) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            progress(0, 1, "fetch_file")
            api = session.require_api()
            from .batch import cached_read
            file = cached_read(("nexus.file", domain, mod_id, file_id), lambda: api.get_file_info(domain, mod_id, file_id))
            progress(1, 1, "fetch_file")
            return {
                "domain": domain,
                "mod_id": mod_id,
                "file": _file(file),
                "rate_limits": _rate_limits(api),
            }

        return _call(operation)
    return run


def check_updates_runner(
    session: Any,
    game: Any,
    profile_dir: Path,
    game_domain: str,
    mods: list[str] | None,
    sources: tuple[str, ...],
    owner: str,
) -> Callable:
    def run(log: Callable, progress: Callable) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            from Utils.operations import acquire_game_operation

            with acquire_game_operation(
                game, "mods.check_updates", owner=owner,
            ):
                if not profile_dir.is_dir():
                    raise JobError(
                        "profile_not_found",
                        f"Profile '{profile_dir.name}' is no longer available.",
                        {"profile": profile_dir.name},
                    )
                if profile_dir.is_symlink():
                    raise JobError(
                        "unsafe_profile_path",
                        f"Profile '{profile_dir.name}' is a symbolic link and "
                        "cannot be modified.",
                        {"profile": profile_dir.name, "path": str(profile_dir)},
                    )

                from Utils.mods.modlist import read_modlist

                try:
                    entries = read_modlist(profile_dir / "modlist.txt")
                except OSError as exc:
                    raise JobError(
                        "resource_unavailable",
                        f"Could not read profile '{profile_dir.name}'.",
                        {"profile": profile_dir.name, "reason": str(exc)},
                    ) from exc
                available = [
                    entry.name for entry in entries if not entry.is_separator]
                if mods is None:
                    targets = list(dict.fromkeys(available))
                else:
                    missing = [name for name in mods if name not in available]
                    if missing:
                        casing = {
                            name: next(
                                (candidate for candidate in available
                                 if candidate.casefold() == name.casefold()),
                                None,
                            )
                            for name in missing
                        }
                        raise JobError(
                            "mod_not_found",
                            "One or more requested mods were not found in the "
                            "profile.",
                            {"mods": missing, "casing_matches": casing},
                        )
                    targets = list(mods)

                from Utils.profiles.state import profile_uses_specific_mods

                staging = (
                    profile_dir / "mods"
                    if profile_uses_specific_mods(profile_dir)
                    else Path(game.get_mod_staging_path())
                )
                target_set = set(targets)
                nexus_tracked: set[str] = set()
                thunderstore_tracked: set[str] = set()
                modio_tracked: set[str] = set()
                thunderstore_installed: list[Any] = []
                source_errors: list[dict[str, str]] = []

                if "nexus" in sources:
                    from Nexus.nexus_meta import scan_installed_mods

                    try:
                        installed = scan_installed_mods(staging)
                    except OSError as exc:
                        raise JobError(
                            "resource_unavailable",
                            "Could not scan installed Nexus mod metadata.",
                            {"path": str(staging), "reason": str(exc)},
                        ) from exc
                    nexus_tracked = {
                        meta.mod_name for meta in installed
                        if meta.mod_name in target_set
                    }

                if "thunderstore" in sources:
                    from Thunderstore.thunderstore_update_checker import (
                        scan_installed as scan_thunderstore,
                    )

                    try:
                        thunderstore_installed = scan_thunderstore(staging)
                    except Exception as exc:
                        source_errors.append({
                            "source": "thunderstore",
                            "message": str(exc),
                        })
                        log(f"[thunderstore] metadata scan failed: {exc}")
                    else:
                        thunderstore_tracked = {
                            folder.name for folder, _meta
                            in thunderstore_installed
                            if folder.name in target_set
                        }

                order = {name: index for index, name in enumerate(targets)}
                total_phases = len(sources)
                phase = 0
                update_rows: list[dict[str, Any]] = []
                missing_rows: list[dict[str, Any]] = []
                thunderstore_rows: list[dict[str, Any]] = []
                thunderstore_unknown: list[dict[str, Any]] = []
                modio_rows: list[dict[str, Any]] = []
                modio_unknown: list[dict[str, Any]] = []
                api = None

                if "nexus" in sources:
                    progress(phase, total_phases, "check_nexus_updates")
                    if nexus_tracked:
                        try:
                            api = session.require_api()
                            from Nexus.nexus_update_checker import check_for_updates

                            updates, missing = check_for_updates(
                                api,
                                staging,
                                game_domain=game_domain,
                                progress_cb=lambda message: log(
                                    f"[nexus] {message}"),
                                save_results=True,
                                enabled_only=nexus_tracked,
                                installed_names=set(available),
                            )
                        except Exception as exc:
                            if len(sources) == 1:
                                raise
                            source_errors.append({
                                "source": "nexus",
                                "message": "Nexus update check failed.",
                            })
                            log(f"[nexus] update check failed: {exc}")
                        else:
                            updates.sort(key=lambda item: (
                                order.get(item.mod_name, len(order)),
                                item.mod_name,
                            ))
                            update_rows = [
                                _available_update(item) for item in updates]
                            from .mod_requirements import read_requirements
                            requirements = read_requirements(game, profile_dir)
                            missing_rows = requirements.missing_report(
                                nexus_tracked, [_missing_requirements(item) for item in missing])
                            try:
                                requirements.index.persist(
                                    requirements.staging, requirements.index.take_writes())
                            except OSError as exc:
                                log(f"[nexus] could not save current requirement state: {exc}")
                    phase += 1
                    progress(phase, total_phases, "check_nexus_updates")

                if "thunderstore" in sources:
                    progress(phase, total_phases,
                             "check_thunderstore_updates")
                    if thunderstore_tracked:
                        try:
                            from Thunderstore.thunderstore_update_checker import (
                                check_for_updates as check_thunderstore_updates,
                            )
                            results = check_thunderstore_updates(
                                staging,
                                only_names=thunderstore_tracked,
                                save_results=True,
                                progress_cb=lambda message: log(
                                    f"[thunderstore] {message}"),
                                installed_mods=thunderstore_installed,
                            )
                        except Exception as exc:
                            source_errors.append({
                                "source": "thunderstore",
                                "message": str(exc),
                            })
                            log(f"[thunderstore] update check failed: {exc}")
                        else:
                            results.sort(key=lambda item: (
                                order.get(item.mod_name, len(order)),
                                item.mod_name,
                            ))
                            rows = [
                                _thunderstore_update(item) for item in results]
                            thunderstore_rows = [
                                item for item in rows if not item["unknown"]]
                            thunderstore_unknown = [
                                item for item in rows if item["unknown"]]
                    phase += 1
                    progress(phase, total_phases,
                             "check_thunderstore_updates")

                if "modio" in sources:
                    progress(phase, total_phases, "check_modio_updates")
                    try:
                        from Utils.bg3.modio_loader import load_modio_module

                        api_key, api_path = load_modio_module(
                            "modio_key").load_modio_credentials()
                        if not api_key or not api_path:
                            raise JobError(
                                "modio_credentials_missing",
                                "The saved mod.io API key and path are required.",
                            )
                        checker = load_modio_module("modio_update_checker")
                        check_errors: list[str] = []
                        results = checker.check_for_updates(
                            staging, api_key, api_path,
                            progress_cb=lambda message: log(
                                f"[modio] {message}"),
                            only_names=target_set,
                            tracked_names=modio_tracked,
                            errors=check_errors,
                        )
                        source_errors.extend({
                            "source": "modio", "message": message,
                        } for message in check_errors)
                    except Exception as exc:
                        source_errors.append({
                            "source": "modio",
                            "message": (
                                str(exc) if isinstance(exc, JobError)
                                else "mod.io update check failed."
                            ),
                        })
                        log(f"[modio] update check failed: {exc}")
                    else:
                        results.sort(key=lambda item: (
                            order.get(item.mod_name, len(order)),
                            item.mod_name,
                        ))
                        rows = [_modio_update(item) for item in results]
                        modio_rows = [
                            item for item in rows if not item["unknown"]]
                        modio_unknown = [
                            item for item in rows if item["unknown"]]
                    phase += 1
                    progress(phase, total_phases, "check_modio_updates")

                plugin_results = {"items": [], "errors": []}
                if "plugins" in sources:
                    from amethyst_plugins.repositories import check_updates
                    plugin_results = check_updates(game, profile_dir, set(targets), log)
                source_errors.extend({"source": "plugins", **error} for error in plugin_results["errors"])
                tracked = nexus_tracked | thunderstore_tracked | modio_tracked | {row["mod"] for row in plugin_results["items"]}
                skipped_reason = (
                    "no_supported_metadata" if len(sources) > 1
                    else f"not_{sources[0]}"
                )
                skipped = [
                    {"mod": name, "reason": skipped_reason}
                    for name in targets if name not in tracked
                ]
                return {
                    "ok": True,
                    "profile": profile_dir.name,
                    "source": sources[0],
                    "sources": list(sources),
                    "selection": {
                        "requested": list(mods) if mods is not None else None,
                        "targets": targets,
                    },
                    "counts": {
                        "targeted": len(targets),
                        "nexus_tracked": len(nexus_tracked),
                        "thunderstore_tracked": len(thunderstore_tracked),
                        "modio_tracked": len(modio_tracked),
                        "updates": len(update_rows),
                        "thunderstore_updates": len(thunderstore_rows),
                        "thunderstore_unknown": len(thunderstore_unknown),
                        "modio_updates": len(modio_rows),
                        "plugin_updates": sum(bool(row["has_update"]) for row in plugin_results["items"]),
                        "modio_unknown": len(modio_unknown),
                        "missing_requirements": len(missing_rows),
                        "missing_dependencies": sum(
                            len(item["missing"]) for item in missing_rows),
                        "skipped": len(skipped),
                        "source_errors": len(source_errors),
                    },
                    "updates": update_rows,
                    "missing_requirements": missing_rows,
                    "thunderstore_updates": thunderstore_rows,
                    "thunderstore_unknown": thunderstore_unknown,
                    "modio_updates": modio_rows,
                    "plugin_updates": plugin_results["items"],
                    "modio_unknown": modio_unknown,
                    "skipped": skipped,
                    "source_errors": source_errors,
                    "rate_limits": _rate_limits(api) if api is not None else None,
                }

        return _call(operation)
    return run


def download_runner(
    session: Any,
    game_name: str,
    domain: str,
    mod_id: int,
    file_id: int,
    *,
    nxm_link: Any = None,
    cancel: Any = None,
) -> Callable:
    def run(_log: Callable, progress: Callable) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            if cancel is not None and cancel.is_set():
                raise JobCancelled("Download cancelled.")

            api = session.require_api()
            needs_premium = nxm_link is None or not nxm_link.key

            file_info = None
            try:
                file_info = api.get_file_info(domain, mod_id, file_id)
            except Exception:
                pass
            known_name = str(getattr(file_info, "file_name", "") or "")
            expected_size = (
                int(getattr(file_info, "size_in_bytes", 0) or 0)
                or int(getattr(file_info, "size_kb", 0) or 0) * 1024)

            from Nexus.nexus_download import NexusDownloader
            from Utils.config_paths import get_download_cache_dir_for_game

            if (not game_name or game_name in (".", "..")
                    or "/" in game_name or "\\" in game_name
                    or any(char in game_name for char in "\x00\r\n")):
                raise JobError(
                    "nexus_download_unsafe_path",
                    "The game's cache folder name is unsafe.",
                )
            destination = get_download_cache_dir_for_game(game_name)
            downloader = NexusDownloader(api, download_dir=destination)
            progress(0, expected_size, "download")
            try:
                if nxm_link is None:
                    result = downloader.download_file(
                        domain,
                        mod_id,
                        file_id,
                        dest_dir=destination,
                        known_file_name=known_name,
                        expected_size_bytes=expected_size,
                        progress_cb=lambda done, total: progress(
                            done, total, "download"),
                        cancel=cancel,
                    )
                    mode = "premium"
                else:
                    result = downloader.download_from_nxm(
                        nxm_link,
                        dest_dir=destination,
                        known_file_name=known_name,
                        expected_size_bytes=expected_size,
                        progress_cb=lambda done, total: progress(
                            done, total, "download"),
                        cancel=cancel,
                    )
                    mode = "nxm"
            finally:
                downloader.close_worker_session()

            if (not result.success
                    and ((cancel is not None and cancel.is_set())
                         or "cancel" in str(result.error or "").lower())):
                raise JobCancelled("Download cancelled.")
            if not result.success or result.file_path is None:
                if (needs_premium
                        and not bool(api.validate().is_premium)):
                    raise JobError(
                        "nexus_premium_required",
                        "Direct Nexus downloads require Premium; supply an "
                        "NXM link from 'Download with Manager' instead.",
                    )
                raise JobError(
                    "nexus_download_failed",
                    "Nexus Mods could not download the requested file.",
                )

            path = result.file_path.absolute()
            try:
                inside_cache = (
                    path.resolve().parent == destination.resolve()
                    and not path.is_symlink())
            except (OSError, RuntimeError):
                inside_cache = False
            if not inside_cache or not path.is_file():
                raise JobError(
                    "nexus_download_unsafe_path",
                    "The downloaded archive was not inside the managed cache.",
                )

            size = path.stat().st_size
            progress(size, size, "download")
            return {
                "ok": True,
                "source": {
                    "provider": "nexus",
                    "mode": mode,
                    "domain": domain,
                    "mod_id": mod_id,
                    "file_id": file_id,
                },
                "archive": {
                    "name": path.name,
                    "path": str(path),
                    "size_bytes": size,
                    "location_kind": "cache",
                    "from_cache": bool(result.from_cache),
                },
                "file": _file(file_info) if file_info is not None else None,
                "rate_limits": _rate_limits(api),
            }

        return _call(operation)
    return run
