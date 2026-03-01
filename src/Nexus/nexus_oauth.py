"""
nexus_oauth.py
Nexus Mods OAuth 2.0 + PKCE authentication flow for desktop apps.

Flow
----
1. Generate a random PKCE code_verifier + SHA-256 code_challenge.
2. Open the user's browser to the Nexus authorisation URL.
3. Spin up a temporary HTTP server on localhost:7890 to receive the redirect.
4. Exchange the auth code for access + refresh tokens via POST /oauth/token.
5. Store tokens in the system keyring; schedule background refresh.

Endpoints (from https://users.nexusmods.com/.well-known/openid-configuration)
    authorization_endpoint : https://users.nexusmods.com/oauth/authorize
    token_endpoint         : https://users.nexusmods.com/oauth/token
    revocation_endpoint    : https://users.nexusmods.com/oauth/revoke

Public API
----------
    NexusOAuthClient(on_token, on_error, on_status, client_id)
        .start()    — begin the flow (non-blocking, background thread)
        .cancel()   — abort
        .is_running — True while waiting for the browser callback

Token persistence (separate from the legacy API key):
    load_oauth_tokens()  → OAuthTokens | None
    save_oauth_tokens(t) → None
    clear_oauth_tokens() → None

    refresh_if_needed(t) → OAuthTokens   (refreshes if <5 min left)
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import secrets
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass
from typing import Callable, Optional

import keyring
import requests

from Utils.app_log import app_log
from version import __version__

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_AUTHORIZE_URL = "https://users.nexusmods.com/oauth/authorize"
_TOKEN_URL     = "https://users.nexusmods.com/oauth/token"
_REVOKE_URL    = "https://users.nexusmods.com/oauth/revoke"

_CALLBACK_PORT = 7890
_CALLBACK_PATH = "/callback"
_REDIRECT_URI  = f"http://localhost:{_CALLBACK_PORT}{_CALLBACK_PATH}"

# Filled in once Nexus Mods issues credentials.  Leave empty until then;
# the login button is disabled when CLIENT_ID is empty.
CLIENT_ID: str = ""

_SCOPES = "openid public"

_KEYRING_SERVICE      = "AmethystModManager"
_KEYRING_ACCESS_KEY   = "nexus_oauth_access_token"
_KEYRING_REFRESH_KEY  = "nexus_oauth_refresh_token"
_KEYRING_EXPIRES_KEY  = "nexus_oauth_expires_at"   # stored as str(float)

# Refresh when fewer than 5 minutes remain
_REFRESH_MARGIN_SECS = 300

APP_VERSION = __version__


# ---------------------------------------------------------------------------
# Token data class
# ---------------------------------------------------------------------------

@dataclass
class OAuthTokens:
    access_token:  str
    refresh_token: str
    expires_at:    float   # Unix timestamp


# ---------------------------------------------------------------------------
# Keyring persistence
# ---------------------------------------------------------------------------

def load_oauth_tokens() -> Optional[OAuthTokens]:
    """Load OAuth tokens from the system keyring. Returns None if absent."""
    try:
        access  = keyring.get_password(_KEYRING_SERVICE, _KEYRING_ACCESS_KEY)
        refresh = keyring.get_password(_KEYRING_SERVICE, _KEYRING_REFRESH_KEY)
        exp_str = keyring.get_password(_KEYRING_SERVICE, _KEYRING_EXPIRES_KEY)
        if not access or not refresh or not exp_str:
            return None
        return OAuthTokens(
            access_token=access,
            refresh_token=refresh,
            expires_at=float(exp_str),
        )
    except Exception as exc:
        app_log(f"OAuth: failed to load tokens from keyring: {exc}")
        return None


def save_oauth_tokens(tokens: OAuthTokens) -> None:
    """Persist OAuth tokens to the system keyring."""
    try:
        keyring.set_password(_KEYRING_SERVICE, _KEYRING_ACCESS_KEY,  tokens.access_token)
        keyring.set_password(_KEYRING_SERVICE, _KEYRING_REFRESH_KEY, tokens.refresh_token)
        keyring.set_password(_KEYRING_SERVICE, _KEYRING_EXPIRES_KEY, str(tokens.expires_at))
    except Exception as exc:
        app_log(f"OAuth: failed to save tokens to keyring: {exc}")
        raise RuntimeError(f"Cannot save OAuth tokens: {exc}") from exc


def clear_oauth_tokens() -> None:
    """Delete all stored OAuth tokens from the keyring."""
    for key in (_KEYRING_ACCESS_KEY, _KEYRING_REFRESH_KEY, _KEYRING_EXPIRES_KEY):
        try:
            keyring.delete_password(_KEYRING_SERVICE, key)
        except keyring.errors.PasswordDeleteError:
            pass
        except Exception as exc:
            app_log(f"OAuth: failed to clear token '{key}': {exc}")


# ---------------------------------------------------------------------------
# Token refresh
# ---------------------------------------------------------------------------

def refresh_if_needed(tokens: OAuthTokens, client_id: str = CLIENT_ID) -> OAuthTokens:
    """
    Return tokens unchanged if still valid, or perform a refresh token exchange.

    Raises RuntimeError on refresh failure.
    """
    if time.time() < tokens.expires_at - _REFRESH_MARGIN_SECS:
        return tokens

    app_log("OAuth: access token expiring soon, refreshing...")
    try:
        resp = requests.post(
            _TOKEN_URL,
            data={
                "grant_type":    "refresh_token",
                "refresh_token": tokens.refresh_token,
                "client_id":     client_id,
                "redirect_uri":  _REDIRECT_URI,
            },
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        raise RuntimeError(f"OAuth token refresh failed: {exc}") from exc

    new_tokens = OAuthTokens(
        access_token=data["access_token"],
        refresh_token=data.get("refresh_token", tokens.refresh_token),
        expires_at=time.time() + data.get("expires_in", 3600),
    )
    save_oauth_tokens(new_tokens)
    app_log("OAuth: token refreshed successfully")
    return new_tokens


# ---------------------------------------------------------------------------
# PKCE helpers
# ---------------------------------------------------------------------------

def _pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for S256 PKCE."""
    verifier  = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    digest    = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


# ---------------------------------------------------------------------------
# Local callback HTTP server
# ---------------------------------------------------------------------------

class _CallbackServer:
    """
    Minimal single-request HTTP server that captures the OAuth redirect.

    Usage::

        srv = _CallbackServer()
        srv.start()
        # ... open browser ...
        code, state = srv.wait(timeout=300)
        srv.stop()
    """

    _HTML_SUCCESS = (
        "<html><body style='font-family:sans-serif;text-align:center;margin-top:60px'>"
        "<h2>Authorised!</h2>"
        "<p>You can close this tab and return to Amethyst Mod Manager.</p>"
        "</body></html>"
    )
    _HTML_ERROR = (
        "<html><body style='font-family:sans-serif;text-align:center;margin-top:60px'>"
        "<h2>Authorisation failed</h2>"
        "<p>Return to the app for details.</p>"
        "</body></html>"
    )

    def __init__(self):
        self._code:  Optional[str] = None
        self._state: Optional[str] = None
        self._error: Optional[str] = None
        self._event = threading.Event()
        self._server: Optional[http.server.HTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        parent = self

        class _Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_):  # silence default access log
                pass

            def do_GET(self):
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path != _CALLBACK_PATH:
                    self.send_response(404)
                    self.end_headers()
                    return

                params = dict(urllib.parse.parse_qsl(parsed.query))
                if "code" in params:
                    parent._code  = params["code"]
                    parent._state = params.get("state")
                    body = parent._HTML_SUCCESS.encode()
                else:
                    parent._error = params.get("error", "unknown")
                    body = parent._HTML_ERROR.encode()

                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                parent._event.set()

        self._server = http.server.HTTPServer(("127.0.0.1", _CALLBACK_PORT), _Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True, name="oauth-callback"
        )
        self._thread.start()

    def wait(self, timeout: float = 300) -> tuple[Optional[str], Optional[str]]:
        """Block until the callback is received or timeout. Returns (code, state)."""
        self._event.wait(timeout)
        return self._code, self._state

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server = None


# ---------------------------------------------------------------------------
# Main OAuth client
# ---------------------------------------------------------------------------

class NexusOAuthClient:
    """
    Background OAuth 2.0 + PKCE client for Nexus Mods.

    Parameters
    ----------
    on_token : callable(OAuthTokens)
        Called (from background thread) when tokens are obtained.
    on_error : callable(str)
        Called on unrecoverable error.
    on_status : callable(str), optional
        Called with human-readable status updates for the UI.
    client_id : str, optional
        OAuth client ID. Defaults to the module-level CLIENT_ID.
    """

    def __init__(
        self,
        on_token:  Callable[[OAuthTokens], None],
        on_error:  Callable[[str], None],
        on_status: Optional[Callable[[str], None]] = None,
        client_id: str = CLIENT_ID,
    ):
        self._on_token  = on_token
        self._on_error  = on_error
        self._on_status = on_status or (lambda _: None)
        self._client_id = client_id

        self._cancelled = False
        self._thread:   Optional[threading.Thread] = None
        self._srv:      Optional[_CallbackServer]  = None

    # -- public API ---------------------------------------------------------

    def start(self) -> None:
        """Begin the OAuth flow in a background thread."""
        self._cancelled = False
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="nexus-oauth"
        )
        self._thread.start()

    def cancel(self) -> None:
        """Abort the flow."""
        self._cancelled = True
        if self._srv:
            self._srv.stop()

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # -- internals ----------------------------------------------------------

    def _run(self) -> None:
        verifier, challenge = _pkce_pair()
        state = secrets.token_urlsafe(16)

        # 1. Start callback server
        self._srv = _CallbackServer()
        try:
            self._srv.start()
        except OSError as exc:
            self._on_error(f"Cannot start callback server on port {_CALLBACK_PORT}: {exc}")
            return

        # 2. Build auth URL and open browser
        params = urllib.parse.urlencode({
            "response_type":         "code",
            "client_id":             self._client_id,
            "redirect_uri":          _REDIRECT_URI,
            "scope":                 _SCOPES,
            "state":                 state,
            "code_challenge":        challenge,
            "code_challenge_method": "S256",
        })
        auth_url = f"{_AUTHORIZE_URL}?{params}"
        self._on_status("Opening browser — please authorise in Nexus Mods...")
        app_log(f"OAuth: opening auth URL")
        webbrowser.open(auth_url)

        # 3. Wait for callback (5-minute timeout)
        self._on_status("Waiting for browser authorisation...")
        code, returned_state = self._srv.wait(timeout=300)
        self._srv.stop()
        self._srv = None

        if self._cancelled:
            return

        if code is None:
            self._on_error("Authorisation cancelled or timed out.")
            return

        if returned_state != state:
            self._on_error("OAuth state mismatch — possible CSRF attack, aborting.")
            return

        # 4. Exchange code for tokens
        self._on_status("Exchanging authorisation code for tokens...")
        try:
            resp = requests.post(
                _TOKEN_URL,
                data={
                    "grant_type":    "authorization_code",
                    "client_id":     self._client_id,
                    "redirect_uri":  _REDIRECT_URI,
                    "code":          code,
                    "code_verifier": verifier,
                },
                timeout=20,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            self._on_error(f"Token exchange failed: {exc}")
            return

        if "access_token" not in data:
            self._on_error(f"No access_token in response: {data.get('error', 'unknown')}")
            return

        tokens = OAuthTokens(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token", ""),
            expires_at=time.time() + data.get("expires_in", 3600),
        )

        # 5. Persist and notify
        try:
            save_oauth_tokens(tokens)
        except Exception as exc:
            self._on_error(f"Failed to save tokens: {exc}")
            return

        app_log("OAuth: tokens obtained and saved successfully")
        self._on_status("Logged in!")
        self._on_token(tokens)
