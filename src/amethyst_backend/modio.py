from __future__ import annotations

from typing import Any, Callable

from Utils.bg3.modio_loader import load_modio_module

from .jobs import JobError


def credential_status() -> dict[str, Any]:
    key, api_path = load_modio_module("modio_key").load_modio_credentials()
    path_valid = False
    if api_path:
        try:
            load_modio_module("modio_api").normalize_api_path(api_path)
            path_valid = True
        except ValueError:
            pass
    return {
        "configured": bool(key and path_valid),
        "api_key_saved": bool(key),
        "api_path": api_path or None,
        "api_path_missing": bool(key and not api_path),
        "api_path_invalid": bool(key and api_path and not path_valid),
    }


def save_credentials_runner(
    api_key: str,
    api_path: str,
) -> Callable[[Callable[..., None], Callable[..., None]], dict[str, Any]]:
    def run(_log: Callable[..., None], progress: Callable[..., None]) -> dict[str, Any]:
        progress(0, 2, "verify_modio_credentials")
        try:
            api = load_modio_module("modio_api").ModioAPI(api_key, api_path)
            try:
                verified = api.test_key()
            finally:
                api._session.close()
        except Exception as exc:
            raise JobError(
                "modio_verification_failed",
                "Could not verify the mod.io API key and path.",
                {"exception": type(exc).__name__},
            ) from exc
        if not verified:
            raise JobError(
                "modio_verification_failed",
                "Could not verify the mod.io API key and path.",
            )
        progress(1, 2, "save_modio_credentials")
        try:
            key_store = load_modio_module("modio_key")
            key_store.save_modio_credentials(api_key, api_path)
            saved_key, saved_path = key_store.load_modio_credentials()
        except Exception as exc:
            raise JobError(
                "modio_save_failed",
                "Could not save the mod.io credentials.",
                {"exception": type(exc).__name__},
            ) from exc
        if (saved_key, saved_path) != (api_key, api_path):
            raise JobError(
                "modio_save_failed",
                "The saved mod.io credentials could not be verified.",
            )
        progress(2, 2, "save_modio_credentials")
        return {
            "configured": True,
            "api_path": api_path,
        }

    return run


def clear_credentials_runner(
) -> Callable[[Callable[..., None], Callable[..., None]], dict[str, Any]]:
    def run(_log: Callable[..., None], progress: Callable[..., None]) -> dict[str, Any]:
        progress(0, 1, "clear_modio_credentials")
        try:
            key_store = load_modio_module("modio_key")
            key_store.clear_modio_key()
            key, api_path = key_store.load_modio_credentials()
        except Exception as exc:
            raise JobError(
                "modio_clear_failed",
                "Could not clear the saved mod.io credentials.",
                {"exception": type(exc).__name__},
            ) from exc
        if key or api_path:
            raise JobError(
                "modio_clear_failed",
                "The saved mod.io credentials are still present.",
            )
        progress(1, 1, "clear_modio_credentials")
        return {"configured": False, "api_path": None}

    return run
