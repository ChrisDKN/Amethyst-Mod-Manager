from __future__ import annotations

import contextlib
import copy
import fcntl
import hashlib
from Utils import json_limits as json
import os
from pathlib import Path
import subprocess
import sys
import time

from .catalog import discover


class ProviderError(ValueError):
    pass


def providers():
    plugins, errors = discover(None)
    return [(f"{p.id}/{provider.id}", p, provider)
            for p in plugins for provider in p.launcher_providers], errors


def provider_info(provider_id):
    for identity, plugin, provider in providers()[0]:
        if identity == provider_id:
            return plugin, provider
    raise ProviderError(f"Launcher provider is unavailable: {provider_id}. Enable or reinstall its plugin.")


def call(provider_id, method, *args, log=lambda message: None, check_cancelled=lambda: None):
    provider_info(provider_id)
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(
        [str(Path(__file__).resolve().parent.parent), *sys.path]))
    request = json.dumps({"provider_id": provider_id, "method": method, "args": args}, max_bytes=4 * 1024 * 1024)
    process = subprocess.Popen([sys.executable, "-m", "amethyst_plugins.launcher_worker"],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, env=env)
    deadline = time.monotonic() + (3600 if method == "update_entry" else 20)
    try:
        first = True
        while True:
            if method != "update_entry":
                check_cancelled()
            if time.monotonic() > deadline:
                raise ProviderError(f"Launcher provider timed out: {provider_id}")
            try:
                stdout, stderr = process.communicate(request if first else None, timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                first = False
        for line in stderr[-16000:].splitlines():
            log(f"{provider_id}: {line}")
        if process.returncode or len(stdout) > 4 * 1024 * 1024:
            raise ProviderError(f"Launcher provider failed: {provider_id}")
        response = json.loads(stdout, max_bytes=4 * 1024 * 1024)
        if "error" in response:
            raise ProviderError(f"{provider_id}: {response['error']}")
        return response
    except ProviderError:
        raise
    except (OSError, ValueError) as exc:
        raise ProviderError(f"Launcher provider failed: {provider_id}: {exc}") from exc
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate()


def _text(value, label, limit=32768):
    if not isinstance(value, str) or "\x00" in value or len(value) > limit:
        raise ProviderError(f"Invalid launcher {label}.")
    return value


def _path(value, label, optional=False):
    if optional and value in (None, ""):
        return None
    value = _text(value, label)
    if not value or not Path(value).is_absolute():
        raise ProviderError(f"Launcher {label} must be an absolute path.")
    return value


def normalize_entry(row, provider_id, label, writable, provider_icon=None):
    if not isinstance(row, dict):
        raise ProviderError("Launcher entry must be an object.")
    entry = dict(row)
    entry["id"] = _text(row.get("id"), "entry ID", 255)
    if not entry["id"] or any(ord(c) < 32 for c in entry["id"]):
        raise ProviderError("Invalid launcher entry ID.")
    entry["name"] = _text(row.get("name"), "name", 200)
    if not entry["name"].strip():
        raise ProviderError("Launcher entry needs a name.")
    for key in ("game_path", "executable"):
        entry[key] = _path(row.get(key), key)
    for key in ("prefix_path", "compat_data_path"):
        entry[key] = _path(row.get(key), key, optional=True)
    runner = row.get("runner", {})
    if not isinstance(runner, dict) or runner.get("kind") not in {"proton", "wine", "native"}:
        raise ProviderError("Launcher runner kind must be proton, wine or native.")
    entry["runner"] = dict(runner)
    if runner["kind"] != "native":
        entry["runner"]["path"] = _path(runner.get("path"), "runner path")
        if not entry["prefix_path"]:
            raise ProviderError("Windows launcher entries require prefix_path.")
    for key in ("arguments", "launch_options"):
        entry[key] = _text(row.get(key, ""), key)
    for key in ("installed", "plain_wine", "wayland"):
        value = row.get(key, key == "installed")
        if type(value) is not bool:
            raise ProviderError(f"Launcher {key} must be a boolean.")
        entry[key] = value
    from Utils.executables.launch import _normalize_lsfg_settings, _normalize_mangohud_settings
    for key, normalize in (("lsfg", _normalize_lsfg_settings), ("mangohud", _normalize_mangohud_settings)):
        if not isinstance(row.get(key, {}), dict):
            raise ProviderError(f"Launcher {key} must be an object.")
        entry[key] = normalize(row.get(key, {}))
    entry["revision"] = _text(row.get("revision"), "revision", 255)
    entry.update(provider_id=provider_id, provider_label=label, writable=writable,
                 provider_icon=str(provider_icon) if provider_icon is not None else None)
    return entry


def list_entries(log=lambda message: None, check_cancelled=lambda: None):
    entries, errors = [], []
    found, discovery_errors = providers()
    errors.extend(discovery_errors)
    for identity, plugin, provider in found:
        check_cancelled()
        try:
            response = call(identity, "list_entries", log=log, check_cancelled=check_cancelled)
            rows = response["result"]
            if not isinstance(rows, list) or len(rows) > 10000:
                raise ProviderError("Expected at most 10000 launcher entries.")
            seen = set()
            for row in rows:
                try:
                    entry = normalize_entry(row, identity, provider.label, response["writable"], provider.icon)
                    if entry["id"] in seen:
                        raise ProviderError(f"Duplicate launcher entry ID: {entry['id']}")
                    seen.add(entry["id"])
                    entries.append(entry)
                except (TypeError, ValueError) as exc:
                    errors.append(f"{identity}: {exc}")
        except ProviderError as exc:
            errors.append(str(exc))
    for error in errors:
        log(error)
    return {"entries": entries, "errors": errors}


def get_entry(provider_id, entry_id, **kwargs):
    _, provider = provider_info(provider_id)
    response = call(provider_id, "get_entry", entry_id, **kwargs)
    entry = normalize_entry(response["result"], provider_id, provider.label, response["writable"], provider.icon)
    if entry["id"] != entry_id:
        raise ProviderError("Launcher provider returned another entry.")
    return entry


@contextlib.contextmanager
def prefix_lock(prefix):
    from Utils.config_paths import get_config_dir
    root = get_config_dir() / "launcher_locks"
    root.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(str(Path(prefix).resolve()).encode()).hexdigest()
    with (root / key).open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ProviderError("This application prefix is busy.") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def update_entry(provider_id, entry_id, changes, revision, **kwargs):
    current = get_entry(provider_id, entry_id, **kwargs)
    if current["revision"] != revision:
        raise ProviderError("Launcher entry changed; refresh it before saving.")
    if not current["writable"]:
        raise ProviderError("This launcher provider exposes read-only settings.")
    with prefix_lock(current["prefix_path"] or current["game_path"]):
        assert_idle(current)
        _, provider = provider_info(provider_id)
        response = call(provider_id, "update_entry", entry_id, changes, revision, **kwargs)
        return normalize_entry(response["result"], provider_id, provider.label, response["writable"], provider.icon)


def identity(game):
    if not callable(getattr(game, "get_saved_launcher_id", None)):
        return None
    provider = game.get_saved_launcher_id("plugin_provider_id")
    entry = game.get_saved_launcher_id("plugin_entry_id")
    return (provider, entry) if provider and entry else None


def selected_entry(game, **kwargs):
    if hasattr(game, "_plugin_launcher_entry"):
        return game._plugin_launcher_entry
    saved = identity(game)
    return get_entry(*saved, **kwargs) if saved else None


def matches_game_path(game, path):
    current = game.get_game_path()
    if current is None or path is None:
        return False
    if Path(current).resolve() == Path(path).resolve():
        return True
    candidate = copy.copy(game)
    candidate._game_path = Path(path)
    resolved = candidate.get_game_path()
    return resolved is not None and Path(current).resolve() == Path(resolved).resolve()


def matches(game, entry):
    if not entry["installed"] or not Path(entry["game_path"]).is_dir():
        return False
    if entry.get("game_id") and entry["game_id"] != game.game_id:
        return False
    names = list(getattr(game, "configure_exe_names", []) or []) or [
        getattr(game, "exe_name", ""), *getattr(game, "exe_name_alts", [])]
    root = Path(entry["game_path"])
    for name in filter(None, names):
        parts = Path(str(name).replace("\\", "/")).parts
        if Path(name).is_absolute() or ".." in parts:
            continue
        path = root
        for part in parts:
            try:
                path = next(p for p in path.iterdir() if p.name.casefold() == part.casefold())
            except (OSError, StopIteration):
                break
        else:
            if path.is_file():
                return True
    return False


def assert_idle(entry):
    from Utils.processes.game import matching_pids, prefix_markers
    if entry["prefix_path"] and matching_pids(prefix_markers(entry["prefix_path"])) != set():
        raise ProviderError("The prefix is running or its process state is unavailable.")


def linked_profiles(provider_id, entry_ids, *, prefix_path=None, installation=None):
    from Utils.games.discovery import discover_games
    linked = []
    wanted = {(provider_id, eid) for eid in entry_ids}
    prefix = Path(prefix_path).resolve() if prefix_path else None
    for game in discover_games().values():
        root = Path(game.get_profile_root()) / "profiles"
        if not root.is_dir():
            continue
        for profile in root.iterdir():
            if not profile.is_dir() or profile.is_symlink():
                continue
            selected = copy.copy(game)
            selected.set_active_profile_dir(profile)
            selected.load_paths(read_only=True, prefix_fallback=False)
            related = prefix is not None and any(path and Path(path).resolve().is_relative_to(prefix)
                for path in (selected.get_prefix_path(), selected.get_game_path(), selected.get_profile_root()))
            same_install = False
            if (installation is not None and installation["installed"]
                    and matches_game_path(selected, installation["game_path"])
                    and str(selected.get_prefix_path() or "") == str(installation["prefix_path"] or "")):
                from Utils.executables.launch import is_game_launch_exe
                same_install = is_game_launch_exe(selected, Path(installation["executable"]))
            if identity(selected) in wanted or related or same_install:
                linked.append((selected, profile))
    return linked
