from __future__ import annotations

import fcntl
import hashlib
import json
import re
import shutil
import uuid
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from amethyst_plugins.catalog import compatibility_error, parse_plugin, version_key
from Utils.atomic_write import write_atomic_text


API = "https://api.github.com/repos/ChrisDKN/Amethyst-Mod-Manager"
RAW = "https://raw.githubusercontent.com/ChrisDKN/Amethyst-Mod-Manager"
RESOURCE_PATH = "Plugins/v2"
STORAGE_FILES = ("settings.json", "data.json")
MAX_FILES = 1024
MAX_BYTES = 64 * 1024 * 1024


def _id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,99}", value):
        raise ValueError("Invalid plugin id.")
    return value


def _relative(value):
    if (not isinstance(value, str) or not value or "\\" in value
            or any(p in ("", ".", "..") for p in value.split("/"))
            or PurePosixPath(value).is_absolute()):
        raise ValueError("Unsafe plugin path.")
    return Path(value)


def _sha(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value):
        raise ValueError("Invalid Git object id.")
    return value


def _blob(data):
    return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()


def _json(path):
    if path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("Plugin store metadata is too large.")
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path, data):
    write_atomic_text(path, json.dumps(data, ensure_ascii=True, indent=2))


def _fetch(url, *, force=False, fresh=False):
    from Utils.github.cache import fetch
    data = fetch(url, accept="application/vnd.github+json" if url.startswith(API) else "*/*",
                 min_interval=0 if fresh else 3600, force=force, allow_stale=not fresh)
    if data is None:
        raise OSError("Could not contact GitHub. Check your connection or GitHub's rate limit.")
    return data


class PluginStore:
    def __init__(self, root=None, plugins=None):
        from Utils.config_paths import get_config_dir, get_plugins_dir
        self.root = Path(root) if root is not None else get_config_dir() / "plugin_store"
        self.plugins = Path(plugins) if plugins is not None else get_plugins_dir()

    @contextmanager
    def _lock(self):
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / "lock").open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("Another plugin store operation is running.") from None
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _state(self):
        try:
            state = _json(self.root / "state.json")
        except FileNotFoundError:
            state = {}
        if not isinstance(state, dict):
            raise ValueError("Invalid plugin store state.")
        for key in ("pending", "saved", "backups"):
            if not isinstance(state.setdefault(key, {}), dict):
                raise ValueError("Invalid plugin store state.")
        return state

    def pending(self):
        return self._state()["pending"]

    def _internal(self, area, token):
        if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{32}", token):
            raise ValueError("Invalid plugin store operation id.")
        path = self.root / area / token
        if path.is_symlink() or not path.resolve().is_relative_to(self.root.resolve()):
            raise ValueError("Unsafe plugin store directory.")
        return path

    def _target(self, name):
        relative = _relative(name)
        if not (len(relative.parts) == 1 or
                len(relative.parts) == 2 and relative.parts[0] == "Headless"):
            raise ValueError("Invalid installed plugin directory.")
        path = self.plugins / relative
        if path.is_symlink() or not path.resolve().is_relative_to(self.plugins.resolve()):
            raise ValueError("Unsafe installed plugin directory.")
        return path

    def installed(self):
        found, errors = {}, []
        paths = sorted(self.plugins.glob("*/plugin.json"))
        paths += sorted((self.plugins / "Headless").glob("*/plugin.json"))
        for path in paths:
            try:
                self._target(path.parent.relative_to(self.plugins).as_posix())
                data = _json(path)
                pid = _id(data.get("id"))
                found.setdefault(pid, []).append({"manifest": data, "directory": path.parent})
            except (OSError, ValueError, TypeError, AttributeError) as exc:
                errors.append(f"{path.parent.name}: {exc}")
        return found, errors

    def catalogue(self, force=False):
        with self._lock():
            warning = ""
            listing_errors = []
            try:
                ref = json.loads(_fetch(f"{API}/git/ref/heads/Resources", force=force, fresh=True))
                commit = _sha(ref["object"]["sha"])
                listing = json.loads(_fetch(f"{API}/contents/{RESOURCE_PATH}?ref={commit}", force=force))
                if not isinstance(listing, list):
                    raise ValueError("Invalid plugin catalogue listing.")
                entries = []
                for item in listing:
                    if not isinstance(item, dict) or item.get("type") != "dir":
                        continue
                    try:
                        folder = item["name"]
                        if len(_relative(folder).parts) != 1:
                            raise ValueError("Invalid plugin folder.")
                        raw = _fetch(f"{RAW}/{commit}/{RESOURCE_PATH}/{quote(folder, safe='')}/plugin.json")
                        if len(raw) > 65536:
                            raise ValueError("Manifest exceeds 64 KiB.")
                        manifest = json.loads(raw)
                        _id(manifest.get("id"))
                        entries.append({"folder": folder, "tree": _sha(item["sha"]),
                                        "commit": commit, "manifest": manifest})
                    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
                        listing_errors.append(f"{item.get('name', '?')}: {exc}")
                _write(self.root / "catalogue.json", entries)
            except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
                warning = str(exc)
                try:
                    entries = _json(self.root / "catalogue.json")
                    warning += " Showing the cached catalogue."
                except FileNotFoundError:
                    entries = []
            errors, seen, duplicates = listing_errors, set(), set()
            for entry in entries:
                pid = entry["manifest"]["id"]
                if pid in seen:
                    duplicates.add(pid)
                seen.add(pid)
            for entry in entries:
                entry["error"] = ""
                try:
                    if entry["manifest"]["id"] in duplicates:
                        raise ValueError("Duplicate plugin id in the catalogue.")
                    plugin, _ = parse_plugin(entry["manifest"], self.root / "metadata",
                                             check_files=False, check_compatibility=False, store=True)
                    entry["error"] = compatibility_error(plugin)
                except (ValueError, TypeError) as exc:
                    entry["error"] = str(exc)
                    errors.append(f"{entry['folder']}: {exc}")
            return entries, "\n".join(filter(None, (warning, *errors)))

    def _single(self, pid):
        matches = self.installed()[0].get(pid, [])
        if len(matches) > 1:
            raise ValueError("Duplicate installed plugin id. Resolve the duplicate folders first.")
        return matches[0] if matches else None

    def _package(self, token, pid):
        base = self._internal("packages", token)
        files = base / "files"
        receipt = _json(base / "receipt.json")
        expected = receipt["files"]
        actual = {}
        total = 0
        for path in files.rglob("*"):
            if path.is_symlink():
                raise ValueError("Plugin packages cannot contain symlinks.")
            if path.is_file():
                if path.stat().st_size > MAX_BYTES:
                    raise ValueError("Plugin package is too large.")
                raw = path.read_bytes()
                total += len(raw)
                if total > MAX_BYTES or len(actual) >= MAX_FILES:
                    raise ValueError("Plugin package is too large.")
                actual[path.relative_to(files).as_posix()] = _blob(raw)
        if actual != expected:
            raise ValueError("The staged plugin package is incomplete or has changed.")
        manifest = files / "plugin.json"
        if manifest.stat().st_size > 65536:
            raise ValueError("Manifest exceeds 64 KiB.")
        plugin, _ = parse_plugin(_json(manifest), files, store=True)
        if plugin.id != pid:
            raise ValueError("Plugin id does not match the queued operation.")
        return files, plugin

    def queue_install(self, entry, *, replace_local=False):
        with self._lock():
            if entry.get("error"):
                raise ValueError(entry["error"])
            if len(_relative(entry["folder"]).parts) != 1:
                raise ValueError("Invalid plugin folder.")
            pid = _id(entry["manifest"]["id"])
            state = self._state()
            if pid in state["pending"]:
                raise ValueError("Cancel the pending change before queuing another.")
            plugin, _ = parse_plugin(entry["manifest"], self.root / "metadata", check_files=False, store=True)
            installed = self._single(pid)
            if installed:
                version = installed["manifest"].get("version", "")
                if not version and not replace_local:
                    raise ValueError("Confirm replacement of the locally installed plugin first.")
                if version and version_key(version) >= version_key(plugin.version):
                    raise ValueError("The installed plugin is already up to date or newer.")
                target = installed["directory"].relative_to(self.plugins).as_posix()
            else:
                target = pid
                if self._target(target).exists():
                    raise ValueError("The destination folder is already occupied.")
            commit, tree = _sha(entry["commit"]), _sha(entry["tree"])
            listing = json.loads(_fetch(f"{API}/git/trees/{tree}?recursive=1"))
            if listing.get("truncated") is not False:
                raise ValueError("Incomplete plugin file listing.")
            blobs, modes, total = {}, {}, 0
            for item in listing["tree"]:
                relative = _relative(item["path"])
                if item["type"] == "tree" and item["mode"] == "040000":
                    continue
                if item["type"] != "blob" or item["mode"] not in ("100644", "100755"):
                    raise ValueError("Plugin packages cannot contain symlinks or submodules.")
                total += item["size"]
                if total > MAX_BYTES or len(blobs) >= MAX_FILES or relative.as_posix() in blobs:
                    raise ValueError("Plugin package is too large or contains duplicate files.")
                blobs[relative.as_posix()] = _sha(item["sha"])
                modes[relative.as_posix()] = item["mode"]
            token = uuid.uuid4().hex
            base = self._internal("packages", token)
            try:
                for name, sha in blobs.items():
                    url = f"{RAW}/{commit}/{RESOURCE_PATH}/{quote(entry['folder'], safe='')}/{quote(name, safe='/')}"
                    raw = _fetch(url)
                    if len(raw) > MAX_BYTES or _blob(raw) != sha:
                        raise ValueError("Downloaded file does not match the catalogue revision.")
                    path = base / "files" / _relative(name)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(raw)
                    path.chmod(0o755 if modes[name] == "100755" else 0o644)
                _write(base / "receipt.json", {"files": blobs})
                _, downloaded = self._package(token, pid)
                if downloaded.version != plugin.version or _json(base / "files" / "plugin.json") != entry["manifest"]:
                    raise ValueError("Downloaded manifest does not match the catalogue.")
                state["pending"][pid] = {"action": "install", "target": target, "package": token,
                                         "version": plugin.version, "name": plugin.name,
                                         "manifest": entry["manifest"], "had_target": bool(installed)}
                _write(self.root / "state.json", state)
            except BaseException:
                shutil.rmtree(base, ignore_errors=True)
                raise

    def queue_remove(self, pid, *, delete_data=False):
        with self._lock():
            pid = _id(pid)
            state = self._state()
            if pid in state["pending"]:
                raise ValueError("Cancel the pending change before queuing another.")
            installed = self._single(pid)
            if installed is None:
                raise ValueError("The plugin is no longer installed.")
            state["pending"][pid] = {"action": "remove", "delete_data": bool(delete_data),
                                     "target": installed["directory"].relative_to(self.plugins).as_posix(),
                                     "name": installed["manifest"].get("name", pid), "had_target": True}
            _write(self.root / "state.json", state)

    def cancel(self, pid):
        with self._lock():
            state = self._state()
            operation = state["pending"].pop(_id(pid), None)
            _write(self.root / "state.json", state)
            if operation and operation.get("package"):
                shutil.rmtree(self._internal("packages", operation["package"]), ignore_errors=True)

    def _cleanup(self, transaction):
        for token in transaction.get("discard", []):
            path = self._internal("backups", token)
            if path.exists():
                shutil.rmtree(path)
        if transaction["operation"].get("delete_data"):
            pid = _id(transaction["id"])
            for folder in ("plugin_settings", "plugin_data"):
                (self.root.parent / folder / f"{pid}.json").unlink(missing_ok=True)
            for area in ("failed", "work"):
                for candidate in (self.root / area).glob("*"):
                    path = self._internal(area, candidate.name)
                    try:
                        manifest = _json(path / "plugin.json")
                    except (OSError, ValueError):
                        continue
                    if isinstance(manifest, dict) and manifest.get("id") == pid:
                        shutil.rmtree(path)
        token = transaction["token"]
        shutil.rmtree(self._internal("work", token), ignore_errors=True)
        package = transaction["operation"].get("package")
        if package:
            shutil.rmtree(self._internal("packages", package), ignore_errors=True)
        (self.root / "transaction.json").unlink(missing_ok=True)

    def _recover(self):
        try:
            transaction = _json(self.root / "transaction.json")
        except FileNotFoundError:
            return
        if self._state().get("completed") == transaction["token"]:
            self._cleanup(transaction)
            return
        target = self._target(transaction["operation"]["target"])
        backup = self._internal("backups", transaction["token"])
        work = self._internal("work", transaction["token"])
        if backup.exists() or not transaction["operation"]["had_target"] and not work.exists():
            if target.exists():
                manifest = _json(target / "plugin.json")
                if manifest.get("id") != transaction["id"]:
                    raise ValueError("Recovery destination has changed; the backup was retained.")
                failed = self._internal("failed", transaction["token"])
                failed.parent.mkdir(parents=True, exist_ok=True)
                target.rename(failed)
            if backup.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                backup.rename(target)
        shutil.rmtree(work, ignore_errors=True)
        (self.root / "transaction.json").unlink()

    def _apply(self, pid, operation, state):
        target = self._target(operation["target"])
        installed = self._single(pid)
        if operation["had_target"]:
            if installed is None or installed["directory"] != target:
                raise ValueError("The installed plugin moved or was removed. Cancel and queue the change again.")
        elif installed or target.exists():
            raise ValueError("The plugin destination changed. Cancel and queue the change again.")
        token = uuid.uuid4().hex
        work = self._internal("work", token)
        backup = self._internal("backups", token)
        try:
            if operation["action"] == "install":
                files, _ = self._package(operation["package"], pid)
                work.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(files, work)
                saved = state["saved"].get(pid)
                source = target if installed else self._internal("backups", saved) if saved else None
                for name in STORAGE_FILES:
                    if source is not None and (source / name).exists():
                        path = source / name
                        if path.is_symlink() or not path.is_file():
                            raise ValueError("Saved plugin data must be regular files.")
                        shutil.copy2(path, work / name)
            elif operation["action"] != "remove":
                raise ValueError("Invalid pending plugin action.")
            discard = []
            if operation["action"] == "remove" and operation.get("delete_data"):
                discard = [*state["backups"].get(pid, []), token]
            transaction = {"id": pid, "token": token, "operation": operation, "discard": discard}
            _write(self.root / "transaction.json", transaction)
        except BaseException:
            shutil.rmtree(work, ignore_errors=True)
            raise
        if installed:
            backup.parent.mkdir(parents=True, exist_ok=True)
            target.rename(backup)
            state["saved"][pid] = token
            state["backups"].setdefault(pid, []).append(token)
        if operation["action"] == "install":
            target.parent.mkdir(parents=True, exist_ok=True)
            work.rename(target)
        if discard:
            state["saved"].pop(pid, None)
            state["backups"].pop(pid, None)
        state["pending"].pop(pid)
        state["completed"] = token
        _write(self.root / "state.json", state)
        self._cleanup(transaction)

    def apply_pending(self, *, instances=None):
        messages = []
        with self._lock():
            if not self.pending() and not (self.root / "transaction.json").exists():
                return messages
            if instances is None:
                from Utils.processes.instances import running_instances
                instances = running_instances
            if instances():
                return ["Plugin changes deferred because another Amethyst instance is running."]
            self._recover()
            for pid, operation in list(self.pending().items()):
                try:
                    self._apply(_id(pid), operation, self._state())
                    messages.append(f"{operation['name']}: {operation['action']} applied.")
                except Exception as exc:
                    self._recover()
                    messages.append(f"{operation.get('name', pid)}: {exc} Change remains pending.")
        return messages
