from __future__ import annotations

from datetime import datetime, timezone
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


def mod_runner(session: Any, domain: str, mod_id: int) -> Callable:
    def run(_log: Callable, progress: Callable) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            progress(0, 1, "fetch_mod")
            api = session.require_api()
            mod = api.get_mod(domain, mod_id)
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
            listing = api.get_mod_files(domain, mod_id)
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
            file = api.get_file_info(domain, mod_id, file_id)
            progress(1, 1, "fetch_file")
            return {
                "domain": domain,
                "mod_id": mod_id,
                "file": _file(file),
                "rate_limits": _rate_limits(api),
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
