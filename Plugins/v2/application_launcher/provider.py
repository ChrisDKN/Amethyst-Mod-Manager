from __future__ import annotations

import base64
import contextlib
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import uuid

from amethyst_plugins.launchers import ProviderError, assert_idle, linked_profiles, prefix_lock
from Utils.atomic_write import write_atomic_text
from .i18n import tr


@contextlib.contextmanager
def library(context, *, write=False):
    root = Path(context["plugin_dir"])
    with (root / ".library.lock").open("a") if write else contextlib.nullcontext() as handle:
        if handle is not None:
            fcntl.flock(handle, fcntl.LOCK_EX)
        path = root / "data.json"
        try:
            if path.stat().st_size > 1024 * 1024:
                raise ValueError(tr('Application library exceeds 1 MiB.'))
            data = json.loads(path.read_text())
        except FileNotFoundError:
            data = {"version": 1, "entries": {}, "prefix_root": str(root / "prefixes")}
        if not isinstance(data, dict) or not isinstance(data.get("entries", {}), dict):
            raise ValueError(tr('Application Launcher data is invalid.'))
        data.setdefault("entries", {})
        yield data


def save(context, data):
    encoded = json.dumps(data, indent=2, allow_nan=False)
    if len(encoded.encode()) > 1024 * 1024:
        raise ValueError(tr('Application library exceeds 1 MiB.'))
    write_atomic_text(Path(context["plugin_dir"]) / "data.json", encoded)


def record(data, entry_id, *, games=None):
    if entry_id not in data["entries"]:
        raise ValueError(tr('This application entry has been removed.'))
    result = copy.deepcopy(data["entries"][entry_id])
    executable = Path(result["executable"])
    if (result.get("installed", True) and result.get("game_path_auto", True)
            and Path(result["game_path"]) == executable.parent):
        result["game_path"] = str(infer_game_root(executable, games=games))
    result["revision"] = hashlib.sha256(json.dumps([data, result], sort_keys=True).encode()).hexdigest()
    return result


def pending(context):
    return Path(context["plugin_dir"]) / "operations" / "prefix-move.json"


def pending_removal(context):
    return Path(context["plugin_dir"]) / "operations" / "prefix-removal.json"


def list_entries(context):
    if pending(context).exists() or pending_removal(context).exists():
        raise ValueError(tr('An interrupted prefix change needs recovery. Open Application Launcher.'))
    with library(context) as data:
        from Utils.games.discovery import discover_games
        games = tuple(discover_games().values())
        return [record(data, key, games=games) for key in data["entries"]]


def get_entry(context, entry_id):
    if pending(context).exists() or pending_removal(context).exists():
        raise ValueError(tr('An interrupted prefix change needs recovery. Open Application Launcher.'))
    with library(context) as data:
        return record(data, entry_id)


def add_entry(context, executable, *, installer=False):
    from Utils.launchers.steam import list_installed_proton
    path = Path(executable)
    if not path.is_file() or path.suffix.casefold() not in {".exe", ".msi"}:
        raise ValueError(tr('Choose an existing EXE or MSI file.'))
    builds = list_installed_proton()
    with library(context, write=True) as data:
        entry_id = uuid.uuid4().hex
        prefix = str(prefix_for_name(context, data, path.stem))
        data["entries"][entry_id] = {
            "id": entry_id, "name": path.stem, "game_path": str(path.parent if installer else infer_game_root(path)),
            "executable": str(path), "prefix_path": prefix, "compat_data_path": prefix,
            "runner": {"kind": "proton", "path": str(builds[0]) if builds else "/missing/proton"},
            "installed": not installer, "arguments": "", "launch_options": "",
            "plain_wine": False, "wayland": False, "lsfg": {}, "mangohud": {},
            "prefix_name_pending": True, "game_path_auto": True}
        save(context, data)
        return record(data, entry_id)


def prefix_for_name(context, data, name, *, exclude=None):
    root = Path(data.get("prefix_root") or Path(context["plugin_dir"]) / "prefixes")
    name = re.sub(r"[^\w .()-]+", "-", name).strip(" .-")
    name = name.encode("utf-8")[:160].decode("utf-8", errors="ignore").rstrip(" .-") or "Application"
    used = {str(Path(row["prefix_path"]).resolve()).casefold() for key, row in data["entries"].items() if key != exclude}
    existing = {path.name.casefold() for path in root.iterdir()} if root.is_dir() else set()
    candidate, suffix = root / name, 2
    while str(candidate.resolve()).casefold() in used or candidate.name.casefold() in existing:
        candidate = root / f"{name} ({suffix})"
        suffix += 1
    return candidate


def infer_game_root(path, *, games=None):
    from Utils.games.discovery import discover_games
    from amethyst_plugins.launchers import matches
    path = Path(path)
    if games is None:
        games = tuple(discover_games().values())
    for root in list(Path(path).parents)[:5]:
        if root == Path.home() or len(root.parts) < 3:
            break
        candidate = {"installed": True, "game_path": str(root)}
        relative = path.relative_to(root).as_posix().casefold()
        for game in games:
            names = list(getattr(game, "configure_exe_names", []) or []) or [
                getattr(game, "exe_name", ""), *getattr(game, "exe_name_alts", [])]
            if (relative in {str(name).replace("\\", "/").casefold() for name in names if name}
                    and matches(game, candidate)):
                return root
    return path.parent


def remove_entry(context, entry_id, *, delete_prefix=False, revision=None):
    current = get_entry(context, entry_id)
    if revision is not None and revision != current["revision"]:
        raise ValueError(tr('The application changed. Refresh before removing it.'))
    with prefix_lock(current["prefix_path"]):
        assert_idle(current)
        if linked_profiles(context["provider_id"], [entry_id]):
            raise ValueError(tr('This entry is linked to Amethyst. Choose another installation in Configure Game before removing it.'))
        with library(context, write=True) as data:
            latest = record(data, entry_id)
            if latest["revision"] != current["revision"]:
                raise ValueError(tr('The application changed. Refresh before removing it.'))
            prefix = Path(current["prefix_path"])
            removed = None
            if delete_prefix:
                if any(key != entry_id and Path(row["prefix_path"]).resolve() == prefix.resolve()
                       for key, row in data["entries"].items()):
                    raise ValueError(tr('This prefix is shared with another application. Remove the entry while keeping its prefix.'))
                if linked_profiles(context["provider_id"], [], prefix_path=prefix):
                    raise ValueError(tr('An Amethyst game uses this prefix or files inside it. Reconfigure that game before deleting the prefix.'))
                if (prefix.is_symlink() or prefix.resolve() in {Path("/"), Path.home().resolve()}
                        or Path(context["plugin_dir"]).resolve().is_relative_to(prefix.resolve())):
                    raise ValueError(tr('Choose a dedicated, real prefix directory before deleting it.'))
                if prefix.exists():
                    if not prefix.is_dir() or any(prefix.iterdir()) and not (prefix / "drive_c").is_dir():
                        raise ValueError(tr('This directory does not look like a Wine prefix; it was kept.'))
                    removed = prefix.with_name(f".{prefix.name}.removing-{uuid.uuid4().hex}")
                    stat = prefix.stat()
                    journal = pending_removal(context)
                    journal.parent.mkdir(parents=True, exist_ok=True)
                    write_atomic_text(journal, json.dumps({"id": entry_id, "source": str(prefix),
                        "removed": str(removed), "identity": [stat.st_dev, stat.st_ino]}))
                    prefix.rename(removed)
            data["entries"].pop(entry_id, None)
            try:
                save(context, data)
            except Exception:
                if removed is not None:
                    removed.rename(prefix)
                    pending_removal(context).unlink(missing_ok=True)
                raise
            if removed is not None:
                try:
                    shutil.rmtree(removed)
                except OSError as exc:
                    raise ValueError(tr('Application removed, but some prefix files remain at {removed}: {exc}', removed=removed, exc=exc)) from exc
                pending_removal(context).unlink(missing_ok=True)


def update_entry(context, entry_id, changes, revision):
    from amethyst_plugins.launchers import normalize_entry
    from Utils.deployment.locking import game_mutation_lock
    from amethyst_backend.reconfigure import plan_reconfigure_game, reconfigure_game
    allowed = {"name", "executable", "game_path", "prefix_path", "runner", "arguments",
               "launch_options", "plain_wine", "wayland", "lsfg", "mangohud", "installed", "prefix_action"}
    if not isinstance(changes, dict) or not changes or set(changes) - allowed:
        raise ValueError(tr('Supply known application settings.'))
    if pending(context).exists():
        raise ValueError(tr('Recover the interrupted prefix change before saving.'))
    with library(context, write=True) as data:
        current = record(data, entry_id)
        if current["revision"] != revision:
            raise ValueError(tr('The application library changed. Refresh before saving.'))
        before = copy.deepcopy(data)
        proposed = {**current, **changes}
        proposed.pop("revision", None)
        if "game_path" in changes and proposed["game_path"] != current["game_path"]:
            proposed["game_path_auto"] = False
        if (current.get("prefix_name_pending") and "name" in changes and "prefix_path" not in changes
                and not Path(current["prefix_path"]).exists()
                and not any(key != entry_id and row["prefix_path"] == current["prefix_path"]
                            for key, row in data["entries"].items())):
            proposed["prefix_path"] = str(prefix_for_name(context, data, proposed["name"], exclude=entry_id))
        proposed["prefix_name_pending"] = False
        action = proposed.pop("prefix_action", "select")
        if action not in {"select", "move"}:
            raise ValueError(tr('Choose move or select for the prefix change.'))
        prefix_changed = proposed["prefix_path"] != current["prefix_path"]
        if prefix_changed:
            proposed["compat_data_path"] = proposed["prefix_path"]
            if action == "select":
                from Utils.wine.prefix import normalize_prefix_path
                selected = Path(proposed["prefix_path"])
                normalized = normalize_prefix_path(selected)
                proposed["prefix_path"] = str(normalized)
                if normalized == selected and selected.name == "pfx" and (selected.parent / "config_info").is_file():
                    proposed["compat_data_path"] = str(selected.parent)
                if selected.exists() and any(selected.iterdir()) and not (normalized / "drive_c").is_dir():
                    raise ValueError(tr('Select an empty directory or an existing Wine/Proton prefix.'))
        normalize_entry({**proposed, "revision": revision}, context["provider_id"], "Application Launcher", True)
        old, new = Path(current["prefix_path"]), Path(proposed["prefix_path"])
        if old.resolve() == new.resolve():
            proposed["prefix_path"] = current["prefix_path"]
            proposed["compat_data_path"] = current["compat_data_path"]
            new = old
            prefix_changed = False
        if prefix_changed and new.resolve() in {Path("/"), Path.home().resolve()}:
            raise ValueError(tr('Choose a dedicated prefix directory.'))
        affected = [key for key, row in data["entries"].items()
                    if Path(row["prefix_path"]).resolve() == old.resolve()]
        if prefix_changed and action == "select":
            affected = [entry_id]
            target_group = [row for key, row in data["entries"].items()
                            if key != entry_id and Path(row["prefix_path"]).resolve() == new.resolve()]
            if target_group:
                proposed["runner"] = copy.deepcopy(target_group[0]["runner"])
        structural = prefix_changed or any(proposed[k] != data["entries"][entry_id][k]
                                          for k in ("game_path", "executable", "runner"))
        links = linked_profiles(context["provider_id"], affected) if structural else []
        with contextlib.ExitStack() as stack:
            if prefix_changed:
                stack.enter_context(prefix_lock(new))
                assert_idle({**current, "prefix_path": str(new)})
            for game in sorted({str(g.get_profile_root()): g for g, _ in links}.values(), key=lambda g: str(g.get_profile_root())):
                stack.enter_context(game_mutation_lock(game))
                if game.get_deploy_active():
                    raise ValueError(tr('Restore linked Amethyst profiles before changing their prefix, runner or installation.'))
                if prefix_changed and action == "move" and Path(game.get_profile_root()).resolve().is_relative_to(old.resolve()):
                    raise ValueError(tr('Move the linked Amethyst staging directory outside this prefix before relocating it.'))
            if prefix_changed and action == "move":
                if (not (old / "drive_c").is_dir() or old.is_symlink() or old.resolve() == new.resolve()
                        or new.resolve().is_relative_to(old.resolve())
                        or Path(context["plugin_dir"]).resolve().is_relative_to(old.resolve())):
                    raise ValueError(tr('Choose an existing prefix and a separate destination.'))
                if new.exists() and (not new.is_dir() or new.is_symlink() or any(new.iterdir())):
                    raise ValueError(tr('Move destination must be empty.'))
                if new.with_name(new.name + ".amethyst-move").exists():
                    raise ValueError(tr('A previous move directory exists; recover it before continuing.'))
            snapshots = {}
            for game, profile in links:
                from amethyst_backend.game_launcher import _state_files
                for path in [*_state_files(game), Path(game._settings_file)]:
                    snapshots[str(path)] = base64.b64encode(path.read_bytes()).decode() if path.exists() else None
            transaction = {"before": before, "snapshots": snapshots, "updated": {}, "old": str(old), "new": str(new),
                           "moving": prefix_changed and action == "move", "committed": False}
            journal = pending(context)
            journal.parent.mkdir(parents=True, exist_ok=True)
            if structural:
                write_atomic_text(journal, json.dumps(transaction))
            try:
                if prefix_changed and action == "move":
                    if new.exists():
                        new.rmdir()
                    temporary = new.with_name(new.name + ".amethyst-move")
                    if temporary.exists():
                        raise ValueError(tr('A previous move directory exists; recover it before continuing.'))
                    new.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copytree(old, temporary, symlinks=True)
                    temporary.rename(new)
                    for key in affected:
                        row = data["entries"][key]
                        for field in ("executable", "game_path"):
                            path = Path(row[field])
                            if path.is_relative_to(old):
                                row[field] = str(new / path.relative_to(old))
                        row.update(prefix_path=str(new), compat_data_path=str(new))
                    for field in ("executable", "game_path"):
                        path = Path(proposed[field])
                        if path.is_relative_to(old):
                            proposed[field] = str(new / path.relative_to(old))
                for key in affected:
                    data["entries"][key]["runner"] = copy.deepcopy(proposed["runner"])
                data["entries"][entry_id] = proposed
                for game, profile in links:
                    linked_id = game.get_saved_launcher_id("plugin_entry_id")
                    target = data["entries"][linked_id]
                    path_changes = {"prefix_path": Path(target["prefix_path"]), "game_path": Path(target["game_path"])}
                    plan = plan_reconfigure_game(game, profile, path_changes)
                    reconfigure_game(game, profile, path_changes, plan["revision"], False,
                                     "application_launcher", context["log"], lambda *args: None)
                    transaction["updated"] = {filename: base64.b64encode(Path(filename).read_bytes()).decode()
                                              if Path(filename).exists() else None for filename in snapshots}
                    write_atomic_text(journal, json.dumps(transaction))
                save(context, data)
                if structural:
                    transaction["committed"] = True
                    write_atomic_text(journal, json.dumps(transaction))
            except Exception:
                if structural:
                    _restore(context, transaction)
                raise
            if structural:
                _finish_move(transaction)
                journal.unlink()
            return record(data, entry_id)


def _finish_move(transaction):
    old, new = Path(transaction["old"]), Path(transaction["new"])
    if transaction["moving"] and old.exists() and (new / "drive_c").exists():
        shutil.rmtree(old)


def _restore(context, transaction):
    from Utils.atomic_write import write_atomic
    for filename, original in transaction["snapshots"].items():
        path = Path(filename)
        raw = base64.b64encode(path.read_bytes()).decode() if path.exists() else None
        if raw != original and raw != transaction.get("updated", {}).get(filename, original):
            raise ValueError(tr('A linked configuration changed after the interrupted operation. Both prefixes and recovery records were retained.'))
    for filename, raw in transaction["snapshots"].items():
        path = Path(filename)
        if raw is None:
            path.unlink(missing_ok=True)
        else:
            write_atomic(path, base64.b64decode(raw))
    save(context, transaction["before"])
    if transaction["moving"]:
        new = Path(transaction["new"])
        shutil.rmtree(new.with_name(new.name + ".amethyst-move"), ignore_errors=True)
        shutil.rmtree(new, ignore_errors=True)
    pending(context).unlink(missing_ok=True)


def recover(context):
    removal = pending_removal(context)
    if removal.exists():
        transaction = json.loads(removal.read_text())
        source, removed = Path(transaction["source"]), Path(transaction["removed"])
        with prefix_lock(source), library(context, write=True) as data:
            assert_idle({"prefix_path": str(source)})
            if removed.exists():
                stat = removed.lstat()
                if removed.is_symlink() or [stat.st_dev, stat.st_ino] != transaction["identity"]:
                    raise ValueError(tr('Prefix removal destination changed; files were retained.'))
                if transaction["id"] in data["entries"]:
                    if source.exists():
                        raise ValueError(tr('Prefix recovery destination already exists; both folders were retained.'))
                    removed.rename(source)
                else:
                    shutil.rmtree(removed)
            removal.unlink()
    journal = pending(context)
    if not journal.exists():
        return
    transaction = json.loads(journal.read_text())
    with library(context, write=True), contextlib.ExitStack() as stack:
        for prefix in sorted({transaction["old"], transaction["new"]}):
            stack.enter_context(prefix_lock(prefix))
        assert_idle({"prefix_path": transaction["old"]})
        assert_idle({"prefix_path": transaction["new"]})
        from Utils.deployment.locking import game_mutation_lock
        links = linked_profiles(context["provider_id"], list(transaction["before"]["entries"]))
        for game in sorted({str(g.get_profile_root()): g for g, _ in links}.values(), key=lambda g: str(g.get_profile_root())):
            stack.enter_context(game_mutation_lock(game))
            if game.get_deploy_active():
                raise ValueError(tr('Restore linked profiles before recovering the interrupted prefix change.'))
        if transaction["committed"]:
            _finish_move(transaction)
            journal.unlink()
        else:
            _restore(context, transaction)
