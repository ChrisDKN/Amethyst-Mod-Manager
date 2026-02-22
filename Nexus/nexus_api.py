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
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

from Utils.config_paths import get_config_dir

log = logging.getLogger(__name__)

API_BASE = "https://api.nexusmods.com/v1"
GRAPHQL_BASE = "https://api.nexusmods.com/v2/graphql"
APP_NAME = "AmethystModManager"
APP_VERSION = "1.0.0"

# How long to wait after a 429 before retrying (seconds)
_RATE_LIMIT_BACKOFF = 2.0
_MAX_RETRIES = 3


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
    created_timestamp: int = 0
    updated_timestamp: int = 0
    available: bool = True
    contains_adult_content: bool = False
    status: str = ""
    uploaded_by: str = ""


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


# ---------------------------------------------------------------------------
# API key persistence
# ---------------------------------------------------------------------------

def _api_key_path() -> Path:
    return get_config_dir() / "nexus_api_key"


def load_api_key() -> str:
    """Load saved API key from disk, or return empty string."""
    p = _api_key_path()
    if p.is_file():
        return p.read_text().strip()
    return ""


def save_api_key(key: str) -> None:
    """Persist the API key to the config directory."""
    p = _api_key_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(key.strip())
    # Restrict permissions: owner-only read
    try:
        p.chmod(0o600)
    except OSError:
        pass


def clear_api_key() -> None:
    """Delete the stored API key."""
    p = _api_key_path()
    try:
        p.unlink(missing_ok=True)
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
        self._session = requests.Session()
        self._session.headers.update({
            "APIKEY": self._key,
            "Content-Type": "application/json",
            "Application-Name": APP_NAME,
            "Application-Version": APP_VERSION,
            "Accept": "application/json",
        })

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

    def _get(self, path: str, params: dict | None = None,
             retries: int = _MAX_RETRIES) -> Any:
        """Issue a GET request against the v1 API, with retry on 429."""
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

            if resp.status_code == 429:
                wait = _RATE_LIMIT_BACKOFF * (attempt + 1)
                log.warning("Nexus 429 rate-limited, backing off %.1fs "
                            "(attempt %d/%d)", wait, attempt + 1, retries)
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

    # -- Account ------------------------------------------------------------

    def validate(self) -> NexusUser:
        """Validate the current API key and return user info."""
        data = self._get("/users/validate")
        return NexusUser(
            user_id=data["user_id"],
            name=data["name"],
            email=data.get("email", ""),
            is_premium=data.get("is_premium", False),
            is_supporter=data.get("is_supporter", False),
            profile_url=data.get("profile_url", ""),
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

    # -- Mods ---------------------------------------------------------------

    def get_mod(self, game_domain: str, mod_id: int) -> NexusModInfo:
        """Retrieve details about a specific mod."""
        d = self._get(f"/games/{game_domain}/mods/{mod_id}")
        return NexusModInfo(
            mod_id=d["mod_id"],
            name=d["name"],
            summary=d.get("summary", ""),
            description=d.get("description", ""),
            version=d.get("version", ""),
            author=d.get("author", ""),
            category_id=d.get("category_id", 0),
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
        """Return currently trending mods for a game."""
        items = self._get(f"/games/{game_domain}/mods/trending")
        return [self._parse_mod_info(m, game_domain) for m in items]

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
        query ($modId: Int!, $gameDomainName: String!) {
            modRequirements(modId: $modId, gameDomainName: $gameDomainName) {
                nexusRequirements {
                    nodes {
                        modId
                        modName
                        gameId
                        url
                        externalRequirement
                    }
                }
            }
        }
        """
        variables = {"modId": mod_id, "gameDomainName": game_domain}
        try:
            resp = self._session.post(
                GRAPHQL_BASE,
                json={"query": query, "variables": variables},
                timeout=self._timeout,
            )
            if not resp.ok:
                log.debug("GraphQL requirements query failed: %s", resp.status_code)
                return []
            data = resp.json()
            nodes = (
                data.get("data", {})
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
                ))
            return results
        except Exception as exc:
            log.debug("GraphQL requirements query error: %s", exc)
            return []

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
        resp.raise_for_status()
        return resp.json()

    # -- Helpers ------------------------------------------------------------

    def _parse_mod_info(self, d: dict,
                        game_domain: str) -> NexusModInfo:
        return NexusModInfo(
            mod_id=d.get("mod_id", 0),
            name=d.get("name", ""),
            summary=d.get("summary", ""),
            description=d.get("description", ""),
            version=d.get("version", ""),
            author=d.get("author", ""),
            category_id=d.get("category_id", 0),
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
