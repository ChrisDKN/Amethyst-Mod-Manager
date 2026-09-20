from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from version import __version__

from .jobs import JobManager

PROTOCOL_NAME = "amethyst.headless"
PROTOCOL_VERSION = 1

_NEXUS_QUERY_MODES = ("browse", "name", "author", "uploader_id", "trending")
_NEXUS_QUERY_SORTS = {
    "downloads": "downloads",
    "endorsements": "endorsements",
    "created": "createdAt",
    "updated": "updatedAt",
}
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

    METHODS = (
        "system.handshake",
        "system.ping",
        "games.list",
        "games.get",
        "profiles.list",
        "profiles.create.start",
        "profiles.groups.create.start",
        "profiles.groups.set_members.start",
        "profiles.convert.start",
        "profiles.rename.start",
        "profiles.remove.start",
        "profiles.select.start",
        "wabbajack.list",
        "wabbajack.remove.start",
        "wabbajack.reset_load_order.start",
        "downloads.list",
        "downloads.install.start",
        "nexus.auth.get",
        "nexus.auth.login.start",
        "nexus.auth.login.submit_code",
        "nexus.auth.login.cancel",
        "nexus.auth.logout.start",
        "nexus.user.get.start",
        "nexus.mods.filters.start",
        "nexus.mods.search.start",
        "nexus.mods.get.start",
        "nexus.mods.files.start",
        "nexus.files.get.start",
        "nexus.download.start",
        "mods.list",
        "mods.check_updates.start",
        "mods.reinstall.start",
        "mods.set_enabled.start",
        "mods.move.start",
        "mods.remove.start",
        "plugins.list.start",
        "plugins.set_enabled.start",
        "plugins.move.start",
        "plugins.sort.start",
        "boundaries.list.start",
        "boundaries.move.start",
        "boundaries.remove.start",
        "deploy.start",
        "restore.start",
        "launch.get",
        "launch.start",
        "launch.stop",
        "jobs.get",
        "jobs.list",
        "jobs.events",
        "jobs.cancel",
    )

    def __init__(self, games: dict[str, Any] | None = None):
        if games is None:
            from Utils.games.discovery import discover_games
            games = discover_games()
        self._games = dict(games)
        self._jobs = JobManager()
        from .launch import LaunchManager
        self._launches = LaunchManager()
        from Nexus.session import NexusLoginManager, NexusSession
        self._nexus_session = NexusSession()
        self._nexus_login = NexusLoginManager(self._nexus_session)
        self._handlers: dict[str, Callable[[dict[str, Any]], Any]] = {
            "system.handshake": self._handshake,
            "system.ping": self._ping,
            "games.list": self._list_games,
            "games.get": self._get_game,
            "profiles.list": self._list_profiles,
            "profiles.create.start": self._start_create_profile,
            "profiles.groups.create.start": self._start_create_group,
            "profiles.groups.set_members.start": self._start_set_group_members,
            "profiles.convert.start": self._start_convert_profile,
            "profiles.rename.start": self._start_rename_profile,
            "profiles.remove.start": self._start_remove_profile,
            "profiles.select.start": self._start_select_profile,
            "wabbajack.list": self._list_wabbajack,
            "wabbajack.remove.start": self._start_remove_wabbajack,
            "wabbajack.reset_load_order.start": self._start_reset_wabbajack,
            "downloads.list": self._list_downloads,
            "downloads.install.start": self._start_install_download,
            "nexus.auth.get": self._get_nexus_auth,
            "nexus.auth.login.start": self._start_nexus_login,
            "nexus.auth.login.submit_code": self._submit_nexus_login_code,
            "nexus.auth.login.cancel": self._cancel_nexus_login,
            "nexus.auth.logout.start": self._start_nexus_logout,
            "nexus.user.get.start": self._start_nexus_user,
            "nexus.mods.filters.start": self._start_nexus_filters,
            "nexus.mods.search.start": self._start_nexus_search,
            "nexus.mods.get.start": self._start_nexus_mod,
            "nexus.mods.files.start": self._start_nexus_files,
            "nexus.files.get.start": self._start_nexus_file,
            "nexus.download.start": self._start_nexus_download,
            "mods.list": self._list_mods,
            "mods.check_updates.start": self._start_check_mod_updates,
            "mods.reinstall.start": self._start_reinstall_mod,
            "mods.set_enabled.start": self._start_set_enabled,
            "mods.move.start": self._start_move,
            "mods.remove.start": self._start_remove,
            "plugins.list.start": self._start_list_plugins,
            "plugins.set_enabled.start": self._start_set_plugins_enabled,
            "plugins.move.start": self._start_move_plugins,
            "plugins.sort.start": self._start_sort_plugins,
            "boundaries.list.start": self._start_list_boundary,
            "boundaries.move.start": self._start_move_boundary,
            "boundaries.remove.start": self._start_remove_boundary,
            "deploy.start": self._start_deploy,
            "restore.start": self._start_restore,
            "launch.get": self._get_launch,
            "launch.start": self._start_launch,
            "launch.stop": self._stop_launch,
            "jobs.get": self._get_job,
            "jobs.list": self._list_jobs,
            "jobs.events": self._job_events,
            "jobs.cancel": self._cancel_job,
        }

    def close(self) -> None:
        self._jobs.close(wait=True)
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
        return handler(params)

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
                "read_only": False,
                "events": True,
                "event_delivery": "poll",
                "cancellation": True,
                "cancellable_jobs": ["nexus.download"],
                "mutations": [
                    "profiles.create", "profiles.groups.create",
                    "profiles.groups.set_members",
                    "profiles.convert",
                    "profiles.rename", "profiles.remove",
                    "wabbajack.remove",
                    "wabbajack.reset_load_order",
                    "profiles.select", "mods.set_enabled", "mods.move",
                    "mods.remove", "mods.check_updates", "mods.reinstall",
                    "plugins.set_enabled", "plugins.move", "plugins.sort",
                    "boundaries.move", "boundaries.remove",
                    "deploy", "restore",
                    "nexus.auth.login", "nexus.auth.logout",
                    "nexus.download", "downloads.install", "launch",
                    "launch.stop"],
                "installation": {
                    "source": "download_inventory",
                    "installer_policies": ["reject", "defaults"],
                    "existing_mod_policies": ["fail", "replace"],
                    "interactive": False,
                },
                "mods": {
                    "update_check": {
                        "sources": ["nexus"],
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
                "launch": {
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

    def _start_install_download(
        self, params: dict[str, Any],
    ) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(
            params,
            {
                "game", "profile", "archive_id", "name",
                "installer_policy", "if_exists",
            },
            {"game", "profile", "archive_id"},
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

        archive_id = self._archive_id(params.get("archive_id"))

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
        if installer_policy not in {"reject", "defaults"}:
            raise ServiceError(
                -32602,
                "invalid_params",
                "'installer_policy' must be 'reject' or 'defaults'.",
                {"parameter": "installer_policy"},
            )
        if_exists = params.get("if_exists", "fail")
        if if_exists not in {"fail", "replace"}:
            raise ServiceError(
                -32602,
                "invalid_params",
                "'if_exists' must be 'fail' or 'replace'.",
                {"parameter": "if_exists"},
            )

        job = self._jobs.submit(
            "downloads.install",
            game,
            self._canonical_game(game),
            profile_dir.name,
            {
                "archive_id": archive_id,
                "name": name or None,
                "installer_policy": installer_policy,
                "if_exists": if_exists,
            },
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
            game, domain = self._resolve_nexus_domain({
                "game": params["game"], "domain": parsed.game_domain})
            mod_id, file_id = parsed.mod_id, parsed.file_id
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

        import threading
        from .nexus import download_runner
        cancel = threading.Event()
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
                cancel=cancel,
            ),
            cancel=cancel.set,
        )
        return {"job": job}

    def _list_mods(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(params, {"game", "profile"}, {"game", "profile"})
        game = self._resolve_game(self._required_text(params, "game"))
        profile_dir = self._resolve_profile(
            game, self._required_text(params, "profile"))

        from Utils.mods.modlist import read_modlist
        from Utils.profiles.state import read_profile_settings

        try:
            entries = read_modlist(profile_dir / "modlist.txt")
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
        mods: list[dict[str, Any]] = []
        for index, entry in enumerate(entries):
            priority = None
            if not entry.is_separator:
                priority = real_count - real_index - 1
                real_index += 1
            mods.append(self._mod_summary(
                entry, index=index, priority=priority,
                staging_dir=staging_dir,
            ))

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
            "counts": {
                "mods": real_count,
                "enabled": enabled_count,
                "disabled": real_count - enabled_count,
                "separators": len(entries) - real_count,
            },
            "mods": mods,
        }

    def _start_check_mod_updates(
        self, params: dict[str, Any],
    ) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(
            params, {"game", "profile", "mods"}, {"game", "profile"})
        game, domain = self._resolve_nexus_domain(params)
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
            {"mods": mods, "source": "nexus"},
            runner=check_updates_runner(
                self._nexus_session,
                game,
                profile_dir,
                domain,
                mods,
                "headless:mods.check_updates",
            ),
        )
        return {"job": job}

    def _start_reinstall_mod(
        self, params: dict[str, Any],
    ) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(
            params,
            {
                "game", "profile", "mod", "archive_id",
                "installer_policy", "confirm",
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
        if installer_policy not in {"reject", "defaults"}:
            raise ServiceError(
                -32602,
                "invalid_params",
                "'installer_policy' must be 'reject' or 'defaults'.",
                {"parameter": "installer_policy"},
            )
        if params.get("confirm") is not True:
            raise ServiceError(
                -32602,
                "invalid_params",
                "'confirm' must be true because reinstalling replaces the "
                "existing mod folder.",
                {"parameter": "confirm"},
            )

        options = {
            "mod": mod,
            "installer_policy": installer_policy,
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
        if (launch is not None and launch["active"]
                and launch["game"]["id"] == self._canonical_game(game)["id"]):
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

    def _resolve_nexus_domain(
        self, params: dict[str, Any],
    ) -> tuple[Any, str]:
        game = self._resolve_game(self._required_text(params, "game"))
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
                "plugin_sort": bool(
                    plugin_extensions
                    and self._safe_attr(game, "loot_sort_enabled", False)
                    and self._loot_available()),
                "profile_groups": bool(
                    self._safe_attr(game, "profile_groups_supported", True)),
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
                     staging_dir: Path) -> dict[str, Any]:
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
                "sources": {"nexus": None, "thunderstore": None},
            }

        safe_name = (Path(entry.name).name == entry.name
                     and entry.name not in (".", ".."))
        mod_dir = Path(staging_dir) / entry.name if safe_name else None
        meta_path = mod_dir / "meta.ini" if mod_dir is not None else None
        nexus = None
        thunderstore = None
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
                meta = read_nexus_meta(meta_path)
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
            "sources": {"nexus": nexus, "thunderstore": thunderstore},
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
