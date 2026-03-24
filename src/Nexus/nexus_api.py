"""
nexus_api.py
Nexus Mods REST API v1 client.

Wraps the public API at https://api.nexusmods.com/v1.
Requires a personal API key generated at https://www.nexusmods.com/settings/api-keys

Rate limits
-----------
  Free  users: 300 requests burst, recovers 1 req/s
  Premium    : 600 requests burst, recovers 1 req/s

The server returns remaining quota in response headers:
  x-rl-hourly-remaining, x-rl-daily-remaining

HTTP 429 → rate-limited; back off and retry.

Usage
-----
    from Nexus.nexus_api import NexusAPI

    api = NexusAPI(api_key="...")
    user = api.validate()
    mod  = api.get_mod("skyrimspecialedition", 2014)
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import keyring
import requests

from Utils.config_paths import get_config_dir
from Utils.app_log import app_log
from version import __version__

API_BASE = "https://api.nexusmods.com/v1"
GRAPHQL_BASE = "https://api.nexusmods.com/v2/graphql"
APP_NAME = "amethyst"
APP_VERSION = __version__

# How long to wait after a 429 before retrying (seconds)
_RATE_LIMIT_BACKOFF = 2.0
_MAX_RETRIES = 3

# Keys to redact when logging API responses (values replaced with [REDACTED])
_SENSITIVE_KEYS = frozenset({"key", "email", "api_key", "token", "authorization", "password"})


def _redact_sensitive_response(text: str) -> str:
    """Return response text with sensitive fields redacted for safe logging."""
    if not text or not text.strip():
        return text
    try:
        data = json.loads(text)
        redacted = _redact_sensitive_dict(data)
        return json.dumps(redacted, indent=None, default=str)
    except Exception:
        return text


def _redact_sensitive_dict(obj: Any) -> Any:
    """Recursively copy obj, replacing values for sensitive keys with [REDACTED]."""
    if isinstance(obj, dict):
        return {
            k: "[REDACTED]" if k.lower() in _SENSITIVE_KEYS else _redact_sensitive_dict(v)
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_redact_sensitive_dict(item) for item in obj]
    return obj


# ---------------------------------------------------------------------------
# Data classes for typed responses
# ---------------------------------------------------------------------------

@dataclass
class NexusUser:
    """Validated user info returned by /users/validate."""
    user_id: int
    name: str
    email: str
    is_premium: bool
    is_supporter: bool
    profile_url: str


@dataclass
class NexusGameInfo:
    """Basic game info from the Nexus API."""
    id: int
    name: str
    domain_name: str
    nexusmods_url: str
    genre: str = ""
    file_count: int = 0
    downloads: int = 0
    mods_count: int = 0


@dataclass
class NexusCategory:
    """A mod category for a game."""
    category_id: int
    name: str
    parent_category: int | None = None  # None = top-level


@dataclass
class NexusModInfo:
    """Mod metadata from /games/{domain}/mods/{id}."""
    mod_id: int
    name: str
    summary: str
    description: str
    version: str
    author: str
    category_id: int
    game_id: int
    domain_name: str
    picture_url: str = ""
    endorsement_count: int = 0
    downloads_total: int = 0
    created_timestamp: int = 0
    updated_timestamp: int = 0
    available: bool = True
    contains_adult_content: bool = False
    status: str = ""
    uploaded_by: str = ""
    category_name: str = ""


@dataclass
class NexusModFile:
    """A single file entry for a mod."""
    file_id: int
    name: str
    version: str
    category_name: str       # "MAIN", "UPDATE", "OPTIONAL", "OLD_VERSION", "MISCELLANEOUS"
    file_name: str            # actual archive filename
    size_in_bytes: int | None = None
    size_kb: int = 0
    mod_version: str = ""
    description: str = ""
    uploaded_timestamp: int = 0
    is_primary: bool = False
    changelog_html: str = ""
    external_virus_scan_url: str = ""


@dataclass
class NexusModFiles:
    """File listing for a mod."""
    files: list[NexusModFile] = field(default_factory=list)
    file_updates: list[dict] = field(default_factory=list)


@dataclass
class NexusDownloadLink:
    """A CDN download link returned by the API."""
    name: str        # mirror name, e.g. "Nexus CDN"
    short_name: str
    URI: str         # the actual download URL


@dataclass
class NexusRateLimits:
    """Current rate limit state."""
    hourly_remaining: int = -1
    daily_remaining: int = -1
    hourly_limit: int = -1
    daily_limit: int = -1


@dataclass
class NexusModRequirement:
    """A single mod requirement (dependency)."""
    mod_id: int
    mod_name: str
    game_domain: str = ""
    url: str = ""
    is_external: bool = False  # True if it's an external (non-Nexus) requirement
    notes: str = ""  # Notes about the mod requirement (from GraphQL ModRequirement)


@dataclass
class NexusModUpdateInfo:
    """Lightweight update status for a mod, returned by the GraphQL batch checker."""
    mod_id: int
    name: str
    version: str
    updated_at: Optional[datetime] = None   # when any file was last uploaded
    viewer_update_available: Optional[bool] = None  # Nexus-native flag (requires tracking)
    requirements: list["NexusModRequirement"] = field(default_factory=list)  # mod dependencies
    category_id: int = 0                    # Nexus mod category (e.g. Armor, Weapons)
    category_name: str = ""                 # Category display name
    files: list["NexusModFile"] = field(default_factory=list)  # from batch; avoids REST get_mod_files


@dataclass
class NexusCollection:
    """A Nexus Mods collection, returned by the GraphQL collections query."""
    id: int = 0
    slug: str = ""
    name: str = ""
    summary: str = ""
    user_name: str = ""
    total_downloads: int = 0
    endorsements: int = 0
    mod_count: int = 0
    tile_image_url: str = ""
    game_domain: str = ""


@dataclass
class NexusCollectionMod:
    """A single mod entry inside a collection revision."""
    mod_id: int = 0
    file_id: int = 0
    mod_name: str = ""
    mod_author: str = ""
    file_name: str = ""
    version: str = ""
    size_bytes: int = 0
    optional: bool = False
    source_type: str = "nexus"  # "nexus", "bundle", "browse", "direct"


# ---------------------------------------------------------------------------
# API key persistence (system keyring, with file fallback)
# ---------------------------------------------------------------------------

_KEYRING_SERVICE = "AmethystModManager"
_KEYRING_USER = "nexus_api_key"
_API_KEY_FILE = "nexus_api_key.bin"


def _api_key_path() -> Path:
    """Path of legacy plaintext key file (used only for migration)."""
    return get_config_dir() / "nexus_api_key"


def _api_key_file_path() -> Path:
    """Path for file-based API key fallback."""
    return get_config_dir() / _API_KEY_FILE


def _keyring_ok() -> bool:
    """Check if keyring is available (reuses probe from nexus_oauth)."""
    try:
        from Nexus.nexus_oauth import _keyring_available
        return _keyring_available
    except Exception:
        return True  # Assume available if we can't check


def _derive_key() -> bytes:
    """Derive a Fernet key from the machine ID so keys are only usable on this device."""
    import base64, hashlib
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


def _load_key_file() -> str:
    """Load API key from encrypted file fallback."""
    p = _api_key_file_path()
    try:
        if not p.is_file():
            return ""
        from cryptography.fernet import Fernet
        import json as _json
        cipher = Fernet(_derive_key())
        data = _json.loads(cipher.decrypt(p.read_bytes()))
        return data.get("api_key", "").strip()
    except Exception:
        return ""


def _save_key_file(key: str) -> None:
    """Save API key to encrypted file fallback."""
    from cryptography.fernet import Fernet
    import json as _json, os as _os
    p = _api_key_file_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    cipher = Fernet(_derive_key())
    p.write_bytes(cipher.encrypt(_json.dumps({"api_key": key}).encode()))
    _os.chmod(p, 0o600)


def _clear_key_file() -> None:
    """Remove file-based API key."""
    p = _api_key_file_path()
    try:
        if p.is_file():
            p.unlink()
    except OSError:
        pass


def _migrate_legacy_key() -> str:
    """If legacy plaintext file exists, move it to keyring/file and return the key."""
    p = _api_key_path()
    if not p.is_file():
        return ""
    try:
        key = p.read_text().strip()
        if key:
            if _keyring_ok():
                try:
                    keyring.set_password(_KEYRING_SERVICE, _KEYRING_USER, key)
                except Exception:
                    _save_key_file(key)
            else:
                _save_key_file(key)
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass
        return key
    except OSError as e:
        app_log(f"Nexus API key migration from file failed: {e}")
        return ""


def load_api_key() -> str:
    """Load saved API key from system keyring or file fallback."""
    if not _keyring_ok():
        return _load_key_file() or _migrate_legacy_key()
    try:
        key = keyring.get_password(_KEYRING_SERVICE, _KEYRING_USER)
        if key:
            return key.strip()
        return _migrate_legacy_key()
    except UnicodeDecodeError as e:
        app_log(f"Nexus API key in keyring is invalid/corrupted ({e}). Clear and re-enter in Nexus settings.")
        try:
            keyring.delete_password(_KEYRING_SERVICE, _KEYRING_USER)
        except Exception:
            pass
        return _migrate_legacy_key()
    except keyring.errors.KeyringError as e:
        app_log(f"Keyring unavailable for Nexus API key: {e} — using file fallback")
        return _load_key_file() or _migrate_legacy_key()


def save_api_key(key: str) -> None:
    """Persist the API key to the system keyring or file fallback."""
    key = key.strip()
    if not _keyring_ok():
        _save_key_file(key)
        return
    try:
        keyring.set_password(_KEYRING_SERVICE, _KEYRING_USER, key)
    except keyring.errors.KeyringError as e:
        app_log(f"Keyring unavailable for saving Nexus API key: {e} — using file fallback")
        _save_key_file(key)
        return
    # Remove legacy file if it exists
    try:
        _api_key_path().unlink(missing_ok=True)
    except OSError:
        pass


def clear_api_key() -> None:
    """Delete the stored API key from keyring and file."""
    _clear_key_file()
    if _keyring_ok():
        try:
            keyring.delete_password(_KEYRING_SERVICE, _KEYRING_USER)
        except keyring.errors.PasswordDeleteError:
            pass
        except keyring.errors.KeyringError as e:
            app_log(f"Keyring unavailable when clearing Nexus API key: {e}")
    try:
        _api_key_path().unlink(missing_ok=True)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Main API client
# ---------------------------------------------------------------------------

class NexusAPIError(Exception):
    """Raised for non-recoverable API errors."""
    def __init__(self, message: str, status_code: int = 0, url: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.url = url


class RateLimitError(NexusAPIError):
    """Raised when the server returns HTTP 429."""
    def __init__(self, url: str = ""):
        super().__init__("Rate limit exceeded — slow down", 429, url)


class NexusAPI:
    """
    Synchronous Nexus Mods v1 REST client.

    Supports two auth modes:
      - API key  (legacy):  NexusAPI(api_key="...")
      - OAuth Bearer token: NexusAPI.from_oauth(tokens)

    Parameters
    ----------
    api_key : str
        Personal API key from nexusmods.com/settings/api-keys.
    timeout : float
        Request timeout in seconds.
    """

    def __init__(self, api_key: str, timeout: float = 30.0):
        self._key = api_key.strip()
        self._timeout = timeout
        self._rate = NexusRateLimits()
        self._cached_user: "NexusUser | None" = None
        self._cached_user_ts: float = 0.0
        self._oauth_tokens = None
        self._session = requests.Session()
        self._session.headers.update({
            "APIKEY": self._key,
            "Content-Type": "application/json",
            "Application-Name": APP_NAME,
            "Application-Version": APP_VERSION,
            "Accept": "application/json",
        })

    @classmethod
    def from_oauth(cls, tokens: "Any", timeout: float = 30.0) -> "NexusAPI":
        """
        Create a NexusAPI instance authenticated via an OAuth Bearer token.

        Parameters
        ----------
        tokens : OAuthTokens
            Tokens obtained from nexus_oauth.load_oauth_tokens() or the login flow.
            The access_token is used as a Bearer token; the APIKEY header is omitted.
        timeout : float
            Request timeout in seconds.
        """
        # Import here to avoid a circular dependency at module load time
        from Nexus.nexus_oauth import refresh_if_needed
        tokens = refresh_if_needed(tokens)

        instance = cls.__new__(cls)
        instance._key = ""
        instance._timeout = timeout
        instance._rate = NexusRateLimits()
        instance._cached_user = None
        instance._cached_user_ts = 0.0
        instance._oauth_tokens = tokens
        instance._session = requests.Session()
        instance._session.headers.update({
            "Authorization": f"Bearer {tokens.access_token}",
            "Content-Type": "application/json",
            "Application-Name": APP_NAME,
            "Application-Version": APP_VERSION,
            "Accept": "application/json",
        })
        return instance

    def _refresh_oauth_if_needed(self) -> None:
        """If this instance uses OAuth, refresh the access token if it is expiring soon and update the session header."""
        tokens = getattr(self, "_oauth_tokens", None)
        if tokens is None:
            return
        from Nexus.nexus_oauth import refresh_if_needed
        new_tokens = refresh_if_needed(tokens)
        if new_tokens.access_token != tokens.access_token:
            self._oauth_tokens = new_tokens
            self._session.headers["Authorization"] = f"Bearer {new_tokens.access_token}"

    # -- low-level ----------------------------------------------------------

    def _update_rate_limits(self, resp: requests.Response) -> None:
        """Parse rate-limit headers from the response."""
        h = resp.headers
        if "x-rl-hourly-remaining" in h:
            self._rate.hourly_remaining = int(h["x-rl-hourly-remaining"])
        if "x-rl-daily-remaining" in h:
            self._rate.daily_remaining = int(h["x-rl-daily-remaining"])
        if "x-rl-hourly-limit" in h:
            self._rate.hourly_limit = int(h["x-rl-hourly-limit"])
        if "x-rl-daily-limit" in h:
            self._rate.daily_limit = int(h["x-rl-daily-limit"])

    def _log_response(self, method: str, path: str, resp: requests.Response) -> None:
        """Log request and response to the app log (status + body, truncated). Redacts sensitive fields (key, email, etc.)."""
        try:
            status_msg = f"Nexus API {method} {path} → {resp.status_code}"
            app_log(status_msg)
            body_str = resp.text if resp.text is not None else "(empty)"
            body_str = _redact_sensitive_response(body_str)
            if len(body_str) > 1200:
                body_str = body_str[:1200] + "..."
            app_log(f"  Response: {body_str}")
        except Exception:
            try:
                app_log(f"Nexus API {method} {path} → {resp.status_code}")
            except Exception:
                pass

    def _get(self, path: str, params: dict | None = None,
             retries: int = _MAX_RETRIES) -> Any:
        """Issue a GET request against the v1 API, with retry on 429."""
        self._refresh_oauth_if_needed()
        url = API_BASE + path
        for attempt in range(retries):
            try:
                resp = self._session.get(url, params=params,
                                         timeout=self._timeout)
            except requests.ConnectionError as exc:
                raise NexusAPIError(
                    f"Connection failed: {exc}", url=url) from exc
            except requests.Timeout as exc:
                raise NexusAPIError(
                    f"Request timed out after {self._timeout}s",
                    url=url) from exc

            self._update_rate_limits(resp)
            self._log_response("GET", path, resp)

            if resp.status_code == 429:
                wait = _RATE_LIMIT_BACKOFF * (attempt + 1)
                app_log(f"Nexus 429 rate-limited, backing off {wait:.1f}s "
                        f"(attempt {attempt + 1}/{retries})")
                time.sleep(wait)
                continue

            if resp.status_code == 401:
                raise NexusAPIError(
                    "Invalid or expired API key", 401, url)

            if not resp.ok:
                try:
                    body = resp.json()
                    msg = body.get("message", resp.reason)
                except Exception:
                    msg = resp.text[:300] or resp.reason
                raise NexusAPIError(msg, resp.status_code, url)

            return resp.json()

        raise RateLimitError(url)

    @property
    def rate_limits(self) -> NexusRateLimits:
        """Return the most recently observed rate limits."""
        return self._rate

    def refresh_rate_limits(self) -> None:
        """Make a GET request to update rate limit state from response headers.
        Uses the same endpoint as Vortex's API Limit Checker (/games.json) so the
        returned remaining counts reflect all usage (this app + Swagger + others).
        Does not log the response body to avoid log spam.
        """
        url = API_BASE + "/games.json"
        try:
            resp = self._session.get(url, timeout=self._timeout)
        except requests.ConnectionError as exc:
            raise NexusAPIError(f"Connection failed: {exc}", url=url) from exc
        except requests.Timeout as exc:
            raise NexusAPIError(
                f"Request timed out after {self._timeout}s", url=url
            ) from exc
        self._update_rate_limits(resp)
        # Log raw rate-limit headers and stored values (to verify server sends cumulative counts)
        rl_headers = {k: v for k, v in resp.headers.items() if "rl" in k.lower()}
        app_log(f"Nexus API: rate limit headers received: {rl_headers}")
        r = self._rate
        app_log(
            f"Nexus API: rate limits refreshed — "
            f"hourly {r.hourly_remaining}/{r.hourly_limit}, daily {r.daily_remaining}/{r.daily_limit}"
        )
        if resp.status_code == 429:
            raise RateLimitError(url)
        if resp.status_code == 401:
            raise NexusAPIError("Invalid or expired API key", 401, url)
        if not resp.ok:
            try:
                body = resp.json()
                msg = body.get("message", resp.reason)
            except Exception:
                msg = resp.text[:300] if resp.text else resp.reason
            raise NexusAPIError(msg, resp.status_code, url)

    # -- Account ------------------------------------------------------------

    _VALIDATE_CACHE_TTL = 300.0  # seconds

    def validate(self, bypass_cache: bool = False) -> "NexusUser":
        """Validate the current API key (or OAuth token) and return user info.

        Result is cached for 5 minutes so repeated calls (e.g. one per mod
        install) consume only a single rate-limited request per session.
        Pass ``bypass_cache=True`` to force a fresh request.
        """
        if not bypass_cache and self._cached_user is not None:
            if time.monotonic() - self._cached_user_ts < self._VALIDATE_CACHE_TTL:
                return self._cached_user

        # OAuth mode: v1 /users/validate doesn't accept Bearer tokens — use userinfo instead
        if not self._key and "Authorization" in self._session.headers:
            user = self._validate_via_oauth_userinfo()
        else:
            data = self._get("/users/validate")
            user = NexusUser(
                user_id=data["user_id"],
                name=data["name"],
                email=data.get("email", ""),
                is_premium=data.get("is_premium", False),
                is_supporter=data.get("is_supporter", False),
                profile_url=data.get("profile_url", ""),
            )

        self._cached_user = user
        self._cached_user_ts = time.monotonic()
        return user

    def _validate_via_oauth_userinfo(self) -> "NexusUser":
        """Fetch user info via OpenID userinfo endpoint (OAuth Bearer auth)."""
        resp = self._session.get(
            "https://users.nexusmods.com/oauth/userinfo",
            timeout=self._timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        # Determine premium/supporter from userinfo membership_roles / premium_expiry
        membership_roles = data.get("membership_roles") or []
        premium_expiry = data.get("premium_expiry")
        is_premium = (
            "premium" in [str(r).lower() for r in membership_roles]
            or (premium_expiry is not None and premium_expiry != 0)
        )
        is_supporter = "supporter" in [str(r).lower() for r in membership_roles]
        return NexusUser(
            user_id=int(data.get("sub", 0) or 0),
            name=data.get("name", "") or data.get("preferred_username", "") or data.get("sub", ""),
            email=data.get("email", ""),
            is_premium=is_premium,
            is_supporter=is_supporter,
            profile_url=data.get("picture", "") or data.get("avatar", ""),
        )

    # -- Games --------------------------------------------------------------

    def get_games(self) -> list[NexusGameInfo]:
        """Return a list of all games supported by Nexus Mods."""
        items = self._get("/games")
        return [
            NexusGameInfo(
                id=g["id"],
                name=g["name"],
                domain_name=g["domain_name"],
                nexusmods_url=g.get("nexusmods_url", ""),
                genre=g.get("genre", ""),
                file_count=g.get("file_count", 0),
                downloads=g.get("downloads", 0),
                mods_count=g.get("mods_count", 0),
            )
            for g in items
        ]

    def get_game(self, game_domain: str) -> NexusGameInfo:
        """Get info for a specific game by its Nexus domain name."""
        g = self._get(f"/games/{game_domain}")
        return NexusGameInfo(
            id=g["id"],
            name=g["name"],
            domain_name=g["domain_name"],
            nexusmods_url=g.get("nexusmods_url", ""),
            genre=g.get("genre", ""),
            file_count=g.get("file_count", 0),
            downloads=g.get("downloads", 0),
            mods_count=g.get("mods_count", 0),
        )

    def get_game_categories(self, game_domain: str) -> list[NexusCategory]:
        """Return the mod categories for a game via the v1 REST API."""
        data = self._get(f"/games/{game_domain}")
        result: list[NexusCategory] = []
        for c in data.get("categories", []):
            # parent_category is either False or {"category_id": N, "name": "..."}
            pc = c.get("parent_category")
            parent = pc.get("category_id") if isinstance(pc, dict) else None
            result.append(NexusCategory(
                category_id=c.get("category_id", 0),
                name=c.get("name", ""),
                parent_category=parent,
            ))
        return result

    @staticmethod
    def _build_mods_filter(
        game_domain: str, category_names: list[str] | None = None
    ) -> dict:
        """Build a ModsFilter dict, optionally restricting to specific category names."""
        base: dict = {"gameDomainName": {"value": game_domain}}
        if not category_names:
            return base
        if len(category_names) == 1:
            base["categoryName"] = {"value": category_names[0]}
            return base
        # Multiple categories: AND(domain, OR(cat1, cat2, ...))
        return {
            "op": "AND",
            "filter": [
                {"gameDomainName": {"value": game_domain}},
                {
                    "op": "OR",
                    "filter": [{"categoryName": {"value": n}} for n in category_names],
                },
            ],
        }

    # -- Mods ---------------------------------------------------------------

    def get_mod(self, game_domain: str, mod_id: int) -> NexusModInfo:
        """Retrieve details about a specific mod."""
        d = self._get(f"/games/{game_domain}/mods/{mod_id}")
        try:
            cat_id = int(d.get("category_id", 0) or 0)
        except (TypeError, ValueError):
            cat_id = 0
        cat_name = d.get("category_name", "") or ""
        if not cat_name:
            cat = d.get("category")
            if isinstance(cat, dict):
                cat_name = (cat.get("name") or "").strip()
            elif isinstance(cat, str):
                cat_name = cat.strip()
        if not cat_name and cat_id:
            # REST API often returns only category_id; look up name from game categories
            try:
                for c in self.get_game_categories(game_domain):
                    if c.category_id == cat_id:
                        cat_name = c.name or ""
                        break
            except Exception:
                pass
        return NexusModInfo(
            mod_id=d["mod_id"],
            name=d["name"],
            summary=d.get("summary", ""),
            description=d.get("description", ""),
            version=d.get("version", ""),
            author=d.get("author", ""),
            category_id=cat_id,
            category_name=cat_name,
            game_id=d.get("game_id", 0),
            domain_name=d.get("domain_name", game_domain),
            picture_url=d.get("picture_url", ""),
            endorsement_count=d.get("endorsement_count", 0),
            created_timestamp=d.get("created_timestamp", 0),
            updated_timestamp=d.get("updated_timestamp", 0),
            available=d.get("available", True),
            contains_adult_content=d.get("contains_adult_content", False),
            status=d.get("status", ""),
            uploaded_by=d.get("uploaded_by", ""),
        )

    def get_latest_added(self, game_domain: str) -> list[NexusModInfo]:
        """Return the most recently added mods for a game."""
        items = self._get(f"/games/{game_domain}/mods/latest_added")
        return [self._parse_mod_info(m, game_domain) for m in items]

    def get_latest_updated(self, game_domain: str) -> list[NexusModInfo]:
        """Return the most recently updated mods for a game."""
        items = self._get(f"/games/{game_domain}/mods/latest_updated")
        return [self._parse_mod_info(m, game_domain) for m in items]

    def get_trending(self, game_domain: str) -> list[NexusModInfo]:
        """Return trending mods for a game (REST). Prefer get_trending_mods_graphql for consistency."""
        items = self._get(f"/games/{game_domain}/mods/trending")
        return [self._parse_mod_info(m, game_domain) for m in items]

    def get_trending_mods_graphql(
        self,
        game_domain: str,
        count: int = 20,
        offset: int = 0,
        category_names: list[str] | None = None,
    ) -> list[NexusModInfo]:
        """
        Fetch trending mods via GraphQL: mods published in the last 7 days,
        sorted by endorsements (highest first).
        """
        seven_days_ago = int(time.time()) - (7 * 24 * 60 * 60)
        base_filter = self._build_mods_filter(game_domain, category_names)
        if "filter" in base_filter:
            base_filter["filter"].append({
                "createdAt": [{"value": str(seven_days_ago), "op": "GTE"}],
            })
        else:
            base_filter = {
                "op": "AND",
                "filter": [
                    base_filter,
                    {"createdAt": [{"value": str(seven_days_ago), "op": "GTE"}]},
                ],
            }
        query = """
        query TrendingMods($filter: ModsFilter, $count: Int, $offset: Int) {
            mods(
                filter: $filter
                sort: [{ endorsements: { direction: DESC } }]
                count: $count
                offset: $offset
            ) {
                nodes {
                    modId
                    name
                    summary
                    description
                    author
                    version
                    endorsements
                    downloads
                    pictureUrl
                }
            }
        }
        """
        variables = {
            "filter": base_filter,
            "count": count,
            "offset": offset,
        }
        try:
            resp = self._session.post(
                GRAPHQL_BASE,
                json={"query": query, "variables": variables},
                timeout=self._timeout,
            )
            self._log_response("POST", "GraphQL trendingMods", resp)
            if not resp.ok:
                raise NexusAPIError(
                    f"GraphQL trending query failed: {resp.status_code}",
                    resp.status_code,
                )
            data = resp.json()
            if "errors" in data:
                raise NexusAPIError(
                    f"GraphQL error: {data['errors'][0].get('message', 'unknown')}"
                )
            nodes = data.get("data", {}).get("mods", {}).get("nodes", [])
            results: list[NexusModInfo] = []
            for n in nodes:
                results.append(NexusModInfo(
                    mod_id=n.get("modId", 0),
                    name=n.get("name", "") or "",
                    summary=n.get("summary", "") or "",
                    description=n.get("description", "") or "",
                    version=n.get("version", "") or "",
                    author=n.get("author", "") or "",
                    category_id=0,
                    game_id=0,
                    domain_name=game_domain,
                    endorsement_count=n.get("endorsements", 0) or 0,
                    downloads_total=n.get("downloads", 0) or 0,
                    picture_url=n.get("pictureUrl", "") or "",
                ))
            return results
        except NexusAPIError:
            raise
        except Exception as exc:
            raise NexusAPIError(f"GraphQL trending error: {exc}") from exc

    def get_updated_mods(self, game_domain: str,
                         period: str = "1w") -> list[dict]:
        """Get mods updated within a period (1d, 1w, 1m)."""
        return self._get(
            f"/games/{game_domain}/mods/updated",
            params={"period": period},
        )

    # -- Files --------------------------------------------------------------

    def get_mod_files(self, game_domain: str,
                      mod_id: int) -> NexusModFiles:
        """List all files uploaded for a mod."""
        data = self._get(f"/games/{game_domain}/mods/{mod_id}/files")
        files = [
            NexusModFile(
                file_id=f["file_id"],
                name=f.get("name", ""),
                version=f.get("version", ""),
                category_name=f.get("category_name", ""),
                file_name=f.get("file_name", ""),
                size_in_bytes=f.get("size_in_bytes"),
                size_kb=f.get("size_kb", 0),
                mod_version=f.get("mod_version", ""),
                description=f.get("description", ""),
                uploaded_timestamp=f.get("uploaded_timestamp", 0),
                is_primary=f.get("is_primary", False),
                changelog_html=f.get("changelog_html", ""),
                external_virus_scan_url=f.get("external_virus_scan_url", ""),
            )
            for f in data.get("files", [])
        ]
        return NexusModFiles(
            files=files,
            file_updates=data.get("file_updates", []),
        )

    def get_file_info(self, game_domain: str, mod_id: int,
                      file_id: int) -> NexusModFile:
        """Get details about a specific file."""
        f = self._get(
            f"/games/{game_domain}/mods/{mod_id}/files/{file_id}")
        return NexusModFile(
            file_id=f["file_id"],
            name=f.get("name", ""),
            version=f.get("version", ""),
            category_name=f.get("category_name", ""),
            file_name=f.get("file_name", ""),
            size_in_bytes=f.get("size_in_bytes"),
            size_kb=f.get("size_kb", 0),
            mod_version=f.get("mod_version", ""),
            description=f.get("description", ""),
            uploaded_timestamp=f.get("uploaded_timestamp", 0),
            is_primary=f.get("is_primary", False),
            changelog_html=f.get("changelog_html", ""),
            external_virus_scan_url=f.get("external_virus_scan_url", ""),
        )

    def get_download_links(
        self,
        game_domain: str,
        mod_id: int,
        file_id: int,
        key: str | None = None,
        expires: int | None = None,
    ) -> list[NexusDownloadLink]:
        """
        Generate download URLs for a file.

        Premium users can call this directly (no key/expires needed).
        Free users must provide key + expires from an nxm:// link
        (the "Download with Manager" button on the website).

        Parameters
        ----------
        game_domain : str  Nexus game domain, e.g. "skyrimspecialedition"
        mod_id      : int  Nexus mod ID
        file_id     : int  Nexus file ID
        key         : str  Download key from nxm:// link (free users)
        expires     : int  Expiry timestamp from nxm:// link (free users)

        Returns
        -------
        List of download mirror URLs.
        """
        path = (f"/games/{game_domain}/mods/{mod_id}"
                f"/files/{file_id}/download_link")
        params: dict[str, Any] = {}
        if key is not None and expires is not None:
            params["key"] = key
            params["expires"] = str(expires)
        data = self._get(path, params=params or None)
        return [
            NexusDownloadLink(
                name=d.get("name", ""),
                short_name=d.get("short_name", ""),
                URI=d["URI"],
            )
            for d in data
        ]

    # -- MD5 lookup ---------------------------------------------------------

    def get_file_by_md5(self, game_domain: str,
                        md5: str) -> list[dict]:
        """
        Find mod/file info by MD5 hash.

        Useful for identifying already-downloaded archives.
        May return multiple results if the same file was uploaded
        to different mods.
        """
        return self._get(
            f"/games/{game_domain}/mods/md5_search/{md5}")

    # -- Endorsements -------------------------------------------------------

    def get_endorsements(self) -> list[dict]:
        """Get the current user's endorsements."""
        return self._get("/user/endorsements")

    def endorse_mod(self, game_domain: str, mod_id: int, version: str = "") -> dict:
        """Endorse a mod on Nexus Mods."""
        resp = self._session.post(
            f"{API_BASE}/games/{game_domain}/mods/{mod_id}/endorse",
            json={"Version": version},
            timeout=self._timeout,
        )
        self._update_rate_limits(resp)
        self._log_response("POST", f"/games/{game_domain}/mods/{mod_id}/endorse", resp)
        resp.raise_for_status()
        return resp.json()

    def abstain_mod(self, game_domain: str, mod_id: int, version: str = "") -> dict:
        """Abstain from endorsing a mod on Nexus Mods."""
        resp = self._session.post(
            f"{API_BASE}/games/{game_domain}/mods/{mod_id}/abstain",
            json={"Version": version},
            timeout=self._timeout,
        )
        self._update_rate_limits(resp)
        self._log_response("POST", f"/games/{game_domain}/mods/{mod_id}/abstain", resp)
        resp.raise_for_status()
        return resp.json()

    # -- Mod requirements (GraphQL v2) --------------------------------------

    def get_mod_requirements(
        self, game_domain: str, mod_id: int
    ) -> list[NexusModRequirement]:
        """
        Fetch the Nexus-listed requirements for a mod via the GraphQL v2 API.

        Returns a list of NexusModRequirement (one per required mod).
        External requirements (non-Nexus links) are included with is_external=True.
        """
        query = """
        query ModRequirements($ids: [CompositeDomainWithIdInput!]!) {
            legacyModsByDomain(ids: $ids) {
                nodes {
                    modRequirements {
                        nexusRequirements {
                            nodes {
                                modId
                                modName
                                gameId
                                url
                                externalRequirement
                                notes
                            }
                        }
                    }
                }
            }
        }
        """
        variables = {"ids": [{"gameDomain": game_domain, "modId": mod_id}]}
        try:
            resp = self._session.post(
                GRAPHQL_BASE,
                json={"query": query, "variables": variables},
                timeout=self._timeout,
            )
            self._log_response("POST", "GraphQL modRequirements", resp)
            if not resp.ok:
                app_log(f"GraphQL requirements query failed: {resp.status_code}")
                return []
            data = resp.json()
            mod_nodes = (
                data.get("data", {})
                .get("legacyModsByDomain", {})
                .get("nodes", [])
            )
            if not mod_nodes:
                return []
            nodes = (
                mod_nodes[0]
                .get("modRequirements", {})
                .get("nexusRequirements", {})
                .get("nodes", [])
            )
            results: list[NexusModRequirement] = []
            for n in nodes:
                mid_raw = n.get("modId", "0")
                try:
                    mid = int(mid_raw)
                except (ValueError, TypeError):
                    mid = 0
                results.append(NexusModRequirement(
                    mod_id=mid,
                    mod_name=n.get("modName", ""),
                    game_domain=n.get("gameId", game_domain),
                    url=n.get("url", ""),
                    is_external=bool(n.get("externalRequirement", False)),
                    notes=n.get("notes", "") or "",
                ))
            return results
        except Exception as exc:
            app_log(f"GraphQL requirements query error: {exc}")
            return []

    # -- NXM download helper (GraphQL v2) -----------------------------------

    def get_mod_and_file_info_graphql(
        self,
        game_domain: str,
        mod_id: int,
        file_id: int,
    ) -> "tuple[NexusModInfo | None, NexusModFile | None]":
        """
        Fetch mod info + a specific file's metadata in a single GraphQL request,
        replacing the two REST calls (get_mod + get_mod_files) used during NXM
        downloads.

        Returns (NexusModInfo, NexusModFile) — either may be None on failure.
        Falls back gracefully so callers can still use partial data.
        """
        # Mod type has no 'files' field; request mod + category only (file_info from link)
        query = """
        query NxmModAndFile($ids: [CompositeDomainWithIdInput!]!) {
            legacyModsByDomain(ids: $ids) {
                nodes {
                    modId
                    name
                    summary
                    version
                    author
                    modCategory { categoryId name }
                    game { domainName }
                }
            }
        }
        """
        variables = {"ids": [{"gameDomain": game_domain, "modId": mod_id}]}
        try:
            resp = self._session.post(
                GRAPHQL_BASE,
                json={"query": query, "variables": variables},
                timeout=self._timeout,
            )
            self._log_response("POST", "GraphQL NxmModAndFile", resp)
            if not resp.ok:
                app_log(f"GraphQL NxmModAndFile failed: {resp.status_code}")
                return (None, None)
            data = resp.json()
            if "errors" in data:
                app_log(f"GraphQL NxmModAndFile errors: {data['errors']}")
            nodes = (
                (data.get("data") or {})
                .get("legacyModsByDomain") or {}
            ).get("nodes") or []
            if not nodes:
                return (None, None)
            n = nodes[0]
            mid = int(n.get("modId") or mod_id)
            domain = (n.get("game") or {}).get("domainName") or game_domain
            mcat = n.get("modCategory") or {}
            cat_id = int(mcat.get("categoryId") or 0) if isinstance(mcat.get("categoryId"), (int, str)) else 0
            cat_name = (mcat.get("name") or "").strip() if isinstance(mcat.get("name"), str) else ""
            mod_info = NexusModInfo(
                mod_id=mid,
                name=n.get("name", "") or "",
                summary=n.get("summary", "") or "",
                description="",
                version=n.get("version", "") or "",
                author=n.get("author", "") or "",
                category_id=cat_id,
                category_name=cat_name,
                game_id=0,
                domain_name=domain,
            )
            # Mod has no 'files' field; file_info not available from this query
            return (mod_info, None)
        except Exception as exc:
            app_log(f"GraphQL NxmModAndFile error: {exc}")
            return (None, None)

    # -- Batch update check (GraphQL v2) ------------------------------------

    _GRAPHQL_UPDATE_BATCH = 20  # legacyModsByDomain returns at most 20 nodes per request

    def graphql_mod_update_info_batch(
        self,
        ids: list[tuple[str, int]],
    ) -> dict[int, "NexusModUpdateInfo"]:
        """
        Fetch update-relevant info for a batch of mods via a single GraphQL
        request (or a small number of them for large lists).

        Parameters
        ----------
        ids : list of (game_domain, mod_id)

        Returns
        -------
        dict mapping mod_id → NexusModUpdateInfo
        """
        query = """
        query BatchUpdateCheck($ids: [CompositeDomainWithIdInput!]!) {
            legacyModsByDomain(ids: $ids) {
                nodes {
                    modId
                    name
                    version
                    updatedAt
                    viewerUpdateAvailable
                    modCategory { categoryId name }
                    modRequirements {
                        nexusRequirements {
                            nodes {
                                modId
                                modName
                                gameId
                                url
                                externalRequirement
                                notes
                            }
                        }
                    }
                }
            }
        }
        """
        results: dict[int, NexusModUpdateInfo] = {}
        batch_size = self._GRAPHQL_UPDATE_BATCH
        for i in range(0, len(ids), batch_size):
            batch = ids[i: i + batch_size]
            variables = {
                "ids": [{"gameDomain": gd, "modId": mid} for gd, mid in batch]
            }
            try:
                resp = self._session.post(
                    GRAPHQL_BASE,
                    json={"query": query, "variables": variables},
                    timeout=self._timeout,
                )
                self._log_response("POST", "GraphQL batchUpdateCheck", resp)
                if not resp.ok:
                    app_log(f"GraphQL batch update check failed: {resp.status_code}")
                    continue
                data = resp.json()
                if not isinstance(data, dict):
                    app_log("GraphQL batch update check: unexpected response format")
                    continue
                if "errors" in data:
                    app_log(f"GraphQL batch update check errors: {data['errors']}")
                nodes = (
                    (data.get("data") or {})
                    .get("legacyModsByDomain") or {}
                ).get("nodes") or []
                for n in nodes:
                    mid = int(n.get("modId", 0))
                    updated_at = None
                    raw_ts = n.get("updatedAt")
                    if raw_ts:
                        try:
                            updated_at = datetime.fromisoformat(
                                raw_ts.replace("Z", "+00:00")
                            )
                        except ValueError:
                            pass
                    vua = n.get("viewerUpdateAvailable")
                    req_nodes = (
                        (n.get("modRequirements") or {})
                        .get("nexusRequirements") or {}
                    ).get("nodes") or []
                    reqs = []
                    for rn in req_nodes:
                        try:
                            rmid = int(rn.get("modId", 0))
                        except (ValueError, TypeError):
                            rmid = 0
                        reqs.append(NexusModRequirement(
                            mod_id=rmid,
                            mod_name=rn.get("modName", "") or "",
                            game_domain=rn.get("gameId", "") or "",
                            url=rn.get("url", "") or "",
                            is_external=bool(rn.get("externalRequirement", False)),
                            notes=rn.get("notes", "") or "",
                        ))
                    mcat = n.get("modCategory") or {}
                    cat_id = int(mcat.get("categoryId") or 0) if isinstance(mcat.get("categoryId"), (int, str)) else 0
                    cat_name = (mcat.get("name") or "").strip() if isinstance(mcat.get("name"), str) else ""
                    # Mod type from legacyModsByDomain has no 'files' field; file-level checks use REST get_mod_files
                    results[mid] = NexusModUpdateInfo(
                        mod_id=mid,
                        name=n.get("name", "") or "",
                        version=n.get("version", "") or "",
                        updated_at=updated_at,
                        viewer_update_available=None if vua is None else bool(vua),
                        requirements=reqs,
                        category_id=cat_id,
                        category_name=cat_name,
                        files=[],  # Mod has no files field in GraphQL; REST used for file checks
                    )
            except Exception as exc:
                app_log(f"GraphQL batch update check error: {exc}")
        return results

    def graphql_mod_info_batch(
        self,
        ids: list[tuple[str, int]],
    ) -> "dict[int, NexusModInfo]":
        """
        Fetch full display info (name, author, version, summary, picture,
        endorsements, downloads) for a batch of mods via a single GraphQL
        request (or a small number of them for large lists).

        Uses the same ``legacyModsByDomain`` endpoint as the update-check
        batch, but requests the full field set needed for the Tracked/Endorsed
        panels — replacing N individual ``get_mod()`` REST calls with
        ceil(N/20) rate-limit-free GraphQL requests.

        Parameters
        ----------
        ids : list of (game_domain, mod_id) tuples

        Returns
        -------
        dict mapping mod_id → NexusModInfo
        """
        query = """
        query ModInfoBatch($ids: [CompositeDomainWithIdInput!]!) {
            legacyModsByDomain(ids: $ids) {
                nodes {
                    modId
                    name
                    summary
                    version
                    author
                    endorsements
                    downloads
                    pictureUrl
                    game { domainName }
                }
            }
        }
        """
        results: dict[int, NexusModInfo] = {}
        batch_size = self._GRAPHQL_UPDATE_BATCH
        for i in range(0, len(ids), batch_size):
            batch = ids[i: i + batch_size]
            variables = {
                "ids": [{"gameDomain": gd, "modId": mid} for gd, mid in batch]
            }
            try:
                resp = self._session.post(
                    GRAPHQL_BASE,
                    json={"query": query, "variables": variables},
                    timeout=self._timeout,
                )
                self._log_response("POST", "GraphQL modInfoBatch", resp)
                if not resp.ok:
                    app_log(f"GraphQL modInfoBatch failed: {resp.status_code}")
                    continue
                data = resp.json()
                if "errors" in data:
                    app_log(f"GraphQL modInfoBatch errors: {data['errors']}")
                nodes = (
                    (data.get("data") or {})
                    .get("legacyModsByDomain") or {}
                ).get("nodes") or []
                for n in nodes:
                    mid = int(n.get("modId", 0))
                    domain = (n.get("game") or {}).get("domainName", "") or ""
                    # Use the domain from the input batch if GraphQL doesn't return it
                    if not domain:
                        domain = next((gd for gd, bid in batch if bid == mid), "")
                    results[mid] = NexusModInfo(
                        mod_id=mid,
                        name=n.get("name", "") or "",
                        summary=n.get("summary", "") or "",
                        description="",
                        version=n.get("version", "") or "",
                        author=n.get("author", "") or "",
                        category_id=0,
                        game_id=0,
                        domain_name=domain,
                        picture_url=n.get("pictureUrl", "") or "",
                        endorsement_count=int(n.get("endorsements", 0) or 0),
                        downloads_total=int(n.get("downloads", 0) or 0),
                    )
            except Exception as exc:
                app_log(f"GraphQL modInfoBatch error: {exc}")
        return results

    # -- Top mods (GraphQL v2) -----------------------------------------------

    def get_top_mods(
        self, game_domain: str, count: int = 10, offset: int = 0,
        category_names: list[str] | None = None,
    ) -> list[NexusModInfo]:
        """
        Fetch the all-time most-downloaded mods for a game via the GraphQL v2 API.

        Results are sorted by total downloads descending.
        Pass category_names to restrict results to specific categories.
        """
        query = """
        query TopMods($filter: ModsFilter, $count: Int, $offset: Int) {
            mods(
                filter: $filter
                sort: [{ downloads: { direction: DESC } }]
                count: $count
                offset: $offset
            ) {
                nodes {
                    modId
                    name
                    summary
                    description
                    author
                    version
                    endorsements
                    downloads
                    pictureUrl
                }
            }
        }
        """
        variables = {
            "filter": self._build_mods_filter(game_domain, category_names),
            "count": count,
            "offset": offset,
        }
        try:
            self._refresh_oauth_if_needed()
            resp = self._session.post(
                GRAPHQL_BASE,
                json={"query": query, "variables": variables},
                timeout=self._timeout,
            )
            self._log_response("POST", "GraphQL topMods", resp)
            if not resp.ok:
                raise NexusAPIError(
                    f"GraphQL top-mods query failed: {resp.status_code}",
                    resp.status_code,
                )
            data = resp.json()
            if "errors" in data:
                raise NexusAPIError(
                    f"GraphQL error: {data['errors'][0].get('message', 'unknown')}"
                )
            nodes = data.get("data", {}).get("mods", {}).get("nodes", [])
            results: list[NexusModInfo] = []
            for n in nodes:
                results.append(NexusModInfo(
                    mod_id=n.get("modId", 0),
                    name=n.get("name", "") or "",
                    summary=n.get("summary", "") or "",
                    description=n.get("description", "") or "",
                    version=n.get("version", "") or "",
                    author=n.get("author", "") or "",
                    category_id=0,
                    game_id=0,
                    domain_name=game_domain,
                    endorsement_count=n.get("endorsements", 0) or 0,
                    downloads_total=n.get("downloads", 0) or 0,
                    picture_url=n.get("pictureUrl", "") or "",
                ))
            return results
        except NexusAPIError:
            raise
        except Exception as exc:
            raise NexusAPIError(f"GraphQL top-mods error: {exc}") from exc

    def search_mods(
        self, game_domain: str, query_text: str, count: int = 10, offset: int = 0,
        category_names: list[str] | None = None,
    ) -> list[NexusModInfo]:
        """
        Search mods by name for a game via the GraphQL v2 API.
        Pass category_names to restrict results to specific categories.
        """
        base_filter = self._build_mods_filter(game_domain, category_names)
        # Inject the name search into the filter
        if "filter" in base_filter:
            # nested AND structure — append name condition
            base_filter["filter"].append({"nameStemmed": {"value": query_text}})
        else:
            base_filter["nameStemmed"] = {"value": query_text}
        query = """
        query SearchMods($filter: ModsFilter, $count: Int, $offset: Int) {
            mods(
                filter: $filter
                sort: [{ downloads: { direction: DESC } }]
                count: $count
                offset: $offset
            ) {
                nodes {
                    modId
                    name
                    summary
                    description
                    author
                    version
                    endorsements
                    downloads
                    pictureUrl
                }
            }
        }
        """
        variables = {
            "filter": base_filter,
            "count": count,
            "offset": offset,
        }
        try:
            resp = self._session.post(
                GRAPHQL_BASE,
                json={"query": query, "variables": variables},
                timeout=self._timeout,
            )
            self._log_response("POST", "GraphQL searchMods", resp)
            if not resp.ok:
                raise NexusAPIError(
                    f"GraphQL search query failed: {resp.status_code}",
                    resp.status_code,
                )
            data = resp.json()
            if "errors" in data:
                raise NexusAPIError(
                    f"GraphQL error: {data['errors'][0].get('message', 'unknown')}"
                )
            nodes = data.get("data", {}).get("mods", {}).get("nodes", [])
            results: list[NexusModInfo] = []
            for n in nodes:
                results.append(NexusModInfo(
                    mod_id=n.get("modId", 0),
                    name=n.get("name", "") or "",
                    summary=n.get("summary", "") or "",
                    description=n.get("description", "") or "",
                    version=n.get("version", "") or "",
                    author=n.get("author", "") or "",
                    category_id=0,
                    game_id=0,
                    domain_name=game_domain,
                    endorsement_count=n.get("endorsements", 0) or 0,
                    downloads_total=n.get("downloads", 0) or 0,
                    picture_url=n.get("pictureUrl", "") or "",
                ))
            return results
        except NexusAPIError:
            raise
        except Exception as exc:
            raise NexusAPIError(f"GraphQL search error: {exc}") from exc

    # -- Tracked mods -------------------------------------------------------

    def get_tracked_mods(self) -> list[dict]:
        """Get all mods being tracked by the current user."""
        return self._get("/user/tracked_mods")

    def track_mod(self, game_domain: str, mod_id: int) -> dict:
        """Start tracking a mod."""
        resp = self._session.post(
            f"{API_BASE}/user/tracked_mods",
            json={"domain_name": game_domain, "mod_id": mod_id},
            timeout=self._timeout,
        )
        self._update_rate_limits(resp)
        self._log_response("POST", "/user/tracked_mods", resp)
        if resp.status_code == 422:
            # Already tracked — not an error
            return {"message": "Already tracked"}
        resp.raise_for_status()
        return resp.json()

    def untrack_mod(self, game_domain: str, mod_id: int) -> dict:
        """Stop tracking a mod."""
        resp = self._session.delete(
            f"{API_BASE}/user/tracked_mods",
            json={"domain_name": game_domain, "mod_id": mod_id},
            timeout=self._timeout,
        )
        self._update_rate_limits(resp)
        self._log_response("DELETE", "/user/tracked_mods", resp)
        resp.raise_for_status()
        return resp.json()

    # -- Collections (GraphQL v2) -------------------------------------------

    _COLLECTIONS_QUERY = """
    query Collections(
        $gameDomain: String!
        $count: Int
        $offset: Int
    ) {
        collectionsV2(
            filter: { gameDomain: [{ value: $gameDomain }] }
            count: $count
            offset: $offset
            sort: [{ downloads: { direction: DESC } }]
        ) {
            nodes {
                id
                slug
                name
                summary
                tileImage { url }
                user { name }
                game { domainName }
                latestPublishedRevision { modCount }
                totalDownloads
                endorsements
            }
        }
    }
    """

    @staticmethod
    def _parse_collection_nodes(nodes: list, game_domain: str) -> list["NexusCollection"]:
        results: list[NexusCollection] = []
        for n in nodes:
            tile = (n.get("tileImage") or {}).get("url", "")
            user_name = (n.get("user") or {}).get("name", "")
            rev = n.get("latestPublishedRevision") or {}
            mod_count = rev.get("modCount", 0) or 0
            domain = (n.get("game") or {}).get("domainName", game_domain) or game_domain
            results.append(NexusCollection(
                id=n.get("id", 0) or 0,
                slug=n.get("slug", "") or "",
                name=n.get("name", "") or "",
                summary=n.get("summary", "") or "",
                user_name=user_name,
                total_downloads=n.get("totalDownloads", 0) or 0,
                endorsements=n.get("endorsements", 0) or 0,
                mod_count=mod_count,
                tile_image_url=tile,
                game_domain=domain,
            ))
        return results

    def get_collections(
        self, game_domain: str, count: int = 20, offset: int = 0
    ) -> list[NexusCollection]:
        """
        Fetch collections for a game domain via GraphQL, sorted by most downloaded.
        """
        variables = {"gameDomain": game_domain, "count": count, "offset": offset}
        try:
            resp = self._session.post(
                GRAPHQL_BASE,
                json={"query": self._COLLECTIONS_QUERY, "variables": variables},
                timeout=self._timeout,
            )
            self._log_response("POST", "GraphQL get_collections", resp)
            if not resp.ok:
                app_log(f"GraphQL get_collections failed: {resp.status_code}")
                return []
            data = resp.json()
            if "errors" in data:
                app_log(f"GraphQL get_collections errors: {data['errors']}")
                return []
            nodes = (
                data.get("data", {})
                .get("collectionsV2", {})
                .get("nodes", [])
            )
            return self._parse_collection_nodes(nodes, game_domain)
        except Exception as exc:
            app_log(f"GraphQL get_collections error: {exc}")
            return []

    def search_collections(
        self, game_domain: str, query: str, count: int = 20, offset: int = 0
    ) -> list[NexusCollection]:
        """
        Search collections for a game domain by fetching a large batch and
        filtering client-side (case-insensitive substring match on name/summary).

        The Nexus GraphQL API does not expose a reliable partial-text search for
        collections, so we over-fetch and filter locally.
        """
        _FETCH_BATCH = 200
        q_lower = query.lower()
        try:
            all_cols = self.get_collections(game_domain, count=_FETCH_BATCH, offset=0)
            matched = [
                c for c in all_cols
                if q_lower in c.name.lower() or q_lower in c.summary.lower()
            ]
            return matched[offset: offset + count]
        except Exception as exc:
            app_log(f"GraphQL search_collections error: {exc}")
            return []

    _COLLECTION_DETAIL_QUERY = """
    query CollectionDetail($slug: String!, $domain: String!) {
        collection(slug: $slug, domainName: $domain) {
            name slug totalDownloads
            revisions {
                revisionNumber
                revisionStatus
            }
            latestPublishedRevision {
                revisionNumber
                modCount totalSize assetsSizeBytes
                downloadLink
                modFiles {
                    optional
                    fileId
                    file {
                        name version sizeInBytes
                        mod { modId name author }
                    }
                }
            }
        }
    }
    """

    _COLLECTION_REVISION_QUERY = """
    query CollectionRevision($slug: String!, $domain: String!, $revision: Int!) {
        collectionRevision(slug: $slug, domainName: $domain, revision: $revision) {
            revisionNumber
            modCount totalSize assetsSizeBytes
            downloadLink
            modFiles {
                optional
                fileId
                file {
                    name version sizeInBytes
                    mod { modId name author }
                }
            }
        }
    }
    """

    def get_collection_detail(
        self, slug: str, game_domain: str, revision_number: "int | None" = None
    ) -> "tuple[str, int, int, list[NexusCollectionMod], str, list[dict]]":
        """
        Fetch the full mod list for a collection revision.

        Uses a fresh session so this method is safe to call from a background
        thread without interfering with the shared session used elsewhere.

        Parameters
        ----------
        revision_number:
            If given, fetch that specific revision instead of the latest published.

        Returns
        -------
        (collection_name, total_size_bytes, mod_count, mods, download_link_path, revisions)
        where ``revisions`` is a list of dicts with ``revisionNumber`` and ``revisionStatus``
        (only populated on the initial/latest fetch, not on specific-revision fetches).
        """
        headers = dict(self._session.headers)
        try:
            # Always fetch the main collection query to get name + full revisions list
            variables = {"slug": slug, "domain": game_domain}
            resp = requests.post(
                GRAPHQL_BASE,
                json={"query": self._COLLECTION_DETAIL_QUERY, "variables": variables},
                headers=headers,
                timeout=max(self._timeout, 90),
            )
            self._log_response("POST", "GraphQL get_collection_detail", resp)
            if not resp.ok:
                app_log(f"GraphQL get_collection_detail failed: {resp.status_code}")
                return ("", 0, 0, [], "", [])
            data = resp.json()
            if "errors" in data:
                app_log(f"GraphQL get_collection_detail errors: {data['errors']}")
                return ("", 0, 0, [], "", [])
            col = data.get("data", {}).get("collection") or {}
            col_name = col.get("name", "") or ""
            revisions: list[dict] = col.get("revisions") or []
            latest_rev = col.get("latestPublishedRevision") or {}
            latest_rev_num = int(latest_rev.get("revisionNumber") or 0)

            if revision_number is not None and revision_number != latest_rev_num:
                # Fetch the specific revision's mod files separately
                rev_variables = {"slug": slug, "domain": game_domain, "revision": revision_number}
                rev_resp = requests.post(
                    GRAPHQL_BASE,
                    json={"query": self._COLLECTION_REVISION_QUERY, "variables": rev_variables},
                    headers=headers,
                    timeout=max(self._timeout, 90),
                )
                self._log_response("POST", "GraphQL get_collection_detail (specific revision)", rev_resp)
                if not rev_resp.ok:
                    app_log(f"GraphQL get_collection_detail (revision) failed: {rev_resp.status_code}")
                    return ("", 0, 0, [], "", [])
                rev_data = rev_resp.json()
                if "errors" in rev_data:
                    app_log(f"GraphQL get_collection_detail (revision) errors: {rev_data['errors']}")
                    return ("", 0, 0, [], "", [])
                rev = rev_data.get("data", {}).get("collectionRevision") or {}
            else:
                rev = latest_rev

            total_size = int(rev.get("totalSize") or 0) + int(rev.get("assetsSizeBytes") or 0)
            mod_count = int(rev.get("modCount") or 0)
            download_link_path = rev.get("downloadLink") or ""
            mods: list[NexusCollectionMod] = []
            for entry in (rev.get("modFiles") or []):
                f = entry.get("file") or {}
                mod = f.get("mod") or {}
                mods.append(NexusCollectionMod(
                    mod_id=int(mod.get("modId") or 0),
                    file_id=int(entry.get("fileId") or 0),
                    mod_name=mod.get("name", "") or "",
                    mod_author=mod.get("author", "") or "",
                    file_name=f.get("name", "") or "",
                    version=f.get("version", "") or "",
                    size_bytes=int(f.get("sizeInBytes") or 0),
                    optional=bool(entry.get("optional", False)),
                ))
            return (col_name, total_size, mod_count, mods, download_link_path, revisions)
        except Exception as exc:
            app_log(f"GraphQL get_collection_detail error: {exc}")
            return ("", 0, 0, [], "", [])

    def get_collection_archive_json(self, download_link_path: str) -> dict:
        """
        Resolve a collection ``downloadLink`` path to an archive CDN URL,
        download the ``.7z`` archive, extract ``collection.json`` from it,
        and return the parsed JSON dict.

        Parameters
        ----------
        download_link_path:
            The path returned by GraphQL, e.g.
            ``/v2/collections/49623/revisions/649988/download_link``.

        Returns
        -------
        Parsed ``collection.json`` dict, or empty dict on any failure.
        """
        import tempfile

        import py7zr
        import requests as _requests

        headers = dict(self._session.headers)
        try:
            # Step 1: resolve download-link path → CDN URI
            link_resp = _requests.get(
                f"https://api.nexusmods.com{download_link_path}",
                headers=headers,
                timeout=30,
            )
            link_resp.raise_for_status()
            cdn_url = (link_resp.json().get("download_links") or [{}])[0].get("URI", "")
            if not cdn_url:
                app_log("get_collection_archive_json: no CDN URI returned")
                return {}

            # Step 2: download the archive into a temp file
            with tempfile.NamedTemporaryFile(suffix=".7z", delete=False) as tmp:
                tmp_path = tmp.name
            try:
                dl_resp = _requests.get(cdn_url, headers=headers, stream=True, timeout=120)
                dl_resp.raise_for_status()
                with open(tmp_path, "wb") as fh:
                    for chunk in dl_resp.iter_content(chunk_size=65536):
                        if chunk:
                            fh.write(chunk)

                # Step 3: extract collection.json using a nested temp dir
                import json as _json
                import os as _os
                import tempfile as _tempfile
                with _tempfile.TemporaryDirectory() as extract_dir:
                    with py7zr.SevenZipFile(tmp_path, mode="r") as arc:
                        names = arc.getnames()
                        target = next(
                            (n for n in names if n.lstrip("/") == "collection.json"),
                            None,
                        )
                        if target is None:
                            app_log("get_collection_archive_json: collection.json not found in archive")
                            return {}
                        arc.extract(path=extract_dir, targets=[target])
                    out_path = _os.path.join(extract_dir, target.lstrip("/"))
                    if not _os.path.isfile(out_path):
                        app_log("get_collection_archive_json: collection.json not found after extract")
                        return {}
                    with open(out_path, "r", encoding="utf-8") as fh:
                        return _json.load(fh)
            finally:
                try:
                    import os
                    os.unlink(tmp_path)
                except Exception:
                    pass
        except Exception as exc:
            app_log(f"get_collection_archive_json error: {exc}")
            return {}

    def get_collection_archive_full(
        self, download_link_path: str, extract_dir: str
    ) -> dict:
        """
        Resolve a collection ``downloadLink`` path to a CDN URL, download the
        ``.7z`` archive, extract **all** contents to ``extract_dir``, and
        return the parsed ``collection.json`` dict.

        Unlike ``get_collection_archive_json`` this keeps the full archive
        contents on disk so the caller can install bundled assets from it.

        Parameters
        ----------
        download_link_path:
            The path returned by GraphQL, e.g.
            ``/v2/collections/49623/revisions/649988/download_link``.
        extract_dir:
            Directory to extract the archive into (must already exist).

        Returns
        -------
        Parsed ``collection.json`` dict, or empty dict on any failure.
        """
        import json as _json
        import os as _os
        import tempfile

        import py7zr
        import requests as _requests

        headers = dict(self._session.headers)
        try:
            link_resp = _requests.get(
                f"https://api.nexusmods.com{download_link_path}",
                headers=headers,
                timeout=30,
            )
            link_resp.raise_for_status()
            cdn_url = (link_resp.json().get("download_links") or [{}])[0].get("URI", "")
            if not cdn_url:
                app_log("get_collection_archive_full: no CDN URI returned")
                return {}

            with tempfile.NamedTemporaryFile(suffix=".7z", delete=False) as tmp:
                tmp_path = tmp.name
            try:
                dl_resp = _requests.get(cdn_url, headers=headers, stream=True, timeout=300)
                dl_resp.raise_for_status()
                with open(tmp_path, "wb") as fh:
                    for chunk in dl_resp.iter_content(chunk_size=65536):
                        if chunk:
                            fh.write(chunk)

                with py7zr.SevenZipFile(tmp_path, mode="r") as arc:
                    arc.extractall(path=extract_dir)

                cj_path = _os.path.join(extract_dir, "collection.json")
                if not _os.path.isfile(cj_path):
                    app_log("get_collection_archive_full: collection.json not found after extract")
                    return {}
                with open(cj_path, "r", encoding="utf-8") as fh:
                    return _json.load(fh)
            finally:
                try:
                    _os.unlink(tmp_path)
                except Exception:
                    pass
        except Exception as exc:
            app_log(f"get_collection_archive_full error: {exc}")
            return {}

    # -- Helpers ------------------------------------------------------------

    def _parse_mod_info(self, d: dict,
                        game_domain: str) -> NexusModInfo:
        cat_name = d.get("category_name", "") or d.get("category", "") or ""
        return NexusModInfo(
            mod_id=d.get("mod_id", 0),
            name=d.get("name", ""),
            summary=d.get("summary", ""),
            description=d.get("description", ""),
            version=d.get("version", ""),
            author=d.get("author", ""),
            category_id=d.get("category_id", 0),
            category_name=cat_name if isinstance(cat_name, str) else "",
            game_id=d.get("game_id", 0),
            domain_name=d.get("domain_name", game_domain),
            picture_url=d.get("picture_url", ""),
            endorsement_count=d.get("endorsement_count", 0),
            created_timestamp=d.get("created_timestamp", 0),
            updated_timestamp=d.get("updated_timestamp", 0),
            available=d.get("available", True),
            contains_adult_content=d.get("contains_adult_content", False),
            status=d.get("status", ""),
            uploaded_by=d.get("uploaded_by", ""),
        )
