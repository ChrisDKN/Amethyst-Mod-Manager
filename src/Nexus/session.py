from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _token_expiry(tokens: Any) -> str | None:
    try:
        value = float(tokens.expires_at)
        return datetime.fromtimestamp(value, timezone.utc).isoformat().replace(
            "+00:00", "Z")
    except (AttributeError, OSError, OverflowError, TypeError, ValueError):
        return None


def _saved_logout_is_authoritative() -> bool:
    try:
        from Utils.config_paths import get_config_dir
        storage = get_config_dir() / "nexus_oauth_storage_v2.bin"
        if not storage.is_file():
            return False
        state = json.loads(storage.read_text(encoding="utf-8"))
        return (isinstance(state, dict)
                and state.get("version") == 2
                and state.get("mode") == "cleared")
    except (OSError, TypeError, ValueError):
        return False


def load_saved_nexus_api() -> dict[str, Any]:
    result = {"api": None, "error": "", "revoked": False}
    if _saved_logout_is_authoritative():
        return result

    from Nexus.nexus_api import NexusAPI
    from Nexus.nexus_oauth import (
        OAuthRefreshError, clear_oauth_tokens, load_oauth_tokens)

    tokens = load_oauth_tokens()
    if tokens is None:
        return result
    try:
        result["api"] = NexusAPI.from_oauth(tokens)
    except OAuthRefreshError as exc:
        result["error"] = str(exc)
        result["revoked"] = bool(exc.token_revoked)
        if exc.token_revoked:
            try:
                clear_oauth_tokens()
            except Exception as clear_exc:
                result["error"] += f"; could not clear login: {clear_exc}"
    except Exception as exc:
        result["error"] = str(exc)
    return result


class NexusSessionError(RuntimeError):
    def __init__(self, name: str, message: str,
                 details: dict[str, Any] | None = None):
        super().__init__(message)
        self.name = name
        self.details = details or {}


class NexusSession:
    def __init__(self):
        self._lock = threading.RLock()
        self._api: Any = None

    def status(self) -> dict[str, Any]:
        with self._lock:
            tokens = getattr(self._api, "_oauth_tokens", None)
        try:
            if tokens is None and not _saved_logout_is_authoritative():
                from Nexus.nexus_oauth import load_oauth_tokens
                tokens = load_oauth_tokens()
        except Exception as exc:
            return {
                "authenticated": False,
                "credential_type": None,
                "access_token_expires_at": None,
                "access_token_expired": None,
                "refresh_required": None,
                "error": str(exc) or type(exc).__name__,
            }

        expires_at = _token_expiry(tokens) if tokens is not None else None
        try:
            expires_value = float(tokens.expires_at)
        except (AttributeError, TypeError, ValueError):
            expires_value = 0.0
        return {
            "authenticated": tokens is not None,
            "credential_type": "oauth" if tokens is not None else None,
            "access_token_expires_at": expires_at,
            "access_token_expired": (
                time.time() >= expires_value if tokens is not None else None),
            "refresh_required": (
                time.time() >= expires_value - 300
                if tokens is not None else None),
            "error": None,
        }

    def require_api(self) -> Any:
        with self._lock:
            if self._api is not None:
                return self._api
            result = load_saved_nexus_api()
            if result["api"] is not None:
                self._api = result["api"]
                return self._api
            if result["revoked"]:
                raise NexusSessionError(
                    "nexus_auth_expired",
                    "The saved Nexus Mods login expired; log in again.",
                )
            if result["error"]:
                raise NexusSessionError(
                    "nexus_auth_unavailable",
                    "The saved Nexus Mods login could not be loaded.",
                    {"reason": result["error"]},
                )
            raise NexusSessionError(
                "nexus_not_authenticated",
                "Log in to Nexus Mods before using this method.",
            )

    def adopt_oauth_tokens(self, tokens: Any) -> None:
        from Nexus.nexus_api import NexusAPI

        api = NexusAPI.from_oauth(tokens)
        with self._lock:
            previous, self._api = self._api, api
        self._close_api(previous)

    def clear_credentials(self) -> None:
        errors: list[str] = []
        try:
            from Nexus.nexus_api import clear_api_key
            clear_api_key()
        except Exception as exc:
            errors.append(str(exc) or type(exc).__name__)
        try:
            from Nexus.nexus_oauth import clear_oauth_tokens
            clear_oauth_tokens()
        except Exception as exc:
            errors.append(str(exc) or type(exc).__name__)

        with self._lock:
            api, self._api = self._api, None
        self._close_api(api)
        if errors:
            raise NexusSessionError(
                "nexus_logout_failed",
                "Nexus Mods credentials could not be completely cleared.",
                {"reasons": errors},
            )

    def close(self) -> None:
        with self._lock:
            api, self._api = self._api, None
        self._close_api(api)

    @staticmethod
    def _close_api(api: Any) -> None:
        session = getattr(api, "_session", None)
        close = getattr(session, "close", None)
        if callable(close):
            close()


class NexusLoginManager:
    _ACTIVE_STATES = {"starting", "waiting_for_user", "exchanging"}

    def __init__(
        self,
        session: NexusSession,
        client_factory: Callable[..., Any] | None = None,
    ):
        self._session = session
        self._client_factory = client_factory
        self._lock = threading.RLock()
        self._ready = threading.Event()
        self._client: Any = None
        self._state = "idle"
        self._authorize_url: str | None = None
        self._message: str | None = None
        self._error: str | None = None
        self._started_at: str | None = None
        self._finished_at: str | None = None

    def status(self) -> dict[str, Any]:
        with self._lock:
            return self._snapshot_locked()

    def start(self, *, open_browser: bool = False) -> dict[str, Any]:
        with self._lock:
            if self._state in self._ACTIVE_STATES:
                raise NexusSessionError(
                    "nexus_login_in_progress",
                    "A Nexus Mods login is already in progress.",
                )
            self._state = "starting"
            self._authorize_url = None
            self._message = "Starting Nexus Mods login."
            self._error = None
            self._started_at = _utc_now()
            self._finished_at = None
            self._ready.clear()

        try:
            factory = self._client_factory
            if factory is None:
                from Nexus.nexus_oauth import NexusOAuthClient
                factory = NexusOAuthClient
            client = factory(
                on_token=self._on_token,
                on_error=self._on_error,
                on_status=self._on_status,
                on_authorize=self._on_authorize,
                open_browser=open_browser,
            )
            with self._lock:
                self._client = client
            client.start()
        except Exception as exc:
            self._on_error(str(exc) or type(exc).__name__)
            raise NexusSessionError(
                "nexus_login_unavailable",
                "The Nexus Mods login flow could not be started.",
                {"reason": str(exc) or type(exc).__name__},
            ) from exc

        self._ready.wait(2.0)
        return self.status()

    def submit_manual_code(self, code: str) -> tuple[bool, str]:
        with self._lock:
            client = self._client
            active = self._state in self._ACTIVE_STATES
        if client is None or not active:
            return False, "No Nexus Mods login is waiting for a code."
        return client.submit_manual_code(code)

    def cancel(self) -> dict[str, Any]:
        with self._lock:
            client = self._client
            active = self._state in self._ACTIVE_STATES
            if active:
                self._state = "cancelled"
                self._authorize_url = None
                self._message = "Nexus Mods login cancelled."
                self._error = None
                self._finished_at = _utc_now()
                self._ready.set()
        if client is not None and active:
            client.cancel()
        return self.status()

    def close(self) -> None:
        self.cancel()

    def _on_authorize(self, url: str) -> None:
        with self._lock:
            if self._state not in self._ACTIVE_STATES:
                return
            self._authorize_url = url
            self._state = "waiting_for_user"
            self._message = "Open the authorisation URL and approve Amethyst."
            self._ready.set()

    def _on_status(self, message: str) -> None:
        with self._lock:
            if self._state not in self._ACTIVE_STATES:
                return
            self._message = str(message)
            if "Exchanging" in self._message:
                self._state = "exchanging"

    def _on_error(self, message: str) -> None:
        with self._lock:
            if self._state == "cancelled":
                return
            self._state = "failed"
            self._authorize_url = None
            self._message = "Nexus Mods login failed."
            self._error = str(message)
            self._finished_at = _utc_now()
            self._ready.set()

    def _on_token(self, tokens: Any) -> None:
        with self._lock:
            if self._state == "cancelled":
                return
        try:
            self._session.adopt_oauth_tokens(tokens)
        except Exception as exc:
            self._on_error(str(exc) or type(exc).__name__)
            return
        with self._lock:
            if self._state == "cancelled":
                return
            self._state = "succeeded"
            self._authorize_url = None
            self._message = "Logged in to Nexus Mods."
            self._error = None
            self._finished_at = _utc_now()
            self._ready.set()

    def _snapshot_locked(self) -> dict[str, Any]:
        return {
            "state": self._state,
            "running": self._state in self._ACTIVE_STATES,
            "authorize_url": self._authorize_url,
            "message": self._message,
            "error": self._error,
            "started_at": self._started_at,
            "finished_at": self._finished_at,
        }
