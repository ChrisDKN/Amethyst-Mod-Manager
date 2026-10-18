"""
modio_api.py  (Baldur's Gate 3)

Minimal read-only client for the public mod.io REST API.  Used by BG3
update-checking, which only needs the per-mod file list and profile URL.
Requests route through ``resolve_ca_bundle()`` with a small session cache.
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from urllib.parse import urlparse

import requests

from Utils.app_log import app_log
from Utils.ca_bundle import resolve_ca_bundle

_GAME = 6715
_MAX_INLINE_RETRY_DELAY = 10.0
_LEGACY_API_ROOT = "https://api.mod.io/v1"

# Cache credentials and mod identity -> (timestamp, list[ModioFile]).
_FILES_CACHE: dict[tuple[str, str, int], tuple[float, "list[ModioFile]"]] = {}
_CACHE_TTL = 600.0  # seconds


@dataclass
class ModioFile:
    """One released modfile from the mod.io files endpoint."""

    file_id: int = 0
    version: str = ""
    date_added: int = 0
    filesize: int = 0
    filesize_uncompressed: int = 0
    filename: str = ""
    md5: str = ""
    changelog: str = ""
    mod_id: int = 0
    download_url: str = ""
    download_expires: int = 0

    @classmethod
    def from_json(cls, d: dict) -> "ModioFile":
        return cls(
            file_id=int(d.get("id") or 0),
            version=str(d.get("version") or ""),
            date_added=int(d.get("date_added") or 0),
            filesize=int(d.get("filesize") or 0),
            filesize_uncompressed=int(d.get("filesize_uncompressed") or 0),
            filename=str(d.get("filename") or ""),
            md5=str((d.get("filehash") or {}).get("md5") or "").lower(),
            changelog=str(d.get("changelog") or ""),
            mod_id=int(d.get("mod_id") or 0),
            download_url=str((d.get("download") or {}).get("binary_url") or ""),
            download_expires=int((d.get("download") or {}).get("date_expires") or 0),
        )


@dataclass
class ModioModSummary:
    """A mod's live file + page URL, from the batched mods endpoint."""

    mod_id: int = 0
    name: str = ""
    profile_url: str = ""
    latest_file_id: int = 0
    latest_version: str = ""
    latest_date_added: int = 0

    @classmethod
    def from_json(cls, d: dict) -> "ModioModSummary":
        mf = d.get("modfile") or {}
        return cls(
            mod_id=int(d.get("id") or 0),
            name=str(d.get("name") or ""),
            profile_url=str(d.get("profile_url") or ""),
            latest_file_id=int(mf.get("id") or 0),
            latest_version=str(mf.get("version") or ""),
            latest_date_added=int(mf.get("date_added") or 0),
        )


class ModioAPIError(Exception):
    """Raised on a failed mod.io API request (network or HTTP error)."""


def normalize_api_path(api_path: str) -> str:
    """Validate and normalize the per-user/per-game mod.io API path."""
    value = (api_path or "").strip().rstrip("/")
    try:
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError:
        parsed, host, port = None, "", None
    if (parsed is None or parsed.scheme != "https"
            or parsed.username or parsed.password or port is not None
            or re.fullmatch(r"[ug]-\d+\.modapi\.io", host) is None
            or parsed.path.rstrip("/") != "/v1"
            or parsed.params or parsed.query or parsed.fragment):
        raise ValueError(
            "Enter the API path shown on mod.io's API Access page "
            "(for example https://u-123.modapi.io/v1)"
        )
    return f"https://{host}/v1"


class ModioAPI:
    """Read-only mod.io client.  Requires a public read-only API key."""

    def __init__(self, api_key: str, api_path: str, timeout: float = 30.0):
        if not api_key:
            raise ValueError("mod.io API key is required")
        self._api_key = api_key.strip()
        self._api_root = normalize_api_path(api_path)
        self._use_legacy_read_api = False
        self._timeout = timeout
        self._session = requests.Session()
        self._session.verify = resolve_ca_bundle() or True
        self._session.headers.update({
            "Accept": "application/json",
            "User-Agent": "AmethystModManager",
            # BG3 enables per-platform moderation. Amethyst manages the Windows
            # game build (including through Proton), so request its live files.
            "X-Modio-Platform": "windows",
        })

    @staticmethod
    def _error_message(resp, fallback: str = "mod.io request failed") -> str:
        """Return mod.io's structured error message without discarding its ref."""
        message = ""
        error_ref = 0
        try:
            error = (resp.json() or {}).get("error") or {}
            message = str(error.get("message") or "").strip()
            error_ref = int(error.get("error_ref") or 0)
        except (AttributeError, TypeError, ValueError):
            pass
        if not message:
            message = fallback
        suffix = f"HTTP {resp.status_code}"
        if error_ref:
            suffix += f", error_ref {error_ref}"
        return f"{message} ({suffix})"

    def _get(self, url: str, params: dict, *, retries: int = 3):
        """GET with bounded retry for HTTP 429 rate limits.

        A 403 is a permanent permission failure according to mod.io and returns
        immediately. Long or rolling rate limits also return immediately so an
        interactive update check does not block for up to a minute. Returns the
        final ``requests.Response``; the caller reports unsuccessful statuses.
        """
        delay = 1.0
        for attempt in range(retries + 1):
            resp = self._session.get(url, params=params, timeout=self._timeout)
            if resp.status_code != 429 or attempt >= retries:
                return resp

            retry_after = resp.headers.get("retry-after")
            if retry_after is None:
                wait = delay
            else:
                try:
                    wait = float(retry_after)
                except (TypeError, ValueError):
                    wait = delay

            # retry-after=0 is a rolling rate limit for which mod.io recommends
            # waiting 60 seconds. Leave that retry to the user instead of
            # freezing the current update check. The same applies to long waits.
            if wait <= 0 or wait > _MAX_INLINE_RETRY_DELAY:
                return resp
            time.sleep(wait)
            delay *= 2

        return resp

    def _get_api(self, path: str, params: dict):
        """GET an API path, falling back for incompatible user API hosts.

        Some personal ``u-*.modapi.io`` paths accept the key and game lookup
        but return 403 for BG3's read-only mod/file endpoints. The legacy host
        still serves those endpoints. Probe it once after such a 403 and, only
        when that probe succeeds, retain it for the rest of this short-lived
        client session. No 403 is retried against the same endpoint.
        """
        path = "/" + path.lstrip("/")
        if self._use_legacy_read_api:
            return self._get(f"{_LEGACY_API_ROOT}{path}", params)

        resp = self._get(f"{self._api_root}{path}", params)
        if resp.status_code != 403:
            return resp

        legacy = self._get(f"{_LEGACY_API_ROOT}{path}", params)
        if legacy.status_code == 200:
            self._use_legacy_read_api = True
            app_log("mod.io: assigned API path denied BG3 reads; using compatibility endpoint.")
            return legacy
        return resp

    def get_mod_files(self, mod_id: int, *, use_cache: bool = True) -> "list[ModioFile]":
        """Return all released files for *mod_id*, newest first.

        Raises :class:`ModioAPIError` on network/HTTP failure.
        """
        if mod_id <= 0:
            raise ValueError("mod_id must be a positive integer")

        cache_key = (self._api_root, hashlib.sha256(self._api_key.encode()).hexdigest(), mod_id)
        if use_cache:
            cached = _FILES_CACHE.get(cache_key)
            if cached and (time.time() - cached[0]) < _CACHE_TTL:
                return cached[1]

        path = f"/games/{_GAME}/mods/{mod_id}/files"
        params = {
            "api_key": self._api_key,
            "_sort": "-date_added",
            "_limit": 100,
        }
        files, seen, offset, expected_total = [], set(), 0, None
        while True:
            payload = self._read_json(path, {**params, "_offset": offset})
            try:
                data = payload["data"]
                total = int(payload["result_total"])
                if (not isinstance(data, list) or total < 0
                        or (expected_total is not None and total != expected_total)
                        or offset + len(data) > total
                        or int(payload.get("result_offset", offset)) != offset
                        or int(payload.get("result_count", len(data))) != len(data)):
                    raise ValueError("Invalid page")
                expected_total = total
                page = [ModioFile.from_json(d) for d in data]
                for file in page:
                    if file.file_id <= 0 or file.file_id in seen or file.mod_id not in (0, mod_id):
                        raise ValueError("Invalid file identity")
                    seen.add(file.file_id)
                if not page and offset < total:
                    raise ValueError("Incomplete page")
            except (KeyError, AttributeError, TypeError, ValueError) as exc:
                raise ModioAPIError("Invalid or changed mod.io file catalogue; retry the request.") from exc
            files.extend(page)
            offset += len(page)
            if offset >= total:
                break
        # mod.io already sorts -date_added, but guard against API drift.
        files.sort(key=lambda f: f.date_added, reverse=True)
        _FILES_CACHE[cache_key] = (time.time(), files)
        return files

    def _read_json(self, path: str, params: dict) -> dict:
        try:
            resp = self._get_api(path, params)
        except requests.RequestException as exc:
            raise ModioAPIError(f"mod.io network error ({type(exc).__name__})") from exc
        if resp.status_code != 200:
            raise ModioAPIError(self._error_message(resp))
        try:
            payload = resp.json()
            if not isinstance(payload, dict):
                raise ValueError("Expected object")
            return payload
        except ValueError as exc:
            raise ModioAPIError("Invalid mod.io JSON response.") from exc

    def search_mods(self, *, query="", sort="-downloads_total", offset=0, limit=20):
        payload = self._read_json(f"/games/{_GAME}/mods", {
            "api_key": self._api_key, "_q": query, "_sort": sort,
            "_offset": offset, "_limit": limit,
        })
        try:
            data = payload["data"]
            total = int(payload["result_total"])
            actual_offset = int(payload["result_offset"])
            if not isinstance(data, list) or total < 0 or actual_offset != offset or len(data) > limit:
                raise ValueError()
            rows = [ModioModSummary.from_json(item) for item in data]
            if any(row.mod_id <= 0 for row in rows) or len({row.mod_id for row in rows}) != len(rows):
                raise ValueError()
            return rows, total
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            raise ModioAPIError("Invalid mod.io search response.") from exc

    def get_mod_file(self, mod_id: int, file_id: int) -> ModioFile:
        if mod_id <= 0 or file_id <= 0:
            raise ValueError("mod_id and file_id must be positive integers")
        payload = self._read_json(f"/games/{_GAME}/mods/{mod_id}/files/{file_id}",
                                  {"api_key": self._api_key})
        try:
            file = ModioFile.from_json(payload)
            if (file.file_id, file.mod_id) != (file_id, mod_id):
                raise ValueError("Wrong identity")
            return file
        except (AttributeError, TypeError, ValueError) as exc:
            raise ModioAPIError("The mod.io response identifies another mod or file.") from exc

    def get_mods_latest_batch(self, mod_ids: "list[int]") -> "dict[int, ModioModSummary]":
        """Fetch the live file + page URL for many mods in one request.

        Uses the ``id-in`` filter on the mods endpoint (which embeds the live
        ``modfile``), so N mods cost one HTTP call instead of N.  Splits into
        pages of 100.  Raises :class:`ModioAPIError` on failure.
        """
        ids = sorted({i for i in mod_ids if i > 0})
        out: dict[int, ModioModSummary] = {}
        for start in range(0, len(ids), 100):
            chunk = ids[start:start + 100]
            path = f"/games/{_GAME}/mods"
            params = {
                "api_key": self._api_key,
                "id-in": ",".join(str(i) for i in chunk),
                "_limit": 100,
            }
            payload = self._read_json(path, params)
            try:
                data = payload["data"]
                if not isinstance(data, list):
                    raise ValueError("Expected list")
                for d in data:
                    s = ModioModSummary.from_json(d)
                    if s.mod_id not in chunk or s.mod_id in out:
                        raise ValueError("Invalid mod identity")
                    out[s.mod_id] = s
            except (KeyError, AttributeError, TypeError, ValueError) as exc:
                raise ModioAPIError("Invalid mod.io mod summary response.") from exc
        return out

    def get_mod_profile_url(self, mod_id: int) -> str:
        """Return the mod's public mod.io page URL (its ``profile_url``).

        The page is slug-based (e.g. .../m/ancient-mega-pack-rel); the numeric
        id does NOT resolve client-side, so we must fetch the real URL.
        Returns "" on failure.
        """
        if mod_id <= 0:
            return ""
        path = f"/games/{_GAME}/mods/{mod_id}"
        try:
            resp = self._get_api(path, {"api_key": self._api_key})
            if resp.status_code != 200:
                return ""
            return str(resp.json().get("profile_url") or "")
        except (requests.RequestException, ValueError) as e:
            app_log(f"mod.io: profile_url lookup failed for {mod_id}: {type(e).__name__}")
            return ""

    def test_key(self) -> bool:
        """Lightweight key validation: a cheap games query that needs auth.

        Returns True if the key is accepted, False otherwise.  Never raises.
        """
        url = f"{self._api_root}/games/{_GAME}"
        try:
            resp = self._session.get(url, params={"api_key": self._api_key},
                                     timeout=self._timeout)
        except requests.RequestException as e:
            app_log(f"mod.io key test network error: {type(e).__name__}")
            return False
        return resp.status_code == 200
