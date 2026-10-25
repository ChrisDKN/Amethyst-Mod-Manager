from __future__ import annotations

from pathlib import Path
from collections import OrderedDict
import json
import time
from typing import Any
from functools import partial

from version import __version__

from .jobs import JobError, JobManager
from .batch import METHODS as BATCH_METHODS
from .custom_games import CONTRACTS as CUSTOM_GAME_METHODS
from .methods import REGISTRY, METHOD_NAMES, MUTATIONS, CANCELLABLE_JOBS, PAUSABLE_JOBS

PROTOCOL_NAME = "amethyst.headless"
PROTOCOL_VERSION = 1

SNAPSHOT_METHODS = frozenset({"games.list", "games.get", "profiles.list", "mods.list", "downloads.list"})

_NEXUS_QUERY_MODES = ("browse", "name", "author", "uploader_id", "trending")
_NEXUS_QUERY_SORTS = {
    "downloads": "downloads",
    "endorsements": "endorsements",
    "created": "createdAt",
    "updated": "updatedAt",
}
_COLLECTION_SORTS = ("downloads", "endorsements", "rating", "recent")
_NEXUS_FILTER_FIELDS = (
    "tag_includes", "tag_excludes", "languages", "hide_translations",
    "min_file_size_kb", "max_file_size_kb", "min_downloads",
    "max_downloads", "min_endorsements", "max_endorsements", "adult",
    "supports_vortex", "has_updated", "title_contains",
    "description_contains", "author_contains", "uploader_contains",
)


class ServiceError(Exception):
    def __init__(self, rpc_code: int, name: str, message: str,
                 details: dict[str, Any] | None = None):
        super().__init__(message)
        self.rpc_code = rpc_code
        self.name = name
        self.details = details or {}


class AmethystService:
    """Application facade for non-Qt clients."""

    METHODS = METHOD_NAMES

    def __init__(self, games: dict[str, Any] | None = None):
        if games is None:
            from Utils.games.discovery import discover_games
            games = discover_games()
        self._games = dict(games)
        self._read_snapshots = OrderedDict()
        self._snapshot_bytes = 0
        from .history import JobHistory
        self._history = JobHistory()
        self._jobs = JobManager(history=self._history)
        from .launch import LaunchManager
        self._launches = LaunchManager()
        from Nexus.session import NexusLoginManager, NexusSession
        self._nexus_session = NexusSession()
        self._nexus_login = NexusLoginManager(self._nexus_session)
        self._manual_collection_sessions: dict[str, Any] = {}
        from .client_workflows import ClientWorkflows
        self._client_workflows = ClientWorkflows(self)
        self._handlers = {name: partial(method.handler, self) for name, method in REGISTRY.items()}

    def disconnect(self) -> None:
        self._client_workflows.cancel_pending()
        self._jobs.cancel_client_jobs()
        for job_id in tuple(self._manual_collection_sessions):
            try:
                self._jobs.cancel(job_id)
            except KeyError:
                pass

    def close(self) -> None:
        self.disconnect()
        self._jobs.close(wait=True)
        self._client_workflows.close()
        self._launches.close()
        self._nexus_login.close()
        self._nexus_session.close()

    def call(self, method: str, params: Any = None) -> Any:
        handler = self._handlers.get(method)
        if handler is None:
            raise ServiceError(
                -32601,
                "method_not_found",
                f"Unknown method: {method}",
                {"method": method},
            )
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise ServiceError(
                -32602,
                "invalid_params",
                "Parameters must be a JSON object.",
            )
        cache_key = None
        if method in SNAPSHOT_METHODS:
            params = dict(params)
            allow_cached = params.pop("allow_cached", True)
            if not isinstance(allow_cached, bool):
                raise ServiceError(-32602, "invalid_params", "'allow_cached' must be boolean.")
            cache_key = (method, json.dumps(params, sort_keys=True, ensure_ascii=True))
            active = self._jobs.active()
            if active is not None and allow_cached and cache_key in self._read_snapshots:
                raw, captured_at, captured_clock = self._read_snapshots[cache_key]
                self._read_snapshots.move_to_end(cache_key)
                return {**json.loads(raw), "snapshot": {
                    "cached": True, "captured_at": captured_at,
                    "age_seconds": max(0, time.monotonic() - captured_clock),
                    "active_job_id": active["id"],
                }}
        from Utils.profiles.state import read_only_profile_state
        with read_only_profile_state():
            result = handler(params)
        if cache_key is not None:
            from .jobs import _utc_now
            captured_at = _utc_now()
            raw = json.dumps(result, ensure_ascii=True)
            previous = self._read_snapshots.pop(cache_key, None)
            if previous:
                self._snapshot_bytes -= len(previous[0])
            if len(raw) <= 32 * 1024 * 1024:
                self._read_snapshots[cache_key] = (raw, captured_at, time.monotonic())
                self._snapshot_bytes += len(raw)
                while len(self._read_snapshots) > 64 or self._snapshot_bytes > 32 * 1024 * 1024:
                    _, removed = self._read_snapshots.popitem(last=False)
                    self._snapshot_bytes -= len(removed[0])
            result = {**result, "snapshot": {"cached": False, "captured_at": captured_at,
                                            "age_seconds": 0, "active_job_id": None}}
        return result

    @staticmethod
    def _check_params(params: dict[str, Any], allowed: set[str],
                      required: set[str] | frozenset[str] = frozenset()) -> None:
        unknown = sorted(set(params) - allowed)
        missing = sorted(required - set(params))
        if unknown or missing:
            details: dict[str, Any] = {}
            if unknown:
                details["unknown"] = unknown
            if missing:
                details["missing"] = missing
            raise ServiceError(
                -32602,
                "invalid_params",
                "Invalid method parameters.",
                details,
            )

    @staticmethod
    def _required_text(params: dict[str, Any], name: str) -> str:
        value = params.get(name)
        if not isinstance(value, str) or not value.strip():
            raise ServiceError(
                -32602,
                "invalid_params",
                f"'{name}' must be a non-empty string.",
                {"parameter": name},
            )
        return value.strip()

    @staticmethod
    def _collection_manual_archives(
        params: dict[str, Any],
    ) -> dict[int, Path] | None:
        if "manual_archives" not in params:
            return None
        raw_archives = params["manual_archives"]
        if not isinstance(raw_archives, dict) or len(raw_archives) > 4096:
            raise ServiceError(
                -32602, "invalid_params",
                "'manual_archives' must be a file-ID to absolute-path object.",
                {"parameter": "manual_archives"},
            )
        manual_archives = {}
        for key, value in raw_archives.items():
            if (not isinstance(key, str) or not key.isascii()
                    or not key.isdecimal() or key.startswith("0")
                    or len(key) > 20 or not isinstance(value, str)):
                raise ServiceError(
                    -32602, "invalid_params",
                    "'manual_archives' has an invalid file ID or path.",
                    {"parameter": "manual_archives"},
                )
            path = Path(value).expanduser()
            if not path.is_absolute() or path.is_symlink():
                raise ServiceError(
                    -32602, "invalid_params",
                    "A manual archive must be an absolute, non-symlink path.",
                    {"parameter": f"manual_archives.{key}"},
                )
            manual_archives[int(key)] = path
        return manual_archives

    @staticmethod
    def _manual_archive_options(paths: dict[int, Path] | None) -> dict[str, str] | None:
        return ({str(key): str(path) for key, path in paths.items()}
                if paths is not None else None)

    @staticmethod
    def _collection_manual_session(
        params: dict[str, Any], control: Any,
        archives: dict[int, Path] | None,
    ) -> Any | None:
        interactive = params.get("manual_interactive", False)
        if not isinstance(interactive, bool) or (interactive and archives is not None):
            raise ServiceError(
                -32602, "invalid_params",
                "'manual_interactive' must be a boolean and cannot be combined "
                "with 'manual_archives'.",
                {"parameter": "manual_interactive"},
            )
        if not interactive:
            return None
        from .manual_collections import ManualCollectionSession
        return ManualCollectionSession(control)

    def _bind_manual_collection(self, session: Any | None, job: dict[str, Any]) -> None:
        if session is not None:
            for old_id in tuple(self._manual_collection_sessions):
                try:
                    self._jobs.get(old_id)
                except KeyError:
                    del self._manual_collection_sessions[old_id]
            self._manual_collection_sessions[job["id"]] = session
            session.bind(self._jobs, job["id"])

    @staticmethod
    def _bain_selection(params: dict[str, Any], installer_policy: str) -> list[str] | None:
        if "bain_selected" not in params:
            return None
        value = params["bain_selected"]
        if (not isinstance(value, list) or len(value) > 256
                or any(not isinstance(name, str) or not name for name in value)
                or len(set(value)) != len(value)):
            raise ServiceError(-32602, "invalid_params",
                               "'bain_selected' must be a list of unique subpackage names.",
                               {"parameter": "bain_selected"})
        if installer_policy == "defaults":
            raise ServiceError(-32602, "invalid_params",
                               "Explicit BAIN choices cannot be combined with defaults.",
                               {"parameter": "installer_policy"})
        return list(value)

    @staticmethod
    def _fomod_selection(params: dict[str, Any], installer_policy: str) -> dict | None:
        if "fomod_selections" not in params:
            return None
        value = params["fomod_selections"]
        if (installer_policy == "defaults" or "bain_selected" in params
                or not isinstance(value, dict) or len(value) > 256):
            raise ServiceError(-32602, "invalid_params",
                               "Explicit FOMOD choices must be supplied alone.",
                               {"parameter": "fomod_selections"})
        for step, groups in value.items():
            if (not isinstance(step, str) or not step.isdecimal()
                    or not isinstance(groups, dict) or len(groups) > 64):
                raise ServiceError(-32602, "invalid_params",
                                   "FOMOD steps must map indexes to groups.",
                                   {"parameter": "fomod_selections"})
            for group, names in groups.items():
                if (not isinstance(group, str) or not group
                        or not isinstance(names, list) or len(names) > 256
                        or any(not isinstance(name, str) or not name for name in names)
                        or len(set(names)) != len(names)):
                    raise ServiceError(-32602, "invalid_params",
                                       "FOMOD groups must list unique option names.",
                                       {"parameter": "fomod_selections"})
        return {step: {group: list(names) for group, names in groups.items()}
                for step, groups in value.items()}

    @staticmethod
    def _bundle_selection(params: dict[str, Any], installer_policy: str) -> list[str] | None:
        if "bundle_selected" not in params:
            return None
        value = params["bundle_selected"]
        if (installer_policy == "defaults" or "bain_selected" in params
                or "fomod_selections" in params or not isinstance(value, list)
                or len(value) > 1024
                or any(not isinstance(folder, str) or not folder for folder in value)
                or len(set(value)) != len(value)):
            raise ServiceError(-32602, "invalid_params",
                               "Explicit bundle choices must list unique folders alone.",
                               {"parameter": "bundle_selected"})
        return list(value)

    @staticmethod
    def _positive_integer(params: dict[str, Any], name: str) -> int:
        value = params.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ServiceError(
                -32602,
                "invalid_params",
                f"'{name}' must be a positive integer.",
                {"parameter": name},
            )
        return value

    @staticmethod
    def _archive_id(value: Any) -> str:
        if not isinstance(value, str):
            value = ""
        digest = value.removeprefix("archive_")
        if (not value.startswith("archive_") or len(digest) != 64
                or any(char not in "0123456789abcdef" for char in digest)):
            raise ServiceError(
                -32602,
                "invalid_params",
                "'archive_id' must be an ID returned by downloads.list.",
                {"parameter": "archive_id"},
            )
        return value

    @classmethod
    def _archive_ids(cls, value: Any) -> list[str]:
        if (not isinstance(value, list) or not 1 <= len(value) <= 64
                or any(not isinstance(item, str) for item in value)):
            raise ServiceError(-32602, "invalid_params",
                               "'archive_ids' must contain 1 to 64 archive IDs.",
                               {"parameter": "archive_ids"})
        archive_ids = [cls._archive_id(item) for item in value]
        if len(set(archive_ids)) != len(archive_ids):
            raise ServiceError(-32602, "invalid_params",
                               "'archive_ids' must be distinct.",
                               {"parameter": "archive_ids"})
        return sorted(archive_ids)

    @staticmethod
    def _download_location_id(value: Any) -> str:
        if not isinstance(value, str):
            value = ""
        digest = value.removeprefix("location_")
        if (not value.startswith("location_") or len(digest) != 64
                or any(char not in "0123456789abcdef" for char in digest)):
            raise ServiceError(
                -32602, "invalid_params",
                "'destination_location_id' must come from downloads.list.",
                {"parameter": "destination_location_id"},
            )
        return value

    @staticmethod
    def _integer_range(
        value: Any, parameter: str, minimum: int, maximum: int,
    ) -> int:
        if (isinstance(value, bool) or not isinstance(value, int)
                or not minimum <= value <= maximum):
            raise ServiceError(
                -32602,
                "invalid_params",
                f"'{parameter}' must be an integer from {minimum} to {maximum}.",
                {"parameter": parameter},
            )
        return value

    @staticmethod
    def _text_array(value: Any, parameter: str) -> list[str]:
        if not isinstance(value, list) or len(value) > 64:
            raise ServiceError(
                -32602,
                "invalid_params",
                f"'{parameter}' must be an array of at most 64 strings.",
                {"parameter": parameter},
            )
        result: list[str] = []
        seen: set[str] = set()
        for index, item in enumerate(value):
            name = f"{parameter}[{index}]"
            if (not isinstance(item, str) or not item.strip()
                    or len(item.strip()) > 128):
                raise ServiceError(
                    -32602,
                    "invalid_params",
                    f"'{name}' must be a string from 1 to 128 characters.",
                    {"parameter": name},
                )
            text = item.strip()
            folded = text.casefold()
            if folded in seen:
                raise ServiceError(
                    -32602,
                    "invalid_params",
                    f"'{name}' duplicates an earlier value.",
                    {"parameter": name, "value": text},
                )
            seen.add(folded)
            result.append(text)
        return result

    @staticmethod
    def _boundary_paths(value: Any) -> list[str]:
        if not isinstance(value, list) or not 1 <= len(value) <= 4096:
            raise ServiceError(
                -32602,
                "invalid_params",
                "'paths' must be an array of 1 to 4096 relative paths.",
                {"parameter": "paths"},
            )
        paths: list[str] = []
        seen: set[str] = set()
        for index, item in enumerate(value):
            parameter = f"paths[{index}]"
            parts = item.split("/") if isinstance(item, str) else []
            if (not isinstance(item, str) or not item
                    or len(item) > 4096 or item.startswith("/")
                    or any(part in ("", ".", "..") for part in parts)
                    or "\x00" in item):
                raise ServiceError(
                    -32602,
                    "invalid_params",
                    f"'{parameter}' must be a safe relative inventory path.",
                    {"parameter": parameter},
                )
            if item in seen:
                raise ServiceError(
                    -32602,
                    "invalid_params",
                    f"'{parameter}' duplicates an earlier path.",
                    {"parameter": parameter, "path": item},
                )
            seen.add(item)
            paths.append(item)
        return paths

    @staticmethod
    def _boundary_revision(value: Any) -> str:
        if (not isinstance(value, str) or not value
                or len(value) > 256 or any(char.isspace() for char in value)):
            raise ServiceError(
                -32602,
                "invalid_params",
                "'revision' must be the opaque revision returned by "
                "boundaries.list.start.",
                {"parameter": "revision"},
            )
        return value

    def _nexus_filters(self, value: Any) -> tuple[Any, dict[str, Any]]:
        if not isinstance(value, dict):
            raise ServiceError(
                -32602,
                "invalid_params",
                "'filters' must be a JSON object.",
                {"parameter": "filters"},
            )
        self._check_params(value, set(_NEXUS_FILTER_FIELDS))
        normalized: dict[str, Any] = {}

        for name in ("tag_includes", "tag_excludes", "languages"):
            if name in value:
                normalized[name] = self._text_array(
                    value[name], f"filters.{name}")

        included = {
            item.casefold() for item in normalized.get("tag_includes", [])}
        excluded = {
            item.casefold() for item in normalized.get("tag_excludes", [])}
        overlap = sorted(included.intersection(excluded))
        if overlap:
            raise ServiceError(
                -32602,
                "invalid_params",
                "A Nexus tag cannot be both included and excluded.",
                {"parameter": "filters", "conflicting_tags": overlap},
            )

        for name in ("hide_translations", "has_updated"):
            if name not in value:
                continue
            if not isinstance(value[name], bool):
                raise ServiceError(
                    -32602,
                    "invalid_params",
                    f"'filters.{name}' must be a boolean.",
                    {"parameter": f"filters.{name}"},
                )
            normalized[name] = value[name]

        for name in ("adult", "supports_vortex"):
            if name not in value:
                continue
            if value[name] is not None and not isinstance(value[name], bool):
                raise ServiceError(
                    -32602,
                    "invalid_params",
                    f"'filters.{name}' must be a boolean or null.",
                    {"parameter": f"filters.{name}"},
                )
            normalized[name] = value[name]

        number_fields = (
            "min_file_size_kb", "max_file_size_kb", "min_downloads",
            "max_downloads", "min_endorsements", "max_endorsements",
        )
        for name in number_fields:
            if name in value:
                normalized[name] = self._integer_range(
                    value[name], f"filters.{name}",
                    0, 2_147_483_647)
        for minimum, maximum in (
            ("min_file_size_kb", "max_file_size_kb"),
            ("min_downloads", "max_downloads"),
            ("min_endorsements", "max_endorsements"),
        ):
            if (minimum in normalized and maximum in normalized
                    and normalized[minimum] > normalized[maximum]):
                raise ServiceError(
                    -32602,
                    "invalid_params",
                    f"'filters.{minimum}' cannot exceed 'filters.{maximum}'.",
                    {"parameter": f"filters.{minimum}"},
                )

        text_fields = (
            "title_contains", "description_contains", "author_contains",
            "uploader_contains",
        )
        for name in text_fields:
            if name not in value:
                continue
            text = value[name]
            if (not isinstance(text, str)
                    or not 2 <= len(text.strip()) <= 512):
                raise ServiceError(
                    -32602,
                    "invalid_params",
                    f"'filters.{name}' must contain 2 to 512 characters.",
                    {"parameter": f"filters.{name}"},
                )
            normalized[name] = text.strip()

        from Nexus.nexus_api import NexusSearchFilters
        search_filters = NexusSearchFilters(
            tag_includes=tuple(normalized.get("tag_includes", ())),
            tag_excludes=tuple(normalized.get("tag_excludes", ())),
            languages=tuple(normalized.get("languages", ())),
            hide_translations=bool(
                normalized.get("hide_translations", False)),
            min_file_size_kb=normalized.get("min_file_size_kb"),
            max_file_size_kb=normalized.get("max_file_size_kb"),
            min_downloads=normalized.get("min_downloads"),
            max_downloads=normalized.get("max_downloads"),
            min_endorsements=normalized.get("min_endorsements"),
            max_endorsements=normalized.get("max_endorsements"),
            adult=normalized.get("adult"),
            supports_vortex=normalized.get("supports_vortex"),
            has_updated=bool(normalized.get("has_updated", False)),
            title_contains=normalized.get("title_contains"),
            description_contains=normalized.get("description_contains"),
            author_contains=normalized.get("author_contains"),
            uploader_contains=normalized.get("uploader_contains"),
        )
        return search_filters, normalized

    @staticmethod
    def _mod_name(value: Any, parameter: str) -> str:
        if (not isinstance(value, str) or not value
                or value in (".", "..")
                or Path(value).name != value
                or any(char in value for char in "\x00\r\n")):
            raise ServiceError(
                -32602,
                "invalid_params",
                f"'{parameter}' must be a valid mod name.",
                {"parameter": parameter},
            )
        return value

    @staticmethod
    def _profile_name(value: Any, parameter: str) -> str:
        if (not isinstance(value, str) or not value.strip()
                or value != value.strip()
                or value in (".", "..")
                or Path(value).name != value
                or "\\" in value
                or any(char in value for char in "\x00\r\n")):
            raise ServiceError(
                -32602,
                "invalid_params",
                f"'{parameter}' must be a valid profile name.",
                {"parameter": parameter},
            )
        return value

    @staticmethod
    def _plugin_name(value: Any, parameter: str) -> str:
        if (not isinstance(value, str) or not value
                or value != value.strip() or len(value) > 255
                or value in (".", "..") or Path(value).name != value
                or "\\" in value
                or any(char in value for char in "\x00\r\n")):
            raise ServiceError(
                -32602,
                "invalid_params",
                f"'{parameter}' must be a valid plugin filename.",
                {"parameter": parameter},
            )
        return value

    @staticmethod
    def _launch_summary(launch: dict[str, Any]) -> dict[str, Any]:
        summary = dict(launch)
        logs = summary.pop("logs", [])
        summary["log_count"] = len(logs)
        return summary

    def _handshake(self, params: dict[str, Any]) -> dict[str, Any]:
        from Utils.mods.notes import MAX_CHANGES, MAX_NOTE_LENGTH, MAX_NOTE_PAYLOAD

        self._check_params(params, {"protocol_version", "client"})
        requested = params.get("protocol_version")
        if (requested is not None
                and (isinstance(requested, bool)
                     or not isinstance(requested, int))):
            raise ServiceError(
                -32602,
                "invalid_params",
                "'protocol_version' must be an integer.",
                {"parameter": "protocol_version"},
            )
        if requested is not None and requested != PROTOCOL_VERSION:
            raise ServiceError(
                -32010,
                "unsupported_protocol_version",
                f"Protocol version {requested} is not supported.",
                {"minimum": PROTOCOL_VERSION, "maximum": PROTOCOL_VERSION},
            )
        client = params.get("client")
        if client is not None and not isinstance(client, dict):
            raise ServiceError(
                -32602,
                "invalid_params",
                "'client' must be a JSON object.",
                {"parameter": "client"},
            )
        return {
            "protocol": {
                "name": PROTOCOL_NAME,
                "version": PROTOCOL_VERSION,
                "minimum_version": PROTOCOL_VERSION,
                "maximum_version": PROTOCOL_VERSION,
                "rpc": "2.0",
                "framing": "ndjson",
            },
            "application": {
                "name": "Amethyst Mod Manager",
                "version": __version__,
            },
            "capabilities": {
                "installer_sessions": {"open": "installers.sessions.open.start", "type": "fomod",
                                       "dynamic_choices": True, "images": True, "revision_checked": True,
                                       "saved_drafts": True, "resume": "installers.sessions.resume.start",
                                       "maximum_sessions": 8, "idle_expiry_seconds": 1800},
                "installer_requests": {"get": "installers.requests.get", "update": "installers.requests.update",
                                       "submit": "installers.requests.submit", "types": ["layout", "choice", "fomod", "bain"],
                                       "collections": True},
                "nexus_manual_download": {"method": "nexus.download.manual.start", "browser_url": True,
                                          "exact_file": True, "returns_archive_id": True, "installs": False},
                "thunderstore_install": {"plan": "thunderstore.install.plan.start", "apply": "thunderstore.install.start",
                                         "dependencies_first": True, "revision_checked": True,
                                         "dependency_selection": True, "partial_results": True},
                "schemas": {"method": "system.schema.get", "version": 1,
                            "dialect": "https://json-schema.org/draft/2020-12/schema"},
                "cached_reads": {"methods": sorted(SNAPSHOT_METHODS), "opt_out": "allow_cached:false",
                                 "first_read": "requires_idle", "maximum_bytes": 33554432},
                "job_history": {"persistent": True, "retention": self._history.retention,
                                "get": "jobs.history.get", "list": "jobs.history.list",
                                "recovery": "jobs.recovery.list", "events_persisted": True,
                                "events_method": "jobs.history.events", "event_retention": 1000,
                                "events_storage_error": self._history.events_error,
                                "storage_error": self._history.error},
                "read_only": False,
                "events": True,
                "event_delivery": "poll",
                "input_waits": {"state": "waiting_for_input", "request_field": "waiting_for",
                                "client_dependency_field": "requires_client", "cancel_on_disconnect": True},
                "cancellation": True,
                "game_version": {
                    "read_only": True,
                    "profile_aware": True,
                    "source": "executable_resources",
                },
                "custom_games": {"definitions": True, "presets": True, "share_codes": True,
                                 "revision_checked": True, "reload_after_save": True},
                "plugin_repositories": {"versions": True, "updates": True, "quick_update": True,
                                        "actions": True, "dependencies": True, "pinned_collections": True,
                                        "game_handlers": True, "protocols": True,
                                        "source_prefix": "plugin:", "maximum_dependency_artifacts": 256},
                "batching": {
                    "maximum_items": 256,
                    "methods": dict(BATCH_METHODS),
                    "shared_conflict_snapshot": True,
                    "layout_plan_apply": True,
                    "shared_property_inventory": True,
                    "partial_results": "error.details.batch",
                    "cancellation": "between_items_and_supported_active_operations",
                },
                "cancellable_jobs": list(CANCELLABLE_JOBS),
                "pausable_jobs": list(PAUSABLE_JOBS),
                "mutations": list(MUTATIONS),
                "wine_overrides": {
                    "prefix_inventory": True, "revision_checked": True,
                    "persistent_removals": True, "reset": True,
                    "apply": True, "running_prefix_guard": True,
                },
                "plugin_userlist": {
                    "actions": ["set_rules", "set_group", "remove_plugins", "save_group",
                                "remove_group", "flip_rule", "clear", "reset"],
                    "scope": "profile", "revision_checked": True,
                    "game_fallback": True, "preserves_metadata": True,
                    "cycle_diagnostics": "userlist_only", "maximum_file_bytes": 4194304,
                    "maximum_names": 4096,
                },
                "prefix_health": {
                    "report": True, "read_only": True, "repair": True, "fix_all": True,
                    "revision_checked": True, "live_verification": True,
                    "shared_desktop_dispatch": True, "running_prefix_guard": True,
                    "strict_prefix": True, "repair_scope": "reported_checks",
                    "cancellation": "between_checks_and_repairs",
                },
                "wine_dependencies": {
                    "report": True, "read_only": True, "install": True,
                    "revision_checked": True, "optional_components": True,
                    "shared_desktop_dispatch": True, "running_prefix_guard": True,
                    "strict_prefix": True, "verification": ["live", "installer_marker"],
                    "cancellation": "between_checks_and_installers",
                },
                "game_registry": {
                    "inspect": True, "write": True, "force": True,
                    "revision_checked": True, "verified_views": True,
                    "shared_desktop_execution": True, "running_prefix_guard": True,
                    "cancellation": "process_group_and_prefix_cleanup",
                },
                "tool_catalogue": {
                    "built_in": True, "profile_aware": True, "read_only": True,
                    "synthesis": {"acquisition": True, "inspection": True, "installation": True,
                                  "prefix_setup": True, "pipeline_execution": False,
                                  "revision_checked": True, "shared_desktop_installation": True,
                                  "shared_desktop_setup": True},
                    "wrye_bash": {"acquisition": True, "inspection": True, "installation": True,
                                  "launch": True, "patch_capture": True, "restore_after_run": True,
                                  "revision_checked": True, "shared_desktop_execution": True},
                    "lod": {"acquisition": True, "inspection": True, "installation": True,
                            "revision_checked": True, "shared_desktop_installation": True,
                            "generation": True, "output_import": True, "restore_after_run": True},
                    "xedit": {"acquisition": True, "inspection": True, "installation": True,
                              "launch": True, "quick_auto_clean": True, "revision_checked": True},
                    "smapi": {"acquisition": True, "inspection": True, "installation": True,
                              "install_modes": ["game", "root", "mod"], "revision_checked": True},
                    "four_gb_patch": {"inspect": True, "apply": True, "restore": True,
                                      "revision_checked": True, "shared_desktop_transaction": True},
                    "reshade_options": True, "reshade_dll_acquisition": True,
                    "reshade_shader_acquisition": True,
                    "reshade_inspection": True, "reshade_installation": True,
                    "reshade_workflow": True, "reshade_shader_replacement": True,
                    "script_extender_sources": True, "script_extender_acquisition": True,
                    "script_extender_inspection": True, "script_extender_installation": True,
                    "external_plugins": False,
                    "archive_packing": {"scan": True, "plan": True, "pack": True, "unpack": True,
                                        "tracked_unpack": True, "selected_archives": True,
                                        "revision_checked": True, "shared_desktop_execution": True},
                },
                "profile_share": {
                    "format": "AMMCODE1", "sources": ["nexus", "thunderstore", "plugin"],
                    "inspect": True, "import": True, "profile_groups": True,
                    "maximum_code_length": 8388608, "maximum_manifest_bytes": 16777216,
                    "upload": False, "local_files": False,
                },
                "profile_export": {
                    "format": "amethyst", "sources": ["nexus", "thunderstore", "plugin", "direct", "browse", "manual", "bundle", "ignore"],
                    "preview": True, "saved_settings": True, "profile_groups": True,
                    "file_edits": True, "revision_checked": True, "atomic": True,
                    "overwrite": "explicit", "maximum_changes": 4096,
                    "maximum_changes_bytes": 8388608,
                    "cancellation": "file_chunks_and_between_patch_files",
                },
                "mod_notes": {
                    "scope": "profile", "bulk": True, "append": True,
                    "remove": True, "revision_checked": True,
                    "maximum_note_length": MAX_NOTE_LENGTH, "maximum_changes": MAX_CHANGES,
                    "maximum_payload_bytes": MAX_NOTE_PAYLOAD,
                },
                "mod_properties": {
                    "version_label": True, "root_folder": True,
                    "update_ignores": ["nexus", "thunderstore", "plugin"],
                    "revision_checked": True, "owner_resolution": True,
                    "routing_sync": "affected_profiles",
                },
                "mod_versions": {
                    "sources": ["nexus", "thunderstore", "modio", "plugin"], "catalog": True,
                    "plugin_source_prefix": "plugin:",
                    "installed_highlights": True, "name_matched_recommendations": True,
                    "revision_checked": True, "owner_resolution": True,
                    "cached_archives": True, "nxm_downloads": True,
                    "installer_choices": True, "routing_sync": "affected_profiles",
                    "previous_version_history": ["nexus", "thunderstore"],
                    "plugin_sync": "affected_profiles",
                    "thunderstore_dependencies": ["reject", "ignore"],
                },
                "thunderstore": {
                    "version_catalog": True, "downloads": True,
                    "authentication": False, "community_checked": True,
                    "archive_identity": True, "installation_metadata": True,
                    "integrity": "size_and_manifest",
                },
                "quick_update": {
                    "sources": ["nexus", "plugins"], "plan": True, "revision_checked": True,
                    "parallel_download_sources": ["nexus"],
                    "group_selection": True, "cached_archives": True, "parallel_downloads": True,
                    "download_only": True, "installer_choices": True, "partial_results": True,
                    "rollback": "per_mod", "cancellation": "acquisition_and_between_replacements",
                },
                "mod_creation": {
                    "empty": True, "version": "1.0", "revision_checked": True,
                    "positions": ["top", "bottom", "before", "after"],
                    "group_anchor_policy": "outside_group", "enabled": True,
                    "root_folder": True, "profile_groups": True, "rollback": True,
                },
                "nexus_actions": {
                    "actions": ["endorse", "abstain", "track", "untrack"],
                    "live_state": True, "revision_checked": True, "group_selection": True,
                    "identity_deduplication": True, "remote_verification": True,
                    "metadata_scope": "selected_owners", "partial_results": True,
                    "cancellation": "between_targets",
                },
                "launcher_providers": {"discovery": True, "headless": True,
                                       "shared_settings": True, "managed_runs": True},
                "launch_settings": {
                    "revision_checked": True, "launcher_mode": True,
                    "arguments": True, "launch_options": True, "wayland": True,
                    "lsfg": True, "mangohud": True, "game_toggles": True,
                },
                "downloads": {
                    "interrupted": {"inventory": True, "revision_checked": True,
                                    "resume": True, "discard": True},
                },
                "installation": {
                    "source": "download_inventory",
                    "local_archive_paths": True,
                    "content_prefix": True,
                    "inspection": True,
                    "installer_policies": ["reject", "defaults", "verbatim"],
                    "bain_selections": True,
                    "fomod_selections": True,
                    "bundle_selections": True,
                    "multi_mod_archives": True,
                    "multi_mod_selection": True,
                    "multi_mod_names": True,
                    "multi_mod_policies": ["fail", "replace", "skip"],
                    "existing_mod_policies": ["fail", "replace"],
                    "interactive": True,
                },
                "mods": {
                    "requirements": {
                        "inventory": True,
                        "satisfaction": "enabled_profile_mods",
                        "source": "cached_nexus_metadata",
                        "disabled_providers": True,
                        "required_by": True,
                        "profile_ignores": True,
                        "individual_ignores": True,
                        "revisioned_ignores": True,
                    },
                    "groups": {
                        "inventory": True,
                        "revisioned_edits": True,
                        "cosmetic_headers": True,
                        "actions": ["create", "add", "ungroup", "promote", "rename", "collapse", "dissolve"],
                        "group_aware_moves": True,
                    },
                    "update_check": {
                        "sources": ["nexus", "thunderstore", "modio", "plugins"],
                        "selection": True,
                        "persists_metadata": True,
                    },
                    "reinstall": {
                        "source": "recorded_archive",
                        "archive_selection": True,
                        "preserves_profile_state": True,
                        "profile_group_routing": True,
                        "requires_confirmation": True,
                    },
                },
                "collections": {
                    "installed_inventory": True,
                    "profile_filter": True,
                    "state_revision": True,
                    "installation": {
                        "new_profile": True,
                        "local_import": True,
                        "r2x_import": True,
                        "premium_automatic": True,
                        "optional_selection": True,
                        "game_version_confirmation": True,
                        "manual": True,
                        "manual_archives": True,
                        "download_only": True,
                        "resume": True,
                        "update": True,
                        "append": True,
                        "grouping": True,
                        "thunderstore_entries": True,
                        "thunderstore_downloads": True,
                        "plugin_entries": True,
                    },
                    "appended_mod_activation": True,
                    "appended_removal_plan": True,
                    "load_order_reset": True,
                    "group_load_order_reset": True,
                    "catalogue": {
                        "search": True,
                        "revision_detail": True,
                        "manifest": True,
                        "sorts": list(_COLLECTION_SORTS),
                        "maximum_page_size": 50,
                    },
                },
                "nexus": {
                    "authentication": "oauth_pkce",
                    "browser_open_optional": True,
                    "manual_code_fallback": True,
                    "credentials_exposed": False,
                    "download_modes": ["premium", "nxm"],
                    "catalogue": {
                        "query_modes": list(_NEXUS_QUERY_MODES),
                        "sorts": list(_NEXUS_QUERY_SORTS),
                        "maximum_page_size": 50,
                        "filters": list(_NEXUS_FILTER_FIELDS),
                    },
                },
                "modio": {
                    "game": "baldurs_gate_3",
                    "credentials_management": True,
                    "credentials_exposed": False,
                    "update_check": True,
                    "version_catalog": True, "downloads": True,
                    "platform": "windows", "archive_identity": True,
                    "installation_metadata": True, "integrity": "size_and_md5",
                },
                "launch": {
                    "cross_process_guard": True,
                    "survives_disconnect": True,
                    "active_inventory": "launch.active.list",
                    "requires_deployed_profile": True,
                    "deploy_automatically": False,
                    "lifecycle_polling": True,
                    "stop": True,
                    "arbitrary_commands": False,
                },
                "plugins": {
                    "inventory": "job",
                    "order": "first_loaded_first",
                    "activation": True,
                    "constrained_block_move": True,
                    "automatic_sort": self._loot_available(),
                    "sorter": "loot",
                    "metadata_refresh": self._loot_available(),
                },
                "boundaries": {
                    "inventory": "job",
                    "types": ["overwrite", "root_folder"],
                    "default_page_size": 500,
                    "maximum_page_size": 5000,
                    "revisioned": True,
                    "move_to_mod": True,
                    "create_mod": True,
                    "remove": True,
                    "mutation_requires_revision": True,
                },
                "maximum_event_wait_ms": 30000,
                "methods": list(self.METHODS),
            },
        }

    def _ping(self, params: dict[str, Any]) -> dict[str, Any]:
        self._check_params(params, set())
        return {
            "ok": True,
            "protocol_version": PROTOCOL_VERSION,
            "application_version": __version__,
        }

    def _get_schema(self, params):
        self._check_params(params, {"method"})
        method = self._required_text(params, "method") if "method" in params else None
        if method is not None and method not in self._handlers:
            raise ServiceError(-32601, "method_not_found", f"Unknown method: {method}")
        from .schemas import catalogue
        return catalogue(method)

    def _appearance(self, params, operation):
        from .appearance import get_appearance, list_themes, set_appearance
        if operation == "set":
            self._check_params(params, {"theme_id", "confirm"}, {"theme_id", "confirm"})
            if params["confirm"] is not True:
                raise ServiceError(-32602, "invalid_params", "appearance.set requires confirm:true.")
            try:
                return set_appearance(params["theme_id"])
            except ValueError as exc:
                raise ServiceError(-32602, "invalid_params", str(exc)) from exc
        self._check_params(params, set())
        return list_themes() if operation == "list" else get_appearance()

    def _settings_read(self, reader):
        try:
            return reader()
        except JobError as exc:
            raise ServiceError(-32003, exc.name, str(exc), exc.details) from None

    def _settings_revision(self, params):
        revision = self._required_text(params, "revision")
        if len(revision) != 64 or any(char not in "0123456789abcdef" for char in revision):
            raise ServiceError(-32602, "invalid_params", "Supply the revision returned by the matching settings read.")
        return revision

    def _settings_validate(self, validator, *values):
        try:
            return validator(*values)
        except JobError as exc:
            raise ServiceError(-32602, exc.name, str(exc), exc.details) from None

    def _get_application_settings(self, params):
        self._check_params(params, set())
        from .application_settings import snapshot
        return self._settings_read(snapshot)

    def _start_set_application_settings(self, params):
        self._require_idle_state()
        self._check_params(params, {"revision", "changes"}, {"revision", "changes"})
        from .application_settings import set_settings, validate_changes
        revision = self._settings_revision(params)
        changes = self._settings_validate(validate_changes, params["changes"])
        job = self._jobs.submit(
            "settings.set", None, None, None,
            {"settings": sorted(changes)},
            runner=lambda log, _progress: set_settings(revision, changes, log))
        return {"job": job}

    def _start_reset_application_notices(self, params):
        self._require_idle_state()
        self._check_params(params, {"revision", "confirm"}, {"revision", "confirm"})
        if params["confirm"] is not True:
            raise ServiceError(-32602, "invalid_params", "Resetting dismissed prompts requires confirm:true.")
        revision = self._settings_revision(params)
        from .application_settings import set_settings
        return {"job": self._jobs.submit(
            "settings.notices.reset", None, None, None,
            runner=lambda log, _progress: set_settings(revision, {}, log, reset_notices=True))}

    def _get_nexus_forwarding(self, params):
        self._check_params(params, set())
        from .settings_connections import forwarding_snapshot
        return self._settings_read(forwarding_snapshot)

    def _list_nexus_forwarding_handlers(self, params):
        self._check_params(params, set())
        from .settings_connections import forwarding_handlers
        return self._settings_read(forwarding_handlers)

    def _start_set_nexus_forwarding(self, params):
        self._require_idle_state()
        self._check_params(params, {"revision", "domain", "handler"}, {"revision", "domain", "handler"})
        revision = self._settings_revision(params)
        from .settings_connections import set_forwarding, validate_route
        domain, handler = self._settings_validate(validate_route, params["domain"], params["handler"])
        return {"job": self._jobs.submit(
            "nexus.forwarding.set", None, None, None, {"domain": domain},
            runner=lambda log, _progress: set_forwarding(revision, domain, handler, log))}

    def _get_loverslab_credentials(self, params):
        self._check_params(params, set())
        from .settings_connections import loverslab_status
        return {"credentials": self._settings_read(loverslab_status)}

    def _start_set_loverslab_credentials(self, params):
        self._require_idle_state()
        self._check_params(params, {"email", "password"}, {"email", "password"})
        from .settings_connections import save_loverslab, validate_loverslab
        email, password = self._settings_validate(validate_loverslab, params["email"], params["password"])
        return {"job": self._jobs.submit(
            "loverslab.credentials.set", None, None, None,
            runner=lambda log, progress: save_loverslab(email, password, log, progress))}

    def _start_clear_loverslab_credentials(self, params):
        self._require_idle_state()
        self._check_params(params, {"confirm"}, {"confirm"})
        if params["confirm"] is not True:
            raise ServiceError(-32602, "invalid_params", "Clearing LoversLab credentials requires confirm:true.")
        from .settings_connections import clear_loverslab
        return {"job": self._jobs.submit(
            "loverslab.credentials.clear", None, None, None, runner=clear_loverslab)}

    def _custom_game(self, method, params):
        from Games.Custom.custom_game import (
            decode_custom_game_definition, encode_custom_game_definition,
            load_builtin_game_templates,
        )
        from Utils.games.custom_definitions import definition_schema, validate_definition
        from Utils.github.handler_sources import HandlerSources
        from .custom_games import get_definition, plan_save, plan_remove, save, remove

        self._require_idle_state()
        required, optional, _ = CUSTOM_GAME_METHODS[method]
        self._check_params(params, set((required + " " + optional).split()), set(required.split()))
        try:
            if method == "games.custom.schema.get":
                return {"definition_schema": definition_schema()}
            if method == "games.custom.templates.list":
                return {"templates": load_builtin_game_templates()}
            if method == "games.custom.list":
                return {"games": [get_definition(row["game_id"]) for row in HandlerSources().games()]}
            if method == "games.custom.code.import":
                code = self._required_text(params, "code")
                if len(code) > 1024 * 1024:
                    raise ValueError("Share code exceeds 1 MiB.")
                return {"definition": validate_definition(decode_custom_game_definition(code))}
            if method in ("games.custom.validate", "games.custom.code.export"):
                definition = validate_definition(params["definition"])
                if method.endswith("export"):
                    return {"code": encode_custom_game_definition(definition)}
                return {"definition": definition}
            game = self._resolve_game(self._required_text(params, "game")) if "game" in params else None
            if method == "games.custom.get":
                return get_definition(game.game_id)
            if game is not None:
                self._require_no_managed_launch(game)
            deleting = ".remove." in method
            plan = plan_remove(game) if deleting else plan_save(self._games, params["definition"], game)
            if method.endswith(".plan"):
                return plan
            if params["confirm"] is not True:
                raise ValueError("confirm must be true to save or remove a custom game.")
            revision = self._required_text(params, "revision")
            if revision != plan["revision"]:
                raise JobError("stale_custom_game", "The plan changed; request a fresh plan.")
            runner = ((lambda log, progress: remove(self._games, game, revision, log, progress))
                      if deleting else (lambda log, progress: save(
                          self._games, plan["definition"], revision, game, log, progress)))
            return {"job": self._jobs.submit(
                method.removesuffix(".start"), game,
                self._canonical_game(game) if game is not None else None, None,
                {"game_id": plan["definition"]["game_id"]}, runner=runner)}
        except JobError as exc:
            raise ServiceError(-32027, exc.name, str(exc), exc.details) from exc
        except (ValueError, TypeError, OSError) as exc:
            raise ServiceError(-32602, "invalid_custom_game", str(exc)) from exc

    def _list_games(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(params, {"configured_only"})
        configured_only = params.get("configured_only", False)
        if not isinstance(configured_only, bool):
            raise ServiceError(
                -32602,
                "invalid_params",
                "'configured_only' must be a boolean.",
                {"parameter": "configured_only"},
            )
        games = [self._game_summary(game) for game in self._games.values()]
        if configured_only:
            games = [game for game in games if game["configured"]]
        games.sort(key=lambda game: (game["name"].casefold(), game["id"]))
        try:
            from Utils.games.discovery import get_load_failures
            failures = [
                {"handler": handler, "error": error}
                for handler, error in get_load_failures()
            ]
        except Exception:
            failures = []
        return {"games": games, "load_failures": failures}

    def _get_game(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(params, {"game"}, {"game"})
        game = self._resolve_game(self._required_text(params, "game"))
        return {"game": self._game_summary(game)}

    def _start_get_game_version(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(params, {"game", "profile"}, {"game", "profile"})
        game = self._resolve_game(self._required_text(params, "game"))
        profile = self._resolve_profile(game, self._required_text(params, "profile"))

        def runner(log, progress):
            import copy
            from Utils.collections.export import detect_game_version

            selected = copy.copy(game)
            selected._game_path = None
            selected.set_active_profile_dir(profile)
            selected.load_paths(read_only=True, prefix_fallback=False)
            game_path = selected.get_game_path()
            progress(0, 1, "detect_game_version")
            version = detect_game_version(selected, root=game_path)
            progress(1, 1, "detect_game_version")
            return {"game": self._canonical_game(game), "profile": profile.name,
                    "game_path": self._path_text(game_path),
                    "version": version or None}

        job = self._jobs.submit(
            "games.version.get", game, self._canonical_game(game), profile.name,
            runner=runner,
        )
        return {"job": job}

    def _start_list_game_installations(
        self, params: dict[str, Any],
    ) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(params, {"game"}, {"game"})
        game = self._resolve_game(self._required_text(params, "game"))

        def runner(log, progress):
            from Utils.games.installations import discover_installations

            progress(0, 1, "scan_installations")
            candidates = discover_installations(game, log_fn=log)
            progress(1, 1, "scan_installations")
            return {
                "game": self._canonical_game(game),
                "installations": [
                    {"source": item["source"],
                     "game_path": str(item["path"]),
                     "prefix_path": str(item["prefix"])
                     if item["prefix"] is not None else None,
                     "launcher_id": str(item["id"])
                     if item["id"] is not None else None,
                     "prefix_mode": item["prefix_mode"],
                     "executable": item["executable"],
                     "provider_id": item.get("provider_id"),
                     "provider_label": item.get("provider_label"),
                     "provider_icon": item.get("provider_icon"),
                     "name": item.get("name")}
                    for item in candidates
                ],
            }

        job = self._jobs.submit(
            "games.installations.list", game, self._canonical_game(game), None,
            runner=runner,
        )
        return {"job": job}

    def _start_detect_game_prefix(
        self, params: dict[str, Any],
    ) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(
            params, {"game", "game_path", "source", "launcher_id", "provider_id"},
            {"game", "game_path", "source"})
        game = self._resolve_game(self._required_text(params, "game"))
        game_path = Path(self._required_text(params, "game_path")).expanduser()
        if not game_path.is_absolute():
            raise ServiceError(-32602, "invalid_params",
                               "'game_path' must be an absolute path.",
                               {"parameter": "game_path"})
        source = self._required_text(params, "source")
        if source == "plugin":
            self._required_text(params, "provider_id")
        if source not in ("steam", "heroic", "lutris", "faugus", "bottles", "shortcut", "plugin"):
            raise ServiceError(-32602, "invalid_params",
                               "'source' must name a supported launcher.",
                               {"parameter": "source"})
        launcher_id = params.get("launcher_id")
        if source == "steam":
            if launcher_id is not None:
                raise ServiceError(-32602, "invalid_params",
                                   "'launcher_id' must be omitted for Steam.",
                                   {"parameter": "launcher_id"})
        elif (not isinstance(launcher_id, str) or not launcher_id
              or launcher_id != launcher_id.strip() or len(launcher_id) > 255
              or any(ord(char) < 32 or ord(char) == 127 for char in launcher_id)):
            raise ServiceError(-32602, "invalid_params",
                               "'launcher_id' must be a non-empty launcher identifier.",
                               {"parameter": "launcher_id"})
        if source == "shortcut" and not (
                launcher_id.isascii() and launcher_id.isdecimal()
                and 0 < int(launcher_id) <= 0xFFFFFFFF):
            raise ServiceError(-32602, "invalid_params",
                               "A shortcut launcher ID must be an unsigned 32-bit app ID.",
                               {"parameter": "launcher_id"})

        def runner(log, progress):
            from Utils.games.installations import discover_prefix

            progress(0, 1, "scan_prefix")
            found = discover_prefix(game, game_path, source, launcher_id, log,
                                    provider_id=params.get("provider_id"))
            progress(1, 1, "scan_prefix")
            return {
                "game": self._canonical_game(game),
                "game_path": str(game_path),
                "requested_source": source,
                "provider_id": params.get("provider_id"),
                "prefix_path": str(found["prefix"])
                if found["prefix"] is not None else None,
                "source": found["source"],
                "launcher_id": launcher_id if found["source"] else None,
            }

        job = self._jobs.submit(
            "games.prefix.detect", game, self._canonical_game(game), None,
            {"game_path": str(game_path), "source": source,
             "launcher_id": launcher_id}, runner=runner)
        return {"job": job}

    def _list_tools(self, params):
        from .jobs import JobError
        from .tools import catalogue

        self._require_idle_state()
        self._check_params(params, {"game", "profile"}, {"game"})
        game = self._resolve_game(self._required_text(params, "game"))
        profile = (self._resolve_profile(game, self._required_text(params, "profile"))
                   if "profile" in params else None)
        try:
            return catalogue(game, profile)
        except JobError as exc:
            raise ServiceError(-32027, exc.name, str(exc), exc.details) from exc
        except (OSError, TypeError, ValueError, KeyError, IndexError) as exc:
            raise ServiceError(-32003, "tool_catalogue_unavailable", str(exc)) from exc

    def _start_archive_packing(self, params, *, stage):
        from .archive_packing import Control, apply_runner, inspect_runner

        self._require_idle_state()
        required = {"game", "profile"}
        allowed = required.copy()
        if stage != "scan":
            required |= {"action", "mods"}
            allowed |= {"action", "mods", "options", "archives"}
        if stage == "apply":
            required |= {"revision", "confirm"}
            allowed |= {"revision", "confirm"}
        self._check_params(params, allowed, required)
        game = self._resolve_game(self._required_text(params, "game"))
        profile = self._resolve_profile(game, self._required_text(params, "profile"))
        request = None
        if stage != "scan":
            action = params["action"]
            names = params["mods"]
            if action not in ("pack", "unpack"):
                raise ServiceError(-32602, "invalid_params", "action must be pack or unpack.")
            if (not isinstance(names, list) or not names or len(names) > 4096
                    or any(not isinstance(name, str) or not name or Path(name).name != name
                           or name in (".", "..") or "\\" in name or any(ord(char) < 32 for char in name) for name in names)
                    or len({name.casefold() for name in names}) != len(names)):
                raise ServiceError(-32602, "invalid_params", "mods must contain unique installed mod folder names.")
            options = params.get("options", {})
            keys = {"compress", "delete_loose", "split_textures", "skip_winners"}
            if (not isinstance(options, dict) or set(options) - keys
                    or any(not isinstance(value, bool) for value in options.values())
                    or (action == "unpack" and options)):
                raise ServiceError(-32602, "invalid_params", "Packing options must be boolean; unpack uses no packing options.")
            options = {"compress": True, "delete_loose": False, "split_textures": False,
                       "skip_winners": True, **options} if action == "pack" else {}
            archives = params.get("archives", {})
            if not isinstance(archives, dict) or (action == "pack" and archives) or set(archives) - set(names):
                raise ServiceError(-32602, "invalid_params", "archives maps selected unpack mods to archive filenames.")
            for values in archives.values():
                if (not isinstance(values, list) or not values or len(values) > 1024
                        or any(not isinstance(name, str) or not name or Path(name).name != name
                               or "\\" in name or any(ord(char) < 32 for char in name)
                               or Path(name).suffix.lower() not in (".bsa", ".ba2") for name in values)
                        or len({name.casefold() for name in values}) != len(values)):
                    raise ServiceError(-32602, "invalid_params", "Choose unique BSA/BA2 filenames inside each mod.")
            request = {"action": action, "mods": list(names), "options": options, "archives": archives}
        control = Control()
        if stage == "apply":
            self._require_no_managed_launch(game)
            revision = self._required_text(params, "revision")
            if (params["confirm"] is not True or len(revision) != 64
                    or any(char not in "0123456789abcdef" for char in revision)):
                raise ServiceError(-32602, "invalid_params", "Supply the plan revision and confirm:true.")
            runner = apply_runner(game, profile, request, revision, control)
        else:
            runner = inspect_runner(game, profile, request, control)
        kind = f"tools.archive_packing.{stage}"
        job = self._jobs.submit(kind, game, self._canonical_game(game), profile.name,
                                {"action": request["action"], "mods": request["mods"]} if request else {},
                                runner=runner, cancel=control.cancel)
        return {"job": job}

    def _xedit_choice(self, params):
        return self._application_choice(params, "xedit")

    def _application_choice(self, params, family):
        from .jobs import JobError
        from .tools import catalogue

        game = self._resolve_game(self._required_text(params, "game"))
        profile = self._resolve_profile(game, self._required_text(params, "profile"))
        tool_id = self._required_text(params, "tool_id")
        revision = self._required_text(params, "revision")
        if len(revision) != 64 or any(char not in "0123456789abcdef" for char in revision):
            raise ServiceError(-32602, "invalid_params", "Use the revision returned by tools.list.")
        try:
            listing = catalogue(game, profile)
        except JobError as exc:
            raise ServiceError(-32027, exc.name, str(exc), exc.details) from exc
        except (OSError, TypeError, ValueError, KeyError, IndexError) as exc:
            raise ServiceError(-32003, "tool_catalogue_unavailable", str(exc)) from exc
        if listing["revision"] != revision:
            raise ServiceError(-32013, "stale_tool_catalogue", "Refresh the selected profile's tools.list catalogue.")
        row = next((tool for tool in listing["tools"] if tool["id"] == tool_id and family in tool), None)
        if row is None:
            raise ServiceError(-32602, "invalid_tool_choice", f"Choose a built-in {family} tool.")
        return game, profile, tool_id, row[family]

    def _start_application_acquisition(self, params, family="lod"):
        from Utils.downloads.control import DownloadControl
        from .tool_acquisition import acquire_runner

        self._require_idle_state()
        required = {"game", "profile", "tool_id", "revision"}
        self._check_params(params, required, required)
        game, profile, tool_id, choice = self._application_choice(params, family)
        control = DownloadControl()
        job = self._jobs.submit(f"tools.{family}.acquire", game, self._canonical_game(game), profile.name,
                                {"tool_id": tool_id, "source_kind": choice["source"]["kind"]},
                                runner=acquire_runner(self._nexus_session, game, tool_id, 0, choice, control),
                                cancel=control.cancel, pause=control.pause, resume=control.resume)
        return {"job": job}

    def _start_xedit_acquisition(self, params):
        from Utils.downloads.control import DownloadControl
        from .tool_acquisition import acquire_runner

        self._require_idle_state()
        self._check_params(params, {"game", "profile", "tool_id", "revision", "nxm_url"},
                           {"game", "profile", "tool_id", "revision"})
        game, profile, tool_id, choice = self._xedit_choice(params)
        source = choice["source"]
        nxm_link = None
        if "nxm_url" in params:
            if source["kind"] != "nexus":
                raise ServiceError(-32602, "invalid_params", "This xEdit tool has no pinned Nexus file.")
            nxm_link = self._parse_nxm_link(params)
            if (nxm_link.game_domain.casefold(), nxm_link.mod_id, nxm_link.file_id) != (
                    source["domain"].casefold(), source["mod_id"], source["file_id"]):
                raise ServiceError(-32602, "tool_source_mismatch", "The NXM link identifies another xEdit file.")
        control = DownloadControl()
        job = self._jobs.submit("tools.xedit.acquire", game, self._canonical_game(game), profile.name,
                                {"tool_id": tool_id, "source_kind": source["kind"]},
                                runner=acquire_runner(self._nexus_session, game, tool_id, 0, choice,
                                                      control, nxm_link=nxm_link),
                                cancel=control.cancel, pause=control.pause, resume=control.resume)
        return {"job": job}

    def _start_application_install(self, params, *, install, family="xedit"):
        from threading import Event
        from .application_install import inspect_runner, install_runner

        self._require_idle_state()
        required = {"game", "profile", "tool_id", "revision", "archive_path"}
        allowed = required.copy()
        if install:
            required |= {"plan_revision", "confirm"}
            allowed |= {"plan_revision", "confirm", "allow_unverified_source"}
        self._check_params(params, allowed, required)
        game, profile, tool_id, choice = self._application_choice(params, family)
        archive = self._required_text(params, "archive_path")
        if not Path(archive).expanduser().is_absolute():
            raise ServiceError(-32602, "invalid_params", "Choose an absolute application archive path.")
        cancel = Event()
        job_options = {"tool_id": tool_id, "archive_path": archive, "catalogue_revision": params["revision"]}
        if install:
            self._require_no_managed_launch(game)
            revision = self._required_text(params, "plan_revision")
            unverified = params.get("allow_unverified_source", False)
            if (params["confirm"] is not True or len(revision) != 64
                    or any(char not in "0123456789abcdef" for char in revision) or not isinstance(unverified, bool)):
                raise ServiceError(-32602, "invalid_params", "Supply the inspection revision, confirm:true, and a boolean source flag.")
            runner = install_runner(game, profile, tool_id, params["revision"], choice,
                                    archive, revision, unverified, cancel, family=family)
            job_options.update(plan_revision=revision, allow_unverified_source=unverified)
        else:
            runner = inspect_runner(game, profile, tool_id, params["revision"], choice, archive, cancel, family=family)
        kind = f"tools.{family}.install" if install else f"tools.{family}.inspect"
        job = self._jobs.submit(kind, game, self._canonical_game(game), profile.name,
                                job_options, runner=runner, cancel=cancel.set)
        return {"job": job}

    def _start_xedit_run(self, params, *, execute):
        from threading import Event
        from .xedit_run import plan_runner, run_runner

        self._require_idle_state()
        required = {"game", "profile", "tool_id", "revision"}
        settings = {"mode", "plugins", "use_64bit", "proton_name", "prefix_mode", "launch_args",
                    "launch_env", "winetricks_style", "deploy", "root_folder_enabled", "save_settings"}
        allowed = required | settings
        if execute:
            allowed |= {"plan_revision", "confirm"}
            required |= {"plan_revision", "confirm"}
        self._check_params(params, allowed, required)
        game, profile, tool_id, choice = self._xedit_choice(params)
        request = {key: params[key] for key in settings if key in params}
        for key in ("use_64bit", "winetricks_style", "deploy", "root_folder_enabled", "save_settings"):
            if key in request and not isinstance(request[key], bool):
                raise ServiceError(-32602, "invalid_params", f"{key} must be a boolean.")
        for key in ("proton_name", "launch_args", "launch_env"):
            if key in request and (not isinstance(request[key], str) or len(request[key]) > 16384
                                   or "\x00" in request[key]):
                raise ServiceError(-32602, "invalid_params", f"{key} must be text without NUL characters (maximum 16384 characters).")
        if request.get("mode", "interactive") not in ("interactive", "qac_all"):
            raise ServiceError(-32602, "invalid_params", "mode must be interactive or qac_all.")
        if request.get("prefix_mode", "isolated") not in ("isolated", "shared", "game"):
            raise ServiceError(-32602, "invalid_params", "prefix_mode must be isolated, shared, or game.")
        if "plugins" in request:
            plugins = request["plugins"]
            if (not isinstance(plugins, list) or len(plugins) > 4096
                    or any(not isinstance(value, str) or not value or len(value) > 255 for value in plugins)
                    or len({value.casefold() for value in plugins}) != len(plugins)):
                raise ServiceError(-32602, "invalid_params", "plugins must be a list of unique plugin names.")
        cancel = Event()
        if execute:
            self._require_no_managed_launch(game)
            revision = self._required_text(params, "plan_revision")
            if (params["confirm"] is not True or len(revision) != 64
                    or any(char not in "0123456789abcdef" for char in revision)):
                raise ServiceError(-32602, "invalid_params", "Supply the run-plan revision and confirm:true.")
            runner = run_runner(game, profile, tool_id, params["revision"], choice, request, revision, cancel)
        else:
            runner = plan_runner(game, profile, tool_id, params["revision"], choice, request, cancel)
        kind = "tools.xedit.run" if execute else "tools.xedit.run.plan"
        job = self._jobs.submit(kind, game, self._canonical_game(game), profile.name,
                                {"tool_id": tool_id, "mode": request.get("mode", "interactive")},
                                runner=runner, cancel=cancel.set)
        return {"job": job}

    def _start_synthesis_prefix(self, params, *, setup):
        from threading import Event
        from .synthesis_prefix import plan_runner, setup_runner

        self._require_idle_state()
        required = {"game", "profile", "tool_id", "revision"}
        allowed = required | {"proton_name", "save_settings"}
        if setup:
            required |= {"plan_revision", "confirm"}
            allowed |= {"plan_revision", "confirm"}
        self._check_params(params, allowed, required)
        game, profile, tool_id, choice = self._application_choice(params, "synthesis")
        request = {key: params[key] for key in ("proton_name", "save_settings") if key in params}
        name = request.get("proton_name", "")
        if (not isinstance(name, str) or len(name) > 255
                or any(ord(char) < 32 for char in name) or "/" in name or chr(92) in name):
            raise ServiceError(-32602, "invalid_params", "proton_name must be an installed build name.")
        if "save_settings" in request and not isinstance(request["save_settings"], bool):
            raise ServiceError(-32602, "invalid_params", "save_settings must be a boolean.")
        cancel = Event()
        if setup:
            self._require_no_managed_launch(game)
            revision = self._required_text(params, "plan_revision")
            if params["confirm"] is not True or len(revision) != 64 or any(c not in "0123456789abcdef" for c in revision):
                raise ServiceError(-32602, "invalid_params", "Supply the prefix-plan revision and confirm:true.")
            runner = setup_runner(game, profile, tool_id, params["revision"], choice, request, revision, cancel)
        else:
            runner = plan_runner(game, profile, tool_id, params["revision"], choice, request, cancel)
        kind = "tools.synthesis.prefix.setup" if setup else "tools.synthesis.prefix.plan"
        job = self._jobs.submit(kind, game, self._canonical_game(game), profile.name,
                                {"tool_id": tool_id, **request}, runner=runner, cancel=cancel.set)
        return {"job": job}

    def _start_wrye_bash_run(self, params, *, execute):
        from threading import Event
        from .wrye_bash_run import plan_runner, run_runner

        self._require_idle_state()
        required = {"game", "profile", "tool_id", "revision"}
        settings = {"proton_name", "prefix_mode", "launch_args", "launch_env", "winetricks_style",
                    "deploy", "root_folder_enabled", "save_settings"}
        allowed = required | settings
        if execute:
            required |= {"plan_revision", "confirm"}
            allowed |= {"plan_revision", "confirm"}
        self._check_params(params, allowed, required)
        game, profile, tool_id, choice = self._application_choice(params, "wrye_bash")
        request = {key: params[key] for key in settings if key in params}
        for key in ("winetricks_style", "deploy", "root_folder_enabled", "save_settings"):
            if key in request and not isinstance(request[key], bool):
                raise ServiceError(-32602, "invalid_params", f"{key} must be a boolean.")
        for key in ("proton_name", "launch_args", "launch_env"):
            if key in request and (not isinstance(request[key], str) or len(request[key]) > 16384 or "\x00" in request[key]):
                raise ServiceError(-32602, "invalid_params", f"{key} must be text without NUL characters.")
        if request.get("prefix_mode", "isolated") not in ("isolated", "shared", "game"):
            raise ServiceError(-32602, "invalid_params", "prefix_mode must be isolated, shared, or game.")
        cancel = Event()
        if execute:
            self._require_no_managed_launch(game)
            revision = self._required_text(params, "plan_revision")
            if params["confirm"] is not True or len(revision) != 64 or any(c not in "0123456789abcdef" for c in revision):
                raise ServiceError(-32602, "invalid_params", "Supply the run-plan revision and confirm:true.")
            runner = run_runner(game, profile, tool_id, params["revision"], choice, request, revision, cancel)
        else:
            runner = plan_runner(game, profile, tool_id, params["revision"], choice, request, cancel)
        kind = "tools.wrye_bash.run" if execute else "tools.wrye_bash.run.plan"
        job = self._jobs.submit(kind, game, self._canonical_game(game), profile.name,
                                {"tool_id": tool_id}, runner=runner, cancel=cancel.set)
        return {"job": job}

    def _start_lod_run(self, params, *, execute):
        from threading import Event
        from Utils.bethesda.lod_run import new_output_path
        from .lod_run import plan_runner, run_runner

        self._require_idle_state()
        required = {"game", "profile", "tool_id", "revision"}
        settings = {"proton_name", "prefix_mode", "launch_args", "launch_env", "winetricks_style",
                    "prefer_discrete_gpu", "deploy", "root_folder_enabled", "save_settings",
                    "mod_name", "replace_output", "output_path"}
        allowed = required | settings
        if execute:
            required |= {"plan_revision", "confirm", "output_path"}
            allowed |= {"plan_revision", "confirm"}
        self._check_params(params, allowed, required)
        game, profile, tool_id, choice = self._application_choice(params, "lod")
        request = {key: params[key] for key in settings if key in params}
        for key in ("winetricks_style", "prefer_discrete_gpu", "deploy", "root_folder_enabled", "save_settings", "replace_output"):
            if key in request and not isinstance(request[key], bool):
                raise ServiceError(-32602, "invalid_params", f"{key} must be a boolean.")
        for key in ("proton_name", "launch_args", "launch_env", "mod_name", "output_path"):
            if key in request and (not isinstance(request[key], str) or len(request[key]) > 16384 or "\x00" in request[key]):
                raise ServiceError(-32602, "invalid_params", f"{key} must be text without NUL characters.")
        if "mod_name" in request and (not request["mod_name"] or len(request["mod_name"]) > 255):
            raise ServiceError(-32602, "invalid_params", "mod_name must contain 1 to 255 characters.")
        if request.get("prefix_mode", "isolated") not in ("isolated", "shared", "game"):
            raise ServiceError(-32602, "invalid_params", "prefix_mode must be isolated, shared, or game.")
        if "output_path" in request and not Path(request["output_path"]).expanduser().is_absolute():
            raise ServiceError(-32602, "invalid_params", "output_path must be an absolute new work folder.")
        request.setdefault("output_path", str(new_output_path()))
        cancel = Event()
        if execute:
            self._require_no_managed_launch(game)
            revision = self._required_text(params, "plan_revision")
            if params["confirm"] is not True or len(revision) != 64 or any(c not in "0123456789abcdef" for c in revision):
                raise ServiceError(-32602, "invalid_params", "Supply the run-plan revision and confirm:true.")
            runner = run_runner(game, profile, tool_id, params["revision"], choice, request, revision, cancel)
        else:
            runner = plan_runner(game, profile, tool_id, params["revision"], choice, request, cancel)
        kind = "tools.lod.run" if execute else "tools.lod.run.plan"
        job = self._jobs.submit(kind, game, self._canonical_game(game), profile.name,
                                {"tool_id": tool_id, "output_path": request["output_path"]}, runner=runner, cancel=cancel.set)
        return {"job": job}

    def _start_smapi_acquisition(self, params):
        from Utils.downloads.control import DownloadControl
        from .smapi import acquire_runner

        self._require_idle_state()
        self._check_params(params, {"game"}, {"game"})
        game = self._resolve_game(self._required_text(params, "game"))
        control = DownloadControl()
        job = self._jobs.submit("tools.smapi.acquire", game, self._canonical_game(game), None,
                                runner=acquire_runner(game, control), cancel=control.cancel,
                                pause=control.pause, resume=control.resume)
        return {"job": job}

    def _start_smapi_install(self, params, *, install):
        from threading import Event
        from Utils.profiles.groups import is_group
        from Utils.profiles.state import read_only_profile_state
        from .smapi import inspect_runner, install_runner

        self._require_idle_state()
        required = {"game", "profile", "archive_path", "mode"}
        allowed = required | {"mod_name"}
        if install:
            required |= {"plan_revision", "confirm"}
            allowed |= {"plan_revision", "confirm", "allow_unverified_source", "replace_existing"}
        self._check_params(params, allowed, required)
        game = self._resolve_game(self._required_text(params, "game"))
        profile = self._resolve_profile(game, self._required_text(params, "profile"))
        archive = self._required_text(params, "archive_path")
        mode = self._required_text(params, "mode")
        if (not Path(archive).expanduser().is_absolute() or Path(archive).suffix.casefold() != ".zip"
                or mode not in ("game", "root", "mod")):
            raise ServiceError(-32602, "invalid_params", "Choose an absolute SMAPI ZIP path and game, root, or mod mode.")
        name = self._required_text(params, "mod_name") if "mod_name" in params else "SMAPI"
        if mode != "mod" and "mod_name" in params:
            raise ServiceError(-32602, "invalid_params", "A mod name is only used in mod mode.")
        with read_only_profile_state():
            if mode == "mod" and is_group(profile):
                raise ServiceError(-32009, "operation_not_supported", "Install SMAPI into a member profile.")
        cancel = Event()
        options = {"archive_path": archive, "mode": mode, "mod_name": name}
        if install:
            self._require_configured(game)
            self._require_no_managed_launch(game)
            revision = self._required_text(params, "plan_revision")
            unverified = params.get("allow_unverified_source", False)
            replace = params.get("replace_existing", False)
            if (params["confirm"] is not True or len(revision) != 64
                    or any(char not in "0123456789abcdef" for char in revision)
                    or not isinstance(unverified, bool) or not isinstance(replace, bool)):
                raise ServiceError(-32602, "invalid_params", "Supply the inspection revision, confirm:true, and boolean flags.")
            runner = install_runner(game, profile, archive, mode, name, revision, unverified, replace, cancel)
            options.update(plan_revision=revision, allow_unverified_source=unverified, replace_existing=replace)
        else:
            runner = inspect_runner(game, profile, archive, mode, name, cancel)
        kind = "tools.smapi.install" if install else "tools.smapi.inspect"
        job = self._jobs.submit(kind, game, self._canonical_game(game), profile.name,
                                options, runner=runner, cancel=cancel.set)
        return {"job": job}

    def _start_4gb_patch(self, params, action):
        from threading import Event
        from .patch4gb import inspect_runner, mutate_runner

        self._require_idle_state()
        required = {"game", "profile"}
        if action != "inspect":
            required |= {"revision", "confirm"}
        self._check_params(params, required, required)
        game = self._resolve_game(self._required_text(params, "game"))
        profile = self._resolve_profile(game, self._required_text(params, "profile"))
        cancel = Event()
        options = {}
        if action == "inspect":
            runner = inspect_runner(game, profile, cancel)
        else:
            self._require_configured(game)
            self._require_no_managed_launch(game)
            revision = self._required_text(params, "revision")
            if (params["confirm"] is not True or len(revision) != 64
                    or any(char not in "0123456789abcdef" for char in revision)):
                raise ServiceError(-32602, "invalid_params",
                                   "Supply the inspection revision and confirm:true.")
            options = {"revision": revision, "confirm": True}
            runner = mutate_runner(game, profile, action, revision, cancel)
        job = self._jobs.submit(f"tools.4gb_patch.{action}", game, self._canonical_game(game),
                                profile.name, options, runner=runner, cancel=cancel.set)
        return {"job": job}

    def _get_reshade_options(self, params):
        from .jobs import JobError
        from .reshade_options import options

        self._require_idle_state()
        self._check_params(params, {"game", "profile"}, {"game", "profile"})
        game = self._resolve_game(self._required_text(params, "game"))
        profile = self._resolve_profile(game, self._required_text(params, "profile"))
        try:
            return options(game, profile)
        except JobError as exc:
            raise ServiceError(-32027, exc.name, str(exc), exc.details) from exc
        except (OSError, TypeError, ValueError, RuntimeError) as exc:
            raise ServiceError(-32003, "reshade_options_unavailable", str(exc)) from exc

    def _start_reshade_dll_acquisition(self, params):
        from Utils.downloads.control import DownloadControl
        from .reshade_acquisition import acquire_runner

        self._require_idle_state()
        self._check_params(params, {"game", "profile", "architecture"},
                           {"game", "profile", "architecture"})
        game = self._resolve_game(self._required_text(params, "game"))
        profile = self._resolve_profile(game, self._required_text(params, "profile"))
        architecture = params["architecture"]
        if (isinstance(architecture, bool) or not isinstance(architecture, int)
                or architecture not in (32, 64)):
            raise ServiceError(-32602, "invalid_params", "Choose a 32-bit or 64-bit ReShade DLL.")
        control = DownloadControl()
        job = self._jobs.submit(
            "tools.reshade.dll.acquire", game, self._canonical_game(game), profile.name,
            {"architecture": architecture},
            runner=acquire_runner(game, profile, architecture, control),
            cancel=control.cancel, pause=control.pause, resume=control.resume,
        )
        return {"job": job}

    def _start_reshade_shaders_acquisition(self, params):
        from Utils.downloads.control import DownloadControl
        from Utils.wizards.reshade import OPTIONAL_SHADER_PACKS
        from .reshade_shaders import acquire_runner

        self._require_idle_state()
        self._check_params(params, {"game", "profile", "pack_indices", "preset_path"},
                           {"game", "profile"})
        game = self._resolve_game(self._required_text(params, "game"))
        profile = self._resolve_profile(game, self._required_text(params, "profile"))
        indices = params.get("pack_indices", [])
        if (not isinstance(indices, list) or len(indices) > len(OPTIONAL_SHADER_PACKS)
                or any(isinstance(index, bool) or not isinstance(index, int)
                       or not 0 <= index < len(OPTIONAL_SHADER_PACKS) for index in indices)
                or len(set(indices)) != len(indices)):
            raise ServiceError(-32602, "invalid_params", "Choose distinct shader pack indices from ReShade options.")
        indices = sorted(indices)
        preset = (self._required_text(params, "preset_path")
                  if "preset_path" in params else None)
        control = DownloadControl()
        job = self._jobs.submit(
            "tools.reshade.shaders.acquire", game, self._canonical_game(game), profile.name,
            {"pack_indices": indices, "preset_path": preset},
            runner=acquire_runner(game, profile, tuple(indices), preset, control),
            cancel=control.cancel, pause=control.pause, resume=control.resume,
        )
        return {"job": job}

    def _reshade_install_request(self, params, *, install):
        from Utils.profiles.groups import is_group
        from Utils.profiles.state import read_only_profile_state
        from Utils.wizards.reshade import API_CHOICES

        self._require_idle_state()
        allowed = {"game", "profile", "dll_path", "shader_path", "dll_name", "mode", "mod_name"}
        required = {"game", "profile", "dll_path", "shader_path", "dll_name", "mode"}
        if install:
            allowed |= {"plan_revision", "confirm", "replace_existing"}
            required |= {"plan_revision", "confirm"}
        self._check_params(params, allowed, required)
        game = self._resolve_game(self._required_text(params, "game"))
        profile = self._resolve_profile(game, self._required_text(params, "profile"))
        if install:
            self._require_no_managed_launch(game)
        dll = self._required_text(params, "dll_path")
        shaders = self._required_text(params, "shader_path")
        if not Path(dll).expanduser().is_absolute() or not Path(shaders).expanduser().is_absolute():
            raise ServiceError(-32602, "invalid_params", "ReShade sources must be absolute paths.")
        dll_name = self._required_text(params, "dll_name")
        if dll_name not in {name for _label, name in API_CHOICES}:
            raise ServiceError(-32602, "invalid_params", "Choose a proxy DLL from ReShade options.")
        mode = self._required_text(params, "mode")
        if mode not in ("game", "root", "mod"):
            raise ServiceError(-32602, "invalid_params", "Choose game, root, or mod installation.")
        mod_name = self._required_text(params, "mod_name") if "mod_name" in params else "ReShade"
        if mode != "mod" and "mod_name" in params:
            raise ServiceError(-32602, "invalid_params", "A mod name is only used for managed-mod installation.")
        with read_only_profile_state():
            if mode == "mod" and is_group(profile):
                raise ServiceError(-32009, "operation_not_supported",
                                   "Install into a member profile rather than a profile group.")
        return game, profile, dll, shaders, dll_name, mode, mod_name

    def _start_reshade_inspection(self, params):
        from threading import Event
        from .reshade_install import inspect_runner

        game, profile, dll, shaders, dll_name, mode, mod_name = self._reshade_install_request(
            params, install=False)
        cancel = Event()
        job = self._jobs.submit(
            "tools.reshade.inspect", game, self._canonical_game(game), profile.name,
            {"dll_path": dll, "shader_path": shaders, "dll_name": dll_name,
             "mode": mode, "mod_name": mod_name},
            runner=inspect_runner(game, profile, dll, shaders, dll_name, mode, mod_name, cancel),
            cancel=cancel.set,
        )
        return {"job": job}

    def _start_reshade_installation(self, params):
        from threading import Event
        from .reshade_install import install_runner

        game, profile, dll, shaders, dll_name, mode, mod_name = self._reshade_install_request(
            params, install=True)
        if params["confirm"] is not True:
            raise ServiceError(-32602, "invalid_params", "'confirm' must be true to install ReShade.")
        revision = self._required_text(params, "plan_revision")
        if len(revision) != 64 or any(char not in "0123456789abcdef" for char in revision):
            raise ServiceError(-32602, "invalid_params", "Use the revision returned by tools.reshade.inspect.start.")
        replace_existing = params.get("replace_existing", False)
        if not isinstance(replace_existing, bool):
            raise ServiceError(-32602, "invalid_params", "'replace_existing' must be a boolean.")
        cancel = Event()
        job = self._jobs.submit(
            "tools.reshade.install", game, self._canonical_game(game), profile.name,
            {"dll_path": dll, "shader_path": shaders, "dll_name": dll_name,
             "mode": mode, "mod_name": mod_name, "plan_revision": revision,
             "replace_existing": replace_existing},
            runner=install_runner(game, profile, dll, shaders, dll_name, mode, mod_name,
                                  revision, replace_existing, cancel),
            cancel=cancel.set,
        )
        return {"job": job}

    def _script_extender_choice(self, params):
        from .jobs import JobError
        from .tools import catalogue

        game = self._resolve_game(self._required_text(params, "game"))
        profile = (self._resolve_profile(game, self._required_text(params, "profile"))
                   if "profile" in params else None)
        tool_id = self._required_text(params, "tool_id")
        revision = self._required_text(params, "revision")
        if len(revision) != 64 or any(char not in "0123456789abcdef" for char in revision):
            raise ServiceError(-32602, "invalid_params", "Use the revision returned by tools.list.")
        try:
            listing = catalogue(game, profile)
        except JobError as exc:
            raise ServiceError(-32027, exc.name, str(exc), exc.details) from exc
        except (OSError, TypeError, ValueError, KeyError, IndexError) as exc:
            raise ServiceError(-32003, "tool_catalogue_unavailable", str(exc)) from exc
        if listing["revision"] != revision:
            raise ServiceError(-32013, "stale_tool_catalogue", "The game's tool choices changed; refresh tools.list.")
        row = next((item for item in listing["tools"] if item["id"] == tool_id), None)
        if row is None or "script_extender" not in row:
            raise ServiceError(-32602, "invalid_tool_choice", "Choose a built-in script-extender tool.")
        versions = row["script_extender"]["versions"]
        index = params.get("version_index")
        if index is None and len(versions) == 1:
            index = 0
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(versions):
            raise ServiceError(-32602, "invalid_tool_choice", "Choose one declared version_index.")
        version = versions[index]
        return game, profile, tool_id, index, version

    def _start_tool_acquisition(self, params):
        self._require_idle_state()
        self._check_params(params, {"game", "profile", "tool_id", "version_index", "revision", "nxm_url"},
                           {"game", "tool_id", "revision"})
        game, profile, tool_id, index, version = self._script_extender_choice(params)
        source = version["source"]
        nxm_link = None
        if "nxm_url" in params:
            if source["kind"] != "nexus":
                raise ServiceError(-32602, "invalid_params", "An NXM link is valid only for a Nexus tool choice.")
            parsed = self._parse_nxm_link(params)
            if (parsed.game_domain.casefold(), parsed.mod_id, parsed.file_id) != (
                    source["domain"].casefold(), source["mod_id"], source["file_id"]):
                raise ServiceError(-32602, "tool_source_mismatch", "The NXM link identifies another tool file.")
            nxm_link = parsed

        from Utils.downloads.control import DownloadControl
        from .tool_acquisition import acquire_runner
        control = DownloadControl()
        job = self._jobs.submit(
            "tools.script_extender.acquire", game, self._canonical_game(game),
            profile.name if profile else None,
            {"tool_id": tool_id, "version_index": index, "source_kind": source["kind"]},
            runner=acquire_runner(self._nexus_session, game, tool_id, index, version,
                                  control, nxm_link=nxm_link),
            cancel=control.cancel, pause=control.pause, resume=control.resume,
        )
        return {"job": job}

    def _tool_install_request(self, params, *, install):
        from Utils.profiles.groups import is_group
        from Utils.profiles.state import read_only_profile_state

        self._require_idle_state()
        allowed = {"game", "profile", "tool_id", "version_index", "revision",
                   "archive_path", "mode"}
        required = {"game", "profile", "tool_id", "revision", "archive_path", "mode"}
        if install:
            allowed |= {"plan_revision", "confirm", "allow_unverified_source", "replace_existing"}
            required |= {"plan_revision", "confirm"}
        self._check_params(params, allowed, required)
        game, profile, tool_id, index, version = self._script_extender_choice(params)
        if install:
            self._require_no_managed_launch(game)
        archive_path = self._required_text(params, "archive_path")
        if not Path(archive_path).expanduser().is_absolute():
            raise ServiceError(-32602, "invalid_params", "'archive_path' must be absolute.")
        mode = self._required_text(params, "mode")
        if mode not in ("game", "root", "mod"):
            raise ServiceError(-32602, "invalid_params", "Choose game, root, or mod installation.")
        with read_only_profile_state():
            if mode == "mod" and is_group(profile):
                raise ServiceError(-32009, "operation_not_supported",
                                   "Install into a member profile rather than a profile group.")
        return game, profile, tool_id, index, version, archive_path, mode

    def _start_tool_inspection(self, params):
        from threading import Event
        from .tool_install import inspect_runner

        game, profile, tool_id, index, version, archive, mode = self._tool_install_request(
            params, install=False)
        cancel = Event()
        job = self._jobs.submit(
            "tools.script_extender.inspect", game, self._canonical_game(game), profile.name,
            {"tool_id": tool_id, "version_index": index, "archive_path": archive,
             "mode": mode, "catalogue_revision": params["revision"]},
            runner=inspect_runner(game, profile, tool_id, index, version,
                                  params["revision"], archive, mode, cancel),
            cancel=cancel.set,
        )
        return {"job": job}

    def _start_tool_installation(self, params):
        from threading import Event
        from .tool_install import install_runner

        game, profile, tool_id, index, version, archive, mode = self._tool_install_request(
            params, install=True)
        if params["confirm"] is not True:
            raise ServiceError(-32602, "invalid_params", "'confirm' must be true to install the tool.")
        plan_revision = self._required_text(params, "plan_revision")
        if len(plan_revision) != 64 or any(char not in "0123456789abcdef" for char in plan_revision):
            raise ServiceError(-32602, "invalid_params", "Use the revision returned by tools.script_extender.inspect.start.")
        allow_unverified = params.get("allow_unverified_source", False)
        replace_existing = params.get("replace_existing", False)
        if not isinstance(allow_unverified, bool) or not isinstance(replace_existing, bool):
            raise ServiceError(-32602, "invalid_params", "Installation choices must be booleans.")
        cancel = Event()
        job = self._jobs.submit(
            "tools.script_extender.install", game, self._canonical_game(game), profile.name,
            {"tool_id": tool_id, "version_index": index, "archive_path": archive,
             "mode": mode, "catalogue_revision": params["revision"],
             "plan_revision": plan_revision, "allow_unverified_source": allow_unverified,
             "replace_existing": replace_existing},
            runner=install_runner(game, profile, tool_id, index, version,
                                  params["revision"], archive, mode, plan_revision,
                                  allow_unverified, replace_existing, cancel),
            cancel=cancel.set,
        )
        return {"job": job}

    def _get_game_launcher(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(params, {"game", "profile"}, {"game", "profile"})
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_configured(game)
        profile = self._resolve_profile(game, self._required_text(params, "profile"))
        from .game_launcher import get_launcher
        from .jobs import JobError
        try:
            return {"launcher": get_launcher(game, profile)}
        except JobError as exc:
            raise ServiceError(-32027, exc.name, str(exc), exc.details) from exc

    def _start_set_game_launcher(
        self, params: dict[str, Any],
    ) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(
            params, {"game", "profile", "source", "launcher_id", "revision", "provider_id"},
            {"game", "profile", "source", "revision"})
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_no_managed_launch(game)
        self._require_configured(game)
        profile = self._resolve_profile(game, self._required_text(params, "profile"))
        source = self._required_text(params, "source")
        if source not in ("steam", "manual", "heroic", "lutris", "faugus", "bottles", "shortcut", "plugin"):
            raise ServiceError(-32602, "invalid_params",
                               "'source' must name a supported launcher or manual.",
                               {"parameter": "source"})
        launcher_id = params.get("launcher_id")
        if source in ("steam", "manual"):
            if launcher_id is not None:
                raise ServiceError(-32602, "invalid_params",
                                   "'launcher_id' must be null or omitted for this source.",
                                   {"parameter": "launcher_id"})
        elif (not isinstance(launcher_id, str) or not launcher_id
              or launcher_id != launcher_id.strip() or len(launcher_id) > 255
              or any(ord(char) < 32 or ord(char) == 127 for char in launcher_id)):
            raise ServiceError(-32602, "invalid_params",
                               "'launcher_id' must be a non-empty launcher identifier.",
                               {"parameter": "launcher_id"})
        if source == "shortcut" and not (
                launcher_id.isascii() and launcher_id.isdecimal()
                and 0 < int(launcher_id) <= 0xFFFFFFFF):
            raise ServiceError(-32602, "invalid_params",
                               "A shortcut launcher ID must be an unsigned 32-bit app ID.",
                               {"parameter": "launcher_id"})
        revision = self._required_text(params, "revision")
        if len(revision) != 64 or any(char not in "0123456789abcdef"
                                      for char in revision):
            raise ServiceError(-32602, "invalid_params",
                               "'revision' must be returned by games.launcher.get.",
                               {"parameter": "revision"})
        from .game_launcher import set_launcher
        if source == "plugin":
            self._required_text(params, "provider_id")
        job = self._jobs.submit(
            "games.launcher.set", game, self._canonical_game(game), profile.name,
            {"source": source, "launcher_id": launcher_id},
            runner=lambda log, progress: set_launcher(
                game, profile, source, launcher_id, revision, log, provider_id=params.get("provider_id")),
        )
        return {"job": job}

    def _launcher_entries(self, action, params):
        from threading import Event
        from amethyst_plugins import launchers
        from .jobs import JobCancelled
        self._require_idle_state()
        fields = set() if action == "list" else {"provider_id", "launcher_id"}
        if action == "set":
            fields |= {"changes", "revision"}
        required = set(fields)
        if action == "run":
            fields |= {"executable", "arguments"}
        self._check_params(params, fields, required)
        for field in required - {"changes"}:
            self._required_text(params, field)
        if action == "set" and not isinstance(params["changes"], dict):
            raise ServiceError(-32602, "invalid_params", "changes must be an object.")
        for field in ("executable", "arguments"):
            if field in params and not isinstance(params[field], str):
                raise ServiceError(-32602, "invalid_params", f"{field} must be text.")
        cancelled = Event()
        def check():
            if cancelled.is_set():
                raise JobCancelled()
        def runner(log, progress):
            try:
                if action == "list":
                    return launchers.list_entries(log=log, check_cancelled=check)
                entry = launchers.get_entry(params["provider_id"], params["launcher_id"],
                                            log=log, check_cancelled=check)
                if action == "get":
                    return {"entry": entry}
                if action == "set":
                    return {"entry": launchers.update_entry(params["provider_id"], params["launcher_id"],
                        params["changes"], params["revision"], log=log, check_cancelled=check)}
                from .launch import entry_launch_runner
                return entry_launch_runner(entry, params, self._launches, check)(log, progress)
            except launchers.ProviderError as exc:
                raise JobError("launcher_provider_error", str(exc)) from exc
        return {"job": self._jobs.submit(
            "launchers.entries.list" if action == "list" else f"launchers.entry.{action}",
            None, None, None, runner=runner, cancel=cancelled.set)}

    def _start_configure_game(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(
            params,
            {"game", "game_path", "prefix_path", "staging_path",
             "save_path_override", "appimage_path", "deploy_mode",
             "runtime_mode"},
            {"game", "game_path"},
        )
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_no_managed_launch(game)

        def absolute_path(name: str, required: bool = False) -> Path | None:
            if name not in params and not required:
                return None
            value = Path(self._required_text(params, name)).expanduser()
            if not value.is_absolute():
                raise ServiceError(-32602, "invalid_params",
                                   f"'{name}' must be an absolute path.",
                                   {"parameter": name})
            return value

        game_path = absolute_path("game_path", required=True)
        prefix_path = absolute_path("prefix_path")
        staging_path = absolute_path("staging_path")
        save_path_override = absolute_path("save_path_override")
        appimage_path = absolute_path("appimage_path")
        if staging_path is None:
            from Utils.config_paths import get_default_game_staging_root
            staging_path = get_default_game_staging_root(game.name)
        deploy_mode = params.get("deploy_mode")
        if deploy_mode is not None and deploy_mode not in (
                "hardlink", "symlink", "copy", "vfs"):
            raise ServiceError(-32602, "invalid_params",
                               "'deploy_mode' must be hardlink, symlink, copy, or vfs.",
                               {"parameter": "deploy_mode"})
        runtime_mode = params.get("runtime_mode")
        if runtime_mode is not None and runtime_mode not in ("native", "proton"):
            raise ServiceError(-32602, "invalid_params",
                               "'runtime_mode' must be native or proton.",
                               {"parameter": "runtime_mode"})
        from .game_configuration import configure_game
        job = self._jobs.submit(
            "games.configure", game, self._canonical_game(game), None,
            runner=lambda log, progress: configure_game(
                game, game_path, prefix_path, staging_path,
                deploy_mode, runtime_mode, log,
                save_path_override=save_path_override,
                appimage_path=appimage_path),
        )
        return {"job": job}

    @staticmethod
    def _reconfigure_changes(params: dict[str, Any]) -> dict[str, Any]:
        changes: dict[str, Any] = {}
        for key in ("game_path", "prefix_path", "staging_path",
                    "save_path_override", "appimage_path"):
            if key not in params:
                continue
            value = params[key]
            if value is None and key != "game_path":
                changes[key] = None
                continue
            if (not isinstance(value, str) or not value.strip()
                    or not Path(value).expanduser().is_absolute()):
                raise ServiceError(-32602, "invalid_params",
                                   f"'{key}' must be an absolute path.",
                                   {"parameter": key})
            changes[key] = Path(value).expanduser()
        if "deploy_mode" in params:
            value = params["deploy_mode"]
            if value not in ("hardlink", "symlink", "copy", "vfs"):
                raise ServiceError(-32602, "invalid_params",
                                   "'deploy_mode' must be hardlink, symlink, copy, or vfs.",
                                   {"parameter": "deploy_mode"})
            changes["deploy_mode"] = value
        if "runtime_mode" in params:
            value = params["runtime_mode"]
            if value not in ("native", "proton"):
                raise ServiceError(-32602, "invalid_params",
                                   "'runtime_mode' must be native or proton.",
                                   {"parameter": "runtime_mode"})
            changes["runtime_mode"] = value
        if not changes:
            raise ServiceError(-32602, "invalid_params",
                               "Supply at least one path or deployment setting.")
        return changes

    def _plan_reconfigure_game(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(
            params, {"game", "profile", "game_path", "prefix_path",
                     "staging_path", "save_path_override", "appimage_path",
                     "deploy_mode", "runtime_mode"},
            {"game", "profile"})
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_no_managed_launch(game)
        self._require_configured(game)
        profile = self._resolve_profile(
            game, self._required_text(params, "profile"))
        changes = self._reconfigure_changes(params)
        from .jobs import JobError
        from .reconfigure import plan_reconfigure_game
        try:
            return {"plan": plan_reconfigure_game(game, profile, changes)}
        except JobError as exc:
            raise ServiceError(-32027, exc.name, str(exc), exc.details) from exc

    def _start_reconfigure_game(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(
            params, {"game", "profile", "game_path", "prefix_path",
                     "staging_path", "save_path_override", "appimage_path",
                     "deploy_mode", "runtime_mode", "revision",
                     "migrate_staging", "confirm"},
            {"game", "profile", "revision", "confirm"})
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_no_managed_launch(game)
        self._require_configured(game)
        profile = self._resolve_profile(
            game, self._required_text(params, "profile"))
        changes = self._reconfigure_changes(params)
        revision = self._required_text(params, "revision")
        if (len(revision) != 64
                or any(char not in "0123456789abcdef" for char in revision)):
            raise ServiceError(-32602, "invalid_params",
                               "'revision' must be returned by games.reconfigure.plan.",
                               {"parameter": "revision"})
        if params["confirm"] is not True:
            raise ServiceError(-32602, "invalid_params",
                               "'confirm' must be true to change saved game paths.",
                               {"parameter": "confirm"})
        migrate = params.get("migrate_staging")
        if migrate is not None and not isinstance(migrate, bool):
            raise ServiceError(-32602, "invalid_params",
                               "'migrate_staging' must be a boolean.",
                               {"parameter": "migrate_staging"})
        from .reconfigure import reconfigure_game
        job = self._jobs.submit(
            "games.reconfigure", game, self._canonical_game(game), profile.name,
            {"changes": {key: str(value) if isinstance(value, Path) else value
                         for key, value in changes.items()},
             "revision": revision, "migrate_staging": migrate,
             "confirm": True},
            runner=lambda log, progress: reconfigure_game(
                game, profile, changes, revision, migrate,
                "headless:games.reconfigure", log, progress),
        )
        return {"job": job}

    def _get_game_options(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(params, {"game", "profile"}, {"game", "profile"})
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_configured(game)
        profile = self._resolve_profile(game, self._required_text(params, "profile"))
        from .game_options import list_options
        return {"game": self._canonical_game(game), "profile": profile.name,
                "options": list_options(game, profile)}

    def _start_set_game_option(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(params, {"game", "profile", "key", "value", "expected_value"},
                           {"game", "profile", "key", "value", "expected_value"})
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_no_managed_launch(game)
        self._require_configured(game)
        profile = self._resolve_profile(game, self._required_text(params, "profile"))
        key = self._required_text(params, "key")
        value, expected_value = params["value"], params["expected_value"]
        from .game_options import set_option
        job = self._jobs.submit(
            "games.options.set", game, self._canonical_game(game), profile.name,
            runner=lambda log, progress: set_option(
                game, profile, key, value, expected_value, log),
        )
        return {"job": job}

    def _get_game_routing(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(params, {"game"}, {"game"})
        game = self._resolve_game(self._required_text(params, "game"))
        from .jobs import JobError
        from .routing import get_routing
        try:
            return {"game": self._canonical_game(game),
                    "routing": get_routing(game)}
        except JobError as exc:
            raise ServiceError(-32003, exc.name, str(exc), exc.details) from exc

    def _start_set_game_routing(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(
            params, {"game", "profile", "revision", "rules", "confirm"},
            {"game", "revision", "rules", "confirm"})
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_no_managed_launch(game)
        profile = None
        if "profile" in params:
            self._require_configured(game)
            profile = self._resolve_profile(
                game, self._required_text(params, "profile"))
            if profile.is_symlink():
                raise ServiceError(-32602, "invalid_params",
                                   "A symlinked profile cannot be reconciled.",
                                   {"parameter": "profile"})
        revision = self._required_text(params, "revision")
        if (len(revision) != 64 or any(
                char not in "0123456789abcdef" for char in revision)):
            raise ServiceError(-32602, "invalid_params",
                               "'revision' must be a routing snapshot revision.",
                               {"parameter": "revision"})
        rules = params["rules"]
        if not isinstance(rules, list) or len(rules) > 512:
            raise ServiceError(-32602, "invalid_params",
                               "'rules' must be an array of at most 512 rules.",
                               {"parameter": "rules"})
        if params.get("confirm") is not True:
            raise ServiceError(-32602, "invalid_params",
                               "'confirm' must be true to change per-game routing.",
                               {"parameter": "confirm"})
        from .routing import set_routing
        job = self._jobs.submit(
            "games.routing.set", game, self._canonical_game(game),
            profile.name if profile else None,
            {"revision": revision, "rule_count": len(rules)},
            runner=lambda log, progress: set_routing(
                game, profile, revision, rules,
                log_fn=log, progress_fn=progress),
        )
        return {"job": job}

    def _get_game_blacklist(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(params, {"game"}, {"game"})
        game = self._resolve_game(self._required_text(params, "game"))
        from .blacklist import get_blacklist
        from .jobs import JobError
        try:
            return {"game": self._canonical_game(game),
                    "blacklist": get_blacklist(game)}
        except JobError as exc:
            raise ServiceError(-32003, exc.name, str(exc), exc.details) from exc

    def _start_set_game_blacklist(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(
            params, {"game", "profile", "revision", "overrides", "confirm"},
            {"game", "revision", "overrides", "confirm"})
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_no_managed_launch(game)
        profile = None
        if "profile" in params:
            self._require_configured(game)
            profile = self._resolve_profile(
                game, self._required_text(params, "profile"))
            if profile.is_symlink():
                raise ServiceError(-32602, "invalid_params",
                                   "A symlinked profile cannot be reconciled.",
                                   {"parameter": "profile"})
        revision = self._required_text(params, "revision")
        if (len(revision) != 64 or any(
                char not in "0123456789abcdef" for char in revision)):
            raise ServiceError(-32602, "invalid_params",
                               "'revision' must be a blacklist snapshot revision.",
                               {"parameter": "revision"})
        overrides = params["overrides"]
        if not isinstance(overrides, dict):
            raise ServiceError(-32602, "invalid_params",
                               "'overrides' must be an object.",
                               {"parameter": "overrides"})
        if params.get("confirm") is not True:
            raise ServiceError(-32602, "invalid_params",
                               "'confirm' must be true to change per-game exclusions.",
                               {"parameter": "confirm"})
        from .blacklist import set_blacklist
        job = self._jobs.submit(
            "games.blacklist.set", game, self._canonical_game(game),
            profile.name if profile else None,
            {"revision": revision},
            runner=lambda log, progress: set_blacklist(
                game, profile, revision, overrides,
                log_fn=log, progress_fn=progress),
        )
        return {"job": job}

    def _start_profile_storage(self, params):
        from threading import Event
        from .storage import runner

        self._check_params(params, {"game", "profile", "mods", "include_overwrite", "include_root_folder"},
                           {"game", "profile"})
        game, profile = self._batch_profile(params)
        mods = self._batch_mods(params)
        for key in ("include_overwrite", "include_root_folder"):
            if not isinstance(params.get(key, True), bool):
                raise ServiceError(-32602, "invalid_params", f"'{key}' must be boolean.")
        cancel = Event()
        job = self._jobs.submit("profiles.storage.get", game, self._canonical_game(game), profile.name,
                                {"mods": mods}, runner=runner(game, profile, mods,
                                    params.get("include_overwrite", True), params.get("include_root_folder", True), cancel),
                                cancel=cancel.set)
        return {"job": job}

    def _start_profile_backup(self, params, action):
        from .profile_backups import runner

        required = {"game", "profile"}
        optional = set()
        if action in {"create", "restore"}:
            required.add("revision")
        if action in {"restore", "edit", "remove"}:
            required |= {"backup_id", "backup_revision"}
        if action in {"restore", "remove"}:
            required.add("confirm")
        if action in {"create", "edit"}:
            optional.add("label")
        if action == "edit":
            optional.add("kept")
        self._check_params(params, required | optional, required)
        game, profile = self._batch_profile(params)
        for key in ("revision", "backup_revision", "backup_id"):
            if key in required:
                self._required_text(params, key)
        if "confirm" in required and params["confirm"] is not True:
            raise ServiceError(-32602, "invalid_params", "'confirm' must be true.")
        if "label" in params and (not isinstance(params["label"], str) or len(params["label"]) > 256
                                  or any(char in params["label"] for char in "\x00\r\n")):
            raise ServiceError(-32602, "invalid_params", "'label' must be at most 256 characters on one line.")
        if "kept" in params and not isinstance(params["kept"], bool):
            raise ServiceError(-32602, "invalid_params", "'kept' must be boolean.")
        if action == "edit" and not optional.intersection(params):
            raise ServiceError(-32602, "invalid_params", "Provide 'label' or 'kept'.")
        options = {key: value for key, value in params.items() if key not in {"game", "profile"}}
        job = self._jobs.submit("profiles.backups." + action, game, self._canonical_game(game), profile.name,
                                options, runner=runner(game, profile, action, options))
        return {"job": job}

    def _list_profiles(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(params, {"game"}, {"game"})
        game = self._resolve_game(self._required_text(params, "game"))
        profiles = [
            self._profile_summary(game, profile_dir)
            for profile_dir in self._profile_dirs(game)
        ]
        profiles.sort(key=lambda profile: (
            profile["name"] != "default",
            profile["name"].casefold(),
            profile["name"],
        ))
        return {
            "game": self._canonical_game(game),
            "profile_root": self._path_text(game.get_profile_root()),
            "profiles": profiles,
        }

    def _start_create_profile(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(
            params, {"game", "name", "profile_specific_mods", "copy_from"},
            {"game", "name"})
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_no_managed_launch(game)
        self._require_configured(game)
        name = self._profile_name(params.get("name"), "name")

        specific = params.get("profile_specific_mods", False)
        if not isinstance(specific, bool):
            raise ServiceError(
                -32602,
                "invalid_params",
                "'profile_specific_mods' must be a boolean.",
                {"parameter": "profile_specific_mods"},
            )
        copy_from = params.get("copy_from")
        if copy_from is not None:
            copy_from = self._profile_name(copy_from, "copy_from")
            source_dir = self._resolve_profile(game, copy_from)
            copy_from = source_dir.name
            if "profile_specific_mods" in params:
                raise ServiceError(
                    -32602,
                    "invalid_params",
                    "'profile_specific_mods' cannot be supplied with "
                    "'copy_from'.",
                    {"parameter": "profile_specific_mods"},
                )

        job = self._jobs.submit(
            "profiles.create",
            game,
            self._canonical_game(game),
            name,
            {
                "profile_specific_mods": specific,
                "copy_from": copy_from,
            },
        )
        return {"job": job}

    def _start_copy_profile(self, params: dict[str, Any]) -> dict[str, Any]:
        import threading

        self._require_idle_state()
        self._check_params(params, {"game", "profile", "name"}, {"game", "profile", "name"})
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_no_managed_launch(game)
        self._require_configured(game)
        source = self._resolve_profile(game, self._required_text(params, "profile"))
        name = self._profile_name(params.get("name"), "name")
        stop = threading.Event()

        def run(log, progress):
            from Utils.deployment.locking import coordinated_game_mutation
            from Utils.profiles.copy import copy_profile, InsufficientCopySpace
            from Utils.profiles.manage import _ensure_new_name, _require_profile
            from .jobs import JobCancelled, JobError

            try:
                with coordinated_game_mutation(game, "profiles.copy", owner="headless:profiles.copy"):
                    _require_profile(game, source.name)
                    _ensure_new_name(game, name)
                    target = copy_profile(
                        game, source, name, stop=stop,
                        progress_fn=lambda done, total: progress(done, total, "Copying profile"))
                log(f"Copied '{source.name}' to '{name}'.")
                return {"ok": True, "profile": name, "copied_from": source.name,
                        "path": str(target), "profile_specific_mods": True}
            except InterruptedError as exc:
                raise JobCancelled(str(exc)) from exc
            except InsufficientCopySpace as exc:
                raise JobError("insufficient_disk_space", str(exc),
                               {"required": exc.required, "available": exc.available}) from exc
            except (OSError, ValueError) as exc:
                raise JobError("profile_copy_failed", str(exc)) from exc

        return {"job": self._jobs.submit(
            "profiles.copy", game, self._canonical_game(game), source.name,
            {"name": name}, runner=run, cancel=stop.set)}

    def _group_member_names(
        self, game: Any, raw_members: Any, *, allow_empty: bool,
    ) -> list[str]:
        if not isinstance(raw_members, list) or (not raw_members and not allow_empty):
            message = ("'members' must be a list." if allow_empty else
                       "'members' must be a non-empty list.")
            raise ServiceError(
                -32602, "invalid_params", message, {"parameter": "members"},
            )
        members = []
        for raw in raw_members:
            member = self._resolve_profile(
                game, self._profile_name(raw, "members"))
            if member.is_symlink() or member.name in members:
                raise ServiceError(
                    -32602, "invalid_params",
                    "Members must be distinct real profiles.",
                    {"parameter": "members", "profile": member.name},
                )
            members.append(member.name)
        return members

    def _start_create_group(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(
            params,
            {"game", "name", "members", "ini_source", "overwrite_excluded"},
            {"game", "name", "members"},
        )
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_no_managed_launch(game)
        self._require_configured(game)
        name = self._profile_name(params["name"], "name")

        members = self._group_member_names(
            game, params["members"], allow_empty=False)

        def selected_member(raw: Any, parameter: str) -> str:
            member = self._resolve_profile(
                game, self._profile_name(raw, parameter)).name
            if member not in members:
                raise ServiceError(
                    -32602, "invalid_params",
                    f"'{parameter}' must name a selected member.",
                    {"parameter": parameter, "profile": member},
                )
            return member

        ini_source = (selected_member(params["ini_source"], "ini_source")
                      if "ini_source" in params else None)
        raw_excluded = params.get("overwrite_excluded", [])
        if not isinstance(raw_excluded, list):
            raise ServiceError(
                -32602, "invalid_params",
                "'overwrite_excluded' must be a list.",
                {"parameter": "overwrite_excluded"},
            )
        excluded = []
        for raw in raw_excluded:
            member = selected_member(raw, "overwrite_excluded")
            if member in excluded:
                raise ServiceError(
                    -32602, "invalid_params",
                    "'overwrite_excluded' contains a duplicate member.",
                    {"parameter": "overwrite_excluded", "profile": member},
                )
            excluded.append(member)

        job = self._jobs.submit(
            "profiles.groups.create", game, self._canonical_game(game), name,
            {"members": members, "ini_source": ini_source,
             "overwrite_excluded": excluded},
        )
        return {"job": job}

    def _start_set_group_members(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(
            params, {"game", "profile", "members", "expected_members"},
            {"game", "profile", "members", "expected_members"},
        )
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_no_managed_launch(game)
        self._require_configured(game)
        group = self._resolve_profile(
            game, self._required_text(params, "profile"))
        members = self._group_member_names(
            game, params["members"], allow_empty=True)
        raw_expected = params["expected_members"]
        if not isinstance(raw_expected, list):
            raise ServiceError(
                -32602, "invalid_params",
                "'expected_members' must be a list.",
                {"parameter": "expected_members"},
            )
        expected_members = [
            self._profile_name(raw, "expected_members")
            for raw in raw_expected
        ]
        job = self._jobs.submit(
            "profiles.groups.set_members", game,
            self._canonical_game(game), group.name,
            {"members": members, "expected_members": expected_members},
        )
        return {"job": job}

    def _get_group_exclusions(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(params, {"game", "profile"}, {"game", "profile"})
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_configured(game)
        group = self._resolve_profile(
            game, self._required_text(params, "profile"))
        from .group_exclusions import get_exclusions
        from .jobs import JobError
        try:
            return get_exclusions(group)
        except JobError as exc:
            raise ServiceError(-32003, exc.name, str(exc), exc.details) from exc

    def _start_set_group_exclusions(
        self, params: dict[str, Any],
    ) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(
            params, {"game", "profile", "exclusions", "revision", "confirm"},
            {"game", "profile", "exclusions", "revision", "confirm"})
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_no_managed_launch(game)
        self._require_configured(game)
        group = self._resolve_profile(
            game, self._required_text(params, "profile"))
        revision = self._required_text(params, "revision")
        if (len(revision) != 64 or any(
                char not in "0123456789abcdef" for char in revision)):
            raise ServiceError(-32602, "invalid_params",
                               "'revision' must be an exclusion snapshot revision.",
                               {"parameter": "revision"})
        if params["confirm"] is not True:
            raise ServiceError(-32602, "invalid_params",
                               "'confirm' must be true to change group exclusions.",
                               {"parameter": "confirm"})
        raw = params["exclusions"]
        if not isinstance(raw, list):
            raise ServiceError(-32602, "invalid_params",
                               "'exclusions' must be a list.",
                               {"parameter": "exclusions"})
        selected = []
        for item in raw:
            if not isinstance(item, dict) or set(item) != {"member", "mod"}:
                raise ServiceError(-32602, "invalid_params",
                                   "Each exclusion needs 'member' and 'mod'.",
                                   {"parameter": "exclusions"})
            selected.append({
                "member": self._profile_name(item["member"], "member"),
                "mod": self._mod_name(item["mod"], "mod"),
            })
        from .group_exclusions import set_exclusions
        job = self._jobs.submit(
            "profiles.groups.exclusions.set", game,
            self._canonical_game(game), group.name,
            {"revision": revision, "count": len(selected)},
            runner=lambda log, progress: set_exclusions(
                game, group, selected, revision, log, progress),
        )
        return {"job": job}

    def _start_convert_profile(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(
            params, {"game", "profile", "confirm"},
            {"game", "profile", "confirm"},
        )
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_no_managed_launch(game)
        self._require_configured(game)
        profile_dir = self._resolve_profile(
            game, self._required_text(params, "profile"))
        if params["confirm"] is not True:
            raise ServiceError(
                -32602, "invalid_params",
                "'confirm' must be true because profile conversion changes "
                "the profile's mod storage.",
                {"parameter": "confirm"},
            )
        job = self._jobs.submit(
            "profiles.convert", game, self._canonical_game(game),
            profile_dir.name,
        )
        return {"job": job}

    def _start_rename_profile(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(
            params, {"game", "profile", "new_name"},
            {"game", "profile", "new_name"})
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_no_managed_launch(game)
        self._require_configured(game)
        profile_dir = self._resolve_profile(
            game, self._required_text(params, "profile"))
        new_name = self._profile_name(params.get("new_name"), "new_name")
        job = self._jobs.submit(
            "profiles.rename",
            game,
            self._canonical_game(game),
            profile_dir.name,
            {"new_name": new_name},
        )
        return {"job": job}

    def _start_remove_profile(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(
            params, {"game", "profile", "confirm"},
            {"game", "profile", "confirm"})
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_no_managed_launch(game)
        self._require_configured(game)
        profile_dir = self._resolve_profile(
            game, self._required_text(params, "profile"))
        if params.get("confirm") is not True:
            raise ServiceError(
                -32602,
                "invalid_params",
                "'confirm' must be true because profile removal is irreversible.",
                {"parameter": "confirm"},
            )
        job = self._jobs.submit(
            "profiles.remove",
            game,
            self._canonical_game(game),
            profile_dir.name,
        )
        return {"job": job}

    def _start_select_profile(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(params, {"game", "profile"}, {"game", "profile"})
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_no_managed_launch(game)
        self._require_configured(game)
        profile_dir = self._resolve_profile(
            game, self._required_text(params, "profile"))
        job = self._jobs.submit(
            "profiles.select",
            game,
            self._canonical_game(game),
            profile_dir.name,
        )
        return {"job": job}

    def _list_installed_collections(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(params, {"game", "profile"}, {"game"})
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_configured(game)
        profile_dir = None
        if "profile" in params:
            profile_dir = self._resolve_profile(
                game, self._required_text(params, "profile"))
            if profile_dir.is_symlink():
                raise ServiceError(
                    -32003, "unsafe_profile_path",
                    "The selected profile is a symbolic link.",
                    {"profile": profile_dir.name},
                )
        from .collections import installed_inventory
        return installed_inventory(game, self._canonical_game(game), profile_dir)

    def _start_install_collection(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(
            params,
            {"game", "profile", "domain", "slug", "revision",
             "skipped_optional_file_ids", "accept_game_version_mismatch",
             "manual_archives", "manual_interactive", "interactive"},
            {"game", "profile", "slug"},
        )
        game, domain = self._resolve_nexus_domain(params)
        self._require_no_managed_launch(game)
        self._require_configured(game)
        profile = self._profile_name(params.get("profile"), "profile")
        slug = self._required_text(params, "slug")
        if (len(slug) > 128 or not slug.isascii()
                or any(not (char.isalnum() or char in "-_") for char in slug)):
            raise ServiceError(
                -32602, "invalid_params",
                "'slug' must be a Nexus collection slug.",
                {"parameter": "slug"},
            )
        revision = None
        if "revision" in params:
            revision = self._positive_integer(params, "revision")
        raw_skipped = params.get("skipped_optional_file_ids", [])
        if (not isinstance(raw_skipped, list) or len(raw_skipped) > 4096
                or any(isinstance(value, bool) or not isinstance(value, int)
                       or value <= 0 for value in raw_skipped)
                or len(set(raw_skipped)) != len(raw_skipped)):
            raise ServiceError(
                -32602, "invalid_params",
                "'skipped_optional_file_ids' must contain unique positive integers.",
                {"parameter": "skipped_optional_file_ids"},
            )
        accept_mismatch = params.get("accept_game_version_mismatch", False)
        if not isinstance(accept_mismatch, bool):
            raise ServiceError(
                -32602, "invalid_params",
                "'accept_game_version_mismatch' must be a boolean.",
                {"parameter": "accept_game_version_mismatch"},
            )
        manual_archives = self._collection_manual_archives(params)
        from Utils.collections.install import CollectionInstallControl
        from .collections import install_new_runner
        control = CollectionInstallControl()
        requests = self._installer_requests(params)
        if requests is not None:
            requests.stop = control.stop
        manual_session = self._collection_manual_session(
            params, control, manual_archives)

        def cancel() -> None:
            control.cancel.set()
            control.stop.set()

        job = self._jobs.submit(
            "collections.install", game, self._canonical_game(game), profile,
            {"domain": domain, "slug": slug, "revision": revision,
             "skipped_optional_file_ids": sorted(raw_skipped),
             "accept_game_version_mismatch": accept_mismatch,
             "manual_archives": self._manual_archive_options(manual_archives),
             "manual_interactive": manual_session is not None},
            runner=install_new_runner(
                game, profile, domain, slug, revision, set(raw_skipped),
                accept_mismatch, self._nexus_session, control,
                "headless:collections.install",
                manual_archive_paths=manual_archives,
                manual_session=manual_session, installer_requests=requests),
            cancel=cancel, installer_requests=requests,
        )
        self._bind_manual_collection(manual_session, job)
        return {"job": job}

    def _manual_collection_state(self, job_id: str) -> tuple[dict[str, Any], Any]:
        try:
            job = self._jobs.get(job_id)
        except KeyError:
            self._job_not_found(job_id)
        session = self._manual_collection_sessions.get(job_id)
        if session is None:
            raise ServiceError(
                -32024, "collection_manual_unavailable",
                "This job does not use interactive manual collection downloads.",
                {"job_id": job_id},
            )
        return job, session

    def _get_manual_collection(self, params: dict[str, Any]) -> dict[str, Any]:
        self._check_params(params, {"job_id"}, {"job_id"})
        job, session = self._manual_collection_state(
            self._required_text(params, "job_id"))
        return {"job": job,
                "request": (session.current() if job["state"] in {"running", "waiting_for_input"}
                            else None)}

    def _submit_manual_collection(self, params: dict[str, Any]) -> dict[str, Any]:
        self._check_params(
            params, {"job_id", "request_id", "path", "skip"},
            {"job_id", "request_id"})
        job_id = self._required_text(params, "job_id")
        request_id = params["request_id"]
        skip = params.get("skip", False)
        if (isinstance(request_id, bool) or not isinstance(request_id, int)
                or request_id <= 0 or not isinstance(skip, bool)
                or ("path" in params) == skip):
            raise ServiceError(
                -32602, "invalid_params",
                "Submit an archive path or skip an optional archive request.",
                {"parameter": "request_id"},
            )
        path = None
        if "path" in params:
            value = self._required_text(params, "path")
            path = Path(value).expanduser()
        job, session = self._manual_collection_state(job_id)
        if job["state"] not in {"running", "waiting_for_input"} or job["cancel_requested"]:
            raise ServiceError(
                -32025, "collection_manual_job_inactive",
                "The collection job is no longer accepting manual archives.",
                {"job_id": job_id},
            )
        from .manual_collections import ManualRequestError
        try:
            return session.submit(request_id, path)
        except ManualRequestError as exc:
            raise ServiceError(
                -32026, exc.name, str(exc),
                {"job_id": job_id, "request_id": request_id},
            ) from exc

    def _start_profile_share(self, params):
        import threading
        from .profile_share import code_runner

        self._require_idle_state()
        self._check_params(params, {"game", "profile"}, {"game", "profile"})
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_configured(game)
        profile = self._resolve_profile(game, self._required_text(params, "profile"))
        stop = threading.Event()
        job = self._jobs.submit(
            "profiles.share", game, self._canonical_game(game), profile.name,
            runner=code_runner(game, profile, stop), cancel=stop.set)
        return {"job": job}

    def _inspect_profile_share(self, params):
        from .jobs import JobError
        from .profile_share import inspect_code

        self._check_params(params, {"code"}, {"code"})
        self._required_text(params, "code")
        try:
            return inspect_code(params["code"])
        except JobError as exc:
            raise ServiceError(-32602, exc.name, str(exc), {"parameter": "code"}) from exc

    def _start_profile_export(self, params, action):
        import threading
        import uuid
        from datetime import datetime, timezone
        from .profile_export import OPTION_KEYS, runner, validate_options
        from .profile_share import _name

        self._require_idle_state()
        fields = {"game", "profile"} | OPTION_KEYS
        required = {"game", "profile"}
        if action == "export":
            fields |= {"path", "revision", "overwrite", "allow_patch_warnings"}
            required |= {"path", "revision"}
        elif action == "settings":
            fields |= {"name", "revision", "overwrite"}
            required.add("revision")
        self._check_params(params, fields, required)
        try:
            options = validate_options(params)
        except ValueError as exc:
            raise ServiceError(-32602, "invalid_params", str(exc)) from exc
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_no_managed_launch(game)
        self._require_configured(game)
        profile = self._resolve_profile(game, self._required_text(params, "profile"))
        revision = None
        if action != "get":
            revision = self._required_text(params, "revision")
            if len(revision) != 64 or any(char not in "0123456789abcdef" for char in revision):
                raise ServiceError(-32602, "invalid_params", "'revision' must come from an export preview.")
        overwrite = params.get("overwrite", False)
        allow_warnings = params.get("allow_patch_warnings", False)
        if not isinstance(overwrite, bool) or not isinstance(allow_warnings, bool):
            raise ServiceError(-32602, "invalid_params", "Overwrite and warning acknowledgement must be boolean.")
        destination = settings_name = None
        if action == "export":
            destination = Path(self._required_text(params, "path")).expanduser()
            if not destination.is_absolute() or destination.suffix.casefold() not in (".amethyst", ".zip"):
                raise ServiceError(-32602, "invalid_params", "'path' must be an absolute .amethyst or ZIP path.")
        elif action == "settings":
            settings_name = self._required_text(params, "name") if "name" in params else (
                "export_settings_" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
                + "-" + uuid.uuid4().hex[:8] + ".json")
            try:
                _name(settings_name)
                if not settings_name.endswith(".json") or len(settings_name.encode("utf-8")) > 255:
                    raise ValueError("Use a safe JSON filename for export settings.")
            except (ValueError, TypeError) as exc:
                raise ServiceError(-32602, "invalid_params", str(exc), {"parameter": "name"}) from exc
        stop = threading.Event()
        kind = {"get": "profiles.export.inspect", "export": "profiles.export",
                "settings": "profiles.export.settings.save"}[action]
        job = self._jobs.submit(
            kind, game, self._canonical_game(game), profile.name,
            {**options, "revision": revision, "path": str(destination) if destination else None,
             "settings_name": settings_name, "overwrite": overwrite,
             "allow_patch_warnings": allow_warnings},
            runner=runner(game, profile, options, stop, revision=revision,
                          destination=destination, overwrite=overwrite,
                          allow_patch_warnings=allow_warnings, settings_name=settings_name),
            cancel=stop.set)
        return {"job": job}

    def _start_import_collection(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(
            params,
            {"game", "profile", "path", "code", "skipped_optional_file_ids",
             "accept_game_version_mismatch", "manual_archives",
             "manual_interactive", "interactive"},
            {"game", "profile"},
        )
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_no_managed_launch(game)
        self._require_configured(game)
        profile = self._profile_name(params.get("profile"), "profile")
        if ("path" in params) == ("code" in params):
            raise ServiceError(
                -32602, "invalid_params", "Supply exactly one of 'path' or 'code'.",
            )
        source_path = manifest = None
        if "code" in params:
            manifest = self._inspect_profile_share({"code": params["code"]})["manifest"]
        else:
            raw_path = Path(self._required_text(params, "path")).expanduser()
            if not raw_path.is_absolute():
                raise ServiceError(
                    -32602, "invalid_params", "'path' must be an absolute path.",
                    {"parameter": "path"},
                )
            source_path = raw_path.absolute()
            if (source_path.suffix.casefold() not in {".amethyst", ".zip", ".json", ".r2x", ".txt"}
                    or source_path.is_symlink() or not source_path.is_file()):
                raise ServiceError(
                    -32003, "resource_unavailable",
                    "The local profile manifest was not found or is unsafe.",
                    {"path": str(source_path)},
                )
        raw_skipped = params.get("skipped_optional_file_ids", [])
        if (not isinstance(raw_skipped, list) or len(raw_skipped) > 4096
                or any(isinstance(value, bool) or not isinstance(value, int)
                       or value <= 0 for value in raw_skipped)
                or len(set(raw_skipped)) != len(raw_skipped)):
            raise ServiceError(
                -32602, "invalid_params",
                "'skipped_optional_file_ids' must contain unique positive integers.",
                {"parameter": "skipped_optional_file_ids"},
            )
        accept_mismatch = params.get("accept_game_version_mismatch", False)
        if not isinstance(accept_mismatch, bool):
            raise ServiceError(
                -32602, "invalid_params",
                "'accept_game_version_mismatch' must be a boolean.",
                {"parameter": "accept_game_version_mismatch"},
            )
        manual_archives = self._collection_manual_archives(params)
        from Utils.collections.install import CollectionInstallControl
        from .collections import import_local_runner
        control = CollectionInstallControl()
        requests = self._installer_requests(params)
        if requests is not None:
            requests.stop = control.stop
        manual_session = self._collection_manual_session(
            params, control, manual_archives)

        def cancel() -> None:
            control.cancel.set()
            control.stop.set()

        if manifest is None:
            runner = import_local_runner(
                game, profile, source_path, set(raw_skipped), accept_mismatch,
                self._nexus_session, control, "headless:collections.import",
                manual_archive_paths=manual_archives, manual_session=manual_session, installer_requests=requests)
            source_options = {"path": str(source_path)}
        else:
            from .profile_share import import_runner
            runner = import_runner(
                manifest, game, profile, set(raw_skipped), accept_mismatch,
                self._nexus_session, control, "headless:collections.import",
                manual_archive_paths=manual_archives, manual_session=manual_session, installer_requests=requests)
            source_options = {"source": "share_code", "mod_count": len(manifest["mods"])}

        job = self._jobs.submit(
            "collections.import", game, self._canonical_game(game), profile,
            {**source_options,
             "skipped_optional_file_ids": sorted(raw_skipped),
             "accept_game_version_mismatch": accept_mismatch,
             "manual_archives": self._manual_archive_options(manual_archives),
             "manual_interactive": manual_session is not None},
            runner=runner,
            cancel=cancel, installer_requests=requests,
        )
        self._bind_manual_collection(manual_session, job)
        return {"job": job}

    def _start_download_collection(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(
            params, {"game", "domain", "slug", "revision"},
            {"game", "slug"},
        )
        game, domain = self._resolve_nexus_domain(params)
        slug = self._required_text(params, "slug")
        if (len(slug) > 128 or not slug.isascii()
                or any(not (char.isalnum() or char in "-_") for char in slug)):
            raise ServiceError(
                -32602, "invalid_params",
                "'slug' must be a Nexus collection slug.",
                {"parameter": "slug"},
            )
        revision = (self._positive_integer(params, "revision")
                    if "revision" in params else None)
        from Utils.collections.install import CollectionInstallControl
        from .collections import download_runner
        control = CollectionInstallControl()

        def cancel() -> None:
            control.cancel.set()
            control.stop.set()

        job = self._jobs.submit(
            "collections.download", game, self._canonical_game(game), None,
            {"domain": domain, "slug": slug, "revision": revision},
            runner=download_runner(
                game, domain, slug, revision, self._nexus_session, control),
            cancel=cancel,
        )
        return {"job": job}

    def _start_resume_collection(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(
            params,
            {"game", "profile", "collection_id", "state_revision",
             "accept_game_version_mismatch", "manual_archives",
             "manual_interactive", "interactive"},
            {"game", "profile", "collection_id", "state_revision"},
        )
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_no_managed_launch(game)
        self._require_configured(game)
        profile_dir = self._resolve_profile(
            game, self._required_text(params, "profile"))
        if profile_dir.is_symlink():
            raise ServiceError(
                -32003, "unsafe_profile_path",
                "The selected profile is a symbolic link.",
                {"profile": profile_dir.name},
            )
        collection_id = self._required_text(params, "collection_id")
        if (len(collection_id) > 128
                or any(char in collection_id for char in "\x00\r\n")):
            raise ServiceError(
                -32602, "invalid_params",
                "'collection_id' must be an ID returned by collections.installed.list.",
                {"parameter": "collection_id"},
            )
        revision = self._required_text(params, "state_revision")
        if (len(revision) != 64
                or any(char not in "0123456789abcdef" for char in revision)):
            raise ServiceError(
                -32602, "invalid_params",
                "'state_revision' must be returned by collections.installed.list.",
                {"parameter": "state_revision"},
            )
        accept_mismatch = params.get("accept_game_version_mismatch", False)
        if not isinstance(accept_mismatch, bool):
            raise ServiceError(
                -32602, "invalid_params",
                "'accept_game_version_mismatch' must be a boolean.",
                {"parameter": "accept_game_version_mismatch"},
            )
        manual_archives = self._collection_manual_archives(params)
        from Utils.collections.install import CollectionInstallControl
        from .collections import resume_runner
        control = CollectionInstallControl()
        requests = self._installer_requests(params)
        if requests is not None:
            requests.stop = control.stop
        manual_session = self._collection_manual_session(
            params, control, manual_archives)

        def cancel() -> None:
            control.cancel.set()
            control.stop.set()

        job = self._jobs.submit(
            "collections.resume", game, self._canonical_game(game),
            profile_dir.name,
            {"collection_id": collection_id, "state_revision": revision,
             "accept_game_version_mismatch": accept_mismatch,
             "manual_archives": self._manual_archive_options(manual_archives),
             "manual_interactive": manual_session is not None},
            runner=resume_runner(
                game, profile_dir, collection_id, revision, accept_mismatch,
                self._nexus_session, control,
                "headless:collections.resume",
                manual_archive_paths=manual_archives,
                manual_session=manual_session, installer_requests=requests),
            cancel=cancel, installer_requests=requests,
        )
        self._bind_manual_collection(manual_session, job)
        return {"job": job}

    def _start_update_collection(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(
            params,
            {"game", "profile", "collection_id", "state_revision",
             "target_revision", "skipped_optional_file_ids",
             "accept_game_version_mismatch", "confirm", "manual_archives",
             "manual_interactive", "interactive"},
            {"game", "profile", "collection_id", "state_revision",
             "target_revision", "confirm"},
        )
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_no_managed_launch(game)
        self._require_configured(game)
        profile_dir = self._resolve_profile(
            game, self._required_text(params, "profile"))
        if profile_dir.is_symlink():
            raise ServiceError(
                -32003, "unsafe_profile_path",
                "The selected profile is a symbolic link.",
                {"profile": profile_dir.name},
            )
        collection_id = self._required_text(params, "collection_id")
        if (len(collection_id) > 128
                or any(char in collection_id for char in "\x00\r\n")):
            raise ServiceError(
                -32602, "invalid_params",
                "'collection_id' must be an ID returned by collections.installed.list.",
                {"parameter": "collection_id"},
            )
        state_revision = self._required_text(params, "state_revision")
        if (len(state_revision) != 64
                or any(char not in "0123456789abcdef" for char in state_revision)):
            raise ServiceError(
                -32602, "invalid_params",
                "'state_revision' must be returned by collections.installed.list.",
                {"parameter": "state_revision"},
            )
        target_revision = self._positive_integer(params, "target_revision")
        skipped = None
        if "skipped_optional_file_ids" in params:
            raw_skipped = params["skipped_optional_file_ids"]
            if (not isinstance(raw_skipped, list) or len(raw_skipped) > 4096
                    or any(isinstance(value, bool) or not isinstance(value, int)
                           or value <= 0 for value in raw_skipped)
                    or len(set(raw_skipped)) != len(raw_skipped)):
                raise ServiceError(
                    -32602, "invalid_params",
                    "'skipped_optional_file_ids' must contain unique positive integers.",
                    {"parameter": "skipped_optional_file_ids"},
                )
            skipped = set(raw_skipped)
        accept_mismatch = params.get("accept_game_version_mismatch", False)
        if not isinstance(accept_mismatch, bool):
            raise ServiceError(
                -32602, "invalid_params",
                "'accept_game_version_mismatch' must be a boolean.",
                {"parameter": "accept_game_version_mismatch"},
            )
        if params.get("confirm") is not True:
            raise ServiceError(
                -32602, "invalid_params",
                "'confirm' must be true because a collection update can retire mods.",
                {"parameter": "confirm"},
            )
        manual_archives = self._collection_manual_archives(params)
        from Utils.collections.install import CollectionInstallControl
        from .collections import update_runner
        control = CollectionInstallControl()
        requests = self._installer_requests(params)
        if requests is not None:
            requests.stop = control.stop
        manual_session = self._collection_manual_session(
            params, control, manual_archives)

        def cancel() -> None:
            control.cancel.set()
            control.stop.set()

        job = self._jobs.submit(
            "collections.update", game, self._canonical_game(game),
            profile_dir.name,
            {"collection_id": collection_id,
             "state_revision": state_revision,
             "target_revision": target_revision,
             "skipped_optional_file_ids": (
                 sorted(skipped) if skipped is not None else None),
             "accept_game_version_mismatch": accept_mismatch,
             "confirm": True,
             "manual_archives": self._manual_archive_options(manual_archives),
             "manual_interactive": manual_session is not None},
            runner=update_runner(
                game, profile_dir, collection_id, state_revision,
                target_revision, skipped, accept_mismatch,
                self._nexus_session, control,
                "headless:collections.update",
                manual_archive_paths=manual_archives,
                manual_session=manual_session, installer_requests=requests),
            cancel=cancel, installer_requests=requests,
        )
        self._bind_manual_collection(manual_session, job)
        return {"job": job}

    def _start_append_collection(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(
            params,
            {"game", "profile", "domain", "slug", "revision",
             "skipped_optional_file_ids", "overwrite_existing",
             "skip_existing", "accept_game_version_mismatch", "confirm",
             "manual_archives", "manual_interactive", "interactive"},
            {"game", "profile", "slug", "confirm"},
        )
        game, domain = self._resolve_nexus_domain(params)
        self._require_no_managed_launch(game)
        self._require_configured(game)
        profile_dir = self._resolve_profile(
            game, self._required_text(params, "profile"))
        if profile_dir.is_symlink():
            raise ServiceError(
                -32003, "unsafe_profile_path",
                "The selected profile is a symbolic link.",
                {"profile": profile_dir.name},
            )
        slug = self._required_text(params, "slug")
        if (len(slug) > 128 or not slug.isascii()
                or any(not (char.isalnum() or char in "-_") for char in slug)):
            raise ServiceError(
                -32602, "invalid_params",
                "'slug' must be a Nexus collection slug.",
                {"parameter": "slug"},
            )
        revision = (self._positive_integer(params, "revision")
                    if "revision" in params else None)
        raw_skipped = params.get("skipped_optional_file_ids", [])
        if (not isinstance(raw_skipped, list) or len(raw_skipped) > 4096
                or any(isinstance(value, bool) or not isinstance(value, int)
                       or value <= 0 for value in raw_skipped)
                or len(set(raw_skipped)) != len(raw_skipped)):
            raise ServiceError(
                -32602, "invalid_params",
                "'skipped_optional_file_ids' must contain unique positive integers.",
                {"parameter": "skipped_optional_file_ids"},
            )
        overwrite_existing = params.get("overwrite_existing", False)
        skip_existing = params.get("skip_existing", False)
        accept_mismatch = params.get("accept_game_version_mismatch", False)
        if any(not isinstance(value, bool) for value in (
                overwrite_existing, skip_existing, accept_mismatch)):
            raise ServiceError(
                -32602, "invalid_params",
                "Append policy parameters must be booleans.",
            )
        if params.get("confirm") is not True:
            raise ServiceError(
                -32602, "invalid_params",
                "'confirm' must be true because append changes an existing profile.",
                {"parameter": "confirm"},
            )
        manual_archives = self._collection_manual_archives(params)
        from Utils.collections.install import CollectionInstallControl
        from .collections import append_runner
        control = CollectionInstallControl()
        requests = self._installer_requests(params)
        if requests is not None:
            requests.stop = control.stop
        manual_session = self._collection_manual_session(
            params, control, manual_archives)

        def cancel() -> None:
            control.cancel.set()
            control.stop.set()

        job = self._jobs.submit(
            "collections.append", game, self._canonical_game(game),
            profile_dir.name,
            {"domain": domain, "slug": slug, "revision": revision,
             "skipped_optional_file_ids": sorted(raw_skipped),
             "overwrite_existing": overwrite_existing,
             "skip_existing": skip_existing,
             "accept_game_version_mismatch": accept_mismatch,
             "confirm": True,
             "manual_archives": self._manual_archive_options(manual_archives),
             "manual_interactive": manual_session is not None},
            runner=append_runner(
                game, profile_dir, domain, slug, revision, set(raw_skipped),
                overwrite_existing, skip_existing, accept_mismatch,
                self._nexus_session, control,
                "headless:collections.append",
                manual_archive_paths=manual_archives,
                manual_session=manual_session, installer_requests=requests),
            cancel=cancel, installer_requests=requests,
        )
        self._bind_manual_collection(manual_session, job)
        return {"job": job}

    def _start_set_collection_enabled(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(
            params,
            {"game", "profile", "collection_id", "state_revision", "enabled"},
            {"game", "profile", "collection_id", "state_revision", "enabled"},
        )
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_no_managed_launch(game)
        self._require_configured(game)
        profile_dir = self._resolve_profile(
            game, self._required_text(params, "profile"))
        if profile_dir.is_symlink():
            raise ServiceError(
                -32003, "unsafe_profile_path",
                "The selected profile is a symbolic link.",
                {"profile": profile_dir.name},
            )
        collection_id = self._required_text(params, "collection_id")
        if (len(collection_id) > 128
                or any(char in collection_id for char in "\x00\r\n")):
            raise ServiceError(
                -32602, "invalid_params",
                "'collection_id' must be an ID returned by collections.installed.list.",
                {"parameter": "collection_id"},
            )
        revision = self._required_text(params, "state_revision")
        if (len(revision) != 64
                or any(char not in "0123456789abcdef" for char in revision)):
            raise ServiceError(
                -32602, "invalid_params",
                "'state_revision' must be returned by collections.installed.list.",
                {"parameter": "state_revision"},
            )
        enabled = params.get("enabled")
        if not isinstance(enabled, bool):
            raise ServiceError(
                -32602, "invalid_params", "'enabled' must be a boolean.",
                {"parameter": "enabled"},
            )
        from .collections import set_enabled_runner
        job = self._jobs.submit(
            "collections.set_enabled",
            game,
            self._canonical_game(game),
            profile_dir.name,
            {"collection_id": collection_id, "state_revision": revision,
             "enabled": enabled},
            runner=set_enabled_runner(
                game, profile_dir, collection_id, revision, enabled,
                "headless:collections.set_enabled"),
        )
        return {"job": job}

    def _start_group_collection(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(
            params,
            {"game", "profile", "target", "group_name", "collection_id",
             "state_revision", "ini_source", "overwrite_excluded"},
            {"game", "profile", "target", "collection_id", "state_revision"},
        )
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_no_managed_launch(game)
        self._require_configured(game)
        profile_dir = self._resolve_profile(
            game, self._required_text(params, "profile"))
        target_dir = self._resolve_profile(
            game, self._required_text(params, "target"))
        if profile_dir.is_symlink() or target_dir.is_symlink():
            raise ServiceError(
                -32003, "unsafe_profile_path",
                "Collection grouping requires real profile directories.",
                {"profile": profile_dir.name, "target": target_dir.name},
            )
        if profile_dir == target_dir:
            raise ServiceError(
                -32602, "invalid_params",
                "'target' must name a different profile or profile group.",
                {"parameter": "target"},
            )

        from Utils.profiles.groups import is_group
        target_is_group = is_group(target_dir)
        if is_group(profile_dir):
            raise ServiceError(
                -32602, "invalid_params",
                "'profile' must name the collection's member profile.",
                {"parameter": "profile", "profile": profile_dir.name},
            )
        group_name = ""
        if target_is_group:
            if ("group_name" in params or "ini_source" in params
                    or "overwrite_excluded" in params):
                raise ServiceError(
                    -32602, "invalid_params",
                    "Group creation options cannot be supplied for an existing group.",
                    {"parameter": "target", "target": target_dir.name},
                )
        else:
            if "group_name" not in params:
                raise ServiceError(
                    -32602, "invalid_params",
                    "'group_name' is required when 'target' is a profile.",
                    {"parameter": "group_name"},
                )
            group_name = self._profile_name(params["group_name"], "group_name")

        selected = {profile_dir.name, target_dir.name}

        def selected_member(raw: Any, parameter: str) -> str:
            member = self._resolve_profile(
                game, self._profile_name(raw, parameter)).name
            if member not in selected:
                raise ServiceError(
                    -32602, "invalid_params",
                    f"'{parameter}' must name the collection or target profile.",
                    {"parameter": parameter, "profile": member},
                )
            return member

        ini_source = (selected_member(params["ini_source"], "ini_source")
                      if "ini_source" in params else None)
        raw_excluded = params.get("overwrite_excluded", [])
        if not isinstance(raw_excluded, list):
            raise ServiceError(
                -32602, "invalid_params",
                "'overwrite_excluded' must be a list.",
                {"parameter": "overwrite_excluded"},
            )
        excluded = []
        for raw in raw_excluded:
            member = selected_member(raw, "overwrite_excluded")
            if member in excluded:
                raise ServiceError(
                    -32602, "invalid_params",
                    "'overwrite_excluded' contains a duplicate member.",
                    {"parameter": "overwrite_excluded", "profile": member},
                )
            excluded.append(member)

        collection_id = self._required_text(params, "collection_id")
        if (len(collection_id) > 128
                or any(char in collection_id for char in "\x00\r\n")):
            raise ServiceError(
                -32602, "invalid_params",
                "'collection_id' must be an ID returned by collections.installed.list.",
                {"parameter": "collection_id"},
            )
        revision = self._required_text(params, "state_revision")
        if (len(revision) != 64
                or any(char not in "0123456789abcdef" for char in revision)):
            raise ServiceError(
                -32602, "invalid_params",
                "'state_revision' must be returned by collections.installed.list.",
                {"parameter": "state_revision"},
            )

        from .collections import group_runner
        job = self._jobs.submit(
            "collections.group", game, self._canonical_game(game),
            profile_dir.name,
            {"target": target_dir.name, "group_name": group_name,
             "collection_id": collection_id, "state_revision": revision,
             "ini_source": ini_source, "overwrite_excluded": excluded},
            runner=group_runner(
                game, profile_dir, target_dir, target_is_group, group_name,
                collection_id, revision, ini_source, excluded),
        )
        return {"job": job}

    def _start_plan_collection_removal(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(
            params, {"game", "profile", "collection_id", "state_revision"},
            {"game", "profile", "collection_id", "state_revision"},
        )
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_configured(game)
        profile_dir = self._resolve_profile(
            game, self._required_text(params, "profile"))
        if profile_dir.is_symlink():
            raise ServiceError(
                -32003, "unsafe_profile_path",
                "The selected profile is a symbolic link.",
                {"profile": profile_dir.name},
            )
        collection_id = self._required_text(params, "collection_id")
        if (len(collection_id) > 128
                or any(char in collection_id for char in "\x00\r\n")):
            raise ServiceError(
                -32602, "invalid_params",
                "'collection_id' must be an ID returned by collections.installed.list.",
                {"parameter": "collection_id"},
            )
        revision = self._required_text(params, "state_revision")
        if (len(revision) != 64
                or any(char not in "0123456789abcdef" for char in revision)):
            raise ServiceError(
                -32602, "invalid_params",
                "'state_revision' must be returned by collections.installed.list.",
                {"parameter": "state_revision"},
            )
        from .collections import removal_plan_runner
        job = self._jobs.submit(
            "collections.remove.plan", game, self._canonical_game(game),
            profile_dir.name,
            {"collection_id": collection_id, "state_revision": revision},
            runner=removal_plan_runner(
                game, profile_dir, collection_id, revision),
        )
        return {"job": job}

    def _start_remove_collection(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(
            params,
            {"game", "profile", "collection_id", "state_revision",
             "plan_revision", "confirm"},
            {"game", "profile", "collection_id", "state_revision",
             "plan_revision", "confirm"},
        )
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_no_managed_launch(game)
        self._require_configured(game)
        profile_dir = self._resolve_profile(
            game, self._required_text(params, "profile"))
        if profile_dir.is_symlink():
            raise ServiceError(
                -32003, "unsafe_profile_path",
                "The selected profile is a symbolic link.",
                {"profile": profile_dir.name},
            )
        collection_id = self._required_text(params, "collection_id")
        if (len(collection_id) > 128
                or any(char in collection_id for char in "\x00\r\n")):
            raise ServiceError(
                -32602, "invalid_params",
                "'collection_id' must be an ID returned by collections.installed.list.",
                {"parameter": "collection_id"},
            )
        revisions = {}
        for name, source in (
                ("state_revision", "collections.installed.list"),
                ("plan_revision", "collections.remove.plan.start")):
            value = self._required_text(params, name)
            if (len(value) != 64
                    or any(char not in "0123456789abcdef" for char in value)):
                raise ServiceError(
                    -32602, "invalid_params",
                    f"'{name}' must be returned by {source}.",
                    {"parameter": name},
                )
            revisions[name] = value
        if params.get("confirm") is not True:
            raise ServiceError(
                -32602, "invalid_params",
                "'confirm' must be true because collection removal can delete mods.",
                {"parameter": "confirm"},
            )
        from .collections import remove_runner
        job = self._jobs.submit(
            "collections.remove", game, self._canonical_game(game),
            profile_dir.name,
            {"collection_id": collection_id, **revisions},
            runner=remove_runner(
                game, profile_dir, collection_id,
                revisions["state_revision"], revisions["plan_revision"],
                "headless:collections.remove"),
        )
        return {"job": job}

    def _start_reset_collection(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(
            params,
            {"game", "profile", "group", "collection_id", "state_revision"},
            {"game", "profile", "collection_id", "state_revision"},
        )
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_no_managed_launch(game)
        self._require_configured(game)
        profile_dir = self._resolve_profile(
            game, self._required_text(params, "profile"))
        from Utils.profiles.groups import get_members, is_group
        if is_group(profile_dir):
            raise ServiceError(
                -32602, "invalid_params",
                "'profile' must name the collection's member profile, not a profile group.",
                {"parameter": "profile", "profile": profile_dir.name},
            )
        group_dir = None
        if "group" in params:
            group_dir = self._resolve_profile(
                game, self._required_text(params, "group"))
            if not is_group(group_dir) or profile_dir.name not in get_members(group_dir):
                raise ServiceError(
                    -32602, "invalid_params",
                    "'group' must name a profile group containing the collection profile.",
                    {"parameter": "group", "profile": profile_dir.name,
                     "group": group_dir.name},
                )
        collection_id = self._required_text(params, "collection_id")
        if (len(collection_id) > 128
                or any(char in collection_id for char in "\x00\r\n")):
            raise ServiceError(
                -32602, "invalid_params",
                "'collection_id' must be an ID returned by collections.installed.list.",
                {"parameter": "collection_id"},
            )
        revision = self._required_text(params, "state_revision")
        if (len(revision) != 64
                or any(char not in "0123456789abcdef" for char in revision)):
            raise ServiceError(
                -32602, "invalid_params",
                "'state_revision' must be returned by collections.installed.list.",
                {"parameter": "state_revision"},
            )
        from .collections import reset_load_order_runner
        job = self._jobs.submit(
            "collections.reset_load_order", game, self._canonical_game(game),
            profile_dir.name,
            {"collection_id": collection_id, "state_revision": revision,
             "group": group_dir.name if group_dir is not None else None},
            runner=reset_load_order_runner(
                game, profile_dir, group_dir, collection_id, revision,
                self._nexus_session, "headless:collections.reset_load_order"),
        )
        return {"job": job}

    def _list_wabbajack(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(params, {"game"})
        games = self._games
        if "game" in params:
            game = self._resolve_game(self._required_text(params, "game"))
            games = {self._canonical_game(game)["id"]: game}
        from Utils.wabbajack.games import matches_game
        from Utils.wabbajack.installed import installed_lists
        rows = installed_lists(games=games)
        return {"installations": [
            {
                "id": str(row.info.get("id") or ""),
                "game": self._canonical_game(games[row.game_name]),
                "name": row.title,
                "status": row.status,
                "directory": str(row.directory),
                "package_game": str(row.info.get("game") or ""),
                "game_matches": bool(
                    not row.info.get("game") or matches_game(
                        games[row.game_name], str(row.info["game"]))),
                "version": row.info.get("version"),
                "profiles": list(row.profiles),
                "locked_profiles": list(row.locked_profiles),
                "groups": list(row.groups),
            }
            for row in rows
        ]}

    def _start_wabbajack_gallery(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(params, {"game", "refresh", "cached_only"})
        game = self._resolve_game(self._required_text(params, "game")) if "game" in params else None
        refresh = params.get("refresh", False)
        cached_only = params.get("cached_only", False)
        if not isinstance(refresh, bool) or not isinstance(cached_only, bool):
            raise ServiceError(-32602, "invalid_params",
                               "'refresh' and 'cached_only' must be booleans.")
        if refresh and cached_only:
            raise ServiceError(-32602, "invalid_params",
                               "'refresh' and 'cached_only' cannot both be true.")

        import threading
        from .wabbajack import run_gallery
        stop = threading.Event()
        job = self._jobs.submit(
            "wabbajack.gallery", game,
            self._canonical_game(game) if game else None, None,
            runner=lambda log, progress: run_gallery(
                game, refresh, cached_only, stop, log, progress),
            cancel=stop.set,
        )
        return {"job": job}

    def _start_download_wabbajack_package(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(params, {"entry_id", "expected_version"}, {"entry_id"})
        entry_id = self._required_text(params, "entry_id")
        expected_version = (self._required_text(params, "expected_version")
                            if "expected_version" in params else None)
        import threading
        from .wabbajack import download_gallery_package
        stop = threading.Event()
        job = self._jobs.submit(
            "wabbajack.package.download", None, None, None,
            runner=lambda log, progress: download_gallery_package(
                entry_id, expected_version, stop, log, progress),
            cancel=stop.set,
        )
        return {"job": job}

    def _start_inspect_wabbajack(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(params, {"package_path"}, {"package_path"})
        package_path = Path(
            self._required_text(params, "package_path")).expanduser().absolute()
        if not package_path.is_file():
            raise ServiceError(
                -32003, "resource_unavailable",
                "The Wabbajack package was not found.",
                {"package_path": str(package_path)},
            )

        def inspect(log, progress):
            from Utils.wabbajack.manifest import inspect_package
            from .jobs import JobError
            from .wabbajack import package_summary
            progress(0, 1, "Inspecting Wabbajack package")
            try:
                package = inspect_package(package_path, log=log)
            except Exception as exc:
                raise JobError(
                    "wabbajack_inspect_failed", str(exc) or type(exc).__name__,
                    {"package_path": str(package_path),
                     "exception": type(exc).__name__},
                ) from exc
            progress(1, 1, "Package inspected")
            return package_summary(package)

        job = self._jobs.submit(
            "wabbajack.inspect", None, None, None, runner=inspect,
        )
        return {"job": job}

    def _wabbajack_request_options(
        self, params: dict[str, Any], *, install: bool = False,
    ) -> tuple[Any, dict[str, Any]]:
        self._require_idle_state()
        allowed = {
            "game", "package_path", "expected_identity", "mode",
            "installation_name", "installation_id", "downloads_path",
            "profiles", "fixes", "setup_options", "game_roots", "gallery_id",
            "clear_archives",
        }
        if install:
            allowed.update({"accept_known_issues", "conflict_policy"})
        self._check_params(params, allowed, {"game", "package_path"})
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_configured(game)
        if install:
            self._require_no_managed_launch(game)
        package_path = Path(
            self._required_text(params, "package_path")).expanduser().absolute()
        if not package_path.is_file():
            raise ServiceError(
                -32003, "resource_unavailable",
                "The Wabbajack package was not found.",
                {"package_path": str(package_path)},
            )
        mode = params.get("mode", "install")
        if not isinstance(mode, str) or mode not in {"install", "resume", "repair", "update"}:
            raise ServiceError(-32602, "invalid_params", "Invalid Wabbajack mode.",
                               {"parameter": "mode"})
        if mode == "install":
            if "installation_id" in params:
                raise ServiceError(-32602, "invalid_params",
                                   "'installation_id' is only used for an existing installation.")
            name = self._required_text(params, "installation_name")
            if (name in {".", ".."} or Path(name).name != name
                    or "\\" in name or len(name) > 128
                    or any(ord(char) < 32 for char in name)):
                raise ServiceError(-32602, "invalid_params",
                                   "'installation_name' must be a safe directory name.",
                                   {"parameter": "installation_name"})
            if "profiles" not in params:
                raise ServiceError(-32602, "invalid_params",
                                   "Select authored profiles for a new installation.",
                                   {"parameter": "profiles"})
        else:
            if "installation_name" in params:
                raise ServiceError(-32602, "invalid_params",
                                   "'installation_name' is only used for a new installation.")
            installation_id = self._required_text(params, "installation_id")
        options = dict(params)
        options["package_path"] = str(package_path)
        options["mode"] = mode
        options["installation_name" if mode == "install" else "installation_id"] = (
            name if mode == "install" else installation_id)
        for key in ("expected_identity", "downloads_path", "gallery_id"):
            if key in options:
                options[key] = self._required_text(params, key)
        for key in ("profiles", "fixes"):
            if key in options:
                options[key] = self._text_array(options[key], key)
        if "setup_options" in options and not isinstance(options["setup_options"], dict):
            raise ServiceError(-32602, "invalid_params",
                               "'setup_options' must be an object.",
                               {"parameter": "setup_options"})
        if "game_roots" in options:
            roots = options["game_roots"]
            if (not isinstance(roots, dict) or len(roots) > 32
                    or any(not isinstance(key, str) or not key.strip()
                           or not isinstance(value, str) or not value.strip()
                           for key, value in roots.items())):
                raise ServiceError(-32602, "invalid_params",
                                   "'game_roots' must map game names to paths.",
                                   {"parameter": "game_roots"})
        if "clear_archives" in options and not isinstance(options["clear_archives"], bool):
            raise ServiceError(-32602, "invalid_params",
                               "'clear_archives' must be a boolean.",
                               {"parameter": "clear_archives"})
        if install:
            if ("accept_known_issues" in options
                    and not isinstance(options["accept_known_issues"], bool)):
                raise ServiceError(-32602, "invalid_params",
                                   "'accept_known_issues' must be a boolean.",
                                   {"parameter": "accept_known_issues"})
            policy = options.get("conflict_policy", "reject")
            if not isinstance(policy, str) or policy not in {"reject", "keep", "author"}:
                raise ServiceError(-32602, "invalid_params",
                                   "'conflict_policy' must be reject, keep, or author.",
                                   {"parameter": "conflict_policy"})
        return game, options

    def _start_preflight_wabbajack(self, params: dict[str, Any]) -> dict[str, Any]:
        import threading
        from .wabbajack import run_preflight
        game, options = self._wabbajack_request_options(params)
        stop = threading.Event()
        job = self._jobs.submit(
            "wabbajack.preflight", game, self._canonical_game(game), None,
            runner=lambda log, progress: run_preflight(
                game, options, self._nexus_session, stop, log, progress),
            cancel=stop.set,
        )
        return {"job": job}

    def _start_import_wabbajack_archive(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(
            params,
            {"game", "package_path", "expected_identity", "archive_hash",
             "source_path", "downloads_path"},
            {"game", "package_path", "archive_hash", "source_path"},
        )
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_configured(game)
        package_path = Path(self._required_text(params, "package_path")).expanduser().absolute()
        source_path = Path(self._required_text(params, "source_path")).expanduser().absolute()
        archive_hash = self._required_text(params, "archive_hash")
        expected_identity = (self._required_text(params, "expected_identity")
                             if "expected_identity" in params else None)
        downloads_path = None
        if "downloads_path" in params:
            downloads_path = Path(self._required_text(params, "downloads_path")).expanduser()
            if not downloads_path.is_absolute():
                raise ServiceError(-32602, "invalid_params",
                                   "'downloads_path' must be an absolute path.",
                                   {"parameter": "downloads_path"})
        import threading
        from .wabbajack import import_manual_archive
        stop = threading.Event()
        job = self._jobs.submit(
            "wabbajack.archive.import", game, self._canonical_game(game), None,
            runner=lambda log, progress: import_manual_archive(
                game, package_path, expected_identity, archive_hash,
                source_path, downloads_path, stop, log, progress),
            cancel=stop.set,
        )
        return {"job": job}

    def _start_install_wabbajack(self, params: dict[str, Any]) -> dict[str, Any]:
        from Utils.downloads.install import InstallControl
        from .wabbajack import run_install_job
        game, options = self._wabbajack_request_options(params, install=True)
        control = InstallControl()

        def cancel():
            control.cancel.set()
            control.stop.set()

        job = self._jobs.submit(
            "wabbajack.install", game, self._canonical_game(game), None,
            runner=lambda log, progress: run_install_job(
                game, options, self._nexus_session, control, log, progress),
            cancel=cancel,
        )
        return {"job": job}

    def _start_remove_wabbajack(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(
            params, {"game", "installation_id", "confirm"},
            {"game", "installation_id", "confirm"},
        )
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_no_managed_launch(game)
        self._require_configured(game)
        installation_id = self._required_text(params, "installation_id")
        if params["confirm"] is not True:
            raise ServiceError(
                -32602, "invalid_params",
                "'confirm' must be true because removal deletes the managed "
                "installation and its linked profiles.",
                {"parameter": "confirm"},
            )
        from Utils.wabbajack.installed import installation_by_id
        from Utils.wabbajack.paths import WabbajackError
        try:
            installation_by_id(game, installation_id)
        except WabbajackError as exc:
            raise ServiceError(
                -32003, "installation_unavailable", str(exc),
                {"installation_id": installation_id},
            ) from exc
        job = self._jobs.submit(
            "wabbajack.remove", game, self._canonical_game(game), None,
            {"installation_id": installation_id},
        )
        return {"job": job}

    def _start_reset_wabbajack(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(
            params, {"game", "profile"}, {"game", "profile"},
        )
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_no_managed_launch(game)
        self._require_configured(game)
        profile = self._resolve_profile(
            game, self._required_text(params, "profile"))
        job = self._jobs.submit(
            "wabbajack.reset_load_order", game,
            self._canonical_game(game), profile.name,
        )
        return {"job": job}

    def _interrupted_target(self, params: dict[str, Any]):
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_configured(game)
        profile = self._resolve_profile(game, self._required_text(params, "profile"))
        return game, profile

    def _interrupted_selection(self, params: dict[str, Any]):
        game, profile = self._interrupted_target(params)
        download_id = self._required_text(params, "download_id")
        digest = download_id.removeprefix("interrupted_")
        revision = self._required_text(params, "revision")
        if (not download_id.startswith("interrupted_") or len(digest) != 64
                or any(char not in "0123456789abcdef" for char in digest)
                or len(revision) != 64
                or any(char not in "0123456789abcdef" for char in revision)):
            raise ServiceError(-32602, "invalid_params",
                               "Use the download ID and revision from downloads.interrupted.list.")
        return game, profile, download_id, revision

    def _list_interrupted_downloads(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(params, {"game", "profile"}, {"game", "profile"})
        game, profile = self._interrupted_target(params)
        from .interrupted_downloads import inventory
        return {"game": self._canonical_game(game), "profile": profile.name,
                **inventory(game, self._nexus_domains(game))}

    def _start_resume_interrupted_download(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        required = {"game", "profile", "download_id", "revision"}
        self._check_params(params, required | {"nxm_url"}, required)
        game, profile, download_id, revision = self._interrupted_selection(params)
        nxm_link = None
        if "nxm_url" in params:
            parsed = self._parse_nxm_link(params)
            _, domain = self._resolve_nexus_domain({"game": params["game"],
                                                   "domain": parsed.game_domain})
            from Nexus.nxm_handler import NxmLink
            nxm_link = NxmLink(domain, parsed.mod_id, parsed.file_id,
                               parsed.key, parsed.expires)
        from Utils.downloads.control import DownloadControl
        from .interrupted_downloads import resume
        control = DownloadControl()
        job = self._jobs.submit(
            "downloads.interrupted.resume", game, self._canonical_game(game),
            profile.name, {"download_id": download_id, "revision": revision,
                           "mode": "nxm" if nxm_link is not None else "premium"},
            runner=lambda log, progress: resume(
                game, self._nexus_domains(game), download_id, revision,
                self._nexus_session, nxm_link, control, log, progress),
            cancel=control.cancel, pause=control.pause, resume=control.resume)
        return {"job": job}

    def _start_discard_interrupted_download(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        required = {"game", "profile", "download_id", "revision", "confirm"}
        self._check_params(params, required, required)
        if params["confirm"] is not True:
            raise ServiceError(-32602, "confirmation_required",
                               "Set confirm to true to discard a saved transfer.")
        game, profile, download_id, revision = self._interrupted_selection(params)
        import threading
        from .interrupted_downloads import discard
        cancel = threading.Event()
        job = self._jobs.submit(
            "downloads.interrupted.discard", game, self._canonical_game(game),
            profile.name, {"download_id": download_id, "revision": revision},
            runner=lambda log, progress: discard(
                game, self._nexus_domains(game), download_id, revision,
                cancel, log, progress), cancel=cancel.set)
        return {"job": job}

    def _list_downloads(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(params, {"game", "profile"}, {"game", "profile"})
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_configured(game)
        profile_dir = self._resolve_profile(
            game, self._required_text(params, "profile"))

        from Utils.downloads.inventory import build_inventory
        from Utils.profiles.state import profile_uses_specific_mods

        try:
            inventory = build_inventory(game, profile_dir)
        except OSError as exc:
            raise ServiceError(
                -32003,
                "resource_unavailable",
                "Could not scan the configured download locations.",
                {"reason": str(exc)},
            ) from exc
        return {
            "game": self._canonical_game(game),
            "profile": {
                "name": profile_dir.name,
                "path": self._path_text(profile_dir),
                "profile_specific_mods": profile_uses_specific_mods(
                    profile_dir),
            },
            **inventory,
        }

    def _get_download_locations(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(params, set())
        from .download_settings import locations_snapshot
        return {"locations": locations_snapshot()}

    def _start_set_download_locations(
        self, params: dict[str, Any],
    ) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(
            params, {"revision", "settings"}, {"revision", "settings"})
        revision = self._required_text(params, "revision")
        if (len(revision) != 64 or any(
                char not in "0123456789abcdef" for char in revision)):
            raise ServiceError(-32602, "invalid_params",
                               "'revision' must be a download-locations revision.",
                               {"parameter": "revision"})
        settings = params["settings"]
        if not isinstance(settings, dict):
            raise ServiceError(-32602, "invalid_params",
                               "'settings' must be an object.",
                               {"parameter": "settings"})
        from .download_settings import set_locations
        job = self._jobs.submit(
            "downloads.locations.set", None, None, None,
            {"revision": revision},
            runner=lambda log, _progress: set_locations(
                revision, settings, log),
        )
        return {"job": job}

    def _start_set_hidden_downloads(
        self, params: dict[str, Any],
    ) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(
            params, {"game", "profile", "archive_ids", "hidden", "revision"},
            {"game", "profile", "archive_ids", "hidden", "revision"})
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_configured(game)
        profile = self._resolve_profile(
            game, self._required_text(params, "profile"))
        archive_ids = self._archive_ids(params["archive_ids"])
        hidden = params["hidden"]
        if not isinstance(hidden, bool):
            raise ServiceError(-32602, "invalid_params",
                               "'hidden' must be a boolean.",
                               {"parameter": "hidden"})
        revision = self._required_text(params, "revision")
        if (len(revision) != 64 or any(
                char not in "0123456789abcdef" for char in revision)):
            raise ServiceError(-32602, "invalid_params",
                               "'revision' must be a hidden-archives revision.",
                               {"parameter": "revision"})
        from .download_settings import set_hidden
        job = self._jobs.submit(
            "downloads.hidden.set", game, self._canonical_game(game),
            profile.name,
            {"archive_ids": archive_ids, "hidden": hidden,
             "revision": revision},
            runner=lambda log, _progress: set_hidden(
                game, profile, archive_ids, hidden, revision, log),
        )
        return {"job": job}

    def _start_plan_download_removal(
        self, params: dict[str, Any],
    ) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(params, {"game", "profile", "archive_ids"},
                           {"game", "profile", "archive_ids"})
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_no_managed_launch(game)
        self._require_configured(game)
        profile = self._resolve_profile(
            game, self._required_text(params, "profile"))
        archive_ids = self._archive_ids(params["archive_ids"])
        from .download_archives import plan_runner
        job = self._jobs.submit(
            "downloads.remove.plan", game, self._canonical_game(game),
            profile.name, {"archive_ids": archive_ids},
            runner=plan_runner(game, profile, archive_ids),
        )
        return {"job": job}

    def _start_plan_download_move(
        self, params: dict[str, Any],
    ) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(
            params, {"game", "profile", "archive_ids", "destination_location_id"},
            {"game", "profile", "archive_ids", "destination_location_id"})
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_no_managed_launch(game)
        self._require_configured(game)
        profile = self._resolve_profile(
            game, self._required_text(params, "profile"))
        archive_ids = self._archive_ids(params["archive_ids"])
        destination_id = self._download_location_id(
            params["destination_location_id"])
        from .download_archives import plan_move_runner
        job = self._jobs.submit(
            "downloads.move.plan", game, self._canonical_game(game),
            profile.name,
            {"archive_ids": archive_ids, "destination_location_id": destination_id},
            runner=plan_move_runner(game, profile, archive_ids, destination_id),
        )
        return {"job": job}

    def _start_move_downloads(
        self, params: dict[str, Any],
    ) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(
            params,
            {"game", "profile", "archive_ids", "destination_location_id",
             "plan_revision", "if_exists", "confirm"},
            {"game", "profile", "archive_ids", "destination_location_id",
             "plan_revision", "confirm"})
        if params["confirm"] is not True:
            raise ServiceError(-32602, "confirmation_required",
                               "Confirm the archive move plan before moving files.",
                               {"parameter": "confirm"})
        revision = self._required_text(params, "plan_revision")
        if (len(revision) != 64 or any(
                char not in "0123456789abcdef" for char in revision)):
            raise ServiceError(-32602, "invalid_params",
                               "'plan_revision' must be a move-plan revision.",
                               {"parameter": "plan_revision"})
        if_exists = params.get("if_exists", "fail")
        if if_exists not in ("fail", "replace"):
            raise ServiceError(-32602, "invalid_params",
                               "'if_exists' must be 'fail' or 'replace'.",
                               {"parameter": "if_exists"})
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_no_managed_launch(game)
        self._require_configured(game)
        profile = self._resolve_profile(
            game, self._required_text(params, "profile"))
        archive_ids = self._archive_ids(params["archive_ids"])
        destination_id = self._download_location_id(
            params["destination_location_id"])
        from .download_archives import move_runner
        job = self._jobs.submit(
            "downloads.move", game, self._canonical_game(game),
            profile.name,
            {"archive_ids": archive_ids, "destination_location_id": destination_id,
             "plan_revision": revision, "if_exists": if_exists},
            runner=move_runner(
                game, profile, archive_ids, destination_id, revision, if_exists),
        )
        return {"job": job}

    def _start_remove_downloads(
        self, params: dict[str, Any],
    ) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(
            params, {"game", "profile", "archive_ids", "plan_revision", "confirm"},
            {"game", "profile", "archive_ids", "plan_revision", "confirm"})
        if params["confirm"] is not True:
            raise ServiceError(-32602, "confirmation_required",
                               "Confirm the archive removal plan before deleting files.",
                               {"parameter": "confirm"})
        revision = self._required_text(params, "plan_revision")
        if (len(revision) != 64 or any(
                char not in "0123456789abcdef" for char in revision)):
            raise ServiceError(-32602, "invalid_params",
                               "'plan_revision' must be a removal-plan revision.",
                               {"parameter": "plan_revision"})
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_no_managed_launch(game)
        self._require_configured(game)
        profile = self._resolve_profile(
            game, self._required_text(params, "profile"))
        archive_ids = self._archive_ids(params["archive_ids"])
        from .download_archives import remove_runner
        job = self._jobs.submit(
            "downloads.remove", game, self._canonical_game(game),
            profile.name,
            {"archive_ids": archive_ids, "plan_revision": revision},
            runner=remove_runner(game, profile, archive_ids, revision),
        )
        return {"job": job}

    def _install_archive_source(self, params):
        if ("archive_id" in params) == ("archive_path" in params):
            raise ServiceError(-32602, "invalid_params", "Supply exactly one of archive_id or archive_path.")
        if "archive_id" in params:
            return self._archive_id(params["archive_id"]), None
        value = self._required_text(params, "archive_path")
        if not Path(value).is_absolute():
            raise ServiceError(-32602, "invalid_params", "archive_path must be absolute.")
        return None, value

    def _installer_requests(self, params):
        interactive = params.get("interactive", False)
        if not isinstance(interactive, bool):
            raise ServiceError(-32602, "invalid_params", "interactive must be a boolean.")
        if not interactive:
            return None
        from threading import Event
        from .installer_requests import InstallerRequests
        return InstallerRequests(Event())

    def _start_inspect_download(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(
            params, {"game", "profile", "archive_id", "archive_path", "name"},
            {"game", "profile"})
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_configured(game)
        profile_dir = self._resolve_profile(
            game, self._required_text(params, "profile"))
        archive_id, archive_path = self._install_archive_source(params)
        name = ""
        if "name" in params:
            name = self._mod_name(params.get("name"), "name")
            if "\\" in name or name != name.strip():
                raise ServiceError(
                    -32602, "invalid_params",
                    "'name' must be one safe mod-folder name.",
                    {"parameter": "name"})
        from threading import Event
        from .jobs import _inspect_download
        cancel = Event()
        job = self._jobs.submit(
            "downloads.inspect", game, self._canonical_game(game),
            profile_dir.name, {"archive_id": archive_id, "archive_path": archive_path, "name": name or None},
            runner=lambda log, progress: _inspect_download(
                game, profile_dir.name, archive_id, name, log, progress, cancel, archive_path),
            cancel=cancel.set,
        )
        return {"job": job}

    def _start_install_download(
        self, params: dict[str, Any],
    ) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(
            params,
            {
                "game", "profile", "archive_id", "name",
                "installer_policy", "if_exists", "bain_selected",
                "fomod_selections", "bundle_selected", "installer_revision",
                "archive_path", "content_prefix", "multi_mod_selected", "multi_mod_names", "multi_mod_policies", "interactive",
            },
            {"game", "profile"},
        )
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_no_managed_launch(game)
        self._require_configured(game)
        profile_dir = self._resolve_profile(
            game, self._required_text(params, "profile"))
        from Utils.profiles.groups import get_members, is_group
        if is_group(profile_dir):
            raise ServiceError(
                -32009,
                "operation_not_supported",
                "Install into a member profile rather than a profile group.",
                {
                    "profile": profile_dir.name,
                    "members": get_members(profile_dir),
                },
            )

        archive_id, archive_path = self._install_archive_source(params)

        name = ""
        if "name" in params:
            name = self._mod_name(params.get("name"), "name")
            if "\\" in name or name != name.strip():
                raise ServiceError(
                    -32602,
                    "invalid_params",
                    "'name' must be one safe mod-folder name.",
                    {"parameter": "name"},
                )

        installer_policy = params.get("installer_policy", "reject")
        if (not isinstance(installer_policy, str)
                or installer_policy not in {"reject", "defaults", "verbatim"}):
            raise ServiceError(
                -32602,
                "invalid_params",
                "'installer_policy' must be 'reject', 'defaults', or 'verbatim'.",
                {"parameter": "installer_policy"},
            )
        if installer_policy == "verbatim" and any(
                key in params for key in (
                    "bain_selected", "fomod_selections", "bundle_selected")):
            raise ServiceError(-32602, "invalid_params",
                               "Verbatim installation cannot use installer selections.",
                               {"parameter": "installer_policy"})
        bain_selected = self._bain_selection(params, installer_policy)
        fomod_selections = self._fomod_selection(params, installer_policy)
        bundle_selected = self._bundle_selection(params, installer_policy)
        if_exists = params.get("if_exists", "fail")
        if if_exists not in {"fail", "replace"}:
            raise ServiceError(
                -32602,
                "invalid_params",
                "'if_exists' must be 'fail' or 'replace'.",
                {"parameter": "if_exists"},
            )

        installer_revision = params.get("installer_revision")
        if installer_revision is not None and (not isinstance(installer_revision, str)
                or len(installer_revision) != 64 or any(char not in "0123456789abcdef" for char in installer_revision)):
            raise ServiceError(-32602, "invalid_params", "installer_revision must be the inspected plugin installer revision.")
        from .install_options import validate_options
        extra_options = validate_options(params)
        requests = self._installer_requests(params)
        job = self._jobs.submit(
            "downloads.install",
            game,
            self._canonical_game(game),
            profile_dir.name,
            {
                "archive_id": archive_id,
                "archive_path": archive_path,
                **extra_options,
                "name": name or None,
                "installer_policy": installer_policy,
                "bain_selected": bain_selected,
                "fomod_selections": fomod_selections,
                "bundle_selected": bundle_selected,
                "if_exists": if_exists,
                "installer_revision": installer_revision,
            },
            installer_requests=requests,
        )
        return {"job": job}

    def _get_nexus_auth(self, params: dict[str, Any]) -> dict[str, Any]:
        self._check_params(params, set())
        return {
            "credentials": self._nexus_session.status(),
            "login": self._nexus_login.status(),
        }

    def _start_nexus_login(self, params: dict[str, Any]) -> dict[str, Any]:
        self._check_params(params, {"open_browser"})
        open_browser = params.get("open_browser", False)
        if not isinstance(open_browser, bool):
            raise ServiceError(
                -32602,
                "invalid_params",
                "'open_browser' must be a boolean.",
                {"parameter": "open_browser"},
            )
        try:
            login = self._nexus_login.start(open_browser=open_browser)
        except Exception as exc:
            self._raise_nexus_session_error(exc)
        return {"login": login}

    def _submit_nexus_login_code(
        self, params: dict[str, Any],
    ) -> dict[str, Any]:
        self._check_params(params, {"code"}, {"code"})
        code = self._required_text(params, "code")
        if len(code) > 16384:
            raise ServiceError(
                -32602,
                "invalid_params",
                "'code' is too long.",
                {"parameter": "code"},
            )
        accepted, message = self._nexus_login.submit_manual_code(code)
        return {
            "accepted": accepted,
            "message": message,
            "login": self._nexus_login.status(),
        }

    def _cancel_nexus_login(self, params: dict[str, Any]) -> dict[str, Any]:
        self._check_params(params, set())
        return {"login": self._nexus_login.cancel()}

    def _start_nexus_logout(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(params, {"confirm"}, {"confirm"})
        if params.get("confirm") is not True:
            raise ServiceError(
                -32602,
                "invalid_params",
                "'confirm' must be true to clear saved Nexus credentials.",
                {"parameter": "confirm"},
            )
        from .nexus import logout_runner
        job = self._jobs.submit(
            "nexus.auth.logout",
            None,
            None,
            None,
            runner=logout_runner(self._nexus_session, self._nexus_login),
        )
        return {"job": job}

    def _get_modio_credentials(
        self, params: dict[str, Any],
    ) -> dict[str, Any]:
        self._check_params(params, set())
        from .modio import credential_status
        try:
            return {"credentials": credential_status()}
        except Exception as exc:
            raise ServiceError(
                -32003, "resource_unavailable",
                "Could not inspect the saved mod.io credentials.",
                {"exception": type(exc).__name__},
            ) from exc

    def _start_set_modio_credentials(
        self, params: dict[str, Any],
    ) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(
            params, {"api_key", "api_path"}, {"api_key", "api_path"})
        api_key = self._required_text(params, "api_key")
        if (len(api_key) > 4096
                or any(char in api_key for char in "\x00\r\n")):
            raise ServiceError(
                -32602, "invalid_params",
                "'api_key' is not a valid mod.io API key.",
                {"parameter": "api_key"},
            )
        raw_path = self._required_text(params, "api_path")
        if len(raw_path) > 512:
            raise ServiceError(
                -32602, "invalid_params",
                "'api_path' is too long.",
                {"parameter": "api_path"},
            )
        from Utils.bg3.modio_loader import load_modio_module
        try:
            api_path = load_modio_module("modio_api").normalize_api_path(
                raw_path)
        except ValueError as exc:
            raise ServiceError(
                -32602, "invalid_params", str(exc),
                {"parameter": "api_path"},
            ) from exc
        from .modio import save_credentials_runner
        job = self._jobs.submit(
            "modio.credentials.set", None, None, None,
            {"api_path": api_path},
            runner=save_credentials_runner(api_key, api_path),
        )
        return {"job": job}

    def _start_clear_modio_credentials(
        self, params: dict[str, Any],
    ) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(params, {"confirm"}, {"confirm"})
        if params["confirm"] is not True:
            raise ServiceError(
                -32602, "invalid_params",
                "'confirm' must be true to clear saved mod.io credentials.",
                {"parameter": "confirm"},
            )
        from .modio import clear_credentials_runner
        job = self._jobs.submit(
            "modio.credentials.clear", None, None, None,
            runner=clear_credentials_runner(),
        )
        return {"job": job}

    def _start_nexus_user(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(params, {"refresh"})
        refresh = params.get("refresh", False)
        if not isinstance(refresh, bool):
            raise ServiceError(
                -32602,
                "invalid_params",
                "'refresh' must be a boolean.",
                {"parameter": "refresh"},
            )
        from .nexus import account_runner
        job = self._jobs.submit(
            "nexus.user.get",
            None,
            None,
            None,
            {"refresh": refresh},
            runner=account_runner(self._nexus_session, refresh=refresh),
        )
        return {"job": job}

    def _start_collection_search(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(
            params,
            {"game", "domain", "query", "limit", "offset", "sort",
             "include_adult"},
            {"game"},
        )
        game, domain = self._resolve_nexus_domain(params)
        query = None
        if "query" in params:
            query = self._required_text(params, "query")
            if len(query) > 256:
                raise ServiceError(
                    -32602, "invalid_params",
                    "'query' must contain at most 256 characters.",
                    {"parameter": "query"},
                )
        limit = self._integer_range(
            params.get("limit", 20), "limit", 1, 50)
        offset = self._integer_range(
            params.get("offset", 0), "offset", 0, 2_147_483_647)
        sort = params.get("sort", "downloads")
        if not isinstance(sort, str) or sort not in _COLLECTION_SORTS:
            raise ServiceError(
                -32602, "invalid_params",
                "'sort' is not a supported collection catalogue order.",
                {"parameter": "sort", "supported": list(_COLLECTION_SORTS)},
            )
        include_adult = params.get("include_adult", False)
        if not isinstance(include_adult, bool):
            raise ServiceError(
                -32602, "invalid_params",
                "'include_adult' must be a boolean.",
                {"parameter": "include_adult"},
            )
        from .nexus import collection_search_runner
        job = self._jobs.submit(
            "nexus.collections.search", game, self._canonical_game(game), None,
            {"domain": domain, "query": query, "limit": limit,
             "offset": offset, "sort": sort,
             "include_adult": include_adult},
            runner=collection_search_runner(
                self._nexus_session, domain, query=query, limit=limit,
                offset=offset, sort=sort, include_adult=include_adult),
        )
        return {"job": job}

    def _start_collection_detail(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(
            params,
            {"game", "domain", "slug", "revision", "include_manifest"},
            {"game", "slug"},
        )
        game, domain = self._resolve_nexus_domain(params)
        slug = self._required_text(params, "slug")
        if (len(slug) > 128 or not slug.isascii()
                or any(not (char.isalnum() or char in "-_") for char in slug)):
            raise ServiceError(
                -32602, "invalid_params",
                "'slug' must be a Nexus collection slug.",
                {"parameter": "slug"},
            )
        revision = None
        if "revision" in params:
            revision = self._positive_integer(params, "revision")
        include_manifest = params.get("include_manifest", False)
        if not isinstance(include_manifest, bool):
            raise ServiceError(
                -32602, "invalid_params",
                "'include_manifest' must be a boolean.",
                {"parameter": "include_manifest"},
            )
        from .nexus import collection_detail_runner
        job = self._jobs.submit(
            "nexus.collections.get", game, self._canonical_game(game), None,
            {"domain": domain, "slug": slug, "revision": revision,
             "include_manifest": include_manifest},
            runner=collection_detail_runner(
                self._nexus_session, str(getattr(game, "name", "") or ""),
                domain, slug, revision, include_manifest),
        )
        return {"job": job}

    def _start_nexus_filters(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(params, {"game", "domain"}, {"game"})
        game, domain = self._resolve_nexus_domain(params)
        from .nexus import filters_runner
        job = self._jobs.submit(
            "nexus.mods.filters",
            game,
            self._canonical_game(game),
            None,
            {"domain": domain},
            runner=filters_runner(self._nexus_session, domain),
        )
        return {"job": job}

    def _start_nexus_search(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(
            params,
            {
                "game", "domain", "mode", "query", "uploader_id",
                "limit", "offset", "categories", "sort",
                "created_since_days", "filters",
            },
            {"game"},
        )
        game, domain = self._resolve_nexus_domain(params)

        mode = params.get("mode", "browse")
        if not isinstance(mode, str) or mode not in _NEXUS_QUERY_MODES:
            raise ServiceError(
                -32602,
                "invalid_params",
                "'mode' is not a supported Nexus query mode.",
                {"parameter": "mode", "supported": list(_NEXUS_QUERY_MODES)},
            )

        query = None
        if mode in ("name", "author"):
            if "query" not in params:
                raise ServiceError(
                    -32602,
                    "invalid_params",
                    f"'query' is required for mode '{mode}'.",
                    {"parameter": "query"},
                )
            query = self._required_text(params, "query")
            if not 2 <= len(query) <= 256:
                raise ServiceError(
                    -32602,
                    "invalid_params",
                    "'query' must contain 2 to 256 characters.",
                    {"parameter": "query"},
                )
        elif "query" in params:
            raise ServiceError(
                -32602,
                "invalid_params",
                f"'query' is not used by mode '{mode}'.",
                {"parameter": "query", "mode": mode},
            )

        uploader_id = None
        if mode == "uploader_id":
            if "uploader_id" not in params:
                raise ServiceError(
                    -32602,
                    "invalid_params",
                    "'uploader_id' is required for mode 'uploader_id'.",
                    {"parameter": "uploader_id"},
                )
            uploader_id = self._positive_integer(params, "uploader_id")
        elif "uploader_id" in params:
            raise ServiceError(
                -32602,
                "invalid_params",
                f"'uploader_id' is not used by mode '{mode}'.",
                {"parameter": "uploader_id", "mode": mode},
            )

        limit = self._integer_range(
            params.get("limit", 20), "limit", 1, 50)
        offset = self._integer_range(
            params.get("offset", 0), "offset", 0, 2_147_483_647)
        categories = self._text_array(
            params.get("categories", []), "categories")

        sort = params.get("sort", "downloads")
        if not isinstance(sort, str) or sort not in _NEXUS_QUERY_SORTS:
            raise ServiceError(
                -32602,
                "invalid_params",
                "'sort' is not a supported Nexus catalogue order.",
                {"parameter": "sort", "supported": list(_NEXUS_QUERY_SORTS)},
            )
        if mode == "trending" and "sort" in params:
            raise ServiceError(
                -32602,
                "invalid_params",
                "'sort' is fixed to endorsements for trending results.",
                {"parameter": "sort", "mode": mode},
            )

        created_since_days = None
        if "created_since_days" in params:
            if mode != "browse":
                raise ServiceError(
                    -32602,
                    "invalid_params",
                    "'created_since_days' is only used by browse mode.",
                    {"parameter": "created_since_days", "mode": mode},
                )
            created_since_days = self._integer_range(
                params["created_since_days"],
                "created_since_days", 1, 36_500)

        search_filters, normalized_filters = self._nexus_filters(
            params.get("filters", {}))
        options: dict[str, Any] = {
            "domain": domain,
            "mode": mode,
            "limit": limit,
            "offset": offset,
            "categories": categories,
            "sort": "endorsements" if mode == "trending" else sort,
            "filters": normalized_filters,
        }
        if query is not None:
            options["query"] = query
        if uploader_id is not None:
            options["uploader_id"] = uploader_id
        if created_since_days is not None:
            options["created_since_days"] = created_since_days

        from .nexus import search_runner
        job = self._jobs.submit(
            "nexus.mods.search",
            game,
            self._canonical_game(game),
            None,
            options,
            runner=search_runner(
                self._nexus_session,
                domain,
                mode=mode,
                query=query,
                uploader_id=uploader_id,
                limit=limit,
                offset=offset,
                categories=categories,
                sort_key=_NEXUS_QUERY_SORTS[sort],
                created_since_days=created_since_days,
                filters=search_filters,
                options=options,
            ),
        )
        return {"job": job}

    def _start_nexus_mod(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(
            params, {"game", "domain", "mod_id"}, {"game", "mod_id"})
        game, domain = self._resolve_nexus_domain(params)
        mod_id = self._positive_integer(params, "mod_id")
        from .nexus import mod_runner
        job = self._jobs.submit(
            "nexus.mods.get",
            game,
            self._canonical_game(game),
            None,
            {"domain": domain, "mod_id": mod_id},
            runner=mod_runner(self._nexus_session, domain, mod_id),
        )
        return {"job": job}

    def _start_nexus_files(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(
            params, {"game", "domain", "mod_id"}, {"game", "mod_id"})
        game, domain = self._resolve_nexus_domain(params)
        mod_id = self._positive_integer(params, "mod_id")
        from .nexus import files_runner
        job = self._jobs.submit(
            "nexus.mods.files",
            game,
            self._canonical_game(game),
            None,
            {"domain": domain, "mod_id": mod_id},
            runner=files_runner(self._nexus_session, domain, mod_id),
        )
        return {"job": job}

    def _start_nexus_file(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(
            params,
            {"game", "domain", "mod_id", "file_id"},
            {"game", "mod_id", "file_id"},
        )
        game, domain = self._resolve_nexus_domain(params)
        mod_id = self._positive_integer(params, "mod_id")
        file_id = self._positive_integer(params, "file_id")
        from .nexus import file_runner
        job = self._jobs.submit(
            "nexus.files.get",
            game,
            self._canonical_game(game),
            None,
            {"domain": domain, "mod_id": mod_id, "file_id": file_id},
            runner=file_runner(
                self._nexus_session, domain, mod_id, file_id),
        )
        return {"job": job}

    def _parse_nxm_link(self, params: dict[str, Any]):
        url = self._required_text(params, "nxm_url")
        if len(url) > 8192:
            raise ServiceError(
                -32602,
                "invalid_params",
                "'nxm_url' is too long.",
                {"parameter": "nxm_url"},
            )
        from Nexus.nxm_handler import NxmLink
        try:
            parsed = NxmLink.parse(url)
        except (TypeError, ValueError) as exc:
            raise ServiceError(
                -32602,
                "invalid_params",
                "'nxm_url' must identify one Nexus mod file.",
                {"parameter": "nxm_url"},
            ) from exc
        if parsed.mod_id <= 0 or parsed.file_id <= 0:
            raise ServiceError(
                -32602,
                "invalid_params",
                "'nxm_url' must contain positive mod and file IDs.",
                {"parameter": "nxm_url"},
            )
        if bool(parsed.key) != bool(parsed.expires):
            raise ServiceError(
                -32602,
                "invalid_params",
                "The NXM download key and expiry must be supplied together.",
                {"parameter": "nxm_url"},
            )
        if parsed.key:
            import time
            if parsed.expires <= int(time.time()):
                raise ServiceError(
                    -32012,
                    "nexus_download_link_expired",
                    "The NXM download link expired; request a new one.",
                )
        return parsed

    def _start_nexus_download(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        allowed = {"game", "domain", "mod_id", "file_id", "nxm_url"}
        self._check_params(params, allowed, {"game"})

        nxm_link = None
        if "nxm_url" in params:
            conflicts = sorted(
                set(params).intersection({"domain", "mod_id", "file_id"}))
            if conflicts:
                raise ServiceError(
                    -32602,
                    "invalid_params",
                    "'nxm_url' cannot be combined with domain or numeric IDs.",
                    {"conflicting": conflicts},
                )
            parsed = self._parse_nxm_link(params)
            game, domain = self._resolve_nexus_domain({
                "game": params["game"], "domain": parsed.game_domain})
            mod_id, file_id = parsed.mod_id, parsed.file_id
            from Nexus.nxm_handler import NxmLink
            nxm_link = NxmLink(
                game_domain=domain,
                mod_id=mod_id,
                file_id=file_id,
                key=parsed.key,
                expires=parsed.expires,
            )
            mode = "nxm"
        else:
            self._check_params(
                params, allowed, {"game", "mod_id", "file_id"})
            game, domain = self._resolve_nexus_domain(params)
            mod_id = self._positive_integer(params, "mod_id")
            file_id = self._positive_integer(params, "file_id")
            mode = "premium"

        from Utils.downloads.control import DownloadControl
        from .nexus import download_runner
        control = DownloadControl()
        job = self._jobs.submit(
            "nexus.download",
            game,
            self._canonical_game(game),
            None,
            {
                "mode": mode,
                "domain": domain,
                "mod_id": mod_id,
                "file_id": file_id,
            },
            runner=download_runner(
                self._nexus_session,
                str(self._safe_attr(game, "name", "") or ""),
                domain,
                mod_id,
                file_id,
                nxm_link=nxm_link,
                cancel=control,
            ),
            cancel=control.cancel,
            pause=control.pause,
            resume=control.resume,
        )
        return {"job": job}

    def _start_mod_rollback(self, params, *, apply):
        from .mod_rollback import runner

        required = {"game", "profile", "mod"} | ({"revision", "confirm"} if apply else set())
        optional = {"archive_id"}
        if apply:
            optional |= {"installer_policy", "bain_selected", "fomod_selections", "bundle_selected", "dependency_policy"}
        self._check_params(params, required | optional, required)
        game, profile = self._batch_profile(params)
        mod = self._mod_name(self._required_text(params, "mod"), "mod")
        options = {}
        if "archive_id" in params:
            options["archive_id"] = self._archive_id(params["archive_id"])
        if apply:
            if params["confirm"] is not True:
                raise ServiceError(-32602, "invalid_params", "'confirm' must be true to replace the installed mod.")
            options["revision"] = self._required_text(params, "revision")
            policy = params.get("installer_policy", "reject")
            if policy not in ("reject", "defaults", "verbatim"):
                raise ServiceError(-32602, "invalid_params", "Choose a supported installer policy.")
            options.update(installer_policy=policy, bain_selected=self._bain_selection(params, policy),
                           fomod_selections=self._fomod_selection(params, policy),
                           bundle_selected=self._bundle_selection(params, policy))
            dependency_policy = params.get("dependency_policy", "reject")
            if dependency_policy not in ("reject", "ignore"):
                raise ServiceError(-32602, "invalid_params", "Choose 'reject' or 'ignore' dependency policy.")
            options["dependency_policy"] = dependency_policy
        kind = "mods.rollback" if apply else "mods.rollback.get"
        return {"job": self._jobs.submit(kind, game, self._canonical_game(game), profile.name, {"mod": mod, **options},
                                        runner=runner(game, profile, mod, options, apply=apply))}

    def _start_provider_search(self, params, provider, *, filters=False):
        from .provider_search import thunderstore_runner, modio_runner

        optional = set() if filters else {"query", "sort"}
        if not filters:
            optional |= ({"page", "included_categories", "excluded_categories", "section", "include_adult", "include_deprecated"}
                         if provider == "thunderstore" else {"offset", "limit"})
        self._check_params(params, {"game"} | optional, {"game"})
        self._require_idle_state()
        game = self._resolve_game(self._required_text(params, "game"))
        query = params.get("query", "")
        if not isinstance(query, str) or len(query) > 512 or "\x00" in query:
            raise ServiceError(-32602, "invalid_params", "'query' must be text of at most 512 characters.")
        if provider == "thunderstore":
            from Thunderstore.thunderstore_api import ORDERINGS
            import re
            community = getattr(game, "thunderstore_community", "") or ""
            if not isinstance(community, str) or re.fullmatch(r"[a-z0-9_-]+", community) is None:
                raise ServiceError(-32009, "operation_not_supported", "This game has no Thunderstore community.")
            sort = params.get("sort", "last-updated")
            if sort not in [value for _label, value in ORDERINGS]:
                raise ServiceError(-32602, "invalid_params", "Unknown Thunderstore sort order.")
            options = {"query": query, "ordering": sort,
                       "page": self._integer_range(params.get("page", 0), "page", 0, 1000000)}
            for key in ("included_categories", "excluded_categories"):
                if key in params:
                    options[key] = self._text_array(params[key], key)
            if "section" in params:
                options["section"] = self._required_text(params, "section")
            for key, target in (("include_adult", "nsfw"), ("include_deprecated", "deprecated")):
                if not isinstance(params.get(key, False), bool):
                    raise ServiceError(-32602, "invalid_params", f"'{key}' must be boolean.")
                options[target] = params.get(key, False)
            kind = "thunderstore.packages.filters" if filters else "thunderstore.packages.search"
            runner = thunderstore_runner(community, options, filters=filters)
        else:
            if getattr(game, "game_id", "") != "baldurs_gate_3":
                raise ServiceError(-32009, "operation_not_supported", "mod.io browsing is supported for Baldur's Gate 3.")
            sorts = {"downloads": "-downloads_total", "updated": "-date_updated", "newest": "-date_live", "name": "name"}
            sort = params.get("sort", "downloads")
            if not isinstance(sort, str) or sort not in sorts:
                raise ServiceError(-32602, "invalid_params", "Unknown mod.io sort order.")
            options = {"query": query, "sort": sorts[sort],
                       "offset": self._integer_range(params.get("offset", 0), "offset", 0, 2147483647),
                       "limit": self._integer_range(params.get("limit", 20), "limit", 1, 100)}
            kind, runner = "modio.mods.search", modio_runner(options)
        job = self._jobs.submit(kind, game, self._canonical_game(game), None, options, runner=runner)
        return {"job": job}

    def _start_mod_files(self, params, *, edit):
        from threading import Event
        from .mod_files import runner

        required = {"game", "profile", "mod"} | ({"revision", "confirm"} if edit else set())
        optional = {"changes", "strip_prefixes", "reset"} if edit else {"offset", "limit", "revision"}
        self._check_params(params, required | optional, required)
        game, profile = self._batch_profile(params)
        mod = self._mod_name(self._required_text(params, "mod"), "mod")
        options = {key: params[key] for key in (optional | {"revision"}) if key in params}
        if "revision" in options:
            self._required_text(params, "revision")
        if edit:
            if params["confirm"] is not True:
                raise ServiceError(-32602, "invalid_params", "'confirm' must be true.")
            reset = options.get("reset", False)
            if not isinstance(reset, bool) or (reset and {"changes", "strip_prefixes"}.intersection(options)):
                raise ServiceError(-32602, "invalid_params", "Use boolean 'reset' alone or provide file changes.")
            if not reset and not {"changes", "strip_prefixes"}.intersection(options):
                raise ServiceError(-32602, "invalid_params", "Provide file changes, strip prefixes, or reset:true.")
            if "changes" in options:
                changes = options["changes"]
                if not isinstance(changes, list) or not 1 <= len(changes) <= 4096:
                    raise ServiceError(-32602, "invalid_params", "'changes' must contain 1 to 4096 file edits.")
                paths = set()
                for change in changes:
                    if (not isinstance(change, dict) or not {"path"} < set(change)
                            or set(change) - {"path", "enabled", "root"}
                            or not isinstance(change.get("path"), str) or not change["path"]
                            or any(not isinstance(value, bool) for key, value in change.items() if key != "path")
                            or change["path"] in paths):
                        raise ServiceError(-32602, "invalid_params", "Each edit needs a unique path and boolean enabled/root fields.")
                    paths.add(change["path"])
            if "strip_prefixes" in options:
                paths = options["strip_prefixes"]
                if (not isinstance(paths, list) or len(paths) > 4096
                        or any(not isinstance(path, str) or not path for path in paths)
                        or len(set(paths)) != len(paths)):
                    raise ServiceError(-32602, "invalid_params", "'strip_prefixes' must be a list of unique directory paths.")
        else:
            options["offset"] = self._integer_range(params.get("offset", 0), "offset", 0, 2147483647)
            options["limit"] = self._integer_range(params.get("limit", 500), "limit", 1, 5000)
        cancel = Event()
        kind = "mods.files.edit" if edit else "mods.files.get"
        job = self._jobs.submit(kind, game, self._canonical_game(game), profile.name, {"mod": mod, **options},
                                runner=runner(game, profile, mod, options, cancel, edit=edit),
                                cancel=None if edit else cancel.set)
        return {"job": job}

    def _list_mods(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(params, {"game", "profile"}, {"game", "profile"})
        game = self._resolve_game(self._required_text(params, "game"))
        profile_dir = self._resolve_profile(
            game, self._required_text(params, "profile"))

        from Utils.mods.separators import (
            modlist_revision, separator_state_revision,
        )
        from Utils.profiles.state import (
            read_collapsed_seps, read_mod_notes, read_profile_settings,
            read_separator_colors, read_separator_deploy_paths,
            read_separator_locks,
        )

        try:
            modlist_path = profile_dir / "modlist.txt"
            from Utils.mods.groups import group_edit_state, group_state_revision, normalize_groups
            with group_edit_state(modlist_path) as (raw, state, entries, original_groups):
                revision = modlist_revision(raw)
                groups = normalize_groups(original_groups, entries)
                group_revision = group_state_revision(raw, original_groups)
                from .mod_requirements import RequirementState
                requirement_state = RequirementState(game, profile_dir, raw, state, entries)
                notes = read_mod_notes(profile_dir, {"mod_notes": state.get("mod_notes", {})})
        except OSError as exc:
            raise ServiceError(
                -32003,
                "resource_unavailable",
                f"Could not read profile '{profile_dir.name}'.",
                {"profile": profile_dir.name, "reason": str(exc)},
            ) from exc

        settings = read_profile_settings(profile_dir, None)
        if bool(settings.get("profile_specific_mods", False)):
            staging_dir = profile_dir / "mods"
        else:
            staging_dir = game.get_mod_staging_path()

        real_count = sum(not entry.is_separator for entry in entries)
        real_index = 0
        collapsed = read_collapsed_seps(profile_dir)
        separator_locks = read_separator_locks(profile_dir)
        separator_colors = read_separator_colors(profile_dir)
        separator_paths = read_separator_deploy_paths(profile_dir)
        from Utils.mods.links import relationships
        links = relationships(game, profile_dir, staging_dir,
                              [entry.name for entry in entries if not entry.is_separator]) if staging_dir else {}
        mods: list[dict[str, Any]] = []
        for index, entry in enumerate(entries):
            priority = None
            if not entry.is_separator:
                priority = real_count - real_index - 1
                real_index += 1
            summary = self._mod_summary(
                entry, index=index, priority=priority,
                staging_dir=staging_dir,
                is_bg3=self._safe_attr(game, "game_id", "") == "baldurs_gate_3",
                nexus_meta=requirement_state.metas.get(entry.name),
            )
            if not entry.is_separator:
                link = links.get(entry.name)
                summary["link"] = {
                    "source": str(link.source) if link and link.source else None,
                    "source_profile": link.source_profile if link else "",
                    "targets": list(link.targets) if link else [],
                    "available": link.available if link else True,
                    "read_only": bool(link and link.source),
                }
                summary["note"] = notes.get(entry.name, "")
                summary["requirements"]["nexus"] = requirement_state.summary(entry.name)
            if entry.is_separator:
                separator_state = {
                    "collapsed": entry.display_name in collapsed,
                    "locked": bool(separator_locks.get(entry.display_name, False)),
                    "color": separator_colors.get(entry.name),
                    "deploy_path": separator_paths.get(entry.name),
                }
                summary["separator_state"] = {
                    **separator_state,
                    "revision": separator_state_revision(separator_state),
                }
            mods.append(summary)

        enabled_count = sum(
            not entry.is_separator and entry.enabled for entry in entries)
        return {
            "game": self._canonical_game(game),
            "profile": {
                "name": profile_dir.name,
                "path": self._path_text(profile_dir),
                "profile_specific_mods": bool(
                    settings.get("profile_specific_mods", False)),
            },
            "staging_path": self._path_text(staging_dir),
            "order": "highest_priority_first",
            "revision": revision,
            "groups": groups,
            "group_revision": group_revision,
            "requirement_revision": requirement_state.revision,
            "counts": {
                "mods": real_count,
                "enabled": enabled_count,
                "disabled": real_count - enabled_count,
                "separators": len(entries) - real_count,
            },
            "mods": mods,
        }

    def _nexus_actions_target(self, params):
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_configured(game)
        if not self._nexus_domains(game):
            self._unsupported_operation(game, "Nexus mod actions")
        profile = self._resolve_profile(game, self._required_text(params, "profile"))
        raw = params["mods"]
        if not isinstance(raw, list) or not 1 <= len(raw) <= 4096:
            raise ServiceError(-32602, "invalid_params", "'mods' must contain 1 to 4096 mod or mod group names.")
        mods = [self._mod_name(value, f"mods[{index}]") for index, value in enumerate(raw)]
        if len(mods) != len(set(mods)):
            raise ServiceError(-32602, "invalid_params", "The Nexus action selection contains duplicate names.")
        return game, profile, mods

    def _start_get_nexus_actions(self, params):
        self._require_idle_state()
        self._check_params(params, {"game", "profile", "mods"}, {"game", "profile", "mods"})
        game, profile, mods = self._nexus_actions_target(params)
        from .nexus_actions import get_runner
        job = self._jobs.submit(
            "mods.nexus.get", game, self._canonical_game(game), profile.name, {"mods": mods},
            runner=get_runner(self._nexus_session, game, profile, mods))
        return {"job": job}

    def _start_nexus_action(self, params):
        self._require_idle_state()
        self._check_params(params, {"game", "profile", "mods", "revision", "action", "confirm"},
                           {"game", "profile", "mods", "revision", "action", "confirm"})
        game, profile, mods = self._nexus_actions_target(params)
        self._require_no_managed_launch(game)
        if params["confirm"] is not True:
            raise ServiceError(-32602, "confirmation_required", "'confirm' must be true to change Nexus account state.")
        revision = self._required_text(params, "revision")
        if len(revision) != 64 or any(char not in "0123456789abcdef" for char in revision):
            raise ServiceError(-32602, "invalid_params", "Use the revision returned by mods.nexus.get.start.")
        from .nexus_actions import ACTIONS, action_runner
        action = self._required_text(params, "action")
        if action not in ACTIONS:
            raise ServiceError(-32602, "invalid_params", "Choose endorse, abstain, track, or untrack.")
        import threading
        cancel = threading.Event()
        job = self._jobs.submit(
            "mods.nexus.action", game, self._canonical_game(game), profile.name,
            {"mods": mods, "revision": revision, "action": action},
            runner=action_runner(self._nexus_session, game, profile, mods, revision, action, cancel), cancel=cancel.set)
        return {"job": job}

    def _start_create_mod(self, params):
        self._require_idle_state()
        self._check_params(params, {"game", "profile", "name", "revision", "position", "enabled", "root_folder"},
                           {"game", "profile", "name", "revision"})
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_no_managed_launch(game)
        self._require_configured(game)
        profile = self._resolve_profile(game, self._required_text(params, "profile"))
        name = self._mod_name(params["name"], "name")
        from .mod_create import create_runner, validate_name
        from .jobs import JobError
        try:
            validate_name(name)
        except JobError as exc:
            raise ServiceError(-32602, exc.name, str(exc), exc.details) from exc
        revision = self._required_text(params, "revision")
        if len(revision) != 64 or any(char not in "0123456789abcdef" for char in revision):
            raise ServiceError(-32602, "invalid_params", "'revision' must come from the current mod group inventory.")
        position = params.get("position", "top")
        if isinstance(position, str) and position in {"top", "bottom"}:
            pass
        elif isinstance(position, dict) and len(position) == 1 and next(iter(position)) in {"before", "after"}:
            placement, anchor = next(iter(position.items()))
            position = {placement: self._mod_name(anchor, f"position.{placement}")}
        else:
            raise ServiceError(-32602, "invalid_params", "Use 'top', 'bottom', or one 'before'/'after' anchor for 'position'.")
        flags = {key: params.get(key, key == "enabled") for key in ("enabled", "root_folder")}
        if any(not isinstance(value, bool) for value in flags.values()):
            raise ServiceError(-32602, "invalid_params", "'enabled' and 'root_folder' must be booleans.")
        job = self._jobs.submit(
            "mods.create", game, self._canonical_game(game), profile.name,
            {"name": name, "revision": revision, "position": position, **flags},
            runner=create_runner(game, profile, name, revision, position, **flags))
        return {"job": job}

    def _get_mod_properties(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        required = {"game", "profile", "mod"}
        self._check_params(params, required, required)
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_configured(game)
        profile = self._resolve_profile(game, self._required_text(params, "profile"))
        mod = self._mod_name(params["mod"], "mod")
        from .mod_properties import get_properties
        from .jobs import JobError
        from Utils.mods.state import ModStateError
        try:
            result = get_properties(game, profile, mod)
        except (JobError, ModStateError) as exc:
            raise ServiceError(-32003, exc.name, str(exc), exc.details) from exc
        except OSError as exc:
            raise ServiceError(-32003, "resource_unavailable", "Could not read the mod properties.") from exc
        return {"game": self._canonical_game(game), "properties": result}

    def _start_set_mod_properties(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        required = {"game", "profile", "mod", "revision", "changes"}
        self._check_params(params, required, required)
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_configured(game)
        self._require_no_managed_launch(game)
        profile = self._resolve_profile(game, self._required_text(params, "profile"))
        mod = self._mod_name(params["mod"], "mod")
        revision = self._required_text(params, "revision")
        if len(revision) != 64 or any(c not in "0123456789abcdef" for c in revision):
            raise ServiceError(-32602, "invalid_params", "'revision' must come from the current property inventory.")
        from Utils.mods.properties import validate_changes
        from Utils.mods.state import ModStateError
        changes = params["changes"]
        try:
            validate_changes(changes)
        except ModStateError as exc:
            raise ServiceError(-32602, "invalid_params", str(exc), exc.details) from exc
        from .mod_properties import set_properties
        job = self._jobs.submit(
            "mods.properties.set", game, self._canonical_game(game), profile.name,
            {"mod": mod, "revision": revision, "fields": sorted(changes)},
            runner=lambda log, progress: set_properties(
                game, profile, mod, revision, changes, log, progress))
        return {"job": job}

    def _mod_version_target(self, params):
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_configured(game)
        profile = self._resolve_profile(game, self._required_text(params, "profile"))
        mod = self._mod_name(params.get("mod"), "mod")
        source = params.get("source", "nexus")
        if not isinstance(source, str):
            raise ServiceError(-32602, "invalid_params", "'source' must be a source name.")
        import re
        if source not in {"nexus", "thunderstore", "modio"} and re.fullmatch(r"plugin:[a-z0-9][a-z0-9_.-]{0,99}/[a-z0-9][a-z0-9_.-]{0,99}", source) is None:
            raise ServiceError(-32009, "operation_not_supported",
                               "The installed-version source is unsupported.",
                               {"supported_sources": ["nexus", "thunderstore", "modio", "plugin:<plugin-id>/<provider-id>"]})
        if source == "modio" and getattr(game, "game_id", "") != "baldurs_gate_3":
            raise ServiceError(-32009, "operation_not_supported", "mod.io version changes support Baldur's Gate 3.")
        return game, profile, mod

    def _start_get_mod_versions(self, params):
        self._require_idle_state()
        required = {"game", "profile", "mod"}
        self._check_params(params, required | {"source"}, required)
        game, profile, mod = self._mod_version_target(params)
        from .mod_versions import versions_runner
        job = self._jobs.submit(
            "mods.versions.get", game, self._canonical_game(game), profile.name,
            {"mod": mod, "source": params.get("source", "nexus")},
            runner=versions_runner(self._nexus_session, game, profile, mod, params.get("source", "nexus")))
        return {"job": job}

    def _thunderstore_target(self, params, *, version=False):
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_configured(game)
        community = getattr(game, "thunderstore_community", "") or ""
        if not community:
            raise ServiceError(-32009, "operation_not_supported", "This game has no Thunderstore community.")
        from .thunderstore import package_link
        from .jobs import JobError
        try:
            link = package_link(self._required_text(params, "namespace"), self._required_text(params, "package"),
                                self._required_text(params, "version") if version else "0.0.0", community)
        except JobError as exc:
            raise ServiceError(-32602, "invalid_params", str(exc)) from exc
        return game, link

    def _start_thunderstore_versions(self, params):
        self._require_idle_state()
        required = {"game", "namespace", "package"}
        self._check_params(params, required, required)
        game, link = self._thunderstore_target(params)
        from .thunderstore import versions_runner
        job = self._jobs.submit(
            "thunderstore.packages.versions", game, self._canonical_game(game), None,
            {"namespace": link.namespace, "package": link.name, "community": link.community},
            runner=versions_runner(game, link))
        return {"job": job}

    def _start_thunderstore_download(self, params):
        self._require_idle_state()
        required = {"game", "namespace", "package", "version"}
        self._check_params(params, required, required)
        game, link = self._thunderstore_target(params, version=True)
        from Utils.downloads.control import DownloadControl
        from .thunderstore import download_runner
        control = DownloadControl()
        job = self._jobs.submit(
            "thunderstore.download", game, self._canonical_game(game), None,
            {"namespace": link.namespace, "package": link.name, "version": link.version,
             "community": link.community}, runner=download_runner(game, link, control),
            cancel=control.cancel, pause=control.pause, resume=control.resume)
        return {"job": job}

    def _start_change_mod_version(self, params):
        self._require_idle_state()
        source = params.get("source", "nexus")
        required = {"game", "profile", "mod", "revision", "confirm"}
        plugin_source = isinstance(source, str) and source.startswith("plugin:")
        required.add("version" if source == "thunderstore" or plugin_source else "file_id")
        if plugin_source:
            required.add("catalogue_revision")
        allowed = required | {"source", "archive_id", "installer_policy",
                              "bain_selected", "fomod_selections", "bundle_selected"}
        if source == "thunderstore":
            allowed.add("dependency_policy")
        elif source == "nexus":
            allowed.add("nxm_url")
        self._check_params(params, allowed, required)
        game, profile, mod = self._mod_version_target(params)
        self._require_no_managed_launch(game)
        if params.get("confirm") is not True:
            raise ServiceError(-32602, "invalid_params",
                               "'confirm' must be true because changing versions replaces the installed mod.")
        revision = self._required_text(params, "revision")
        if len(revision) != 64 or any(c not in "0123456789abcdef" for c in revision):
            raise ServiceError(-32602, "invalid_params",
                               "'revision' must come from the installed version catalog.")
        file_id = self._positive_integer(params, "file_id") if source != "thunderstore" and not plugin_source else None
        version = None
        if plugin_source:
            version = self._required_text(params, "version")
        if source == "thunderstore":
            version = self._required_text(params, "version")
            import re
            if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version) is None:
                raise ServiceError(-32602, "invalid_params", "Choose a numeric Thunderstore version from the catalogue.")
            dependency_policy = params.get("dependency_policy", "reject")
            if dependency_policy not in ("reject", "ignore"):
                raise ServiceError(-32602, "invalid_params", "Choose 'reject' or 'ignore' dependency policy.")
        policy = params.get("installer_policy", "reject")
        if not isinstance(policy, str) or policy not in {"reject", "defaults", "verbatim"}:
            raise ServiceError(-32602, "invalid_params", "Choose 'reject', 'defaults', or 'verbatim' installer policy.")
        if policy == "verbatim" and any(key in params for key in (
                "bain_selected", "fomod_selections", "bundle_selected")):
            raise ServiceError(-32602, "invalid_params", "Verbatim installation cannot use installer selections.")
        options = {"installer_policy": policy, "bain_selected": self._bain_selection(params, policy),
                   "fomod_selections": self._fomod_selection(params, policy),
                   "bundle_selected": self._bundle_selection(params, policy)}
        if plugin_source:
            options["catalogue_revision"] = self._required_text(params, "catalogue_revision")
        if source == "thunderstore":
            options["dependency_policy"] = dependency_policy
        nxm_link = None
        if "archive_id" in params:
            options["archive_id"] = self._archive_id(params["archive_id"])
        if "nxm_url" in params:
            if "archive_id" in params:
                raise ServiceError(-32602, "invalid_params", "Select an archive or an NXM link.")
            parsed = self._parse_nxm_link(params)
            from Nexus.nexus_meta import normalise_game_domain
            from Nexus.nxm_handler import NxmLink
            nxm_link = NxmLink(game_domain=normalise_game_domain(parsed.game_domain),
                               mod_id=parsed.mod_id, file_id=parsed.file_id,
                               key=parsed.key, expires=parsed.expires)
        from .mod_versions import change_runner
        job = self._jobs.submit(
            "mods.version.change", game, self._canonical_game(game), profile.name,
            {"mod": mod, "source": source, "revision": revision,
             **({"version": version} if source == "thunderstore" or plugin_source else {"file_id": file_id}), **options},
            runner=change_runner(self._nexus_session, game, profile, mod, revision,
                                 file_id, options, nxm_link, source, version))
        return {"job": job}

    def _modio_target(self, params):
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_configured(game)
        if getattr(game, "game_id", "") != "baldurs_gate_3":
            raise ServiceError(-32009, "operation_not_supported", "mod.io downloads support Baldur's Gate 3.")
        return game, self._positive_integer(params, "mod_id")

    def _start_modio_files(self, params):
        self._require_idle_state()
        required = {"game", "mod_id"}
        self._check_params(params, required, required)
        game, mod_id = self._modio_target(params)
        from .modio_versions import files_runner
        job = self._jobs.submit(
            "modio.mods.files", game, self._canonical_game(game), None, {"mod_id": mod_id},
            runner=files_runner(game, mod_id))
        return {"job": job}

    def _start_modio_download(self, params):
        self._require_idle_state()
        required = {"game", "mod_id", "file_id"}
        self._check_params(params, required, required)
        game, mod_id = self._modio_target(params)
        file_id = self._positive_integer(params, "file_id")
        from Utils.downloads.control import DownloadControl
        from .modio_versions import download_runner
        control = DownloadControl()
        job = self._jobs.submit(
            "modio.download", game, self._canonical_game(game), None,
            {"mod_id": mod_id, "file_id": file_id}, runner=download_runner(game, mod_id, file_id, control),
            cancel=control.cancel, pause=control.pause, resume=control.resume)
        return {"job": job}

    def _list_plugin_providers(self, params):
        from amethyst_plugins.downloads import providers
        self._check_params(params, {"game", "source"}, {"game"})
        game = self._resolve_game(self._required_text(params, "game"))
        try:
            result, errors = providers(game.game_id, self._required_text(params, "source") if "source" in params else None)
        except ValueError as exc:
            raise ServiceError(-32602, "invalid_params", str(exc)) from exc
        return {"game": self._canonical_game(game), "providers": result, "errors": errors}

    def _start_plugin_download(self, params, *, execute):
        import hashlib
        import re
        from amethyst_plugins.downloads import source_uri
        from Utils.downloads.control import DownloadControl
        from .plugin_downloads import runner
        self._require_idle_state()
        required = {"game", "plugin_id", "provider_id", "source"} | ({"confirm", "revision"} if execute else set())
        self._check_params(params, required, required)
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_configured(game)
        plugin_id, provider_id = (self._required_text(params, key) for key in ("plugin_id", "provider_id"))
        if any(re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,99}", value) is None for value in (plugin_id, provider_id)):
            raise ServiceError(-32602, "invalid_params", "Supply valid plugin and provider IDs.")
        source = self._required_text(params, "source")
        try:
            source_uri(source)
        except ValueError as exc:
            raise ServiceError(-32602, "invalid_params", str(exc)) from exc
        revision = None
        if execute:
            if params["confirm"] is not True:
                raise ServiceError(-32602, "invalid_params", "Provider downloads require confirm:true.")
            revision = self._required_text(params, "revision")
            if re.fullmatch(r"[0-9a-f]{64}", revision) is None:
                raise ServiceError(-32602, "invalid_params", "revision must identify the resolved provider artifact.")
        control = DownloadControl()
        kind = "manager_plugins.download" if execute else "manager_plugins.download.resolve"
        job = self._jobs.submit(kind, game, self._canonical_game(game), None,
            {"plugin_id": plugin_id, "provider_id": provider_id, "source_fingerprint": hashlib.sha256(source.encode()).hexdigest()},
            runner=runner(game, plugin_id, provider_id, source, control, revision), cancel=control.cancel,
            pause=control.pause if execute else None, resume=control.resume if execute else None)
        return {"job": job}

    def _start_plugin_dependencies(self, params):
        from amethyst_plugins.dependencies import graph
        from amethyst_plugins.repositories import requests
        from Utils.downloads.control import DownloadControl
        self._require_idle_state()
        self._check_params(params, {"game", "items", "revision"}, {"game", "items"})
        game = self._resolve_game(self._required_text(params, "game"))
        try:
            roots = requests(params["items"], game)
        except (ValueError, TypeError) as exc:
            raise ServiceError(-32602, "invalid_params", str(exc)) from exc
        control = DownloadControl()
        def run(log, progress):
            import hashlib
            import json
            from amethyst_plugins.repositories import lookup
            from amethyst_plugins.downloads import _signature
            from .jobs import JobError
            from Utils.operations import OperationCancelled
            from .jobs import JobCancelled
            try:
                items = graph(game, roots, log, control)
            except OperationCancelled as exc:
                raise JobCancelled("Provider dependency lookup cancelled.") from exc
            signatures = [_signature(*lookup(game.game_id, item["plugin_id"], item["provider_id"])) for item in items]
            revision = hashlib.sha256(json.dumps([game.game_id, items, signatures], sort_keys=True).encode()).hexdigest()
            if "revision" in params and params["revision"] != revision:
                raise JobError("stale_plugin_dependencies", "The dependency graph changed; review the install plan again.")
            return {"items": items, "revision": revision}
        return {"job": self._jobs.submit("manager_plugins.dependencies.get", game, self._canonical_game(game), None,
                {"item_count": len(roots)}, runner=run, cancel=control.cancel)}

    def _start_plugin_action(self, params):
        from amethyst_plugins.repositories import callback, catalogue, installed, lookup, source_key
        from .mod_properties import get_properties
        from Utils.operations import acquire_game_operation
        self._require_idle_state()
        required = {"game", "profile", "mod", "source", "action_id", "revision", "catalogue_revision", "confirm"}
        self._check_params(params, required, required)
        game, profile, mod = self._mod_version_target(params)
        self._require_no_managed_launch(game)
        if params["confirm"] is not True:
            raise ServiceError(-32602, "invalid_params", "Repository actions require confirm:true.")
        action = self._required_text(params, "action_id")
        def run(log, progress):
            from .jobs import JobError
            with acquire_game_operation(game, "manager_plugins.action"):
                properties = get_properties(game, profile, mod)
                if properties["revision"] != params["revision"]:
                    raise JobError("stale_mod_properties", "The installed mod changed; review the action again.")
                record = installed(properties["path"])
                if record is None or source_key(record) != params["source"]:
                    raise JobError("plugin_source_mismatch", "The mod does not belong to this provider.")
                catalog = catalogue(game, record, log)
                if catalog["catalogue_revision"] != params["catalogue_revision"] or not any(row["id"] == action for row in catalog["actions"]):
                    raise JobError("stale_plugin_action", "The provider action changed; read its catalogue again.")
                plugin, provider = lookup(game.game_id, record["plugin_id"], record["provider_id"])
                return {"result": callback(game, plugin, provider, "perform_action", {"installed": record, "action_id": action}, log)}
        return {"job": self._jobs.submit("manager_plugins.action", game, self._canonical_game(game), profile.name,
                {"mod": mod, "source": params["source"], "action_id": action}, runner=run)}

    def _get_plugin_metadata(self, params):
        from amethyst_plugins.metadata import get_metadata
        from Utils.mods.state import ModStateError
        self._require_idle_state()
        self._check_params(params, {"game", "profile", "plugin_id", "mods"}, {"game", "profile", "plugin_id"})
        game = self._resolve_game(self._required_text(params, "game"))
        profile = self._resolve_profile(game, self._required_text(params, "profile"))
        if "mods" in params and not isinstance(params["mods"], list):
            raise ServiceError(-32602, "invalid_params", "mods must be an array when supplied.")
        try:
            result = get_metadata(profile, self._required_text(params, "plugin_id"), params.get("mods"))
        except ModStateError as exc:
            raise ServiceError(-32003, exc.name, str(exc), exc.details) from exc
        except OSError as exc:
            raise ServiceError(-32003, "resource_unavailable", "Could not read plugin metadata.") from exc
        return {"game": self._canonical_game(game), **result}

    def _start_set_plugin_metadata(self, params):
        from amethyst_plugins.metadata import edit_metadata, plugin_id, validate_changes
        from Utils.mods.state import ModStateError
        self._require_idle_state()
        required = {"game", "profile", "plugin_id", "revision", "changes", "confirm"}
        self._check_params(params, required, required)
        if params.get("confirm") is not True:
            raise ServiceError(-32602, "invalid_params", "Saving plugin metadata requires confirm:true.")
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_configured(game)
        self._require_no_managed_launch(game)
        profile = self._resolve_profile(game, self._required_text(params, "profile"))
        revision = self._required_text(params, "revision")
        if len(revision) != 64 or any(c not in "0123456789abcdef" for c in revision):
            raise ServiceError(-32602, "invalid_params", "revision must come from a metadata inventory.")
        try:
            pid = plugin_id(params["plugin_id"])
            changes = validate_changes(params["changes"])
        except ModStateError as exc:
            raise ServiceError(-32602, exc.name, str(exc), exc.details) from exc
        def run(log, progress):
            from Utils.operations import acquire_game_operation
            with acquire_game_operation(game, "manager_plugins.metadata.set", owner="headless:manager_plugins.metadata.set"):
                progress(0, 1, "Saving plugin metadata")
                result = edit_metadata(profile, pid, changes, revision)
                progress(1, 1, "Plugin metadata saved")
                log("Saved plugin metadata." if result["changed"] else "Plugin metadata already matched.")
                return result
        job = self._jobs.submit("manager_plugins.metadata.set", game, self._canonical_game(game), profile.name,
                                {"plugin_id": pid, "revision": revision}, runner=run)
        return {"job": job}

    def _get_mod_notes(self, params: dict[str, Any]) -> dict[str, Any]:
        from Utils.mods.notes import MAX_CHANGES, get_mod_notes

        self._require_idle_state()
        self._check_params(params, {"game", "profile", "mods"}, {"game", "profile"})
        game = self._resolve_game(self._required_text(params, "game"))
        profile = self._resolve_profile(game, self._required_text(params, "profile"))
        mods = params.get("mods")
        if mods is not None:
            if not isinstance(mods, list) or not 1 <= len(mods) <= MAX_CHANGES:
                raise ServiceError(-32602, "invalid_params", f"'mods' must contain 1 to {MAX_CHANGES} mod names.")
            mods = [self._mod_name(value, f"mods[{index}]") for index, value in enumerate(mods)]
            if len(set(mods)) != len(mods):
                raise ServiceError(-32602, "invalid_params", "'mods' contains duplicate names.")
        elif "mods" in params:
            raise ServiceError(-32602, "invalid_params", "'mods' must be an array when supplied.")
        from Utils.mods.state import ModStateError
        try:
            result = get_mod_notes(profile, mods)
        except ModStateError as exc:
            raise ServiceError(-32003, exc.name, str(exc), exc.details) from exc
        except OSError as exc:
            raise ServiceError(-32003, "resource_unavailable", "Could not read the profile notes.") from exc
        return {"game": self._canonical_game(game), **result}

    def _start_set_mod_notes(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        required = {"game", "profile", "revision", "changes"}
        self._check_params(params, required | {"append"}, required)
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_configured(game)
        self._require_no_managed_launch(game)
        profile = self._resolve_profile(game, self._required_text(params, "profile"))
        revision = self._required_text(params, "revision")
        if len(revision) != 64 or any(char not in "0123456789abcdef" for char in revision):
            raise ServiceError(-32602, "invalid_params", "'revision' must come from the current note inventory.")
        from Utils.mods.notes import edit_mod_notes, validate_changes
        from Utils.mods.state import ModStateError
        changes, append = params["changes"], params.get("append", False)
        try:
            validate_changes(changes, append)
        except ModStateError as exc:
            raise ServiceError(-32602, "invalid_params", str(exc), exc.details) from exc

        def run(log, progress):
            from Utils.operations import acquire_game_operation
            with acquire_game_operation(game, "mods.notes.set", owner="headless:mods.notes.set"):
                progress(0, 1, "Saving mod notes")
                result = edit_mod_notes(profile, changes, append=append, revision=revision)
                progress(1, 1, "Mod notes saved")
                log("Saved mod notes." if result["changed"] else "Mod notes already matched the request.")
                return result

        job = self._jobs.submit(
            "mods.notes.set", game, self._canonical_game(game), profile.name,
            {"revision": revision, "change_count": len(changes), "append": append}, runner=run)
        return {"job": job}

    def _get_mod_requirements(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(params, {"game", "profile", "mods"}, {"game", "profile"})
        game = self._resolve_game(self._required_text(params, "game"))
        profile = self._resolve_profile(game, self._required_text(params, "profile"))
        mods = None
        if "mods" in params:
            raw = params["mods"]
            if not isinstance(raw, list) or not raw or len(raw) > 4096:
                raise ServiceError(-32602, "invalid_params", "'mods' must contain 1 to 4096 mod names.",
                                   {"parameter": "mods"})
            mods = [self._mod_name(value, f"mods[{index}]") for index, value in enumerate(raw)]
            if len(set(mods)) != len(mods):
                raise ServiceError(-32602, "invalid_params", "'mods' contains duplicate names.",
                                   {"parameter": "mods"})
        from .jobs import JobError
        from .mod_requirements import read_requirements
        try:
            result = read_requirements(game, profile).inventory(profile, mods)
        except JobError as exc:
            raise ServiceError(-32003, exc.name, str(exc), exc.details) from exc
        return {"game": self._canonical_game(game), **result}

    def _start_set_requirement_ignores(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(params, {"game", "profile", "mod", "revision", "ignored", "requirement_ids"},
                           {"game", "profile", "mod", "revision"})
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_no_managed_launch(game)
        self._require_configured(game)
        profile = self._resolve_profile(game, self._required_text(params, "profile"))
        mod = self._mod_name(params["mod"], "mod")
        revision = self._required_text(params, "revision")
        if len(revision) != 64 or any(char not in "0123456789abcdef" for char in revision):
            raise ServiceError(-32602, "invalid_params", "'revision' must come from the current requirement inventory.",
                               {"parameter": "revision"})
        if not {"ignored", "requirement_ids"}.intersection(params):
            raise ServiceError(-32602, "invalid_params", "Supply 'ignored', 'requirement_ids', or both.")
        options = {}
        if "ignored" in params:
            if not isinstance(params["ignored"], bool):
                raise ServiceError(-32602, "invalid_params", "'ignored' must be a boolean.",
                                   {"parameter": "ignored"})
            options["ignored"] = params["ignored"]
        if "requirement_ids" in params:
            ids = params["requirement_ids"]
            if (not isinstance(ids, list) or len(ids) > 4096
                    or any(isinstance(mid, bool) or not isinstance(mid, int) or mid <= 0 for mid in ids)
                    or len(set(ids)) != len(ids)):
                raise ServiceError(-32602, "invalid_params", "'requirement_ids' must contain unique positive integers.",
                                   {"parameter": "requirement_ids"})
            options["requirement_ids"] = sorted(ids)
        from .mod_requirements import set_ignored
        job = self._jobs.submit(
            "mods.requirements.ignore.set", game, self._canonical_game(game), profile.name,
            {"mod": mod, "revision": revision, **options},
            runner=lambda log, progress: set_ignored(
                game, profile, mod, revision, options, "headless:mods.requirements.ignore.set", log, progress))
        return {"job": job}

    def _get_mod_groups(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(params, {"game", "profile"}, {"game", "profile"})
        game = self._resolve_game(self._required_text(params, "game"))
        profile = self._resolve_profile(game, self._required_text(params, "profile"))
        from .mod_groups import get_groups
        return {"game": self._canonical_game(game), **get_groups(profile)}

    def _start_edit_mod_groups(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        from .mod_groups import ACTIONS, edit_groups
        base = {"game", "profile", "action", "revision"}
        self._check_params(params, base | {"mods", "group", "leader", "title", "collapsed", "position"}, base)
        action = self._required_text(params, "action")
        if action not in ACTIONS:
            raise ServiceError(-32602, "invalid_params", "Unknown mod group action.",
                               {"actions": list(ACTIONS)})
        required, optional = ACTIONS[action]
        self._check_params(params, base | required | optional, base | required)
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_no_managed_launch(game)
        self._require_configured(game)
        profile = self._resolve_profile(game, self._required_text(params, "profile"))
        revision = self._required_text(params, "revision")
        if len(revision) != 64 or any(char not in "0123456789abcdef" for char in revision):
            raise ServiceError(-32602, "invalid_params", "'revision' must come from the current group inventory.",
                               {"parameter": "revision"})
        options = {"action": action, "revision": revision}
        for key in ("group", "leader"):
            if key in params:
                options[key] = self._mod_name(params[key], key)
        if "mods" in params:
            raw = params["mods"]
            if not isinstance(raw, list) or not raw or len(raw) > 4096:
                raise ServiceError(-32602, "invalid_params", "'mods' must contain 1 to 4096 mod or group names.",
                                   {"parameter": "mods"})
            mods = [self._mod_name(value, f"mods[{index}]") for index, value in enumerate(raw)]
            if len(set(mods)) != len(mods):
                raise ServiceError(-32602, "invalid_params", "'mods' contains duplicate names.",
                                   {"parameter": "mods"})
            options["mods"] = mods
        if "title" in params:
            title = params["title"]
            if (not isinstance(title, str) or not title.strip() or len(title) > 256
                    or any(char in title for char in "\x00\r\n")):
                raise ServiceError(-32602, "invalid_params", "'title' must contain 1 to 256 characters on one line.",
                                   {"parameter": "title"})
            options["title"] = title.strip()
        if action == "create" and (("leader" in params) == ("title" in params)):
            raise ServiceError(-32602, "invalid_params", "Create a group with exactly one of 'leader' or 'title'.")
        if "collapsed" in params:
            if not isinstance(params["collapsed"], bool):
                raise ServiceError(-32602, "invalid_params", "'collapsed' must be a boolean.",
                                   {"parameter": "collapsed"})
            options["collapsed"] = params["collapsed"]
        if "position" in params:
            position = params["position"]
            if (not isinstance(position, dict) or len(position) != 1
                    or next(iter(position), None) not in {"before", "after"}):
                raise ServiceError(-32602, "invalid_params", "'position' must contain one 'before' or 'after' anchor.",
                                   {"parameter": "position"})
            placement, anchor = next(iter(position.items()))
            options["position"] = {placement: self._mod_name(anchor, f"position.{placement}")}
        job = self._jobs.submit(
            "mods.groups.edit", game, self._canonical_game(game), profile.name, options,
            runner=lambda log, progress: edit_groups(
                game, profile, options, "headless:mods.groups.edit", log, progress))
        return {"job": job}

    def _batch_profile(self, params):
        self._require_idle_state()
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_configured(game)
        self._require_no_managed_launch(game)
        profile = self._resolve_profile(game, self._required_text(params, "profile"))
        if profile.is_symlink():
            raise ServiceError(-32602, "invalid_params", "A symlinked profile is unsafe.")
        return game, profile

    def _batch_mods(self, params):
        if "mods" not in params:
            return None
        raw = params["mods"]
        if not isinstance(raw, list) or not 1 <= len(raw) <= 4096:
            raise ServiceError(-32602, "invalid_params", "'mods' must contain 1 to 4096 exact mod names.")
        names = [self._mod_name(value, "mods") for value in raw]
        if len(set(names)) != len(names):
            raise ServiceError(-32602, "invalid_params", "Duplicate mod names are unsupported.")
        return names

    def _start_batch_conflicts(self, params, *, summary=False):
        required = {"game", "profile"}
        allowed = required | {"mods", "offset", "limit", "revision"}
        if not summary:
            allowed.add("section")
        self._check_params(params, allowed, required)
        game, profile = self._batch_profile(params)
        mods = self._batch_mods(params)
        section = params.get("section", "wins")
        if section not in ("wins", "losses", "uncontested"):
            raise ServiceError(-32602, "invalid_params", "Unknown conflict section.")
        offset = self._integer_range(params.get("offset", 0), "offset", 0, 2147483647)
        limit = self._integer_range(params.get("limit", 500), "limit", 1, 5000)
        revision = params.get("revision")
        if revision is not None and (not isinstance(revision, str) or revision.count(":") != 1
                                     or not all(part.isascii() and part.isdecimal() for part in revision.split(":"))):
            raise ServiceError(-32602, "invalid_params", "'revision' must be an inventory:generation pair.")
        from threading import Event
        from .conflicts import batch_runner
        cancel = Event()
        kind = "mods.conflicts.overview" if summary else "mods.conflicts.inspect.batch"
        job = self._jobs.submit(kind, game, self._canonical_game(game), profile.name,
                                {"mods": mods, "section": section, "offset": offset, "limit": limit},
                                runner=batch_runner(game, profile, mods, section, offset, limit, revision, cancel, summary=summary),
                                cancel=cancel.set)
        return {"job": job}

    def _start_layout(self, params, *, apply):
        base = {"game", "profile", "entries"}
        allowed = base | {"groups", "preserve_conflicts"}
        if apply:
            allowed |= {"plan_revision", "confirm"}
            base |= {"plan_revision", "confirm"}
        self._check_params(params, allowed, base)
        game, profile = self._batch_profile(params)
        if not isinstance(params["entries"], list) or len(params["entries"]) > 16384:
            raise ServiceError(-32602, "invalid_params", "'entries' must be a layout array of at most 16384 entries.")
        if type(params.get("preserve_conflicts", True)) is not bool:
            raise ServiceError(-32602, "invalid_params", "'preserve_conflicts' must be boolean.")
        revision = self._required_text(params, "plan_revision") if apply else None
        if apply and (params["confirm"] is not True or len(revision) != 64
                      or any(c not in "0123456789abcdef" for c in revision)):
            raise ServiceError(-32602, "invalid_params", "Apply requires confirm:true and the plan revision.")
        request = {key: params[key] for key in ("entries", "groups", "preserve_conflicts") if key in params}
        from .layout import runner
        kind = "mods.layout.apply" if apply else "mods.layout.plan"
        return {"job": self._jobs.submit(kind, game, self._canonical_game(game), profile.name,
                                         {"entry_count": len(params["entries"])}, runner=runner(game, profile, request, revision))}

    def _start_batch_properties(self, params, *, write):
        base = {"game", "profile"}
        self._check_params(params, base | ({"items"} if write else {"mods"}), base | ({"items"} if write else set()))
        game, profile = self._batch_profile(params)
        from .mod_properties import get_properties_batch, set_properties_batch
        if not write:
            mods = self._batch_mods(params)
            return {"job": self._jobs.submit("mods.properties.get.batch", game, self._canonical_game(game), profile.name,
                                             {"mods": mods}, runner=lambda log, progress: get_properties_batch(game, profile, mods))}
        raw = params["items"]
        if not isinstance(raw, list) or not 1 <= len(raw) <= 256:
            raise ServiceError(-32602, "invalid_params", "'items' must contain 1 to 256 property edits.")
        from Utils.mods.properties import validate_changes
        from Utils.mods.state import ModStateError
        items = []
        for item in raw:
            if not isinstance(item, dict):
                raise ServiceError(-32602, "invalid_params", "Each property edit must be an object.")
            self._check_params(item, {"mod", "revision", "changes"}, {"mod", "revision", "changes"})
            mod = self._mod_name(item["mod"], "mod")
            revision = self._required_text(item, "revision")
            if len(revision) != 64 or any(c not in "0123456789abcdef" for c in revision):
                raise ServiceError(-32602, "invalid_params", "Use the current per-mod property revision.")
            try:
                validate_changes(item["changes"])
            except ModStateError as exc:
                raise ServiceError(-32602, "invalid_params", str(exc), exc.details) from exc
            items.append({"mod": mod, "revision": revision, "changes": item["changes"]})
        if len({item["mod"] for item in items}) != len(items):
            raise ServiceError(-32602, "invalid_params", "Duplicate property targets are unsupported.")
        return {"job": self._jobs.submit("mods.properties.set.batch", game, self._canonical_game(game), profile.name,
                                         {"mods": [item["mod"] for item in items]},
                                         runner=lambda log, progress: set_properties_batch(game, profile, items, log, progress))}

    def _start_inspect_mod_conflicts(
        self, params: dict[str, Any],
    ) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(
            params,
            {"game", "profile", "mod", "section", "offset", "limit", "revision"},
            {"game", "profile", "mod"},
        )
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_no_managed_launch(game)
        self._require_configured(game)
        profile = self._resolve_profile(
            game, self._required_text(params, "profile"))
        if profile.is_symlink():
            raise ServiceError(-32602, "invalid_params",
                               "A symlinked profile cannot be inspected.",
                               {"parameter": "profile"})
        mod_name = self._mod_name(params["mod"], "mod")
        section = params.get("section", "wins")
        if not isinstance(section, str) or section not in {
                "wins", "losses", "uncontested"}:
            raise ServiceError(
                -32602, "invalid_params",
                "'section' must be wins, losses, or uncontested.",
                {"parameter": "section"},
            )
        offset = self._integer_range(
            params.get("offset", 0), "offset", 0, 2_147_483_647)
        limit = self._integer_range(
            params.get("limit", 500), "limit", 1, 5000)
        revision = params.get("revision")
        if revision is not None:
            if (not isinstance(revision, str) or revision.count(":") != 1
                    or not all(part.isascii() and part.isdecimal()
                               for part in revision.split(":"))):
                raise ServiceError(
                    -32602, "invalid_params",
                    "'revision' must be an inventory:generation pair.",
                    {"parameter": "revision"},
                )
        from .conflicts import inspect_runner
        job = self._jobs.submit(
            "mods.conflicts.inspect", game, self._canonical_game(game),
            profile.name,
            {"mod": mod_name, "section": section,
             "offset": offset, "limit": limit,
             "revision": revision},
            runner=inspect_runner(
                game, profile, mod_name, section, offset, limit, revision,
                "headless:mods.conflicts.inspect"),
        )
        return {"job": job}

    @staticmethod
    def _separator_display_name(value: Any, parameter: str) -> str:
        if (not isinstance(value, str) or not value
                or value != value.strip() or len(value) > 255
                or value in (".", "..") or "/" in value or "\\" in value
                or any(ord(char) < 32 or 0xd800 <= ord(char) <= 0xdfff
                       for char in value)):
            raise ServiceError(
                -32602, "invalid_params",
                f"'{parameter}' must be a valid separator display name.",
                {"parameter": parameter},
            )
        return value

    def _start_separator_edit(self, params: dict[str, Any],
                              action: str) -> dict[str, Any]:
        required = {"game", "profile", "revision"}
        optional: set[str] = set()
        if action == "create":
            required.add("name")
            optional.add("position")
        elif action == "rename":
            required.update({"separator", "new_name"})
        else:
            required.update({"separator", "confirm"})
        self._require_idle_state()
        self._check_params(params, required | optional, required)
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_no_managed_launch(game)
        self._require_configured(game)
        profile_dir = self._resolve_profile(
            game, self._required_text(params, "profile"))
        revision = self._required_text(params, "revision")
        if (len(revision) != 64
                or any(char not in "0123456789abcdef" for char in revision)):
            raise ServiceError(
                -32602, "invalid_params", "'revision' must be a SHA-256 hex digest.",
                {"parameter": "revision"},
            )
        options: dict[str, Any] = {"revision": revision}
        if action == "create":
            options["new_name"] = self._separator_display_name(
                params.get("name"), "name")
            position = params.get("position")
            if position is not None:
                if (not isinstance(position, dict) or len(position) != 1
                        or next(iter(position), None) not in {"before", "after"}):
                    raise ServiceError(
                        -32602, "invalid_params",
                        "'position' must contain one 'before' or 'after' anchor.",
                        {"parameter": "position"},
                    )
                placement, raw_anchor = next(iter(position.items()))
                options["position"] = {
                    placement: self._mod_name(raw_anchor, f"position.{placement}")}
        else:
            separator = self._mod_name(params.get("separator"), "separator")
            if not separator.endswith("_separator"):
                raise ServiceError(
                    -32602, "invalid_params",
                    "'separator' must be an internal separator name.",
                    {"parameter": "separator"},
                )
            options["separator"] = separator
            if action == "rename":
                options["new_name"] = self._separator_display_name(
                    params.get("new_name"), "new_name")
            elif params.get("confirm") is not True:
                raise ServiceError(
                    -32602, "invalid_params",
                    "'confirm' must be true to remove a separator.",
                    {"parameter": "confirm"},
                )
        job = self._jobs.submit(
            f"separators.{action}", game, self._canonical_game(game),
            profile_dir.name, options,
        )
        return {"job": job}

    def _start_create_separator(self, params: dict[str, Any]) -> dict[str, Any]:
        return self._start_separator_edit(params, "create")

    def _start_rename_separator(self, params: dict[str, Any]) -> dict[str, Any]:
        return self._start_separator_edit(params, "rename")

    def _start_remove_separator(self, params: dict[str, Any]) -> dict[str, Any]:
        return self._start_separator_edit(params, "remove")

    def _start_set_separator_settings(
        self, params: dict[str, Any],
    ) -> dict[str, Any]:
        required = {"game", "profile", "separator", "revision",
                    "state_revision", "changes"}
        self._require_idle_state()
        self._check_params(params, required, required)
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_no_managed_launch(game)
        self._require_configured(game)
        profile_dir = self._resolve_profile(
            game, self._required_text(params, "profile"))
        separator = self._mod_name(params.get("separator"), "separator")
        if not separator.endswith("_separator"):
            raise ServiceError(
                -32602, "invalid_params",
                "'separator' must be an internal separator name.",
                {"parameter": "separator"},
            )
        revision = self._required_text(params, "revision")
        state_revision = self._required_text(params, "state_revision")
        for parameter, value in (("revision", revision),
                                 ("state_revision", state_revision)):
            if (len(value) != 64 or any(
                    char not in "0123456789abcdef" for char in value)):
                raise ServiceError(
                    -32602, "invalid_params",
                    f"'{parameter}' must be a SHA-256 hex digest.",
                    {"parameter": parameter},
                )
        changes = params.get("changes")
        allowed = {"collapsed", "locked", "color", "deploy_path"}
        if (not isinstance(changes, dict) or not changes
                or set(changes) - allowed):
            raise ServiceError(
                -32602, "invalid_params",
                "'changes' must contain one or more separator settings.",
                {"parameter": "changes", "allowed": sorted(allowed)},
            )
        changes = dict(changes)
        for key in ("collapsed", "locked"):
            if key in changes and not isinstance(changes[key], bool):
                raise ServiceError(
                    -32602, "invalid_params", f"'changes.{key}' must be boolean.",
                    {"parameter": f"changes.{key}"},
                )
        if "color" in changes:
            color = changes["color"]
            if (color is not None and (not isinstance(color, str)
                    or len(color) != 7 or not color.startswith("#")
                    or any(char not in "0123456789abcdefABCDEF"
                           for char in color[1:]))):
                raise ServiceError(
                    -32602, "invalid_params",
                    "'changes.color' must be #rrggbb or null.",
                    {"parameter": "changes.color"},
                )
            if color is not None:
                changes["color"] = color.lower()
        if "deploy_path" in changes:
            deploy = changes["deploy_path"]
            if deploy is not None:
                if (not isinstance(deploy, dict)
                        or set(deploy) != {"path", "raw", "mode", "merge"}
                        or not isinstance(deploy.get("path"), str)
                        or not isinstance(deploy.get("raw"), bool)
                        or not isinstance(deploy.get("merge"), bool)
                        or not isinstance(deploy.get("mode"), str)
                        or deploy.get("mode") not in {"", "hardlink", "symlink"}):
                    raise ServiceError(
                        -32602, "invalid_params",
                        "'changes.deploy_path' must contain path, raw, mode, and merge.",
                        {"parameter": "changes.deploy_path"},
                    )
                path = deploy["path"]
                if (path and not Path(path).is_absolute()) or any(
                        ord(char) < 32 or 0xd800 <= ord(char) <= 0xdfff
                        for char in path):
                    raise ServiceError(
                        -32602, "invalid_params",
                        "A separator deploy path must be absolute or empty.",
                        {"parameter": "changes.deploy_path.path"},
                    )
                if not (path or deploy["raw"] or deploy["mode"]
                        or deploy["merge"]):
                    changes["deploy_path"] = None
                else:
                    changes["deploy_path"] = dict(deploy)
        job = self._jobs.submit(
            "separators.settings.set", game, self._canonical_game(game),
            profile_dir.name,
            {"separator": separator, "revision": revision,
             "state_revision": state_revision, "changes": changes},
        )
        return {"job": job}

    def _get_bundle(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(params, {"game", "profile", "mod"},
                           {"game", "profile", "mod"})
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_configured(game)
        profile = self._resolve_profile(
            game, self._required_text(params, "profile"))
        mod = self._mod_name(params.get("mod"), "mod")
        from .bundle import installed_choices
        from .jobs import JobError
        try:
            return installed_choices(game, profile, mod)
        except JobError as exc:
            raise ServiceError(-32003, exc.name, str(exc), exc.details) from exc

    def _start_set_bundle(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(
            params, {"game", "profile", "mod", "revision", "bundle_selected"},
            {"game", "profile", "mod", "revision", "bundle_selected"})
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_no_managed_launch(game)
        self._require_configured(game)
        profile = self._resolve_profile(
            game, self._required_text(params, "profile"))
        mod = self._mod_name(params.get("mod"), "mod")
        revision = self._required_text(params, "revision")
        if len(revision) != 64 or any(char not in "0123456789abcdef" for char in revision):
            raise ServiceError(-32602, "invalid_params",
                               "'revision' must be the token returned by mods.bundle.get.",
                               {"parameter": "revision"})
        selected = self._bundle_selection(params, "reject")
        job = self._jobs.submit(
            "mods.bundle.set", game, self._canonical_game(game), profile.name,
            {"mod": mod, "revision": revision, "bundle_selected": selected})
        return {"job": job}

    def _start_check_mod_updates(
        self, params: dict[str, Any],
    ) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(
            params, {"game", "profile", "mods", "sources"},
            {"game", "profile"})
        game = self._resolve_game(self._required_text(params, "game"))
        domains = self._nexus_domains(game)
        is_bg3 = self._safe_attr(game, "game_id", "") == "baldurs_gate_3"
        if "sources" in params:
            sources = [
                source.casefold()
                for source in self._text_array(params["sources"], "sources")
            ]
            if not sources:
                raise ServiceError(
                    -32602,
                    "invalid_params",
                    "'sources' must contain at least one update source.",
                    {"parameter": "sources"},
                )
            unsupported = [
                source for source in sources
                if source not in {"nexus", "thunderstore", "modio", "plugins"}
            ]
            if unsupported:
                raise ServiceError(
                    -32602,
                    "invalid_params",
                    "'sources' contains an unsupported update source.",
                    {
                        "parameter": "sources",
                        "unsupported": unsupported,
                        "supported": ["nexus", "thunderstore", "modio", "plugins"],
                    },
                )
            requested_sources = set(sources)
            sources = [
                source for source in ("nexus", "thunderstore", "modio", "plugins")
                if source in requested_sources
            ]
        else:
            sources = (["nexus"] if domains else []) + ["thunderstore", "plugins"]
            if is_bg3:
                try:
                    from .modio import credential_status
                    if credential_status()["configured"]:
                        sources.append("modio")
                except Exception:
                    pass
        if "nexus" in sources and not domains:
            self._unsupported_operation(game, "Nexus Mods metadata")
        if "modio" in sources and not is_bg3:
            self._unsupported_operation(game, "mod.io updates")
        domain = domains[0] if domains else ""
        self._require_no_managed_launch(game)
        self._require_configured(game)
        profile_dir = self._resolve_profile(
            game, self._required_text(params, "profile"))

        raw_mods = params.get("mods")
        mods = None
        if raw_mods is not None:
            if (not isinstance(raw_mods, list) or not raw_mods
                    or len(raw_mods) > 4096):
                raise ServiceError(
                    -32602,
                    "invalid_params",
                    "'mods' must be an array of 1 to 4096 mod names.",
                    {"parameter": "mods"},
                )
            mods = []
            seen: set[str] = set()
            for index, value in enumerate(raw_mods):
                parameter = f"mods[{index}]"
                name = self._mod_name(value, parameter)
                if name in seen:
                    raise ServiceError(
                        -32602,
                        "invalid_params",
                        f"'{parameter}' duplicates an earlier mod name.",
                        {"parameter": parameter, "mod": name},
                    )
                seen.add(name)
                mods.append(name)

        from .nexus import check_updates_runner
        job = self._jobs.submit(
            "mods.check_updates",
            game,
            self._canonical_game(game),
            profile_dir.name,
            {"mods": mods, "sources": sources},
            runner=check_updates_runner(
                self._nexus_session,
                game,
                profile_dir,
                domain,
                mods,
                tuple(sources),
                "headless:mods.check_updates",
            ),
        )
        return {"job": job}

    def _start_plugin_quick_update(self, params, *, execute):
        from Utils.downloads.control import DownloadControl
        from .plugin_quick_update import runner
        self._require_idle_state()
        required = {"game", "profile"} | ({"revision", "confirm"} if execute else set())
        allowed = required | {"source", "mods"} | ({"download_only", "installer_policy", "per_mod"} if execute else set())
        self._check_params(params, allowed, required)
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_configured(game)
        self._require_no_managed_launch(game)
        profile = self._resolve_profile(game, self._required_text(params, "profile"))
        mods = self._text_array(params["mods"], "mods") if "mods" in params else None
        if mods is not None and (not 1 <= len(mods) <= 4096 or len(mods) != len(set(mods))):
            raise ServiceError(-32602, "invalid_params", "Choose 1 to 4096 unique mods or groups.")
        revision, options = None, {}
        if execute:
            if params["confirm"] is not True or type(params.get("download_only", False)) is not bool:
                raise ServiceError(-32602, "invalid_params", "Provider updates require confirm:true and a boolean download_only.")
            revision = self._required_text(params, "revision")
            options = {"download_only": params.get("download_only", False), "installer_options": self._quick_update_installer_options(params, "reject")}
            overrides = params.get("per_mod", {})
            if not isinstance(overrides, dict) or len(overrides) > 4096:
                raise ServiceError(-32602, "invalid_params", "per_mod must map mod names to installer options.")
            per_mod = {}
            for name, value in overrides.items():
                self._mod_name(name, "per_mod key")
                self._check_params(value, {"installer_policy", "bain_selected", "fomod_selections", "bundle_selected", "archive_id"})
                per_mod[name] = self._quick_update_installer_options(value, options["installer_options"]["installer_policy"])
            options["per_mod"] = per_mod
        control = DownloadControl()
        kind = "mods.quick_update" if execute else "mods.quick_update.plan"
        return {"job": self._jobs.submit(kind, game, self._canonical_game(game), profile.name,
            {"source": "plugins", "mods": mods}, runner=runner(game, profile, mods, revision, options, control), cancel=control.cancel)}

    def _quick_update_target(self, params):
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_configured(game)
        if not self._nexus_domains(game):
            self._unsupported_operation(game, "Nexus Quick Update")
        source = params.get("source", "nexus")
        if not isinstance(source, str):
            raise ServiceError(-32602, "invalid_params", "'source' must be a string.")
        if source != "nexus":
            raise ServiceError(-32009, "operation_not_supported", "Quick Update supports Nexus filename matching.",
                               {"supported_sources": ["nexus"]})
        profile = self._resolve_profile(game, self._required_text(params, "profile"))
        mods = None
        if "mods" in params:
            if not isinstance(params["mods"], list) or not 1 <= len(params["mods"]) <= 4096:
                raise ServiceError(-32602, "invalid_params", "Choose 1 to 4096 mods or mod groups.")
            mods = [self._mod_name(value, f"mods[{index}]") for index, value in enumerate(params["mods"])]
            if len(mods) != len(set(mods)):
                raise ServiceError(-32602, "invalid_params", "Quick Update selection has duplicate names.")
        return game, profile, mods

    def _start_plan_quick_update(self, params):
        if params.get("source") == "plugins":
            return self._start_plugin_quick_update(params, execute=False)
        self._require_idle_state()
        self._check_params(params, {"game", "profile", "mods", "source"}, {"game", "profile"})
        game, profile, mods = self._quick_update_target(params)
        from .quick_update import plan_runner
        job = self._jobs.submit(
            "mods.quick_update.plan", game, self._canonical_game(game), profile.name, {"mods": mods, "source": "nexus"},
            runner=plan_runner(self._nexus_session, game, profile, mods))
        return {"job": job}

    def _quick_update_installer_options(self, params, default_policy):
        policy = params.get("installer_policy", default_policy)
        if not isinstance(policy, str) or policy not in {"reject", "defaults", "verbatim"}:
            raise ServiceError(-32602, "invalid_params", "Choose 'reject', 'defaults', or 'verbatim' installer policy.")
        if policy == "verbatim" and any(key in params for key in ("bain_selected", "fomod_selections", "bundle_selected")):
            raise ServiceError(-32602, "invalid_params", "Verbatim installation cannot use installer selections.")
        options = {"installer_policy": policy, "bain_selected": self._bain_selection(params, policy),
                   "fomod_selections": self._fomod_selection(params, policy),
                   "bundle_selected": self._bundle_selection(params, policy)}
        if "archive_id" in params:
            options["archive_id"] = self._archive_id(params["archive_id"])
        return options

    def _start_quick_update(self, params):
        if params.get("source") == "plugins":
            return self._start_plugin_quick_update(params, execute=True)
        self._require_idle_state()
        self._check_params(params, {"game", "profile", "mods", "source", "revision", "confirm", "download_only",
                                   "installer_policy", "per_mod"}, {"game", "profile", "revision", "confirm"})
        game, profile, mods = self._quick_update_target(params)
        self._require_no_managed_launch(game)
        if params["confirm"] is not True:
            raise ServiceError(-32602, "invalid_params", "'confirm' must be true to execute a Quick Update plan.")
        revision = self._required_text(params, "revision")
        if len(revision) != 64 or any(char not in "0123456789abcdef" for char in revision):
            raise ServiceError(-32602, "invalid_params", "Use the revision returned by mods.quick_update.plan.start.")
        download_only = params.get("download_only", False)
        if not isinstance(download_only, bool):
            raise ServiceError(-32602, "invalid_params", "'download_only' must be a boolean.")
        installer_options = self._quick_update_installer_options(params, "reject")
        overrides = params.get("per_mod", {})
        if not isinstance(overrides, dict) or len(overrides) > 4096:
            raise ServiceError(-32602, "invalid_params", "'per_mod' must map mod names to update options.")
        per_mod, links = {}, {}
        for name, value in overrides.items():
            self._mod_name(name, "per_mod key")
            if not isinstance(value, dict):
                raise ServiceError(-32602, "invalid_params", "Per-mod options must be objects.")
            self._check_params(value, {"archive_id", "nxm_url", "installer_policy", "bain_selected", "fomod_selections", "bundle_selected"})
            if "archive_id" in value and "nxm_url" in value:
                raise ServiceError(-32602, "invalid_params", "Choose an archive or an NXM link for each mod.")
            per_mod[name] = self._quick_update_installer_options(value, installer_options["installer_policy"])
            if "nxm_url" in value:
                from Nexus.nxm_handler import NxmLink
                from Nexus.nexus_meta import normalise_game_domain
                parsed = self._parse_nxm_link(value)
                links[name] = NxmLink(game_domain=normalise_game_domain(parsed.game_domain), mod_id=parsed.mod_id,
                                      file_id=parsed.file_id, key=parsed.key, expires=parsed.expires)
        from Utils.downloads.control import DownloadControl
        from .quick_update import update_runner
        control = DownloadControl()
        options = {"download_only": download_only, "installer_options": installer_options}
        job = self._jobs.submit(
            "mods.quick_update", game, self._canonical_game(game), profile.name,
            {"mods": mods, "source": "nexus", "revision": revision, **options, "per_mod": per_mod},
            runner=update_runner(self._nexus_session, game, profile, mods, revision, options, per_mod, links, control),
            cancel=control.cancel, pause=control.pause, resume=control.resume)
        return {"job": job}

    def _start_reinstall_mod(
        self, params: dict[str, Any],
    ) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(
            params,
            {
                "game", "profile", "mod", "archive_id",
                "installer_policy", "bain_selected", "fomod_selections",
                "bundle_selected", "confirm", "content_prefix", "interactive",
            },
            {"game", "profile", "mod", "confirm"},
        )
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_no_managed_launch(game)
        self._require_configured(game)
        profile_dir = self._resolve_profile(
            game, self._required_text(params, "profile"))
        mod = self._mod_name(params.get("mod"), "mod")
        archive_id = None
        if "archive_id" in params:
            archive_id = self._archive_id(params.get("archive_id"))
        installer_policy = params.get("installer_policy", "reject")
        if (not isinstance(installer_policy, str)
                or installer_policy not in {"reject", "defaults", "verbatim"}):
            raise ServiceError(
                -32602,
                "invalid_params",
                "'installer_policy' must be 'reject', 'defaults', or 'verbatim'.",
                {"parameter": "installer_policy"},
            )
        if installer_policy == "verbatim" and any(
                key in params for key in (
                    "bain_selected", "fomod_selections", "bundle_selected")):
            raise ServiceError(-32602, "invalid_params",
                               "Verbatim installation cannot use installer selections.",
                               {"parameter": "installer_policy"})
        bain_selected = self._bain_selection(params, installer_policy)
        fomod_selections = self._fomod_selection(params, installer_policy)
        bundle_selected = self._bundle_selection(params, installer_policy)
        if params.get("confirm") is not True:
            raise ServiceError(
                -32602,
                "invalid_params",
                "'confirm' must be true because reinstalling replaces the "
                "existing mod folder.",
                {"parameter": "confirm"},
            )

        from .install_options import validate_options
        options = {
            **validate_options(params),
            "mod": mod,
            "installer_policy": installer_policy,
            "bain_selected": bain_selected,
            "fomod_selections": fomod_selections,
            "bundle_selected": bundle_selected,
            "if_exists": "replace",
        }
        if archive_id is not None:
            options["archive_id"] = archive_id
        job = self._jobs.submit(
            "mods.reinstall",
            game,
            self._canonical_game(game),
            profile_dir.name,
            options,
            installer_requests=self._installer_requests(params),
        )
        return {"job": job}

    def _start_list_plugins(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(params, {"game", "profile"}, {"game", "profile"})
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_no_managed_launch(game)
        self._require_configured(game)
        if not (self._safe_attr(game, "plugin_extensions", ()) or ()):
            self._unsupported_operation(game, "plugin management")
        profile_dir = self._resolve_profile(
            game, self._required_text(params, "profile"))

        from .plugins import inventory_runner
        job = self._jobs.submit(
            "plugins.list",
            game,
            self._canonical_game(game),
            profile_dir.name,
            runner=inventory_runner(
                game, profile_dir, "headless:plugins.list"),
        )
        return {"job": job}

    def _start_set_plugins_enabled(
        self, params: dict[str, Any],
    ) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(
            params, {"game", "profile", "changes"},
            {"game", "profile", "changes"})
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_no_managed_launch(game)
        self._require_configured(game)
        if not (self._safe_attr(game, "plugin_extensions", ()) or ()):
            self._unsupported_operation(game, "plugin management")
        profile_dir = self._resolve_profile(
            game, self._required_text(params, "profile"))

        raw_changes = params.get("changes")
        if (not isinstance(raw_changes, list) or not raw_changes
                or len(raw_changes) > 4096):
            raise ServiceError(
                -32602,
                "invalid_params",
                "'changes' must be an array of 1 to 4096 plugin changes.",
                {"parameter": "changes"},
            )
        changes: list[tuple[str, bool]] = []
        seen: set[str] = set()
        for index, change in enumerate(raw_changes):
            parameter = f"changes[{index}]"
            if not isinstance(change, dict) or set(change) != {"name", "enabled"}:
                raise ServiceError(
                    -32602,
                    "invalid_params",
                    f"'{parameter}' must contain only 'name' and 'enabled'.",
                    {"parameter": parameter},
                )
            name = self._plugin_name(change.get("name"), f"{parameter}.name")
            folded = name.casefold()
            if folded in seen:
                raise ServiceError(
                    -32602,
                    "invalid_params",
                    f"'{parameter}.name' duplicates an earlier plugin.",
                    {"parameter": f"{parameter}.name", "plugin": name},
                )
            enabled = change.get("enabled")
            if not isinstance(enabled, bool):
                raise ServiceError(
                    -32602,
                    "invalid_params",
                    f"'{parameter}.enabled' must be a boolean.",
                    {"parameter": f"{parameter}.enabled"},
                )
            seen.add(folded)
            changes.append((name, enabled))

        from .plugins import set_enabled_runner
        job = self._jobs.submit(
            "plugins.set_enabled",
            game,
            self._canonical_game(game),
            profile_dir.name,
            {"changes": [
                {"name": name, "enabled": enabled}
                for name, enabled in changes
            ]},
            runner=set_enabled_runner(
                game,
                profile_dir,
                changes,
                "headless:plugins.set_enabled",
            ),
        )
        return {"job": job}

    def _start_move_plugins(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(
            params, {"game", "profile", "plugins", "position"},
            {"game", "profile", "plugins", "position"})
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_no_managed_launch(game)
        self._require_configured(game)
        if not (self._safe_attr(game, "plugin_extensions", ()) or ()):
            self._unsupported_operation(game, "plugin management")
        profile_dir = self._resolve_profile(
            game, self._required_text(params, "profile"))

        raw_plugins = params.get("plugins")
        if (not isinstance(raw_plugins, list) or not raw_plugins
                or len(raw_plugins) > 4096):
            raise ServiceError(
                -32602,
                "invalid_params",
                "'plugins' must be an array of 1 to 4096 plugin filenames.",
                {"parameter": "plugins"},
            )
        plugins: list[str] = []
        seen: set[str] = set()
        for index, value in enumerate(raw_plugins):
            parameter = f"plugins[{index}]"
            name = self._plugin_name(value, parameter)
            folded = name.casefold()
            if folded in seen:
                raise ServiceError(
                    -32602,
                    "invalid_params",
                    f"'{parameter}' duplicates an earlier plugin.",
                    {"parameter": parameter, "plugin": name},
                )
            seen.add(folded)
            plugins.append(name)

        position = params.get("position")
        if (not isinstance(position, dict) or len(position) != 1
                or next(iter(position), None) not in {"before", "after"}):
            raise ServiceError(
                -32602,
                "invalid_params",
                "'position' must contain exactly one 'before' or 'after' anchor.",
                {"parameter": "position"},
            )
        placement, raw_anchor = next(iter(position.items()))
        anchor = self._plugin_name(raw_anchor, f"position.{placement}")
        if anchor.casefold() in seen:
            raise ServiceError(
                -32602,
                "invalid_params",
                "The move anchor cannot also be a moved plugin.",
                {"parameter": f"position.{placement}", "anchor": anchor},
            )

        from .plugins import move_runner
        job = self._jobs.submit(
            "plugins.move",
            game,
            self._canonical_game(game),
            profile_dir.name,
            {
                "plugins": plugins,
                "position": {placement: anchor},
            },
            runner=move_runner(
                game,
                profile_dir,
                plugins,
                anchor,
                placement == "after",
                "headless:plugins.move",
            ),
        )
        return {"job": job}

    def _plugin_userlist_profile(self, params):
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_configured(game)
        if (not self._safe_attr(game, "plugin_extensions", ())
                or not self._safe_attr(game, "loot_sort_enabled", False)):
            self._unsupported_operation(game, "LOOT user rules")
        profile = self._resolve_profile(game, self._required_text(params, "profile"))
        return game, profile

    def _get_plugin_userlist(self, params):
        from .jobs import JobError
        from .plugin_userlist import get_userlist

        self._require_idle_state()
        self._check_params(params, {"game", "profile"}, {"game", "profile"})
        game, profile = self._plugin_userlist_profile(params)
        try:
            return {"game": self._canonical_game(game), "userlist": get_userlist(game, profile)}
        except JobError as exc:
            raise ServiceError(-32003, exc.name, str(exc), exc.details) from exc

    def _start_edit_plugin_userlist(self, params):
        from .plugin_userlist import ACTIONS, edit_runner

        self._require_idle_state()
        action = params.get("action")
        if not isinstance(action, str) or action not in ACTIONS:
            raise ServiceError(-32602, "invalid_params", "'action' is not a userlist operation.",
                               {"parameter": "action", "supported": list(ACTIONS)})
        required, optional = ACTIONS[action]
        common = {"game", "profile", "revision", "action"}
        self._check_params(params, common | required | optional, common | required)
        game, profile = self._plugin_userlist_profile(params)
        self._require_no_managed_launch(game)
        revision = self._required_text(params, "revision")
        if len(revision) != 64 or any(char not in "0123456789abcdef" for char in revision):
            raise ServiceError(-32602, "invalid_params",
                               "'revision' must come from plugins.userlist.get.",
                               {"parameter": "revision"})

        def group_name(value, parameter):
            if (not isinstance(value, str) or not value or value != value.strip()
                    or len(value) > 255 or any(ord(char) < 32 or 0xd800 <= ord(char) <= 0xdfff
                                               for char in value)):
                raise ServiceError(-32602, "invalid_params", "A LOOT group needs a valid name.",
                                   {"parameter": parameter})
            return value

        def plugin_name(value, parameter):
            name = self._plugin_name(value, parameter)
            extensions = self._safe_attr(game, "plugin_extensions", ())
            if (any(char in name for char in '<>:"|?*')
                    or any(ord(char) < 32 or 0xd800 <= ord(char) <= 0xdfff for char in name)
                    or Path(name).suffix.lower() not in {item.lower() for item in extensions}):
                raise ServiceError(-32602, "invalid_params", "A rule needs an exact plugin filename.",
                                   {"parameter": parameter})
            return name

        def names(value, parameter, validate, *, empty=False, case_sensitive=False):
            if not isinstance(value, list) or not (0 if empty else 1) <= len(value) <= 4096:
                raise ServiceError(-32602, "invalid_params",
                                   "A name list must contain at most 4096 entries.",
                                   {"parameter": parameter})
            result = [validate(item, f"{parameter}[{index}]") for index, item in enumerate(value)]
            identities = result if case_sensitive else [item.casefold() for item in result]
            if len(set(identities)) != len(identities):
                raise ServiceError(-32602, "invalid_params", "A name list cannot contain duplicates.",
                                   {"parameter": parameter})
            return result

        options = {"action": action}
        if "plugin" in required:
            options["plugin"] = plugin_name(params["plugin"], "plugin")
        if "plugins" in required:
            options["plugins"] = names(params["plugins"], "plugins", plugin_name)
        if "group" in required:
            options["group"] = group_name(params["group"], "group")
        if action == "set_rules":
            for field in ("after", "before"):
                options[field] = names(params[field], field, plugin_name, empty=True)
            if options["plugin"].casefold() in {
                    item.casefold() for field in ("after", "before") for item in options[field]}:
                raise ServiceError(-32602, "invalid_params", "A plugin cannot order itself.")
        if action == "save_group":
            options["after"] = names(params["after"], "after", group_name,
                                     empty=True, case_sensitive=True)
            if options["group"] in options["after"]:
                raise ServiceError(-32602, "invalid_params", "A group cannot load after itself.")
            if "description" in params:
                value = params["description"]
                if (not isinstance(value, str) or len(value) > 4096 or "\x00" in value
                        or any(0xd800 <= ord(char) <= 0xdfff for char in value)):
                    raise ServiceError(-32602, "invalid_params", "'description' must be text of at most 4096 characters.",
                                       {"parameter": "description"})
                options["description"] = value
        if action == "remove_group" and options["group"] == "default":
            raise ServiceError(-32602, "invalid_params", "The default group cannot be removed.",
                               {"parameter": "group"})
        if action == "flip_rule":
            if params["field"] not in ("before", "after"):
                raise ServiceError(-32602, "invalid_params", "'field' must be before or after.",
                                   {"parameter": "field"})
            options["field"] = params["field"]
            options["target"] = plugin_name(params["target"], "target")
        if action in ("clear", "reset") and params["confirm"] is not True:
            raise ServiceError(-32602, "invalid_params",
                               "'confirm' must be true to clear or reset user metadata.",
                               {"parameter": "confirm"})
        job = self._jobs.submit(
            "plugins.userlist.edit", game, self._canonical_game(game), profile.name,
            {"revision": revision, **options},
            runner=edit_runner(game, profile, revision, options))
        return {"job": job}

    def _start_sort_plugins(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(
            params, {"game", "profile", "apply_order"}, {"game", "profile"})
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_no_managed_launch(game)
        self._require_configured(game)
        if not (self._safe_attr(game, "plugin_extensions", ()) or ()):
            self._unsupported_operation(game, "plugin management")
        if not bool(self._safe_attr(game, "loot_sort_enabled", False)):
            self._unsupported_operation(game, "LOOT plugin sorting")
        if not self._loot_available():
            try:
                from LOOT.loot_sorter import unavailable_reason
                reason = unavailable_reason()
            except Exception:
                reason = None
            raise ServiceError(
                -32009,
                "loot_unavailable",
                "The LOOT library is not available in this installation.",
                {"reason": reason} if reason else {},
            )
        profile_dir = self._resolve_profile(
            game, self._required_text(params, "profile"))
        apply_order = params.get("apply_order", True)
        if not isinstance(apply_order, bool):
            raise ServiceError(
                -32602,
                "invalid_params",
                "'apply_order' must be a boolean.",
                {"parameter": "apply_order"},
            )

        from .plugins import sort_runner
        job = self._jobs.submit(
            "plugins.sort",
            game,
            self._canonical_game(game),
            profile_dir.name,
            {"apply_order": apply_order},
            runner=sort_runner(
                game,
                profile_dir,
                apply_order,
                "headless:plugins.sort",
            ),
        )
        return {"job": job}

    def _start_list_boundary(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(
            params,
            {"game", "profile", "boundary", "offset", "limit"},
            {"game", "profile", "boundary"},
        )
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_no_managed_launch(game)
        self._require_configured(game)
        profile_dir = self._resolve_profile(
            game, self._required_text(params, "profile"))
        boundary = params.get("boundary")
        if boundary not in {"overwrite", "root_folder"}:
            raise ServiceError(
                -32602,
                "invalid_params",
                "'boundary' must be 'overwrite' or 'root_folder'.",
                {"parameter": "boundary"},
            )
        offset = self._integer_range(
            params.get("offset", 0), "offset", 0, 2_147_483_647)
        limit = self._integer_range(
            params.get("limit", 500), "limit", 1, 5000)

        from .boundaries import inventory_runner
        job = self._jobs.submit(
            "boundaries.list",
            game,
            self._canonical_game(game),
            profile_dir.name,
            {"boundary": boundary, "offset": offset, "limit": limit},
            runner=inventory_runner(
                game,
                profile_dir,
                boundary,
                offset,
                limit,
                "headless:boundaries.list",
            ),
        )
        return {"job": job}

    def _start_move_boundary(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(
            params,
            {
                "game", "profile", "boundary", "paths", "revision",
                "target_mod", "create_mod", "confirm",
            },
            {
                "game", "profile", "boundary", "paths", "revision",
                "target_mod", "confirm",
            },
        )
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_no_managed_launch(game)
        self._require_configured(game)
        profile_dir = self._resolve_profile(
            game, self._required_text(params, "profile"))
        boundary = params.get("boundary")
        if boundary not in {"overwrite", "root_folder"}:
            raise ServiceError(
                -32602,
                "invalid_params",
                "'boundary' must be 'overwrite' or 'root_folder'.",
                {"parameter": "boundary"},
            )
        paths = self._boundary_paths(params.get("paths"))
        revision = self._boundary_revision(params.get("revision"))
        target_mod = self._mod_name(
            params.get("target_mod"), "target_mod")
        create_mod = params.get("create_mod", False)
        if not isinstance(create_mod, bool):
            raise ServiceError(
                -32602,
                "invalid_params",
                "'create_mod' must be a boolean.",
                {"parameter": "create_mod"},
            )
        if params.get("confirm") is not True:
            raise ServiceError(
                -32602,
                "invalid_params",
                "'confirm' must be true because moving boundary files may "
                "replace files in the target mod.",
                {"parameter": "confirm"},
            )

        from .boundaries import move_runner
        options = {
            "boundary": boundary,
            "paths": paths,
            "revision": revision,
            "target_mod": target_mod,
            "create_mod": create_mod,
        }
        job = self._jobs.submit(
            "boundaries.move",
            game,
            self._canonical_game(game),
            profile_dir.name,
            options,
            runner=move_runner(
                game,
                profile_dir,
                boundary,
                paths,
                revision,
                target_mod,
                create_mod,
                "headless:boundaries.move",
            ),
        )
        return {"job": job}

    def _start_remove_boundary(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(
            params,
            {"game", "profile", "boundary", "paths", "revision", "confirm"},
            {"game", "profile", "boundary", "paths", "revision", "confirm"},
        )
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_no_managed_launch(game)
        self._require_configured(game)
        profile_dir = self._resolve_profile(
            game, self._required_text(params, "profile"))
        boundary = params.get("boundary")
        if boundary not in {"overwrite", "root_folder"}:
            raise ServiceError(
                -32602,
                "invalid_params",
                "'boundary' must be 'overwrite' or 'root_folder'.",
                {"parameter": "boundary"},
            )
        paths = self._boundary_paths(params.get("paths"))
        revision = self._boundary_revision(params.get("revision"))
        if params.get("confirm") is not True:
            raise ServiceError(
                -32602,
                "invalid_params",
                "'confirm' must be true because boundary removal is irreversible.",
                {"parameter": "confirm"},
            )

        from .boundaries import remove_runner
        options = {
            "boundary": boundary,
            "paths": paths,
            "revision": revision,
        }
        job = self._jobs.submit(
            "boundaries.remove",
            game,
            self._canonical_game(game),
            profile_dir.name,
            options,
            runner=remove_runner(
                game,
                profile_dir,
                boundary,
                paths,
                revision,
                "headless:boundaries.remove",
            ),
        )
        return {"job": job}

    def _save_context(self, params: dict[str, Any]) -> tuple[Any, Path]:
        self._require_idle_state()
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_no_managed_launch(game)
        self._require_configured(game)
        profile = self._resolve_profile(
            game, self._required_text(params, "profile"))
        return game, profile

    @staticmethod
    def _save_revision(value: Any) -> str:
        if (not isinstance(value, str) or len(value) != 64
                or any(char not in "0123456789abcdef" for char in value)):
            raise ServiceError(
                -32602, "invalid_params",
                "'revision' must be a save entry revision from saves.entries.start.",
                {"parameter": "revision"},
            )
        return value

    @staticmethod
    def _save_file_path(params: dict[str, Any], name: str) -> Path:
        value = AmethystService._required_text(params, name)
        path = Path(value).expanduser()
        if not path.is_absolute():
            raise ServiceError(
                -32602, "invalid_params",
                f"'{name}' must be an absolute file path.",
                {"parameter": name},
            )
        return path

    def _start_save_locations(self, params: dict[str, Any]) -> dict[str, Any]:
        self._check_params(params, {"game", "profile"}, {"game", "profile"})
        game, profile = self._save_context(params)
        from .saves import list_locations
        job = self._jobs.submit(
            "saves.locations", game, self._canonical_game(game), profile.name,
            runner=lambda _log, _progress: list_locations(game, profile),
        )
        return {"job": job}

    def _start_save_entries(self, params: dict[str, Any]) -> dict[str, Any]:
        self._check_params(
            params, {"game", "profile", "location", "directory", "offset", "limit"},
            {"game", "profile", "location"})
        game, profile = self._save_context(params)
        location = self._required_text(params, "location")
        directory = params.get("directory", "")
        if not isinstance(directory, str):
            raise ServiceError(-32602, "invalid_params",
                               "'directory' must be a relative path string.",
                               {"parameter": "directory"})
        offset = self._integer_range(
            params.get("offset", 0), "offset", 0, 2_147_483_647)
        limit = self._integer_range(params.get("limit", 500), "limit", 1, 500)
        from .saves import list_entries
        job = self._jobs.submit(
            "saves.entries", game, self._canonical_game(game), profile.name,
            {"location": location, "directory": directory,
             "offset": offset, "limit": limit},
            runner=lambda _log, _progress: list_entries(
                game, profile, location, directory, offset, limit),
        )
        return {"job": job}

    def _start_save_export(self, params: dict[str, Any]) -> dict[str, Any]:
        self._check_params(
            params, {"game", "profile", "location", "archive_path", "overwrite"},
            {"game", "profile", "location", "archive_path"})
        game, profile = self._save_context(params)
        location = self._required_text(params, "location")
        archive = self._save_file_path(params, "archive_path")
        if archive.suffix.lower() != ".zip":
            raise ServiceError(-32602, "invalid_params",
                               "'archive_path' must end in .zip.",
                               {"parameter": "archive_path"})
        overwrite = params.get("overwrite", False)
        if not isinstance(overwrite, bool):
            raise ServiceError(-32602, "invalid_params",
                               "'overwrite' must be a boolean.",
                               {"parameter": "overwrite"})
        from .saves import export_location
        job = self._jobs.submit(
            "saves.export", game, self._canonical_game(game), profile.name,
            {"location": location, "archive_path": str(archive),
             "overwrite": overwrite},
            runner=lambda log, progress: export_location(
                game, profile, location, archive, overwrite, log, progress),
        )
        return {"job": job}

    def _start_save_import(self, params: dict[str, Any]) -> dict[str, Any]:
        self._check_params(
            params, {"game", "profile", "location", "archive_path", "confirm"},
            {"game", "profile", "location", "archive_path", "confirm"})
        game, profile = self._save_context(params)
        location = self._required_text(params, "location")
        archive = self._save_file_path(params, "archive_path")
        if params.get("confirm") is not True:
            raise ServiceError(-32602, "invalid_params",
                               "'confirm' must be true to import saves.",
                               {"parameter": "confirm"})
        from .saves import import_location
        job = self._jobs.submit(
            "saves.import", game, self._canonical_game(game), profile.name,
            {"location": location, "archive_path": str(archive)},
            runner=lambda log, progress: import_location(
                game, profile, location, archive, log, progress),
        )
        return {"job": job}

    def _start_save_transfer(self, params: dict[str, Any]) -> dict[str, Any]:
        self._check_params(
            params, {"game", "profile", "location", "entry", "revision",
                     "target_profile", "move", "overwrite", "confirm"},
            {"game", "profile", "location", "entry", "revision",
             "target_profile"})
        game, profile = self._save_context(params)
        location = self._required_text(params, "location")
        entry = self._required_text(params, "entry")
        revision = self._save_revision(params.get("revision"))
        target_profile = self._resolve_profile(
            game, self._required_text(params, "target_profile"))
        if target_profile.is_symlink():
            raise ServiceError(-32602, "invalid_params",
                               "A symlinked target profile is unsafe.",
                               {"parameter": "target_profile"})
        move = params.get("move", False)
        overwrite = params.get("overwrite", False)
        if not isinstance(move, bool) or not isinstance(overwrite, bool):
            raise ServiceError(-32602, "invalid_params",
                               "'move' and 'overwrite' must be booleans.")
        if (move or overwrite) and params.get("confirm") is not True:
            raise ServiceError(-32602, "invalid_params",
                               "'confirm' must be true to move or replace a save.",
                               {"parameter": "confirm"})
        from .saves import transfer_entry
        job = self._jobs.submit(
            "saves.transfer", game, self._canonical_game(game), profile.name,
            {"location": location, "entry": entry, "revision": revision,
             "target_profile": target_profile.name, "move": move,
             "overwrite": overwrite},
            runner=lambda log, progress: transfer_entry(
                game, profile, location, entry, revision, target_profile,
                move, overwrite, log, progress),
        )
        return {"job": job}

    def _start_save_remove(self, params: dict[str, Any]) -> dict[str, Any]:
        self._check_params(
            params, {"game", "profile", "location", "entry", "revision", "confirm"},
            {"game", "profile", "location", "entry", "revision", "confirm"})
        game, profile = self._save_context(params)
        location = self._required_text(params, "location")
        entry = self._required_text(params, "entry")
        revision = self._save_revision(params.get("revision"))
        if params.get("confirm") is not True:
            raise ServiceError(-32602, "invalid_params",
                               "'confirm' must be true to delete a save.",
                               {"parameter": "confirm"})
        from .saves import remove_entry
        job = self._jobs.submit(
            "saves.remove", game, self._canonical_game(game), profile.name,
            {"location": location, "entry": entry, "revision": revision},
            runner=lambda log, _progress: remove_entry(
                game, profile, location, entry, revision, log),
        )
        return {"job": job}

    def _start_deploy(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(
            params, {"game", "profile", "options"}, {"game", "profile"})
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_no_managed_launch(game)
        self._require_configured(game)
        profile_dir = self._resolve_profile(
            game, self._required_text(params, "profile"))
        if not callable(self._safe_attr(game, "deploy", None)):
            self._unsupported_operation(game, "deploy")

        options = params.get("options", {})
        if not isinstance(options, dict):
            raise ServiceError(
                -32602,
                "invalid_params",
                "'options' must be a JSON object.",
                {"parameter": "options"},
            )
        self._check_params(
            options, {"root_folder_enabled", "backup"})
        normalized = {
            "root_folder_enabled": options.get("root_folder_enabled", True),
            "backup": options.get("backup", True),
        }
        for name, value in normalized.items():
            if not isinstance(value, bool):
                raise ServiceError(
                    -32602,
                    "invalid_params",
                    f"'options.{name}' must be a boolean.",
                    {"parameter": f"options.{name}"},
                )
        job = self._jobs.submit(
            "deploy",
            game,
            self._canonical_game(game),
            profile_dir.name,
            normalized,
        )
        return {"job": job}

    def _plan_mod_rename(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(
            params, {"game", "profile", "mod", "new_name"},
            {"game", "profile", "mod", "new_name"})
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_configured(game)
        profile = self._resolve_profile(
            game, self._required_text(params, "profile"))
        mod = self._mod_name(params.get("mod"), "mod")
        new_name = self._mod_name(params.get("new_name"), "new_name")
        from .mod_rename import _public, plan_mod_rename
        from Utils.mods.state import ModStateError
        try:
            return {"plan": _public(plan_mod_rename(
                game, profile, mod, new_name))}
        except ModStateError as exc:
            raise ServiceError(
                -32003, exc.name, str(exc), exc.details,
            ) from exc

    def _start_rename_mod(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        required = {"game", "profile", "mod", "new_name",
                    "plan_revision", "confirm"}
        self._check_params(params, required, required)
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_no_managed_launch(game)
        self._require_configured(game)
        profile = self._resolve_profile(
            game, self._required_text(params, "profile"))
        mod = self._mod_name(params.get("mod"), "mod")
        new_name = self._mod_name(params.get("new_name"), "new_name")
        revision = self._required_text(params, "plan_revision")
        if (len(revision) != 64 or any(
                char not in "0123456789abcdef" for char in revision)):
            raise ServiceError(
                -32602, "invalid_params",
                "'plan_revision' must be a SHA-256 hex digest.",
                {"parameter": "plan_revision"},
            )
        if params.get("confirm") is not True:
            raise ServiceError(
                -32602, "invalid_params",
                "'confirm' must be true to rename a staged mod.",
                {"parameter": "confirm"},
            )
        job = self._jobs.submit(
            "mods.rename", game, self._canonical_game(game),
            profile.name,
            {"mod": mod, "new_name": new_name, "plan_revision": revision},
        )
        return {"job": job}

    def _mod_copy_options(
        self, params: dict[str, Any],
    ) -> tuple[Any, Path, Path, list[str], str | None, dict[str, str], str]:
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_no_managed_launch(game)
        self._require_configured(game)
        source = self._resolve_profile(
            game, self._required_text(params, "profile"))
        target = self._resolve_profile(
            game, self._required_text(params, "target_profile"))
        raw_mods = params["mods"]
        if (not isinstance(raw_mods, list) or len(raw_mods) > 64
                or any(not isinstance(item, str) for item in raw_mods)):
            raise ServiceError(-32602, "invalid_params",
                               "'mods' must contain at most 64 mod names.",
                               {"parameter": "mods"})
        mods = [self._mod_name(item, "mods") for item in raw_mods]
        if len(set(mods)) != len(mods):
            raise ServiceError(-32602, "invalid_params",
                               "'mods' must be distinct.",
                               {"parameter": "mods"})
        separator = params.get("separator")
        if separator is not None:
            separator = self._mod_name(separator, "separator")
            if not separator.endswith("_separator"):
                raise ServiceError(-32602, "invalid_params",
                                   "'separator' must be a real separator name.",
                                   {"parameter": "separator"})
        if not mods and separator is None:
            raise ServiceError(-32602, "invalid_params",
                               "Select mods or a separator.",
                               {"parameter": "mods"})
        raw_renames = params.get("renames", {})
        if not isinstance(raw_renames, dict) or len(raw_renames) > 64:
            raise ServiceError(-32602, "invalid_params",
                               "'renames' must be an object of mod names.",
                               {"parameter": "renames"})
        renames = {self._mod_name(name, "renames"):
                   self._mod_name(value, f"renames.{name}")
                   for name, value in raw_renames.items()}
        if_exists = params.get("if_exists", "fail")
        if if_exists not in ("fail", "skip", "replace"):
            raise ServiceError(-32602, "invalid_params",
                               "'if_exists' must be fail, skip, or replace.",
                               {"parameter": "if_exists"})
        return game, source, target, mods, separator, renames, if_exists

    def _plan_mod_copy(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(
            params, {"game", "profile", "target_profile", "mods",
                     "separator", "renames", "if_exists"},
            {"game", "profile", "target_profile", "mods"})
        options = self._mod_copy_options(params)
        from .jobs import JobError
        from .mod_copy import plan_copy
        try:
            return {"plan": plan_copy(*options)}
        except JobError as exc:
            raise ServiceError(-32003, exc.name, str(exc), exc.details) from exc

    def _start_copy_mods(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(
            params, {"game", "profile", "target_profile", "mods",
                     "separator", "renames", "if_exists", "plan_revision",
                     "confirm"},
            {"game", "profile", "target_profile", "mods",
             "plan_revision", "confirm"})
        if params["confirm"] is not True:
            raise ServiceError(-32602, "confirmation_required",
                               "Confirm the mod copy plan before copying files.",
                               {"parameter": "confirm"})
        revision = self._required_text(params, "plan_revision")
        if (len(revision) != 64 or any(
                char not in "0123456789abcdef" for char in revision)):
            raise ServiceError(-32602, "invalid_params",
                               "'plan_revision' must be a SHA-256 hex digest.",
                               {"parameter": "plan_revision"})
        game, source, target, mods, separator, renames, if_exists = (
            self._mod_copy_options(params))
        from .mod_copy import copy_runner
        job = self._jobs.submit(
            "mods.copy", game, self._canonical_game(game), source.name,
            {"target_profile": target.name, "mods": mods,
             "separator": separator, "renames": renames,
             "if_exists": if_exists, "plan_revision": revision},
            runner=copy_runner(game, source, target, mods, separator,
                               renames, if_exists, revision),
        )
        return {"job": job}

    def _plan_mod_move_profile(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(
            params, {"game", "profile", "target_profile", "mods",
                     "separator", "renames", "if_exists"},
            {"game", "profile", "target_profile", "mods"})
        options = self._mod_copy_options(params)
        from .jobs import JobError
        from .mod_copy import plan_move
        try:
            return {"plan": plan_move(*options)}
        except JobError as exc:
            raise ServiceError(-32003, exc.name, str(exc), exc.details) from exc

    def _start_move_mods_to_profile(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(
            params, {"game", "profile", "target_profile", "mods",
                     "separator", "renames", "if_exists", "plan_revision",
                     "confirm"},
            {"game", "profile", "target_profile", "mods",
             "plan_revision", "confirm"})
        if params["confirm"] is not True:
            raise ServiceError(-32602, "confirmation_required",
                               "Confirm the mod move plan before moving files.",
                               {"parameter": "confirm"})
        revision = self._required_text(params, "plan_revision")
        if (len(revision) != 64 or any(
                char not in "0123456789abcdef" for char in revision)):
            raise ServiceError(-32602, "invalid_params",
                               "'plan_revision' must be a SHA-256 hex digest.",
                               {"parameter": "plan_revision"})
        game, source, target, mods, separator, renames, if_exists = (
            self._mod_copy_options(params))
        from .mod_copy import copy_runner
        job = self._jobs.submit(
            "mods.move_profile", game, self._canonical_game(game), source.name,
            {"target_profile": target.name, "mods": mods,
             "separator": separator, "renames": renames,
             "if_exists": if_exists, "plan_revision": revision},
            runner=copy_runner(game, source, target, mods, separator,
                               renames, if_exists, revision, move=True),
        )
        return {"job": job}

    def _start_set_enabled(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(
            params, {"game", "profile", "changes"},
            {"game", "profile", "changes"})
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_no_managed_launch(game)
        self._require_configured(game)
        profile_dir = self._resolve_profile(
            game, self._required_text(params, "profile"))

        raw_changes = params.get("changes")
        if not isinstance(raw_changes, list) or not raw_changes:
            raise ServiceError(
                -32602,
                "invalid_params",
                "'changes' must be a non-empty array.",
                {"parameter": "changes"},
            )
        changes: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, change in enumerate(raw_changes):
            parameter = f"changes[{index}]"
            if not isinstance(change, dict) or set(change) != {"name", "enabled"}:
                raise ServiceError(
                    -32602,
                    "invalid_params",
                    f"'{parameter}' must contain only 'name' and 'enabled'.",
                    {"parameter": parameter},
                )
            name = self._mod_name(
                change.get("name"), f"{parameter}.name")
            if name in seen:
                raise ServiceError(
                    -32602,
                    "invalid_params",
                    f"'{parameter}.name' duplicates an earlier change.",
                    {"parameter": f"{parameter}.name", "mod": name},
                )
            enabled = change.get("enabled")
            if not isinstance(enabled, bool):
                raise ServiceError(
                    -32602,
                    "invalid_params",
                    f"'{parameter}.enabled' must be a boolean.",
                    {"parameter": f"{parameter}.enabled"},
                )
            seen.add(name)
            changes.append({"name": name, "enabled": enabled})

        job = self._jobs.submit(
            "mods.set_enabled",
            game,
            self._canonical_game(game),
            profile_dir.name,
            {"changes": changes},
        )
        return {"job": job}

    def _start_move(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(
            params, {"game", "profile", "mods", "position"},
            {"game", "profile", "mods", "position"})
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_no_managed_launch(game)
        self._require_configured(game)
        profile_dir = self._resolve_profile(
            game, self._required_text(params, "profile"))

        raw_mods = params.get("mods")
        if not isinstance(raw_mods, list) or not raw_mods:
            raise ServiceError(
                -32602,
                "invalid_params",
                "'mods' must be a non-empty array of mod names.",
                {"parameter": "mods"},
            )
        mods: list[str] = []
        seen: set[str] = set()
        for index, value in enumerate(raw_mods):
            parameter = f"mods[{index}]"
            name = self._mod_name(value, parameter)
            if name in seen:
                raise ServiceError(
                    -32602,
                    "invalid_params",
                    f"'{parameter}' duplicates an earlier mod name.",
                    {"parameter": parameter, "mod": name},
                )
            seen.add(name)
            mods.append(name)

        position = params.get("position")
        if (not isinstance(position, dict) or len(position) != 1
                or next(iter(position), None) not in {"before", "after"}):
            raise ServiceError(
                -32602,
                "invalid_params",
                "'position' must contain exactly one 'before' or 'after' anchor.",
                {"parameter": "position"},
            )
        placement, raw_anchor = next(iter(position.items()))
        anchor = self._mod_name(raw_anchor, f"position.{placement}")
        if anchor in seen:
            raise ServiceError(
                -32602,
                "invalid_params",
                "The move anchor cannot also be a moved mod.",
                {"parameter": f"position.{placement}", "anchor": anchor},
            )

        job = self._jobs.submit(
            "mods.move",
            game,
            self._canonical_game(game),
            profile_dir.name,
            {"mods": mods, "position": {placement: anchor}},
        )
        return {"job": job}

    def _start_remove(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(
            params, {"game", "profile", "mods", "confirm"},
            {"game", "profile", "mods", "confirm"})
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_no_managed_launch(game)
        self._require_configured(game)
        profile_dir = self._resolve_profile(
            game, self._required_text(params, "profile"))

        raw_mods = params.get("mods")
        if not isinstance(raw_mods, list) or not raw_mods:
            raise ServiceError(
                -32602,
                "invalid_params",
                "'mods' must be a non-empty array of mod names.",
                {"parameter": "mods"},
            )
        mods: list[str] = []
        seen: set[str] = set()
        for index, value in enumerate(raw_mods):
            parameter = f"mods[{index}]"
            name = self._mod_name(value, parameter)
            if name in seen:
                raise ServiceError(
                    -32602,
                    "invalid_params",
                    f"'{parameter}' duplicates an earlier mod name.",
                    {"parameter": parameter, "mod": name},
                )
            seen.add(name)
            mods.append(name)

        if params.get("confirm") is not True:
            raise ServiceError(
                -32602,
                "invalid_params",
                "'confirm' must be true because mod removal is irreversible.",
                {"parameter": "confirm"},
            )

        job = self._jobs.submit(
            "mods.remove",
            game,
            self._canonical_game(game),
            profile_dir.name,
            {"mods": mods},
        )
        return {"job": job}

    def _start_restore(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(params, {"game", "profile"}, {"game", "profile"})
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_no_managed_launch(game)
        self._require_configured(game)
        profile_dir = self._resolve_profile(
            game, self._required_text(params, "profile"))
        if not callable(self._safe_attr(game, "restore", None)):
            self._unsupported_operation(game, "restore")
        job = self._jobs.submit(
            "restore",
            game,
            self._canonical_game(game),
            profile_dir.name,
        )
        return {"job": job}

    def _get_launch(self, params: dict[str, Any]) -> dict[str, Any]:
        self._check_params(params, {"launch_id"})
        launch_id = None
        if "launch_id" in params:
            launch_id = self._required_text(params, "launch_id")
        from .launch import LaunchNotFound
        try:
            return {"launch": self._launches.get(launch_id)}
        except LaunchNotFound as exc:
            raise ServiceError(
                -32014,
                "launch_not_found",
                f"Launch '{launch_id}' was not found.",
                {"launch_id": launch_id},
            ) from exc

    def _list_active_launches(self, params: dict[str, Any]) -> dict[str, Any]:
        self._check_params(params, {"game"})
        from Utils.operations import _lock_dir, _operation_resources
        from Utils.processes.leases import active_launches
        resources = (_operation_resources(self._resolve_game(self._required_text(params, "game")))
                     if "game" in params else None)
        return {"launches": active_launches(_lock_dir(), resources)}

    def _get_wine_overrides(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(params, {"game", "profile"}, {"game", "profile"})
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_configured(game)
        profile = self._resolve_profile(game, self._required_text(params, "profile"))
        from .wine_overrides import get_overrides
        from .jobs import JobError
        try:
            return {"game": self._canonical_game(game),
                    "wine_overrides": get_overrides(game, profile)}
        except JobError as exc:
            raise ServiceError(-32027, exc.name, str(exc), exc.details) from exc

    def _start_set_wine_overrides(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        required = {"game", "profile", "revision"}
        self._check_params(params, required | {"changes", "reset", "apply"}, required)
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_configured(game)
        self._require_no_managed_launch(game)
        profile = self._resolve_profile(game, self._required_text(params, "profile"))
        revision = self._required_text(params, "revision")
        changes = params.get("changes", {})
        reset = params.get("reset", [])
        apply = params.get("apply", True)
        if (not isinstance(changes, dict) or len(changes) > 1024
                or not isinstance(reset, list) or len(reset) > 1024
                or not isinstance(apply, bool)):
            raise ServiceError(-32602, "invalid_params",
                               "Supply a changes object, reset array, and boolean apply flag.")
        from .wine_overrides import set_overrides
        job = self._jobs.submit(
            "wine.dll_overrides.set", game, self._canonical_game(game), profile.name,
            {"revision": revision, "change_count": len(changes),
             "reset_count": len(reset), "apply": apply},
            runner=lambda log, progress: set_overrides(
                game, profile, revision, changes, reset, apply, log))
        return {"job": job}

    def _start_prefix_health(self, params, repair):
        import threading
        from .prefix_health import repair_runner, report_runner

        self._require_idle_state()
        required = {"game", "profile"}
        fields = required | {"check_proton"}
        if repair:
            fields |= {"revision", "checks", "all"}
            required.add("revision")
        self._check_params(params, fields, required)
        game = self._resolve_game(self._required_text(params, "game"))
        if repair:
            self._require_configured(game)
            self._require_no_managed_launch(game)
        profile = self._resolve_profile(game, self._required_text(params, "profile"))
        check_proton = params.get("check_proton", True)
        if not isinstance(check_proton, bool):
            raise ServiceError(-32602, "invalid_params", "'check_proton' must be boolean.")
        stop = threading.Event()
        options = {"check_proton": check_proton}
        if repair:
            revision = self._required_text(params, "revision")
            checks, all_checks = params.get("checks", []), params.get("all", False)
            if (len(revision) != 64 or any(char not in "0123456789abcdef" for char in revision)
                    or not isinstance(all_checks, bool) or not isinstance(checks, list)
                    or len(checks) > 128 or any(not isinstance(name, str) or not name or len(name) > 255 for name in checks)
                    or len(set(checks)) != len(checks) or (all_checks and "checks" in params)
                    or (not all_checks and not checks)):
                raise ServiceError(-32602, "invalid_params", "Supply a preview revision and either distinct check IDs or all:true.")
            options.update(revision=revision, checks=checks, all=all_checks)
            runner = repair_runner(game, profile, check_proton, stop, revision, checks, all_checks)
        else:
            runner = report_runner(game, profile, check_proton, stop)
        kind = "wine.prefix.repair" if repair else "wine.prefix.health"
        job = self._jobs.submit(kind, game, self._canonical_game(game), profile.name,
                                options, runner=runner, cancel=stop.set)
        return {"job": job}

    def _start_wine_dependencies(self, params, install):
        import threading
        from .wine_dependencies import install_runner, report_runner

        self._require_idle_state()
        required = {"game", "profile"}
        if install:
            required |= {"revision", "dependencies", "confirm"}
        self._check_params(params, required, required)
        game = self._resolve_game(self._required_text(params, "game"))
        if install:
            self._require_configured(game)
            self._require_no_managed_launch(game)
        profile = self._resolve_profile(game, self._required_text(params, "profile"))
        stop = threading.Event()
        options = {}
        if install:
            revision = self._required_text(params, "revision")
            dependencies = params["dependencies"]
            if (len(revision) != 64 or any(char not in "0123456789abcdef" for char in revision)
                    or params["confirm"] is not True or not isinstance(dependencies, list)
                    or not 1 <= len(dependencies) <= 128
                    or any(not isinstance(token, str) or not token or len(token) > 255 for token in dependencies)
                    or len(set(dependencies)) != len(dependencies)):
                raise ServiceError(-32602, "invalid_params", "Supply the report revision, distinct dependency IDs, and confirm:true.")
            options.update(revision=revision, dependencies=dependencies, confirm=True)
            runner = install_runner(game, profile, stop, revision, dependencies)
        else:
            runner = report_runner(game, profile, stop)
        kind = "wine.dependencies.install" if install else "wine.dependencies.list"
        job = self._jobs.submit(kind, game, self._canonical_game(game), profile.name,
                                options, runner=runner, cancel=stop.set)
        return {"job": job}

    def _start_game_registry(self, params, write):
        import threading
        from .game_registry import inspect_runner, write_runner

        self._require_idle_state()
        required = {"game", "profile"}
        if write:
            required |= {"revision", "confirm"}
        self._check_params(params, required | ({"force"} if write else set()), required)
        game = self._resolve_game(self._required_text(params, "game"))
        if write:
            self._require_configured(game)
            self._require_no_managed_launch(game)
        profile = self._resolve_profile(game, self._required_text(params, "profile"))
        stop = threading.Event()
        options = {}
        if write:
            revision = self._required_text(params, "revision")
            force = params.get("force", True)
            if (len(revision) != 64 or any(char not in "0123456789abcdef" for char in revision)
                    or params["confirm"] is not True or not isinstance(force, bool)):
                raise ServiceError(-32602, "invalid_params", "Supply the inspection revision, confirm:true and a boolean force flag.")
            options.update(revision=revision, confirm=True, force=force)
            runner = write_runner(game, profile, revision, force, stop)
        else:
            runner = inspect_runner(game, profile, stop)
        kind = "wine.game_registry.write" if write else "wine.game_registry.inspect"
        job = self._jobs.submit(kind, game, self._canonical_game(game), profile.name,
                                options, runner=runner, cancel=stop.set)
        return {"job": job}

    def _get_launch_settings(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(params, {"game", "profile"}, {"game", "profile"})
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_configured(game)
        profile = self._resolve_profile(game, self._required_text(params, "profile"))
        from .launch_settings import get_settings
        from .jobs import JobError
        try:
            return {"game": self._canonical_game(game),
                    "launch_settings": get_settings(game, profile)}
        except JobError as exc:
            raise ServiceError(-32027, exc.name, str(exc), exc.details) from exc

    def _start_set_launch_settings(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        fields = {"game", "profile", "revision", "changes"}
        self._check_params(params, fields, fields)
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_configured(game)
        self._require_no_managed_launch(game)
        profile = self._resolve_profile(game, self._required_text(params, "profile"))
        revision = self._required_text(params, "revision")
        changes = params["changes"]
        if not isinstance(changes, dict) or not changes:
            raise ServiceError(-32602, "invalid_params", "'changes' must be a non-empty object.")
        from .launch_settings import set_settings
        job = self._jobs.submit(
            "launch.settings.set", game, self._canonical_game(game), profile.name,
            {"revision": revision, "fields": sorted(changes)},
            runner=lambda log, progress: set_settings(game, profile, revision, changes, log))
        return {"job": job}

    def _start_launch(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(params, {"game", "profile"}, {"game", "profile"})
        game = self._resolve_game(self._required_text(params, "game"))
        self._require_configured(game)
        profile_dir = self._resolve_profile(
            game, self._required_text(params, "profile"))

        from .launch import LaunchInProgress, launch_runner
        try:
            launch = self._launches.reserve(
                self._canonical_game(game), profile_dir.name)
        except LaunchInProgress as exc:
            raise ServiceError(
                -32007,
                "operation_in_progress",
                "A managed game launch is already active.",
                {"launch": self._launch_summary(exc.launch)},
            ) from exc

        launch_id = launch["id"]
        try:
            job = self._jobs.submit(
                "launch",
                game,
                self._canonical_game(game),
                profile_dir.name,
                {
                    "launch_id": launch_id,
                    "requires_deployed_profile": True,
                },
                runner=launch_runner(
                    game, profile_dir, launch_id, self._launches),
            )
        except Exception as exc:
            self._launches.fail(
                launch_id,
                "launch_failed",
                "The launch job could not be queued.",
                {"launch_id": launch_id, "exception": type(exc).__name__},
            )
            raise
        return {"job": job, "launch": launch}

    def _stop_launch(self, params: dict[str, Any]) -> dict[str, Any]:
        self._check_params(
            params, {"launch_id", "confirm"}, {"launch_id", "confirm"})
        launch_id = self._required_text(params, "launch_id")
        if params.get("confirm") is not True:
            raise ServiceError(
                -32602,
                "invalid_params",
                "'confirm' must be true to stop a managed game process.",
                {"parameter": "confirm"},
            )
        from .launch import LaunchNotFound, LaunchNotStoppable
        try:
            return {"launch": self._launches.stop(launch_id)}
        except LaunchNotFound as exc:
            raise ServiceError(
                -32014,
                "launch_not_found",
                f"Launch '{launch_id}' was not found.",
                {"launch_id": launch_id},
            ) from exc
        except LaunchNotStoppable as exc:
            current = self._launches.get(launch_id)
            raise ServiceError(
                -32015,
                "launch_not_stoppable",
                f"Launch '{launch_id}' is not running.",
                {"launch": self._launch_summary(current)},
            ) from exc

    def _get_job(self, params: dict[str, Any]) -> dict[str, Any]:
        self._check_params(params, {"job_id"}, {"job_id"})
        job_id = self._required_text(params, "job_id")
        try:
            return {"job": self._jobs.get(job_id)}
        except KeyError:
            self._job_not_found(job_id)

    def _get_job_history_events(self, params):
        self._check_params(params, {"job_id", "after", "limit"}, {"job_id"})
        job_id = self._required_text(params, "job_id")
        after = self._integer_range(params.get("after", 0), "after", 0, 2147483647)
        limit = self._integer_range(params.get("limit", 500), "limit", 1, 1000)
        try:
            return self._history.events(job_id, after=after, limit=limit)
        except KeyError:
            self._job_not_found(job_id)
        except (OSError, ValueError, TypeError) as exc:
            raise ServiceError(-32003, "job_history_unavailable", "The saved job events could not be read.",
                               {"reason": str(exc)}) from exc

    def _get_job_history(self, params):
        self._check_params(params, {"job_id"}, {"job_id"})
        job_id = self._required_text(params, "job_id")
        try:
            return self._history.get(job_id)
        except KeyError:
            self._job_not_found(job_id)
        except (OSError, ValueError, TypeError) as exc:
            raise ServiceError(-32003, "resource_unavailable", "Could not read the job record.") from exc

    def _list_job_history(self, params, *, recovery=False):
        self._check_params(params, {"game", "offset", "limit"})
        game = self._required_text(params, "game") if "game" in params else None
        offset = self._integer_range(params.get("offset", 0), "offset", 0, 2147483647)
        limit = self._integer_range(params.get("limit", 100), "limit", 1, 500)
        result = self._history.list(game=game, recovery_only=recovery)
        records = result.pop("records")
        return {**result, "records": records[offset:offset + limit], "total": len(records),
                "offset": offset, "limit": limit,
                "next_offset": offset + limit if offset + limit < len(records) else None}

    def _list_jobs(self, params: dict[str, Any]) -> dict[str, Any]:
        self._check_params(params, {"active_only"})
        active_only = params.get("active_only", False)
        if not isinstance(active_only, bool):
            raise ServiceError(
                -32602,
                "invalid_params",
                "'active_only' must be a boolean.",
                {"parameter": "active_only"},
            )
        return {"jobs": self._jobs.list(active_only=active_only)}

    def _job_events(self, params: dict[str, Any]) -> dict[str, Any]:
        self._check_params(
            params, {"job_id", "after", "wait_ms"}, {"job_id"})
        job_id = self._required_text(params, "job_id")
        after = params.get("after", 0)
        wait_ms = params.get("wait_ms", 0)
        if (isinstance(after, bool) or not isinstance(after, int)
                or after < 0):
            raise ServiceError(
                -32602,
                "invalid_params",
                "'after' must be a non-negative integer.",
                {"parameter": "after"},
            )
        if (isinstance(wait_ms, bool) or not isinstance(wait_ms, int)
                or not 0 <= wait_ms <= 30000):
            raise ServiceError(
                -32602,
                "invalid_params",
                "'wait_ms' must be an integer from 0 to 30000.",
                {"parameter": "wait_ms"},
            )
        try:
            return self._jobs.events(
                job_id, after=after, wait_ms=wait_ms)
        except KeyError:
            self._job_not_found(job_id)

    def _cancel_job(self, params: dict[str, Any]) -> dict[str, Any]:
        self._check_params(params, {"job_id"}, {"job_id"})
        job_id = self._required_text(params, "job_id")
        from .jobs import JobNotCancellable
        try:
            return {"job": self._jobs.cancel(job_id)}
        except KeyError:
            self._job_not_found(job_id)
        except JobNotCancellable as exc:
            raise ServiceError(
                -32013,
                "job_not_cancellable",
                f"Job '{job_id}' cannot be cancelled.",
                {"job_id": job_id},
            ) from exc

    def _pause_job(self, params: dict[str, Any]) -> dict[str, Any]:
        self._check_params(params, {"job_id"}, {"job_id"})
        job_id = self._required_text(params, "job_id")
        from .jobs import JobNotPausable
        try:
            return {"job": self._jobs.pause(job_id)}
        except KeyError:
            self._job_not_found(job_id)
        except JobNotPausable as exc:
            raise ServiceError(
                -32016, "job_not_pausable",
                f"Job '{job_id}' cannot be paused.",
                {"job_id": job_id},
            ) from exc

    def _resume_job(self, params: dict[str, Any]) -> dict[str, Any]:
        self._check_params(params, {"job_id"}, {"job_id"})
        job_id = self._required_text(params, "job_id")
        from .jobs import JobNotPausable
        try:
            return {"job": self._jobs.resume(job_id)}
        except KeyError:
            self._job_not_found(job_id)
        except JobNotPausable as exc:
            raise ServiceError(
                -32016, "job_not_pausable",
                f"Job '{job_id}' cannot be resumed.",
                {"job_id": job_id},
            ) from exc

    def _require_idle_state(self) -> None:
        job = self._jobs.active()
        if job is not None:
            raise ServiceError(
                -32007,
                "operation_in_progress",
                "Application state is changing while a job is in progress.",
                {"job": job},
            )

    def _require_no_managed_launch(self, game: Any) -> None:
        launch = self._launches.get()
        from amethyst_plugins.launchers import identity
        owned_entry = identity(game)
        if (launch is not None and launch["active"]
                and (launch["game"]["id"] == self._canonical_game(game)["id"]
                     or owned_entry is not None and owned_entry ==
                     (launch["game"].get("provider_id"), launch["game"]["id"]))):
            raise ServiceError(
                -32007,
                "operation_in_progress",
                "Stop the managed game launch before changing its state.",
                {"launch": self._launch_summary(launch)},
            )

    def _require_configured(self, game: Any) -> None:
        if not bool(self._safe_call(game, "is_configured", False)):
            identity = self._canonical_game(game)
            raise ServiceError(
                -32008,
                "game_not_configured",
                f"Game '{identity['name']}' is not configured.",
                {"game": identity},
            )

    @staticmethod
    def _loot_available() -> bool:
        try:
            from LOOT.loot_sorter import is_available
            return bool(is_available())
        except Exception:
            return False

    @staticmethod
    def _raise_nexus_session_error(exc: Exception) -> None:
        from Nexus.session import NexusSessionError
        if not isinstance(exc, NexusSessionError):
            raise exc
        raise ServiceError(-32011, exc.name, str(exc), exc.details) from exc

    def _unsupported_operation(self, game: Any, operation: str) -> None:
        identity = self._canonical_game(game)
        raise ServiceError(
            -32009,
            "operation_not_supported",
            f"Game '{identity['name']}' does not support {operation}.",
            {"game": identity, "operation": operation},
        )

    @staticmethod
    def _job_not_found(job_id: str) -> None:
        raise ServiceError(
            -32006,
            "job_not_found",
            f"Job '{job_id}' was not found.",
            {"job_id": job_id},
        )

    def _resolve_game(self, key: str) -> Any:
        folded = key.casefold()
        by_id = None
        by_steam: list[Any] = []
        for name, game in self._games.items():
            if name.casefold() == folded:
                return game
            game_id = str(self._safe_attr(game, "game_id", "") or "")
            if by_id is None and game_id.casefold() == folded:
                by_id = game
            if folded in {
                    app_id.casefold() for app_id in self._steam_app_ids(game)}:
                by_steam.append(game)
        if by_id is not None:
            return by_id
        if len(by_steam) == 1:
            return by_steam[0]
        if len(by_steam) > 1:
            configured = [
                game for game in by_steam
                if bool(self._safe_call(game, "is_configured", False))
            ]
            if len(configured) == 1:
                return configured[0]
            raise ServiceError(
                -32004,
                "ambiguous_game",
                f"Game identifier '{key}' matches more than one game.",
                {
                    "game": key,
                    "matches": [self._canonical_game(game)
                                for game in by_steam],
                },
            )
        raise ServiceError(
            -32001,
            "game_not_found",
            f"Game '{key}' was not found.",
            {"game": key},
        )

    def _nexus_domains(self, game: Any) -> list[str]:
        raw_domains = self._safe_attr(game, "nexus_game_domains", ()) or ()
        if isinstance(raw_domains, str):
            raw_domains = (raw_domains,)
        domains: list[str] = []
        for raw in raw_domains:
            domain = str(raw or "").strip().lower()
            if domain and domain not in domains:
                domains.append(domain)
        if not domains:
            primary = str(
                self._safe_attr(game, "nexus_game_domain", "") or ""
            ).strip().lower()
            if primary:
                domains.append(primary)
        return domains

    def _resolve_nexus_domain(
        self, params: dict[str, Any],
    ) -> tuple[Any, str]:
        game = self._resolve_game(self._required_text(params, "game"))
        domains = self._nexus_domains(game)
        if not domains:
            self._unsupported_operation(game, "Nexus Mods metadata")

        if "domain" not in params:
            return game, domains[0]
        domain = self._required_text(params, "domain").lower()
        if domain not in domains:
            raise ServiceError(
                -32602,
                "invalid_params",
                f"Nexus domain '{domain}' is not valid for this game.",
                {"parameter": "domain", "supported": domains},
            )
        return game, domain

    def _resolve_profile(self, game: Any, name: str) -> Path:
        profiles = self._profile_dirs(game)
        exact = next((path for path in profiles if path.name == name), None)
        if exact is not None:
            return exact
        matches = [path for path in profiles
                   if path.name.casefold() == name.casefold()]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ServiceError(
                -32005,
                "ambiguous_profile",
                f"Profile name '{name}' is ambiguous.",
                {"profile": name, "matches": [path.name for path in matches]},
            )
        raise ServiceError(
            -32002,
            "profile_not_found",
            f"Profile '{name}' was not found.",
            {"game_id": self._canonical_game(game)["id"], "profile": name},
        )

    @staticmethod
    def _profile_dirs(game: Any) -> list[Path]:
        profiles_dir = game.get_profile_root() / "profiles"
        if not profiles_dir.is_dir():
            return []
        try:
            return [path for path in profiles_dir.iterdir() if path.is_dir()]
        except OSError as exc:
            raise ServiceError(
                -32003,
                "resource_unavailable",
                "Could not read the game's profiles directory.",
                {"reason": str(exc)},
            ) from exc

    def _game_summary(self, game: Any) -> dict[str, Any]:
        configured = bool(self._safe_call(game, "is_configured", False))
        nexus_domains = self._safe_attr(game, "nexus_game_domains", ()) or ()
        if isinstance(nexus_domains, str):
            nexus_domains = (nexus_domains,)
        nexus_domains = [str(domain) for domain in nexus_domains if domain]
        thunderstore = str(
            self._safe_attr(game, "thunderstore_community", "") or "")
        plugin_extensions = self._safe_attr(game, "plugin_extensions", ()) or ()
        return {
            **self._canonical_game(game),
            "configured": configured,
            "game_path": self._path_text(
                self._safe_call(game, "get_game_path", None)),
            "profile_root": self._path_text(
                self._safe_call(game, "get_profile_root", None)),
            "steam_app_ids": self._steam_app_ids(game),
            "effective_steam_app_id": str(
                self._safe_call(game, "effective_steam_id", "") or "") or None,
            "nexus_domains": nexus_domains,
            "thunderstore_community": thunderstore or None,
            "last_active_profile": str(
                self._safe_call(game, "get_last_active_profile", "default")
                or "default"),
            "last_deployed_profile": str(
                self._safe_call(game, "get_last_deployed_profile", "default")
                or "default"),
            "deploy_active": bool(
                self._safe_call(game, "get_deploy_active", False)),
            "features": {
                "deploy": callable(self._safe_attr(game, "deploy", None)),
                "restore": callable(self._safe_attr(game, "restore", None)),
                "launch": configured,
                "install": bool(
                    configured
                    and callable(self._safe_attr(
                        game, "get_mod_staging_path", None))),
                "nexus": bool(nexus_domains),
                "thunderstore": bool(thunderstore),
                "plugins": bool(plugin_extensions),
                "plugin_userlist": bool(
                    plugin_extensions and self._safe_attr(game, "loot_sort_enabled", False)),
                "plugin_sort": bool(
                    plugin_extensions
                    and self._safe_attr(game, "loot_sort_enabled", False)
                    and self._loot_available()),
                "profile_groups": bool(
                    self._safe_attr(game, "profile_groups_supported", True)),
                "vfs_deploy": bool(
                    (self._safe_attr(game, "supports_vfs_deploy", False)
                     or self._safe_attr(game, "supports_profile_vfs", False))
                    and callable(self._safe_attr(game, "set_vfs_enabled", None))),
                "copy_deploy": bool(
                    self._safe_attr(game, "deploy_mode_supports_copy", False)),
                "appimage_path": bool(
                    self._safe_attr(game, "game_id", "") == "morrowind_openmw"
                    and callable(self._safe_attr(game, "get_appimage_path", None))
                    and callable(self._safe_attr(game, "set_appimage_path", None))),
            },
        }

    def _profile_summary(self, game: Any, profile_dir: Path) -> dict[str, Any]:
        from Utils.mods.modlist import read_modlist
        from Utils.profiles.state import read_profile_settings

        settings = read_profile_settings(profile_dir, None)
        modlist_path = profile_dir / "modlist.txt"
        readable = True
        try:
            entries = read_modlist(modlist_path)
        except OSError:
            entries = []
            readable = False
        real_entries = [entry for entry in entries if not entry.is_separator]
        last_active = str(
            self._safe_call(game, "get_last_active_profile", "default")
            or "default")
        last_deployed = str(
            self._safe_call(game, "get_last_deployed_profile", "default")
            or "default")
        members = settings.get("group_members")
        if not isinstance(members, list):
            members = []
        return {
            "name": profile_dir.name,
            "path": self._path_text(profile_dir),
            "is_default": profile_dir.name == "default",
            "original_default": bool(
                profile_dir.name == "default"
                or settings.get("original_default", False)),
            "locked": bool(
                profile_dir.name == "default"
                or settings.get("original_default", False)
                or settings.get("profile_locked", False)),
            "is_group": bool(settings.get("is_group", False)),
            "group_members": [item for item in members if isinstance(item, str)],
            "profile_specific_mods": bool(
                settings.get("profile_specific_mods", False)),
            "collection_url": settings.get("collection_url")
            if isinstance(settings.get("collection_url"), str) else None,
            "modlist_exists": modlist_path.is_file(),
            "modlist_readable": readable,
            "mod_count": len(real_entries),
            "enabled_mod_count": sum(entry.enabled for entry in real_entries),
            "separator_count": len(entries) - len(real_entries),
            "is_last_active": profile_dir.name == last_active,
            "is_last_deployed": profile_dir.name == last_deployed,
            "is_deployed": (
                profile_dir.name == last_deployed
                and bool(self._safe_call(game, "get_deploy_active", False))
            ),
        }

    def _mod_summary(self, entry: Any, *, index: int, priority: int | None,
                     staging_dir: Path, is_bg3: bool, nexus_meta: Any = None) -> dict[str, Any]:
        if entry.is_separator:
            return {
                "kind": "separator",
                "name": entry.name,
                "display_name": entry.display_name,
                "index": index,
                "priority": None,
                "enabled": True,
                "locked": entry.locked,
                "path": None,
                "folder_exists": False,
                "version": None,
                "installed_at": None,
                "update_available": False,
                "requirements": {
                    "nexus": {"all": [], "missing": []},
                },
                "sources": {"nexus": None, "thunderstore": None, "modio": None},
            }

        safe_name = (Path(entry.name).name == entry.name
                     and entry.name not in (".", ".."))
        mod_dir = Path(staging_dir) / entry.name if safe_name else None
        meta_path = mod_dir / "meta.ini" if mod_dir is not None else None
        nexus = None
        thunderstore = None
        modio = None
        plugin_source = None
        version = None
        installed_at = None
        update_available = False
        requirements = {"nexus": {"all": [], "missing": []}}

        if meta_path is not None and meta_path.is_file():
            try:
                from Nexus.nexus_meta import (
                    parse_req_pairs,
                    read_meta as read_nexus_meta,
                )
                meta = nexus_meta if nexus_meta is not None else read_nexus_meta(meta_path)
                version = meta.version or None
                installed_at = meta.installed or None
                nexus_ignored = bool(
                    meta.ignore_update
                    or (meta.latest_version
                        and meta.latest_version == meta.ignored_version))
                nexus_update = bool(meta.has_update and not nexus_ignored)
                update_available = update_available or nexus_update
                if meta.mod_id or meta.game_domain or meta.nexus_url:
                    nexus = {
                        "game_domain": meta.game_domain or None,
                        "mod_id": meta.mod_id or None,
                        "file_id": meta.file_id or None,
                        "name": meta.nexus_name or None,
                        "file_name": meta.nexus_file_name or None,
                        "version": meta.version or None,
                        "latest_file_id": meta.latest_file_id or None,
                        "latest_version": meta.latest_version or None,
                        "has_update": nexus_update,
                        "update_ignored": nexus_ignored,
                        "endorsed": bool(meta.endorsed),
                        "author": meta.author or None,
                        "uploaded_by": meta.uploaded_by or None,
                        "category": meta.category_name or None,
                        "url": meta.nexus_page_url or None,
                        "installation_file": meta.installation_file or None,
                        "from_collection": meta.from_collection or None,
                        "is_fomod": bool(meta.is_fomod),
                        "is_bain": bool(meta.is_bain),
                        "root_folder": bool(meta.root_folder),
                    }
                requirements = {
                    "nexus": {
                        "all": [
                            {"mod_id": mod_id, "name": name}
                            for mod_id, name in parse_req_pairs(
                                meta.nexus_requirements)
                        ],
                        "missing": [
                            {"mod_id": mod_id, "name": name}
                            for mod_id, name in parse_req_pairs(
                                meta.missing_requirements)
                        ],
                    },
                }
            except Exception:
                pass
            if is_bg3:
                try:
                    from Utils.bg3.modio_loader import load_modio_module
                    meta = load_modio_module("modio_meta").read_modio_meta(meta_path)
                    if meta.mod_id > 0:
                        modio_update = bool(
                            meta.has_update or (
                                meta.file_id and meta.latest_file_id
                                and meta.file_id != meta.latest_file_id
                            )
                        )
                        update_available = update_available or modio_update
                        if not version:
                            version = meta.version or None
                        if not installed_at:
                            installed_at = meta.installed or None
                        modio = {
                            "mod_id": meta.mod_id,
                            "file_id": meta.file_id or None,
                            "version": meta.version or None,
                            "latest_file_id": meta.latest_file_id or None,
                            "latest_version": meta.latest_version or None,
                            "has_update": modio_update,
                            "name": meta.name or None,
                            "url": meta.profile_url or None,
                        }
                except Exception:
                    pass
            try:
                from Thunderstore.thunderstore_meta import read_meta as read_ts_meta
                meta = read_ts_meta(meta_path)
                if not version:
                    version = meta.version or None
                if not installed_at:
                    installed_at = meta.installed or None
                ts_update = bool(meta.has_update and not meta.ignore_update)
                update_available = update_available or ts_update
                if meta.package_id or meta.community or meta.full_name:
                    thunderstore = {
                        "community": meta.community or None,
                        "package_id": meta.package_id or None,
                        "full_name": meta.full_name or None,
                        "version": meta.version or None,
                        "latest_version": meta.latest_version or None,
                        "has_update": ts_update,
                        "update_ignored": bool(meta.ignore_update),
                        "website_url": meta.website_url or None,
                    }
            except Exception:
                pass

        if meta_path is not None and meta_path.is_file():
            try:
                from amethyst_plugins.repositories import installed, update_state
                record = installed(meta_path)
                if record is not None:
                    state = update_state(meta_path)
                    plugin_source = {**record, "update": state}
                    update_available = update_available or state.get("has_update", False)
            except (OSError, ValueError):
                pass
        return {
            "kind": "mod",
            "name": entry.name,
            "display_name": entry.display_name,
            "index": index,
            "priority": priority,
            "enabled": bool(entry.enabled),
            "locked": bool(entry.locked),
            "path": self._path_text(mod_dir),
            "folder_exists": bool(mod_dir is not None and mod_dir.is_dir()),
            "version": version,
            "installed_at": installed_at,
            "update_available": update_available,
            "requirements": requirements,
            "sources": {
                "nexus": nexus, "thunderstore": thunderstore,
                "modio": modio,
                "plugin": plugin_source,
            },
        }

    def _canonical_game(self, game: Any) -> dict[str, str]:
        return {
            "id": str(self._safe_attr(game, "game_id", "") or ""),
            "name": str(self._safe_attr(game, "name", "") or ""),
        }

    def _steam_app_ids(self, game: Any) -> list[str]:
        primary = self._safe_attr(game, "steam_id", "")
        alternates = self._safe_attr(game, "alt_steam_ids", ()) or ()
        if isinstance(alternates, (str, int)):
            alternates = (alternates,)
        result: list[str] = []
        for value in (primary, *alternates):
            text = str(value or "").strip()
            if text and text not in result:
                result.append(text)
        return result

    @staticmethod
    def _safe_attr(obj: Any, name: str, default: Any) -> Any:
        try:
            return getattr(obj, name, default)
        except Exception:
            return default

    @staticmethod
    def _safe_call(obj: Any, name: str, default: Any) -> Any:
        try:
            method = getattr(obj, name, None)
            return method() if callable(method) else default
        except Exception:
            return default

    @staticmethod
    def _path_text(path: Any) -> str | None:
        if path is None:
            return None
        try:
            return str(Path(path))
        except (OSError, TypeError, ValueError):
            return None
