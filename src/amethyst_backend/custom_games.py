from __future__ import annotations

import hashlib
import io
import json
import uuid
from types import SimpleNamespace

from Utils.atomic_write import write_atomic_text
from Utils.config_paths import get_config_dir, get_custom_game_images_dir
from Utils.games.custom_definitions import validate_definition
from Utils.github.handler_sources import HandlerSources

from .jobs import JobError
from .methods import contracts


CONTRACTS = contracts('custom_games')


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def _clean(definition):
    return {key: value for key, value in definition.items() if not key.startswith("_")}


def get_definition(game_id):
    store = HandlerSources()
    selected = store.selected_handler(game_id)
    if selected is None:
        raise JobError("custom_game_not_found", "No custom game definition has this ID.")
    definition = dict(selected["definition"])
    definition["game_id"] = game_id
    definition["name"] = store._state()["names"].get(game_id, definition["name"])
    return {"definition": _clean(definition), "source": selected["source_id"],
            "source_key": selected["key"], "revision": _state_revision(game_id),
            "local_on_save": True}


def _state_revision(game_id):
    store = HandlerSources()
    state = store._state()
    options = store._options(state).get(game_id, {})
    definitions = {key: {"definition": item["definition"], "path": str(item["path"])}
                   for key, item in options.items()}
    configs = {}
    for item in options.values():
        name = state["names"].get(game_id, item["definition"]["name"])
        path = get_config_dir() / "games" / name / "paths.json"
        configs[name] = path.read_text() if path.exists() else None
    return _hash({"state": state, "definitions": definitions, "paths": configs})


def _check_editable(game):
    from Utils.ui.config import load_dev_mode
    if not getattr(game, "is_custom", False):
        raise JobError("not_custom_game", "Only JSON custom game handlers can be edited here.")
    selected = HandlerSources().selected_handler(game.game_id)
    editable = selected["definition"].get("editable", True) if selected else game.editable
    if not editable and not load_dev_mode():
        raise JobError("custom_game_not_editable", "This handler requires developer mode to edit.")
    if game.get_deploy_active():
        raise JobError("game_deployed", "Restore the game before changing its handler definition.")


def plan_save(games, definition, game=None):
    try:
        definition = validate_definition(definition)
    except (ValueError, TypeError) as exc:
        raise JobError("invalid_custom_game", str(exc)) from exc
    before = None
    if game is not None:
        _check_editable(game)
        before = get_definition(game.game_id)["definition"]
        if definition["game_id"] != game.game_id:
            raise JobError("custom_game_id_changed", "Keep the existing game_id when editing.")
    gid = definition["game_id"]
    for row in HandlerSources().games():
        if game is not None and row["game_id"] == game.game_id:
            continue
        if row["game_id"].casefold() == gid.casefold() or row["name"].casefold() == definition["name"].casefold():
            raise JobError("custom_game_exists", "The custom game name or ID is already in use.")
    for other in games.values():
        if game is not None and other.game_id == game.game_id:
            continue
        if other.game_id.casefold() == gid.casefold() or other.name.casefold() == definition["name"].casefold():
            raise JobError("custom_game_exists", "The game name or ID is already in use.")
    if game is None and HandlerSources().selected_handler(gid) is not None:
        raise JobError("custom_game_exists", "This custom game already exists; edit it instead.")
    rename = before is not None and before["name"] != definition["name"]
    if rename:
        target = get_config_dir() / "games" / definition["name"]
        if target.exists() or target.is_symlink():
            raise JobError("custom_game_name_in_use", "The new name already has saved game configuration.")
    revision = _hash({"state": _state_revision(gid), "definition": definition,
                      "editing": game is not None})
    return {"revision": revision, "definition": definition, "previous_definition": before,
            "changes": {key: {"before": (before or {}).get(key), "after": definition.get(key)}
                        for key in set(before or {}) | set(definition)
                        if (before or {}).get(key) != definition.get(key)},
            "creates_local_handler": True, "renames_configuration": rename,
            "requires_configuration": game is None,
            "routing_overrides_preserved": True}


def plan_remove(game):
    _check_editable(game)
    snapshot = get_definition(game.game_id)
    return {"revision": snapshot["revision"], "definition": snapshot["definition"],
            "removes_all_handler_sources": True, "preserves_profiles_and_game_files": True}


def _backup(store, game_id):
    from Utils.github.handler_sources import _definition
    state = store._state()
    paths = {item["path"] for item in store._options(state).get(game_id, {}).values()}
    for path in store.handlers.glob("*.json"):
        try:
            if _definition(path.read_text())[0] == game_id:
                paths.add(path)
        except (ValueError, OSError):
            continue
    paths.update(store._candidate_path(sid, entry["filename"])
                 for sid, entry in state["catalog"].get(game_id, {}).items()
                 if store._candidate_path(sid, entry["filename"]).is_file())
    if not paths:
        return None
    folder = store.root / "backups" / game_id / uuid.uuid4().hex
    folder.mkdir(parents=True)
    manifest = {"state": state, "handlers": []}
    for index, path in enumerate(sorted(paths)):
        name = f"{index}.json"
        write_atomic_text(folder / name, path.read_text())
        manifest["handlers"].append({"path": str(path), "backup": name})
    write_atomic_text(folder / "manifest.json", json.dumps(manifest, indent=2))
    return str(folder)


def _cache_image(definition, previous=None):
    import requests
    from PIL import Image
    from Utils.atomic_write import write_atomic

    url = definition.get("image_url", "")
    if not url:
        return None
    path = get_custom_game_images_dir() / (definition["game_id"] + ".png")
    if path.is_symlink():
        raise ValueError("Cannot replace a symlinked banner image.")
    if previous and previous.get("image_url") == url and path.is_file():
        return str(path)
    data = bytearray()
    with requests.get(url, timeout=(10, 20), stream=True) as response:
        response.raise_for_status()
        for chunk in response.iter_content(65536):
            data.extend(chunk)
            if len(data) > 20 * 1024 * 1024:
                raise ValueError("Banner image exceeds 20 MiB.")
    with Image.open(io.BytesIO(data)) as image:
        if image.width * image.height > 25_000_000:
            raise ValueError("Banner image exceeds 25 million pixels.")
        output = io.BytesIO()
        image.convert("RGBA").save(output, "PNG")
    if path.is_symlink():
        raise ValueError("Cannot replace a symlinked banner image.")
    write_atomic(path, output.getvalue())
    return str(path)


def save(games, definition, revision, game, log, progress):
    from Games.Custom.custom_game import make_custom_game
    from Utils.operations import acquire_game_operation

    target = game if game is not None else SimpleNamespace(
        game_id=definition["game_id"], name=definition["name"])
    lease = acquire_game_operation(target, "games.custom.save")
    moved = None
    backup = None
    with lease:
        plan = plan_save(games, definition, game)
        if plan["revision"] != revision:
            raise JobError("stale_custom_game", "The definition or configuration changed; plan again.")
        definition = plan["definition"]
        store = HandlerSources()

        def check():
            nonlocal backup, moved
            if plan_save(games, definition, game)["revision"] != revision:
                raise JobError("stale_custom_game", "The handler changed; plan again.")
            backup = _backup(store, definition["game_id"])
            if plan["renames_configuration"]:
                old = get_config_dir() / "games" / game.name
                new = get_config_dir() / "games" / definition["name"]
                if old.is_symlink():
                    raise JobError("unsafe_game_path", "Cannot rename a symlinked game configuration.")
                if old.is_dir():
                    paths = old / "paths.json"
                    if paths.exists():
                        raw = paths.read_text()
                        data = json.loads(raw)
                        moved = (old, new, raw)
                        if not data.get("staging_path"):
                            data["staging_path"] = str(game.get_profile_root())
                            write_atomic_text(paths, json.dumps(data, indent=2))
                    else:
                        raw = None
                        moved = (old, new, raw)
                    old.rename(new)

        progress(0, 2, "Saving custom game")
        try:
            path = store.save_local(definition, check=check)
        except Exception:
            if moved:
                old, new, raw = moved
                if not old.exists():
                    new.rename(old)
                if raw is not None:
                    write_atomic_text(old / "paths.json", raw)
            raise
        handler = make_custom_game(definition)
        if game is not None:
            games.pop(game.name, None)
        games[handler.name] = handler
        warnings = []
        progress(1, 2, "Caching banner image")
        try:
            image = _cache_image(definition, plan["previous_definition"])
        except Exception as exc:
            image = None
            warnings.append(f"Definition saved, but banner download failed: {exc}")
        progress(2, 2, "Custom game saved")
        log(f"Saved custom game: {handler.name}")
        return {"saved": True, "definition": definition, "path": str(path), "backup": backup,
                "image": image, "warnings": warnings, "revision": _state_revision(handler.game_id)}


def remove(games, game, revision, log, progress):
    from Utils.operations import acquire_game_operation
    store = HandlerSources()
    backup = None
    with acquire_game_operation(game, "games.custom.remove"):
        def check():
            nonlocal backup
            if plan_remove(game)["revision"] != revision:
                raise JobError("stale_custom_game", "The handler changed; plan removal again.")
            backup = _backup(store, game.game_id)
        store.delete_game(game.game_id, check=check)
        games.pop(game.name, None)
        image = get_custom_game_images_dir() / (game.game_id + ".png")
        image.unlink(missing_ok=True)
    log(f"Removed custom handler: {game.name}")
    return {"removed": True, "game_id": game.game_id, "backup": backup,
            "profiles_preserved": True}
