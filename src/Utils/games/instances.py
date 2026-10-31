from __future__ import annotations

import copy
import inspect
import json
import os
import re
import threading
import unicodedata
from contextlib import contextmanager
from functools import wraps
from pathlib import Path


class InstanceError(ValueError):
    def __init__(self, name, message, details=None):
        super().__init__(message)
        self.name = name
        self.details = details or {}


def instance_id(game):
    return getattr(game, "instance_id", getattr(game, "game_id", ""))


def can_duplicate(game):
    if not getattr(game, "supports_game_instances", False):
        return False
    from Games.base_game import BaseGame
    method = getattr(type(game), "get_instance_write_targets", None)
    return bool(method and (not getattr(game, "manager_plugin_id", None)
                           or method is not BaseGame.get_instance_write_targets))


_registry_state = threading.local()
_known_games = {}


@contextmanager
def registry_lock():
    from Utils.operations import acquire_resource_operation

    if getattr(_registry_state, "pid", None) == os.getpid() and getattr(_registry_state, "depth", 0):
        _registry_state.depth += 1
        try:
            yield
        finally:
            _registry_state.depth -= 1
        return
    with acquire_resource_operation("game-instance-registry", "game_instances", blocking=False):
        _registry_state.pid = os.getpid()
        _registry_state.depth = 1
        try:
            yield
        finally:
            _registry_state.depth = 0


def _read(path):
    if path.is_symlink():
        raise InstanceError("unsafe_instance_config", f"Configuration is a symbolic link: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise InstanceError("instance_config_unavailable", f"Cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise InstanceError("instance_config_unavailable", f"Invalid configuration: {path}")
    return value


def validate_name(name, games=(), *, draft=None):
    from Utils.config_paths import get_config_dir, get_default_game_staging_root, get_profiles_dir
    from Utils.downloads.cache import CLEAR_ALL_PRESERVE

    if not isinstance(name, str):
        raise InstanceError("invalid_instance_name", "Enter an instance name.")
    if any(unicodedata.category(c).startswith("C") for c in name):
        raise InstanceError("invalid_instance_name", "Instance names cannot contain control characters.")
    name = unicodedata.normalize("NFKC", name).strip()
    if (not name or len(name) > 100 or name in {".", ".."}
            or any(c in "/\\" or unicodedata.category(c).startswith("C") for c in name)):
        raise InstanceError("invalid_instance_name", "Use a name of 1–100 characters without path separators or control characters.")
    key = re.sub(r"[^a-z0-9_-]+", "_", unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()).strip("_-")
    if not key or len(key) > 100:
        raise InstanceError("invalid_instance_name", "The name must produce an ID containing letters or numbers.")
    names, ids = {".amethyst", "wabbajack", *CLEAR_ALL_PRESERVE}, set()
    for reserved_id, record in _read(get_config_dir() / "instance_reservations.json").items():
        if Path(record["staging"]).exists():
            names.add(record["name"].casefold())
            ids.add(reserved_id.casefold())
    values = games.values() if isinstance(games, dict) else games
    for game in values:
        if game is draft:
            continue
        names.add(game.name.casefold())
        ids.add(instance_id(game).casefold())
        ids.add(game.game_id.casefold())
    root = get_config_dir() / "games"
    for folder in root.iterdir() if root.is_dir() else ():
        names.add(folder.name.casefold())
        record = _read(folder / "instance.json") if folder.is_dir() else {}
        saved = _read(folder / "instance_targets.json") if folder.is_dir() else {}
        ids.update(str(saved.get(key, "")).casefold() for key in ("_instance_id", "_handler_id"))
        if record:
            ids.add(str(record.get("id", "")).casefold())
    for root in (get_profiles_dir(), get_default_game_staging_root(name).parent):
        if root.is_dir():
            names.update(p.name.casefold() for p in root.iterdir())
            ids.update(re.sub(r"[^a-z0-9_-]+", "_", p.name.lower()).strip("_-") for p in root.iterdir())
    if name.casefold() in names or key.casefold() in ids or key.casefold() in names:
        raise InstanceError("instance_exists", "This instance name or generated ID is already in use.", {"name": name, "id": key})
    return name, key


class _InstanceIdentity:
    @property
    def name(self):
        return self._instance_record["name"]

    @property
    def handler_name(self):
        return self._instance_handler_name

    @property
    def game_id(self):
        return self._instance_record["handler"]["id"]

    @property
    def instance_id(self):
        return self._instance_record["id"]

    @property
    def _paths_file(self):
        from Utils.config_paths import get_config_dir
        return get_config_dir() / "games" / self.name / "paths.json"

    @property
    def _settings_file(self):
        return self._paths_file.parent / "game_settings.json"

    def _migrate_old_config(self):
        pass

    def load_paths(self, **kwargs):
        kwargs["prefix_fallback"] = False
        kwargs["read_only"] = kwargs.get("read_only", False) or self._instance_draft
        return super().load_paths(**kwargs)


def handler_reference(game):
    if getattr(game, "is_instance", False):
        return dict(game._instance_record["handler"])
    return {"id": game.game_id, "plugin_id": getattr(game, "manager_plugin_id", None)}


def make_instance(source, record, *, draft=False):
    if not can_duplicate(source):
        raise InstanceError("instance_unsupported", "This handler has not declared support for independent instances.")
    handler_class = getattr(source, "_instance_handler_class", type(source))
    cls = type("GameInstance", (_InstanceIdentity, handler_class), {
        "_instance_record": copy.deepcopy(record),
        "_instance_handler_name": source.handler_name,
        "_instance_handler_class": handler_class,
        "_instance_draft": draft,
        "__module__": type(source).__module__,
    })
    try:
        if "defn" in inspect.signature(handler_class).parameters:
            definition = copy.deepcopy(source._defn)
            definition["game_id"] = source.game_id
            game = cls(definition)
        else:
            game = cls()
    except Exception as exc:
        raise InstanceError("instance_initialization_failed", f"Could not initialize a fresh instance of {source.handler_name}: {exc}") from exc
    if getattr(source, "manager_plugin_id", None):
        game.manager_plugin_id = source.manager_plugin_id
    return game


class UnavailableInstance:
    is_instance = True
    supports_game_instances = False

    def __init__(self, record, reason):
        from Utils.config_paths import get_config_dir, get_profiles_dir

        self._instance_record = record
        self.name, self.instance_id = record["name"], record["id"]
        self.game_id = record["handler"]["id"]
        self.unavailable_reason = reason
        self._paths_file = get_config_dir() / "games" / self.name / "paths.json"
        self._data = _read(self._paths_file)
        self._root = Path(self._data.get("staging_path") or get_profiles_dir() / self.name)

    def is_configured(self):
        return False

    def get_game_path(self):
        return Path(self._data["game_path"]) if self._data.get("game_path") else None

    def get_prefix_path(self):
        return Path(self._data["prefix_path"]) if self._data.get("prefix_path") else None

    def get_profile_root(self):
        return self._root

    def get_mod_staging_path(self):
        return self._root / "mods"

    def missing_configured_paths(self):
        return [self.get_game_path() or self._root]

    def get_deploy_active(self):
        if _read(self._paths_file.parent / "deploy_state.json").get("deploy_active"):
            return True
        from Utils.vfs import has_any_deployment_state
        return has_any_deployment_state(self)

    def load_paths(self, **kwargs):
        return False

    def set_active_profile_dir(self, profile):
        self._active_profile_dir = profile


def draft_instance(source, name, games):
    name, key = validate_name(name, games)
    cache_name = getattr(source, "_instance_record", {}).get("download_cache_name") or source.handler_name
    return make_instance(source, {"version": 1, "id": key, "name": name,
                                 "handler": handler_reference(source),
                                 "download_cache_name": cache_name}, draft=True)


def load_instances(games):
    from Utils.config_paths import get_config_dir

    global _known_games
    _known_games = games
    errors = []
    sources = {(g.game_id, getattr(g, "manager_plugin_id", None)): g
               for g in games.values() if not getattr(g, "is_instance", False)}
    ids = {instance_id(g).casefold() for g in games.values()}
    names = {n.casefold() for n in games}
    for path in sorted((get_config_dir() / "games").glob("*/instance.json")):
        valid_record = False
        try:
            record = _read(path)
            if (record.get("version") != 1 or record.get("name") != path.parent.name
                    or not isinstance(record.get("id"), str)
                    or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,99}", record.get("id", ""))
                    or not isinstance(record.get("handler"), dict)):
                raise InstanceError("invalid_instance", "Invalid instance descriptor.")
            if record["id"].casefold() in ids or record["name"].casefold() in names:
                raise InstanceError("instance_exists", "Instance identity conflicts with another game.")
            ref = record["handler"]
            if (not isinstance(ref.get("id"), str) or not ref["id"]
                    or (ref.get("plugin_id") is not None and not isinstance(ref["plugin_id"], str))):
                raise InstanceError("invalid_instance", "Invalid instance handler reference.")
            valid_record = True
            cache_name = record.get("download_cache_name")
            if cache_name is not None and (not isinstance(cache_name, str) or not cache_name
                                           or cache_name in {".", ".."} or "/" in cache_name or "\\" in cache_name):
                raise InstanceError("invalid_instance", "Invalid shared download cache folder name.")
            source = sources.get((ref.get("id"), ref.get("plugin_id")))
            if source is None:
                message = "The instance's handler is missing or disabled; its configuration and recovery data have been kept."
                games[record["name"]] = UnavailableInstance(record, message)
                ids.add(record["id"].casefold())
                names.add(record["name"].casefold())
                errors.append((f"instance {path.parent.name}", message))
                continue
            game = make_instance(source, record)
            games[game.name] = game
            ids.add(game.instance_id.casefold())
            names.add(game.name.casefold())
        except Exception as exc:
            if valid_record:
                try:
                    games[record["name"]] = UnavailableInstance(record, str(exc))
                    ids.add(record["id"].casefold())
                    names.add(record["name"].casefold())
                except (InstanceError, OSError, TypeError, ValueError):
                    pass
            errors.append((f"instance {path.parent.name}", str(exc)))
    return errors


def normalized_path(path, *, prefix=False):
    from Utils.environment.sandbox import resolve_document_portal_path
    from Utils.wine.prefix import normalize_prefix_path

    path = Path(path).expanduser()
    path = resolve_document_portal_path(path)
    if prefix:
        path = normalize_prefix_path(path)
    return path.resolve()


def paths_overlap(left, right):
    left, right = normalized_path(left), normalized_path(right)
    try:
        if left.exists() and right.exists() and left.samefile(right):
            return True
        for child, ancestor in ((left, right), (right, left)):
            if ancestor.exists() and any(parent.exists() and parent.samefile(ancestor) for parent in child.parents):
                return True
    except OSError:
        pass
    return left == right or left in right.parents or right in left.parents


def write_targets(game):
    prefix = getattr(game, "get_prefix_path", lambda: None)()
    targets = [("game", game.get_game_path()), ("prefix", prefix),
               ("staging", game.get_profile_root())]
    targets.extend(getattr(game, "get_instance_write_targets", lambda: [])())
    result = []
    for label, path in targets:
        if path is not None:
            path = normalized_path(path, prefix=label == "prefix")
            if (str(label), str(path)) not in result:
                result.append((str(label), str(path)))
    return result


def _saved_targets(folder, games, *, staging_only=False):
    from Utils.config_paths import get_profiles_dir
    from Utils.profiles.state import read_profile_settings, read_only_profile_state

    data = _read(folder / "paths.json")
    state = _read(folder / "deploy_state.json")
    cached = {} if staging_only else _read(folder / "instance_targets.json")
    if not data and not state and not cached:
        return
    root = Path(data.get("staging_path") or get_profiles_dir() / folder.name)
    source = games.get(folder.name)
    if staging_only and data.get("game_path"):
        yield "default", [("staging", str(normalized_path(root)))]
    profiles = [] if staging_only else [None, *sorted((root / "profiles").glob("*"))]
    unavailable = source is None or isinstance(source, UnavailableInstance)
    if unavailable and not staging_only:
        present = {profile.name for profile in profiles if profile is not None}
        profiles.extend(root / "profiles" / name for name, values in cached.items()
                        if isinstance(values, list) and name != "default" and name not in present
                        and name not in {".", ".."} and "/" not in name and "\\" not in name)
    for profile in profiles:
        if profile is not None and not profile.is_dir() and not (unavailable and profile.name in cached):
            continue
        profile_name = profile.name if profile else "default"
        settings = read_profile_settings(profile, read_only=True) if profile else {}
        effective = {**data, **settings}
        targets = [("game", effective.get("game_path")), ("prefix", effective.get("prefix_path")),
                   ("staging", root)]
        if unavailable:
            targets.extend(cached.get(profile_name, []))
        if effective.get("game_path") and source is not None and not isinstance(source, UnavailableInstance):
            selected = copy.copy(source)
            selected.set_active_profile_dir(profile)
            with read_only_profile_state():
                selected.load_paths(read_only=True, prefix_fallback=False)
                targets.extend(selected.get_instance_write_targets())
        result = []
        for label, path in targets:
            if path:
                result.append((str(label), str(normalized_path(path, prefix=label == "prefix"))))
        yield profile_name, result
    if state.get("deploy_active"):
        context = state.get("recovery_context") or {}
        targets = []
        for key, label in (("game_root", "game"), ("prefix_root", "prefix"), ("staging_root", "staging")):
            if staging_only and label != "staging":
                continue
            record = context.get(key)
            if isinstance(record, dict) and record.get("path"):
                targets.append((label, record["path"]))
        if not staging_only:
            targets.extend(context.get("instance_write_targets", []))
        yield "deployed:" + str(state.get("last_deployed", "default")), targets


def validate_paths(game, games=None, *, staging_only=False):
    from Utils.config_paths import get_config_dir
    from Utils.games.registry import _GAMES

    if not game.get_game_path() and not staging_only:
        return
    if not staging_only and getattr(game, "is_instance", False) and not game.supports_game_instances:
        raise InstanceError("instance_unsupported", "This handler does not declare independent write targets.")
    validator = getattr(game, "validate_instance_configuration", None)
    if not staging_only and callable(validator):
        validator()
    games = games if games is not None else (_GAMES or _known_games)
    targets = [("staging", str(normalized_path(game.get_profile_root())))] if staging_only else write_targets(game)
    game_root = game.get_game_path()
    staging = game.get_profile_root()
    prefix = getattr(game, "get_prefix_path", lambda: None)()
    if (game_root and paths_overlap(game_root, staging)) or (prefix and paths_overlap(prefix, staging)):
        raise InstanceError("instance_path_overlap", "The staging folder must be separate from the game and prefix folders.")
    staging_targets = [(label, path) for label, path in targets if label == "staging"]
    for key, record in _read(get_config_dir() / "instance_reservations.json").items():
        if key == instance_id(game) or not Path(record["staging"]).exists():
            continue
        for label, path in staging_targets:
            if paths_overlap(path, record["staging"]):
                raise InstanceError("instance_path_conflict", f"{path} overlaps retained storage for {record['name']}: {record['staging']}.",
                                    {"instance": record["name"], "profile": "retained storage", "path": path,
                                     "target": label, "conflicting_path": record["staging"], "conflicting_target": "staging"})
    reference = handler_reference(game)
    for folder in sorted((get_config_dir() / "games").glob("*")):
        if not folder.is_dir() or folder.name == game.name:
            continue
        other = games.get(folder.name)
        descriptor = _read(folder / "instance.json") if other is None else {}
        if other is None and not descriptor:
            continue
        other_reference = handler_reference(other) if other is not None else descriptor.get("handler")
        same_handler = other_reference == reference and not staging_only
        checked_targets = targets if same_handler else staging_targets
        for profile, other_targets in _saved_targets(folder, games, staging_only=not same_handler):
            for label, path in checked_targets:
                for other_label, other_path in other_targets:
                    if paths_overlap(path, other_path):
                        details = {"instance": folder.name, "profile": profile, "path": path,
                                   "target": label, "conflicting_path": str(other_path), "conflicting_target": other_label}
                        raise InstanceError("instance_path_conflict", f"{path} overlaps {folder.name} ({profile}): {other_path}. Choose independent locations.", details)


def save_target_record(game):
    from Utils.atomic_write import write_atomic_text

    if not game.get_game_path():
        return
    path = game._paths_file.parent / "instance_targets.json"
    data = _read(path)
    data.update(_instance_id=instance_id(game), _handler_id=game.game_id,
                _plugin_id=getattr(game, "manager_plugin_id", None))
    profile = getattr(game, "_active_profile_dir", None)
    data[profile.name if profile else "default"] = write_targets(game)
    write_atomic_text(path, json.dumps(data, indent=2))


def reserve_retained_storage(record, staging):
    from Utils.atomic_write import write_atomic_text
    from Utils.config_paths import get_config_dir

    path = get_config_dir() / "instance_reservations.json"
    reservations = _read(path)
    reservations[record["id"]] = {"name": record["name"], "staging": str(staging)}
    write_atomic_text(path, json.dumps(reservations, indent=2))


def validate_handler_identity(key, name):
    from Utils.config_paths import get_config_dir

    records = [_read(path) for path in (get_config_dir() / "games").glob("*/instance.json")]
    records.extend({"id": reserved, **record} for reserved, record in
                   _read(get_config_dir() / "instance_reservations.json").items()
                   if Path(record["staging"]).exists())
    if any(record.get("id", "").casefold() == key.casefold()
           or record.get("name", "").casefold() == name.casefold() for record in records):
        raise InstanceError("instance_exists", "This handler name or ID is reserved by an independent instance.")


def guard_path_save(function):
    @wraps(function)
    def save(game, *args, **kwargs):
        if getattr(game, "_instance_draft", False):
            raise InstanceError("instance_not_saved", "Save the new instance through its configuration form.")
        with registry_lock():
            validate_paths(game)
            if game._paths_file.is_file() and game.get_deploy_active():
                previous = copy.copy(game)
                previous.load_paths(read_only=True, prefix_fallback=False)
                if write_targets(previous) != write_targets(game):
                    raise InstanceError("game_deployed", "Restore this instance before changing its deployment locations.")
            result = function(game, *args, **kwargs)
            save_target_record(game)
            return result
    return save


def require_handler_idle(game):
    reference = handler_reference(game)
    if game.get_deploy_active():
        raise InstanceError("game_deployed", "Restore all instances before changing their shared handler.")
    require_handler_reference_idle(reference["id"], plugin_id=reference.get("plugin_id"), names=[game.handler_name])


def require_handler_reference_idle(handler_id, *, plugin_id=None, names=()):
    from Utils.config_paths import get_config_dir

    names = set(names) | {g.name for g in _known_games.values()
                          if g.game_id == handler_id and not g.is_instance}
    reference = {"id": handler_id, "plugin_id": plugin_id}
    for game in _known_games.values():
        if handler_reference(game) == reference and game.get_deploy_active():
            raise InstanceError("game_deployed", f"Restore {game.name} before changing its shared handler.")
    for folder in (get_config_dir() / "games").glob("*"):
        if not folder.is_dir():
            continue
        descriptor = _read(folder / "instance.json")
        if folder.name in names or descriptor.get("handler") == reference:
            if _read(folder / "deploy_state.json").get("deploy_active"):
                raise InstanceError("game_deployed", f"Restore {folder.name} before changing its shared handler.")


def resolve_game(games, key):
    folded = key.casefold()
    values = list(games.values())
    for game in values:
        if game.name.casefold() == folded:
            return game
    for game in values:
        if instance_id(game).casefold() == folded:
            return game
    matches = []
    for game in values:
        alternates = getattr(game, "alt_steam_ids", ()) or ()
        if isinstance(alternates, (str, int)):
            alternates = (alternates,)
        if folded in {str(value).casefold() for value in (getattr(game, "steam_id", ""), *alternates) if value}:
            matches.append(game)
    if len(matches) > 1:
        configured = [g for g in matches if g.is_configured()]
        if len(configured) == 1:
            return configured[0]
        raise InstanceError("ambiguous_game", f"'{key}' matches multiple instances. Select an instance ID or name.",
                            {"matches": [{"id": instance_id(g), "name": g.name} for g in matches]})
    return matches[0] if matches else None
