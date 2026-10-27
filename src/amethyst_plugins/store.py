from __future__ import annotations

import fcntl
import hashlib
from Utils import json_limits as json
import re
import shutil
import uuid
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from amethyst_plugins.catalog import compatibility_error, parse_plugin, version_key, _persistent_directories
from amethyst_plugins.sources import DEFAULT_SOURCE, api_url, normalize_source, raw_url, read_source
from Utils.atomic_write import write_atomic_text


RESOURCE_PATH = "Plugins/v2"
STORAGE_FILES = ("settings.json", "data.json")
MAX_FILES = 1024
MAX_BYTES = 64 * 1024 * 1024
MAX_README_BYTES = 1024 * 1024


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
    return json.read_json(path, max_bytes=4 * 1024 * 1024)


def _write(path, data):
    write_atomic_text(path, json.dumps(data, max_bytes=4 * 1024 * 1024, ensure_ascii=True, indent=2))


def _fetch(url, *, force=False, fresh=False):
    from Utils.github.cache import fetch
    data = fetch(url, accept="application/vnd.github+json" if url.startswith("https://api.github.com/") else "*/*",
                 min_interval=0 if fresh else 3600, force=force, allow_stale=not fresh)
    if data is None:
        raise OSError("Could not contact GitHub. Check your connection or GitHub's rate limit.")
    return data


class PluginStore:
    def __init__(self, root=None, plugins=None):
        from Utils.config_paths import get_config_dir, get_plugins_dir
        self.root = Path(root) if root is not None else get_config_dir(create=False) / "plugin_store"
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
        for key in ("pending", "saved", "backups", "disabled", "origins"):
            if not isinstance(state.setdefault(key, {}), dict):
                raise ValueError("Invalid plugin store state.")
        return state

    def pending(self):
        return self._state()["pending"]

    def disabled_ids(self):
        disabled = self._state()["disabled"]
        if any(type(value) is not bool for value in disabled.values()):
            raise ValueError("Invalid plugin enablement state.")
        return {_id(pid) for pid, value in disabled.items() if value}

    def sources(self):
        try:
            data = _json(self.root / "repositories.json")
        except FileNotFoundError:
            return [dict(DEFAULT_SOURCE)]
        if not isinstance(data, list):
            raise ValueError("Invalid plugin repository list.")
        sources, seen = [], set()
        for item in data:
            source = read_source(item)
            if source["id"] in seen:
                raise ValueError("Duplicate plugin repository.")
            seen.add(source["id"])
            sources.append(source)
        if "builtin" not in seen:
            sources.insert(0, dict(DEFAULT_SOURCE))
        return sources

    def save_source(self, url, branch="", folder="", *, enabled=True, source_id=None):
        source = normalize_source(url, branch, folder, enabled)
        with self._lock():
            sources = self.sources()
            previous = next((s for s in sources if s["id"] == source_id), None)
            if source_id is not None and previous is None:
                raise ValueError("The repository no longer exists.")
            if source_id == "builtin" and source["id"] != "builtin":
                raise ValueError("The built-in repository cannot be edited.")
            if any(s["id"] == source["id"] and s["id"] != source_id for s in sources):
                raise ValueError("This repository, branch and folder are already configured.")
            if previous is None:
                sources.append(source)
            else:
                sources[sources.index(previous)] = source
            _write(self.root / "repositories.json", sources)

    def remove_source(self, source_id):
        if source_id == "builtin":
            raise ValueError("The built-in repository can be disabled but not removed.")
        with self._lock():
            _write(self.root / "repositories.json", [s for s in self.sources() if s["id"] != source_id])

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
        state = self._state()
        paths = sorted(self.plugins.glob("*/plugin.json"))
        paths += sorted((self.plugins / "Headless").glob("*/plugin.json"))
        for path in paths:
            try:
                self._target(path.parent.relative_to(self.plugins).as_posix())
                data = _json(path)
                pid = _id(data.get("id"))
                found.setdefault(pid, []).append({"manifest": data, "directory": path.parent,
                                                   "enabled": not state["disabled"].get(pid, False),
                                                   "origin": state["origins"].get(pid)})
            except (OSError, ValueError, TypeError, AttributeError) as exc:
                errors.append(f"{path.parent.name}: {exc}")
        return found, errors

    def _tree(self, source, sha, *, force=False):
        listing = json.loads(_fetch(f"{api_url(source)}/git/trees/{_sha(sha)}", force=force))
        if listing.get("truncated") is not False or not isinstance(listing.get("tree"), list):
            raise ValueError("Incomplete plugin directory listing.")
        return listing["tree"]

    def _source_catalogue(self, source, force):
        branch = source["branch"]
        if not branch:
            repository = json.loads(_fetch(api_url(source), force=force, fresh=True))
            branch = repository["default_branch"]
        revision = json.loads(_fetch(
            f"{api_url(source)}/commits/{quote(branch, safe='')}", force=force, fresh=True))
        commit = _sha(revision["sha"])
        tree = _sha(revision["commit"]["tree"]["sha"])
        listing = self._tree(source, tree, force=force)
        for part in source["folder"].split("/") if source["folder"] else ():
            item = next((item for item in listing if item.get("path") == part
                         and item.get("type") == "tree" and item.get("mode") == "040000"), None)
            if item is None:
                raise ValueError(f"Repository folder not found: {source['folder']}")
            tree = _sha(item["sha"])
            listing = self._tree(source, tree, force=force)
        if any(item.get("path") == "plugin.json" and item.get("type") == "blob"
               for item in listing):
            candidates = [(source["folder"], tree)]
        else:
            candidates = []
            for item in listing:
                if item.get("type") != "tree" or item.get("mode") != "040000":
                    continue
                folder = item["path"]
                if len(_relative(folder).parts) != 1:
                    raise ValueError("Invalid plugin folder.")
                child_tree = _sha(item["sha"])
                child_listing = self._tree(source, child_tree, force=force)
                if any(child.get("path") == "plugin.json" and child.get("type") == "blob"
                       for child in child_listing):
                    candidates.append(("/".join(filter(None, (source["folder"], folder))), child_tree))
        entries, errors = [], []
        for path, tree in candidates:
            try:
                location = "/".join(filter(None, (path, "plugin.json")))
                raw = _fetch(f"{raw_url(source)}/{commit}/{quote(location, safe='/')}", force=force)
                if len(raw) > 65536:
                    raise ValueError("Manifest exceeds 64 KiB.")
                manifest = json.loads(raw, max_bytes=65536)
                _id(manifest.get("id"))
                entries.append({"folder": path.rsplit("/", 1)[-1], "path": path,
                                "tree": tree, "commit": commit, "manifest": manifest,
                                "source": dict(source)})
            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                errors.append(f"{path or '/'}: {exc}")
        return entries, errors

    def _entry(self, entry, source=None):
        source = read_source(source or entry.get("source", DEFAULT_SOURCE))
        path = entry.get("path")
        if path is None:
            folder = entry["folder"]
            if len(_relative(folder).parts) != 1 or source["id"] != "builtin":
                raise ValueError("Invalid legacy plugin catalogue entry.")
            path = f"{RESOURCE_PATH}/{folder}"
        if path:
            _relative(path)
        elif not isinstance(path, str):
            raise ValueError("Invalid plugin path.")
        plugin, _ = parse_plugin(entry["manifest"], self.root / "metadata",
                                 check_files=False, check_compatibility=False, store=True)
        return {**entry, "source": source, "path": path,
                "commit": _sha(entry["commit"]), "tree": _sha(entry["tree"]),
                "error": compatibility_error(plugin)}

    def catalogue(self, force=False):
        with self._lock():
            entries, errors = [], []
            for source in self.sources():
                if not source["enabled"]:
                    continue
                label = f"{source['repository']} [{source['branch'] or 'default'}:{source['folder'] or '/'}]"
                cache = self.root / "catalogues" / f"{source['id']}.json"
                try:
                    found, warnings = self._source_catalogue(source, force)
                    _write(cache, found)
                    errors.extend(f"{label}: {warning}" for warning in warnings)
                except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
                    errors.append(f"{label}: {exc}")
                    try:
                        try:
                            found = _json(cache)
                        except FileNotFoundError:
                            if source["id"] != "builtin":
                                raise
                            found = _json(self.root / "catalogue.json")
                        if not isinstance(found, list):
                            raise ValueError("Invalid cached catalogue.")
                        errors.append(f"{label}: Showing the cached catalogue.")
                    except (OSError, ValueError, TypeError) as cache_exc:
                        if not isinstance(cache_exc, FileNotFoundError):
                            errors.append(f"{label}: {cache_exc}")
                        found = []
                valid, counts = [], {}
                for entry in found:
                    try:
                        entry = self._entry(entry, source)
                        pid = entry["manifest"]["id"]
                        counts[pid] = counts.get(pid, 0) + 1
                        valid.append(entry)
                    except (ValueError, KeyError, TypeError, AttributeError) as exc:
                        errors.append(f"{label}: {exc}")
                for entry in valid:
                    if counts[entry["manifest"]["id"]] > 1:
                        entry["error"] = "Duplicate plugin id in this repository source."
                    if entry["error"]:
                        errors.append(f"{label}: {entry['manifest']['id']}: {entry['error']}")
                entries.extend(valid)
            return entries, "\n".join(errors)

    def _single(self, pid):
        matches = self.installed()[0].get(pid, [])
        if len(matches) > 1:
            raise ValueError("Duplicate installed plugin id. Resolve the duplicate folders first.")
        return matches[0] if matches else None

    def readme(self, entry=None, directory=None):
        if entry is not None:
            entry = self._entry(entry)
            listing = self._tree(entry["source"], entry["tree"])
            item = next((item for item in listing if item.get("path", "").casefold() == "readme.md"
                         and item.get("type") == "blob" and item.get("mode") in ("100644", "100755")), None)
            if item is None:
                return None
            if item["size"] > MAX_README_BYTES:
                raise ValueError("Plugin README exceeds 1 MiB.")
            path = "/".join(filter(None, (entry["path"], item["path"])))
            location = f"{entry['commit']}/{quote(path, safe='/')}"
            raw = _fetch(f"{raw_url(entry['source'])}/{location}")
            if len(raw) > MAX_README_BYTES or _blob(raw) != _sha(item["sha"]):
                raise ValueError("Plugin README size or hash does not match the repository.")
            base = f"{entry['source']['url']}/blob/{location}"
        elif directory is not None:
            root = self._target(Path(directory).relative_to(self.plugins).as_posix()).resolve()
            path = next((path for path in sorted(root.iterdir()) if path.name.casefold() == "readme.md"
                         and path.resolve().is_relative_to(root) and path.is_file()), None)
            if path is None:
                return None
            with path.open("rb") as handle:
                raw = handle.read(MAX_README_BYTES + 1)
            if len(raw) > MAX_README_BYTES:
                raise ValueError("Plugin README exceeds 1 MiB.")
            base = path.as_uri()
        else:
            return None
        return raw.decode("utf-8-sig", errors="replace"), base

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
            entry = self._entry(entry)
            if entry["error"]:
                raise ValueError(entry["error"])
            source = entry["source"]
            pid = _id(entry["manifest"]["id"])
            state = self._state()
            if pid in state["pending"]:
                raise ValueError("Cancel the pending change before queuing another.")
            plugin, _ = parse_plugin(entry["manifest"], self.root / "metadata", check_files=False, store=True)
            installed = self._single(pid)
            if installed:
                version = installed["manifest"].get("version", "")
                origin = installed["origin"]
                same_source = bool(origin and origin["source"]["id"] == source["id"])
                if (not same_source or not version) and not replace_local:
                    raise ValueError("Confirm replacement from this repository first.")
                if same_source and version and version_key(version) >= version_key(plugin.version):
                    raise ValueError("The installed plugin is already up to date or newer.")
                target = installed["directory"].relative_to(self.plugins).as_posix()
            else:
                target = pid
                if self._target(target).exists():
                    raise ValueError("The destination folder is already occupied.")
            commit, tree = _sha(entry["commit"]), _sha(entry["tree"])
            listing = json.loads(_fetch(f"{api_url(source)}/git/trees/{tree}?recursive=1"))
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
                    location = "/".join(filter(None, (entry["path"], name)))
                    url = f"{raw_url(source)}/{commit}/{quote(location, safe='/')}"
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
                                         "manifest": entry["manifest"], "had_target": bool(installed),
                                         "origin": {"source": source, "commit": commit,
                                                    "path": entry["path"], "tree": tree}}
                _write(self.root / "state.json", state)
            except BaseException:
                shutil.rmtree(base, ignore_errors=True)
                raise

    def queue_enabled(self, pid, enabled):
        if type(enabled) is not bool:
            raise ValueError("Plugin enablement must be a boolean.")
        with self._lock():
            pid = _id(pid)
            state = self._state()
            if pid in state["pending"]:
                raise ValueError("Cancel the pending change before queuing another.")
            installed = self._single(pid)
            if installed is None:
                raise ValueError("The plugin is no longer installed.")
            if installed["enabled"] == enabled:
                raise ValueError("The plugin already has this enablement state.")
            state["pending"][pid] = {"action": "enable" if enabled else "disable",
                                     "target": installed["directory"].relative_to(self.plugins).as_posix(),
                                     "name": installed["manifest"].get("name", pid), "had_target": True}
            _write(self.root / "state.json", state)

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
        data_source = transaction.get("persistent_source")
        if data_source:
            source = self._internal("backups", data_source)
            for name in transaction.get("persistent_directories", []):
                name = _relative(name)
                if len(name.parts) != 1:
                    raise ValueError("Invalid persistent directory in recovery journal.")
                for donor in (work, self._internal("failed", transaction["token"])):
                    path = donor / name
                    if path.exists():
                        if (source / name).exists():
                            raise ValueError("Persistent plugin data recovery destination changed.")
                        source.mkdir(parents=True, exist_ok=True)
                        path.rename(source / name)
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
        if operation["action"] in ("enable", "disable"):
            if operation["action"] == "disable":
                state["disabled"][pid] = True
            else:
                state["disabled"].pop(pid, None)
            state["pending"].pop(pid)
            _write(self.root / "state.json", state)
            return
        token = uuid.uuid4().hex
        work = self._internal("work", token)
        backup = self._internal("backups", token)
        persistent = []
        persistent_source = None
        try:
            if operation["action"] == "install":
                files, _ = self._package(operation["package"], pid)
                work.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(files, work)
                saved = state["saved"].get(pid)
                source = target if installed else self._internal("backups", saved) if saved else None
                if source is not None and source.exists():
                    old = _json(source / "plugin.json")
                    if not isinstance(old, dict) or _id(old.get("id")) != pid:
                        raise ValueError("Saved plugin data belongs to another plugin.")
                    incoming, _ = parse_plugin(_json(work / "plugin.json"), work,
                                               check_compatibility=False)
                    retained = set(_persistent_directories(old)) | set(incoming.persistent_directories)
                    retained.update(state.get("persistent_directories", {}).get(pid, []))
                    for name in sorted(retained):
                        path = source / name
                        if path.is_symlink() or path.exists() and not path.is_dir():
                            raise ValueError("Persistent plugin data must be real directories.")
                        if (work / name).exists() or (work / name).is_symlink():
                            raise ValueError("Plugin package conflicts with persistent data.")
                        if path.exists():
                            persistent.append(name)
                    persistent_source = token if installed else saved
                    state.setdefault("persistent_directories", {})[pid] = sorted(retained)
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
            transaction = {"id": pid, "token": token, "operation": operation, "discard": discard,
                           "persistent_directories": persistent, "persistent_source": persistent_source}
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
            if persistent_source:
                source = self._internal("backups", persistent_source)
                for name in persistent:
                    (source / name).rename(work / name)
            target.parent.mkdir(parents=True, exist_ok=True)
            work.rename(target)
            state["origins"][pid] = operation.get("origin", {"source": dict(DEFAULT_SOURCE)})
        else:
            state["origins"].pop(pid, None)
            state["disabled"].pop(pid, None)
        if discard:
            state.get("persistent_directories", {}).pop(pid, None)
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
