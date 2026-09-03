from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from version import __version__

from .jobs import JobManager

PROTOCOL_NAME = "amethyst.headless"
PROTOCOL_VERSION = 1


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
        "profiles.rename.start",
        "profiles.remove.start",
        "profiles.select.start",
        "downloads.list",
        "downloads.install.start",
        "nexus.auth.get",
        "nexus.auth.login.start",
        "nexus.auth.login.submit_code",
        "nexus.auth.login.cancel",
        "nexus.auth.logout.start",
        "nexus.user.get.start",
        "nexus.mods.get.start",
        "nexus.mods.files.start",
        "nexus.files.get.start",
        "nexus.download.start",
        "mods.list",
        "mods.set_enabled.start",
        "mods.move.start",
        "mods.remove.start",
        "deploy.start",
        "restore.start",
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
            "profiles.rename.start": self._start_rename_profile,
            "profiles.remove.start": self._start_remove_profile,
            "profiles.select.start": self._start_select_profile,
            "downloads.list": self._list_downloads,
            "downloads.install.start": self._start_install_download,
            "nexus.auth.get": self._get_nexus_auth,
            "nexus.auth.login.start": self._start_nexus_login,
            "nexus.auth.login.submit_code": self._submit_nexus_login_code,
            "nexus.auth.login.cancel": self._cancel_nexus_login,
            "nexus.auth.logout.start": self._start_nexus_logout,
            "nexus.user.get.start": self._start_nexus_user,
            "nexus.mods.get.start": self._start_nexus_mod,
            "nexus.mods.files.start": self._start_nexus_files,
            "nexus.files.get.start": self._start_nexus_file,
            "nexus.download.start": self._start_nexus_download,
            "mods.list": self._list_mods,
            "mods.set_enabled.start": self._start_set_enabled,
            "mods.move.start": self._start_move,
            "mods.remove.start": self._start_remove,
            "deploy.start": self._start_deploy,
            "restore.start": self._start_restore,
            "jobs.get": self._get_job,
            "jobs.list": self._list_jobs,
            "jobs.events": self._job_events,
            "jobs.cancel": self._cancel_job,
        }

    def close(self) -> None:
        self._jobs.close(wait=True)
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
                    "profiles.create", "profiles.rename", "profiles.remove",
                    "profiles.select", "mods.set_enabled", "mods.move",
                    "mods.remove", "deploy", "restore",
                    "nexus.auth.login", "nexus.auth.logout",
                    "nexus.download", "downloads.install"],
                "installation": {
                    "source": "download_inventory",
                    "installer_policies": ["reject", "defaults"],
                    "existing_mod_policies": ["fail", "replace"],
                    "interactive": False,
                },
                "nexus": {
                    "authentication": "oauth_pkce",
                    "browser_open_optional": True,
                    "manual_code_fallback": True,
                    "credentials_exposed": False,
                    "download_modes": ["premium", "nxm"],
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

    def _start_rename_profile(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(
            params, {"game", "profile", "new_name"},
            {"game", "profile", "new_name"})
        game = self._resolve_game(self._required_text(params, "game"))
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

        archive_id = self._required_text(params, "archive_id")
        digest = archive_id.removeprefix("archive_")
        if (not archive_id.startswith("archive_") or len(digest) != 64
                or any(char not in "0123456789abcdef" for char in digest)):
            raise ServiceError(
                -32602,
                "invalid_params",
                "'archive_id' must be an ID returned by downloads.list.",
                {"parameter": "archive_id"},
            )

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

    def _start_deploy(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_idle_state()
        self._check_params(
            params, {"game", "profile", "options"}, {"game", "profile"})
        game = self._resolve_game(self._required_text(params, "game"))
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
                "install": bool(
                    configured
                    and callable(self._safe_attr(
                        game, "get_mod_staging_path", None))),
                "nexus": bool(nexus_domains),
                "thunderstore": bool(thunderstore),
                "plugins": bool(plugin_extensions),
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

        if meta_path is not None and meta_path.is_file():
            try:
                from Nexus.nexus_meta import read_meta as read_nexus_meta
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
