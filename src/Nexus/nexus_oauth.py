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
        .start()    - begin the flow (non-blocking, background thread)
        .cancel()   - abort
        .is_running - True while waiting for the browser callback

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
from Utils.xdg import open_url
from dataclasses import dataclass
from typing import Callable, Optional

import keyring
import requests

from Utils.app_log import app_log
from Utils.ca_bundle import resolve_ca_bundle
from Utils.config_paths import get_config_dir
from version import __version__
from Nexus.nexus_api import _KEYRING_SERVICE

# ---------------------------------------------------------------------------
# Keyring availability check - fall back to file storage when DBus is slow
# or the keyring service is missing (common after SteamOS updates).
# ---------------------------------------------------------------------------
_keyring_available: bool = False

def _probe_keyring() -> bool:
    """Return True if the system keyring is usable."""
    try:
        import subprocess
        # Check if the DBus Secret Service is reachable.
        # org.freedesktop.secrets is the standard interface; if nothing owns it, keyring won't work.
        result = subprocess.run(
            ["dbus-send", "--session", "--print-reply", "--dest=org.freedesktop.DBus",
             "/org/freedesktop/DBus", "org.freedesktop.DBus.NameHasOwner",
             "string:org.freedesktop.secrets"],
            capture_output=True, text=True, timeout=3,
        )
        if "boolean true" not in result.stdout:
            return False
        # Service exists on DBus - verify we can actually list collections
        # (catches broken secretstorage / assertion errors).
        import secretstorage
        conn = secretstorage.dbus_init()
        list(secretstorage.get_all_collections(conn))
        conn.close()
        return True
    except Exception:
        return False

def _check_keyring() -> None:
    """Probe keyring availability; set _keyring_available accordingly."""
    global _keyring_available
    if _probe_keyring():
        _keyring_available = True
        app_log("OAuth: keyring backend available")
    else:
        _keyring_available = False
        app_log("OAuth: no keyring backend available - using file-based token storage")

_check_keyring()

# ---------------------------------------------------------------------------
# Encrypted file-based fallback token storage
# ---------------------------------------------------------------------------
_TOKEN_FILE = "nexus_oauth_tokens.bin"

def _derive_key() -> bytes:
    """Derive a Fernet key from the machine ID so tokens are only usable on this device."""
    import base64
    machine_id = ""
    for p in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            with open(p) as f:
                machine_id = f.read().strip()
            if machine_id:
                break
        except OSError:
            continue
    if not machine_id:
        machine_id = "fallback-no-machine-id"
    dk = hashlib.pbkdf2_hmac("sha256", machine_id.encode(), b"AmethystModManager", 100_000)
    return base64.urlsafe_b64encode(dk)

def _token_file_path() -> os.PathLike:
    return get_config_dir() / _TOKEN_FILE

def _load_tokens_file() -> Optional[OAuthTokens]:
    p = _token_file_path()
    try:
        if not os.path.isfile(p):
            return None
        from cryptography.fernet import Fernet
        cipher = Fernet(_derive_key())
        with open(p, "rb") as f:
            data = json.loads(cipher.decrypt(f.read()))
        access = data.get("access_token", "")
        refresh = data.get("refresh_token", "")
        expires = data.get("expires_at", 0.0)
        if not access or not refresh:
            return None
        return OAuthTokens(access_token=access, refresh_token=refresh, expires_at=float(expires))
    except Exception as exc:
        app_log(f"OAuth: failed to load tokens from file: {exc}")
        return None

def _save_tokens_file(tokens: OAuthTokens) -> None:
    p = _token_file_path()
    try:
        from cryptography.fernet import Fernet
        cipher = Fernet(_derive_key())
        payload = json.dumps({
            "access_token": tokens.access_token,
            "refresh_token": tokens.refresh_token,
            "expires_at": tokens.expires_at,
        }).encode()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        # Create owner-only from the start - no chmod window with looser perms.
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.fchmod(fd, 0o600)  # tighten a pre-existing file too
            with os.fdopen(fd, "wb") as f:
                fd = -1
                f.write(cipher.encrypt(payload))
        finally:
            if fd != -1:
                os.close(fd)
    except Exception as exc:
        app_log(f"OAuth: failed to save tokens to file: {exc}")
        raise RuntimeError(f"Cannot save OAuth tokens: {exc}") from exc

def _clear_tokens_file() -> None:
    p = _token_file_path()
    try:
        if os.path.isfile(p):
            os.remove(p)
    except Exception as exc:
        app_log(f"OAuth: failed to clear token file: {exc}")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_AUTHORIZE_URL = "https://users.nexusmods.com/oauth/authorize"
_TOKEN_URL     = "https://users.nexusmods.com/oauth/token"
_REVOKE_URL    = "https://users.nexusmods.com/oauth/revoke"

_CALLBACK_PORT = 7890
_CALLBACK_PATH = "/callback"
_REDIRECT_URI  = f"http://localhost:{_CALLBACK_PORT}{_CALLBACK_PATH}"

# OAuth credentials issued by Nexus Mods.
CLIENT_ID:     str = "amethyst"
CLIENT_SECRET: str = "d6bc16f2c28a5c5bc19261d458b70117"

_SCOPES = "openid profile public"

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


class OAuthRefreshError(RuntimeError):
    """Raised when a refresh-token exchange fails.

    ``token_revoked`` is True when Nexus rejected the refresh token itself
    (HTTP 400 / ``invalid_grant``) - i.e. the saved refresh token is dead and
    the user must log in again. It is False for transient failures (network,
    TLS, timeout, 5xx) where the token is probably still good and a later
    retry may succeed.
    """

    def __init__(self, message: str, *, token_revoked: bool = False):
        super().__init__(message)
        self.token_revoked = token_revoked


# Serialises refresh-token exchanges across threads. Nexus rotates the refresh
# token on every successful refresh (the old one is revoked server-side), so
# two concurrent refreshes with the same token would race - the second POST
# gets a 400 and, worse, could overwrite the freshly-saved good token with a
# now-revoked one. The lock + reload-under-lock below makes refresh atomic.
_refresh_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Keyring persistence
# ---------------------------------------------------------------------------

def load_oauth_tokens() -> Optional[OAuthTokens]:
    """Load OAuth tokens from the system keyring, or file fallback."""
    if not _keyring_available:
        return _load_tokens_file()
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
        return _load_tokens_file()


def save_oauth_tokens(tokens: OAuthTokens) -> None:
    """Persist OAuth tokens to the system keyring, or file fallback."""
    if not _keyring_available:
        _save_tokens_file(tokens)
        return
    try:
        keyring.set_password(_KEYRING_SERVICE, _KEYRING_ACCESS_KEY,  tokens.access_token)
        keyring.set_password(_KEYRING_SERVICE, _KEYRING_REFRESH_KEY, tokens.refresh_token)
        keyring.set_password(_KEYRING_SERVICE, _KEYRING_EXPIRES_KEY, str(tokens.expires_at))
    except Exception as exc:
        app_log(f"OAuth: keyring save failed, falling back to file: {exc}")
        _save_tokens_file(tokens)


def clear_oauth_tokens() -> None:
    """Delete all stored OAuth tokens from keyring and file."""
    _clear_tokens_file()
    if not _keyring_available:
        return
    for key in (_KEYRING_ACCESS_KEY, _KEYRING_REFRESH_KEY, _KEYRING_EXPIRES_KEY):
        try:
            keyring.delete_password(_KEYRING_SERVICE, key)
        except keyring.errors.PasswordDeleteError:
            pass
        except Exception as exc:
            app_log(f"OAuth: failed to clear token '{key}': {exc}")


# ---------------------------------------------------------------------------
# Connection self-diagnostics
# ---------------------------------------------------------------------------

def _log_connection_diagnostics() -> None:
    """
    Probe connectivity to the Nexus OAuth host and dump a shareable report to
    the app log. Called when a token exchange/refresh fails so users can paste
    the log instead of running curl by hand. Never touches OAuth tokens, so the
    output is safe to share.
    """
    host = "users.nexusmods.com"
    app_log("OAuth diagnostics: --- begin Nexus connection report ---")

    # 1. Environment basics
    try:
        import ssl, platform, sys
        app_log(f"OAuth diagnostics: python={sys.version.split()[0]} "
                f"openssl={ssl.OPENSSL_VERSION} platform={platform.platform()}")
        app_log(f"OAuth diagnostics: requests={requests.__version__}")
        try:
            import certifi
            app_log(f"OAuth diagnostics: certifi bundle={certifi.where()}")
        except Exception as exc:
            app_log(f"OAuth diagnostics: certifi unavailable: {exc}")
        app_log(f"OAuth diagnostics: system time={time.strftime('%Y-%m-%d %H:%M:%S %Z')} "
                f"(wrong clock is a common SSL cause)")
        for var in ("SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
                    "HTTP_PROXY", "HTTPS_PROXY"):
            if os.environ.get(var):
                app_log(f"OAuth diagnostics: env {var}={os.environ[var]}")
    except Exception as exc:
        app_log(f"OAuth diagnostics: env probe failed: {exc}")

    # 2. DNS resolution
    try:
        import socket
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
        ips = sorted({i[4][0] for i in infos})
        app_log(f"OAuth diagnostics: DNS {host} -> {', '.join(ips)}")
    except Exception as exc:
        app_log(f"OAuth diagnostics: DNS resolution FAILED: {exc!r}")

    # 3. Python TLS probe (mirrors how the app connects) against the live endpoint
    try:
        bundle = resolve_ca_bundle()
        app_log(f"OAuth diagnostics: resolved CA bundle = {bundle or 'requests default'}")
        r = requests.get(_TOKEN_URL, timeout=20, verify=bundle or True)
        app_log(f"OAuth diagnostics: python requests GET {_TOKEN_URL} -> "
                f"HTTP {r.status_code} (TLS OK; any HTTP status here means the "
                f"handshake succeeded)")
    except Exception as exc:
        app_log(f"OAuth diagnostics: python requests GET FAILED: {exc!r}")
        app_log("OAuth diagnostics: ^ this is the app's real failure - usually a "
                "CA-bundle / certifi issue in this build")

    # 4. System curl comparison (uses the OS cert store, like the user's manual test)
    try:
        import subprocess, shutil
        if shutil.which("curl"):
            proc = subprocess.run(
                ["curl", "-sS", "-o", "/dev/null", "-w",
                 "HTTP %{http_code} in %{time_total}s", "-v", _TOKEN_URL],
                capture_output=True, text=True, timeout=30,
            )
            # curl -v writes the handshake trace to stderr; keep only the useful lines
            keep = ("SSL", "TLS", "certificate", "subject:", "issuer:",
                    "expire date:", "Trying", "Connected", "HTTP")
            for line in (proc.stderr + "\n" + proc.stdout).splitlines():
                s = line.strip()
                if s and any(k in s for k in keep):
                    app_log(f"OAuth diagnostics: curl| {s}")
        else:
            app_log("OAuth diagnostics: curl not found on PATH; skipping system comparison")
    except Exception as exc:
        app_log(f"OAuth diagnostics: curl probe failed: {exc!r}")

    app_log("OAuth diagnostics: --- end Nexus connection report ---")
    app_log("OAuth diagnostics: if 'python requests' failed but 'curl' succeeded, "
            "the system network is fine and the app's cert bundle is the problem.")


# ---------------------------------------------------------------------------
# Token refresh
# ---------------------------------------------------------------------------

def refresh_if_needed(tokens: OAuthTokens, client_id: str = CLIENT_ID, client_secret: str = CLIENT_SECRET) -> OAuthTokens:
    """
    Return tokens unchanged if still valid, or perform a refresh token exchange.

    Thread-safe: the exchange is serialised so concurrent callers can't race on
    Nexus's rotating refresh token (see ``_refresh_lock``). Callers pass a
    possibly-stale snapshot; once inside the lock we re-load from storage and
    re-check expiry, so a caller that was blocked while another thread refreshed
    picks up the freshly-rotated token instead of POSTing the dead one.

    Raises OAuthRefreshError on refresh failure. ``token_revoked`` on the raised
    error distinguishes a dead refresh token (re-login required) from a
    transient network failure (retry may work).
    """
    if time.time() < tokens.expires_at - _REFRESH_MARGIN_SECS:
        return tokens

    with _refresh_lock:
        # Re-load under the lock: another thread may have just rotated the token
        # while we were blocked. Prefer the stored token if it's fresher.
        stored = load_oauth_tokens()
        if stored is not None and stored.expires_at > tokens.expires_at:
            tokens = stored
            if time.time() < tokens.expires_at - _REFRESH_MARGIN_SECS:
                # Another thread already refreshed for us.
                return tokens

        app_log("OAuth: access token expiring soon, refreshing...")
        try:
            resp = requests.post(
                _TOKEN_URL,
                data={
                    "grant_type":    "refresh_token",
                    "refresh_token": tokens.refresh_token,
                    "client_id":     client_id,
                    "client_secret": client_secret,
                    "redirect_uri":  _REDIRECT_URI,
                },
                timeout=20,
                verify=resolve_ca_bundle() or True,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.exceptions.HTTPError as exc:
            # A 4xx here means Nexus rejected the refresh token itself (rotated
            # out, revoked, or client mismatch) - the saved token is dead and
            # the user must re-authenticate. 5xx is transient; leave the token.
            status = getattr(exc.response, "status_code", None)
            revoked = status is not None and 400 <= status < 500
            app_log(f"OAuth: token refresh rejected (HTTP {status}); "
                    f"{'refresh token is dead - re-login required' if revoked else 'server error, will retry later'}")
            raise OAuthRefreshError(
                f"OAuth token refresh failed: {exc}", token_revoked=revoked
            ) from exc
        except requests.exceptions.RequestException as exc:
            # Network / TLS / timeout - token is probably still valid.
            app_log(f"OAuth: token refresh hit a connection error ({exc!r}); running diagnostics")
            _log_connection_diagnostics()
            raise OAuthRefreshError(f"OAuth token refresh failed: {exc}") from exc
        except Exception as exc:
            raise OAuthRefreshError(f"OAuth token refresh failed: {exc}") from exc

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

# Logo shown on the browser callback page (the http://localhost:7890/callback
# page the browser lands on after the user authorises). Inlined as a base64
# data-URI so the page is self-contained - no second request back to the local
# server. Uses src/icons/Logo.png (bundled into both the AppImage and Flatpak
# source trees - src/appimage/ is NOT shipped, only used as an icon source at
# build time). Loaded + cached once; None (and the page falls back to text-only)
# if the asset isn't present in this build.
_LOGO_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "icons", "Logo.png")
_logo_data_uri: Optional[str] = None
_logo_loaded = False


def _callback_logo_uri() -> Optional[str]:
    """Return the logo as a `data:image/png;base64,...` URI, or None if missing."""
    global _logo_data_uri, _logo_loaded
    if _logo_loaded:
        return _logo_data_uri
    _logo_loaded = True
    try:
        with open(_LOGO_PATH, "rb") as f:
            _logo_data_uri = "data:image/png;base64," + base64.b64encode(f.read()).decode()
    except Exception:
        _logo_data_uri = None
    return _logo_data_uri


def _callback_page(title: str, message: str) -> str:
    """Build the dark-themed browser callback page (logo + a short message)."""
    uri = _callback_logo_uri()
    logo = (f"<img src='{uri}' alt='' width='128' height='128' "
            f"style='display:block;margin:0 auto 28px;'>") if uri else ""
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        "<title>Amethyst Mod Manager</title></head>"
        "<body style='margin:0;min-height:100vh;display:flex;align-items:center;"
        "justify-content:center;background:#1b1b1f;color:#f0f0f2;"
        "font-family:sans-serif;text-align:center;'>"
        "<div>"
        f"{logo}"
        f"<h2 style='margin:0 0 10px;font-weight:600;'>{title}</h2>"
        f"<p style='margin:0;color:#a8a8b0;'>{message}</p>"
        "</div></body></html>"
    )


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
                    body = _callback_page(
                        "Authorised!",
                        "You can close this tab and return to Amethyst Mod Manager."
                    ).encode()
                else:
                    parent._error = params.get("error", "unknown")
                    body = _callback_page(
                        "Authorisation failed",
                        "Return to the app for details."
                    ).encode()

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

    def inject_code(self, code: str, state: str) -> None:
        """Inject a manually-pasted auth code (e.g. from Nexus 'Having issues?' page)."""
        self._code = code
        self._state = state
        self._event.set()

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
        self._verifier: Optional[str] = None
        self._state:    Optional[str] = None

    # -- public API ---------------------------------------------------------

    def submit_manual_code(self, blob: str) -> tuple[bool, str]:
        """
        Submit a manually-pasted auth code from the Nexus 'Having issues?' page.

        The blob is Base64-encoded JSON with authorization_code and state.
        Call this only while the OAuth flow is waiting for the callback.

        Returns (success, message).
        """
        if not self._srv or not self.is_running:
            return False, "Start browser login first, then paste the code if the redirect didn't work."
        if not self._verifier or self._state is None:
            return False, "OAuth session not ready."

        blob = blob.strip()
        if not blob:
            return False, "No code entered."

        try:
            # Nexus uses standard Base64; add padding if needed
            padded = blob + "=" * (4 - len(blob) % 4) if len(blob) % 4 else blob
            decoded = base64.b64decode(padded).decode("utf-8")
            data = json.loads(decoded)
        except Exception:
            return False, "Invalid code format. Paste the full Base64 code from the Nexus page."

        auth_code = data.get("authorization_code")
        pasted_state = data.get("state")
        if not auth_code:
            return False, "Invalid code: missing authorization_code."
        if pasted_state != self._state:
            return False, "Code doesn't match this login session. Start a new login and paste the code from that session."

        self._srv.inject_code(auth_code, pasted_state)
        return True, "Submitting code..."

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
        self._verifier = verifier
        self._state = state

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
        self._on_status("Opening browser - please authorise in Nexus Mods...")
        app_log("OAuth: opening auth URL")
        open_url(auth_url)

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
            self._on_error("OAuth state mismatch - possible CSRF attack, aborting.")
            return

        # 4. Exchange code for tokens
        self._on_status("Exchanging authorisation code for tokens...")
        try:
            resp = requests.post(
                _TOKEN_URL,
                data={
                    "grant_type":    "authorization_code",
                    "client_id":     self._client_id,
                    "client_secret": CLIENT_SECRET,
                    "redirect_uri":  _REDIRECT_URI,
                    "code":          code,
                    "code_verifier": verifier,
                },
                timeout=20,
                verify=resolve_ca_bundle() or True,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            if isinstance(exc, (requests.exceptions.SSLError,
                                requests.exceptions.ConnectionError,
                                requests.exceptions.Timeout)):
                app_log(f"OAuth: token exchange hit a connection error ({exc!r}); running diagnostics")
                _log_connection_diagnostics()
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
