from __future__ import annotations

import fcntl
import json
import re
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

from Utils.atomic_write import write_atomic_text
from Utils.github.repositories import normalize_repository


def normalize_source(url, branch="", folder="", enabled=True):
    source = normalize_repository(url, branch, folder, enabled)
    if (source["repository"].lower(), source["branch"], source["folder"]) == (
            "chrisdkn/amethyst-mod-manager", "Resources", "Custom Handlers"):
        source["id"] = "builtin"
    return source


DEFAULT_SOURCE = normalize_source(
    "https://github.com/ChrisDKN/Amethyst-Mod-Manager", "Resources", "Custom Handlers")


def safe_filename(name):
    return (name if isinstance(name, str) and name.endswith(".json")
            and "/" not in name and "\\" not in name and ".." not in name else None)


def _source_id(value):
    return isinstance(value, str) and re.fullmatch(r"builtin|[0-9a-f]{32}", value)


def _definition(raw):
    definition = json.loads(raw)
    if (not isinstance(definition, dict)
            or not isinstance(definition.get("name"), str) or not definition["name"].strip()
            or not isinstance(definition.get("exe_name"), str) or not definition["exe_name"].strip()):
        raise ValueError("Handler must contain a game name and executable name.")
    gid = definition.get("game_id")
    if not gid:
        gid = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in definition["name"])
        gid = gid.strip("_") or "custom_game"
    if (not isinstance(gid, str) or not gid or gid in (".", "..")
            or any(c in gid for c in ("/", "\\")) or any(ord(c) < 32 for c in gid)):
        raise ValueError("Invalid handler game ID.")
    return gid, definition


def listing_url(source):
    url = (f"https://api.github.com/repos/{source['repository']}/contents/"
           f"{quote(source['folder'], safe='/')}")
    if source["branch"]:
        url += "?ref=" + quote(source["branch"], safe="")
    return url


class HandlerSources:
    def __init__(self, root=None, handlers=None):
        from Utils.config_paths import get_config_dir, get_custom_games_dir
        self.root = Path(root) if root is not None else get_config_dir() / "handler_store"
        self.handlers = Path(handlers) if handlers is not None else get_custom_games_dir()

    @contextmanager
    def _lock(self):
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / "lock").open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("Another handler repository operation is running.") from None
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _state(self):
        try:
            state = json.loads((self.root / "repositories.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            state = {}
        if not isinstance(state, dict):
            raise ValueError("Invalid handler repository configuration.")
        sources = state.setdefault("sources", [dict(DEFAULT_SOURCE)])
        origins = state.setdefault("origins", {})
        catalog = state.setdefault("catalog", {})
        selected = state.setdefault("selected", {})
        names = state.setdefault("names", {})
        if (not isinstance(sources, list) or not isinstance(origins, dict)
                or not isinstance(catalog, dict) or not isinstance(selected, dict)
                or not isinstance(names, dict)):
            raise ValueError("Invalid handler repository configuration.")
        normalized, seen = [], set()
        for item in sources:
            if not isinstance(item, dict):
                raise ValueError("Invalid handler repository configuration.")
            source = normalize_source(item.get("url"), item.get("branch", ""),
                                      item.get("folder", ""), item.get("enabled", True))
            if source["id"] != item.get("id", source["id"]) or source["id"] in seen:
                raise ValueError("Invalid or duplicate handler repository.")
            seen.add(source["id"])
            normalized.append(source)
        if "builtin" not in seen:
            normalized.insert(0, dict(DEFAULT_SOURCE))
        state["sources"] = normalized
        if any(not safe_filename(name) or not _source_id(sid) for name, sid in origins.items()):
            raise ValueError("Invalid handler repository origins.")
        for candidates in catalog.values():
            if not isinstance(candidates, dict):
                raise ValueError("Invalid handler catalog.")
            for sid, entry in candidates.items():
                if (not _source_id(sid) or not isinstance(entry, dict)
                        or not safe_filename(entry.get("filename"))):
                    raise ValueError("Invalid handler catalog entry.")
        if any(not isinstance(name, str) or not name for name in names.values()):
            raise ValueError("Invalid handler game names.")
        if any(not isinstance(key, str) for key in selected.values()):
            raise ValueError("Invalid handler source selection.")
        return state

    def _write(self, state):
        write_atomic_text(self.root / "repositories.json", json.dumps(state, indent=2))

    def _candidate_path(self, source_id, filename):
        if not _source_id(source_id) or not safe_filename(filename):
            raise ValueError("Invalid handler source path.")
        path = self.root / "definitions" / source_id / filename
        if path.is_symlink() or not path.resolve().is_relative_to(self.root.resolve()):
            raise ValueError("Unsafe handler source path.")
        return path

    def sources(self):
        return self._state()["sources"]

    def save_source(self, url, branch="", folder="", *, enabled=True, source_id=None):
        source = normalize_source(url, branch, folder, enabled)
        with self._lock():
            state = self._state()
            sources = state["sources"]
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
                if source_id != source["id"]:
                    if any(source["id"] in candidates for candidates in state["catalog"].values()):
                        raise ValueError("This repository has retained downloads. Add it again as a new source.")
                    for candidates in state["catalog"].values():
                        entry = candidates.get(source_id)
                        if entry:
                            old_path = self._candidate_path(source_id, entry["filename"])
                            write_atomic_text(self._candidate_path(source["id"], entry["filename"]),
                                              old_path.read_text(encoding="utf-8"))
                            candidates[source["id"]] = candidates.pop(source_id)
                            entry.update({key: source[key] for key in ("repository", "branch", "folder")})
                    for gid, selected in state["selected"].items():
                        if selected == source_id:
                            state["selected"][gid] = source["id"]
                    for name, owner in state["origins"].items():
                        if owner == source_id:
                            state["origins"][name] = source["id"]
                sources[sources.index(previous)] = source
            self._write(state)

    def remove_source(self, source_id):
        if source_id == "builtin":
            raise ValueError("The built-in repository can be disabled but not removed.")
        with self._lock():
            state = self._state()
            state["sources"] = [s for s in state["sources"] if s["id"] != source_id]
            self._write(state)

    def _options(self, state):
        options = {}
        for path in sorted(self.handlers.glob("*.json")):
            try:
                gid, definition = _definition(path.read_text(encoding="utf-8"))
                key = "local:" + path.name
                options.setdefault(gid, {})[key] = {
                    "key": key, "filename": path.name, "definition": definition,
                    "path": path, "source_id": None,
                }
            except (OSError, ValueError):
                continue
        order = {source["id"]: i for i, source in enumerate(state["sources"])}
        for gid, candidates in state["catalog"].items():
            for sid in sorted(candidates, key=lambda sid: (order.get(sid, len(order)), sid)):
                entry = candidates[sid]
                try:
                    path = self._candidate_path(sid, entry["filename"])
                    actual_gid, definition = _definition(path.read_text(encoding="utf-8"))
                    if actual_gid != gid:
                        continue
                    game_options = options.setdefault(gid, {})
                    for key, local in list(game_options.items()):
                        if local["source_id"] is None and state["origins"].get(local["filename"]) == sid:
                            del game_options[key]
                    game_options[sid] = {
                        **entry, "key": sid, "filename": entry["filename"], "definition": definition,
                        "path": path, "source_id": sid,
                    }
                except (OSError, ValueError):
                    continue
        return options

    @staticmethod
    def _chosen(state, gid, options):
        return options.get(state["selected"].get(gid)) or next(iter(options.values()))

    def games(self):
        state = self._state()
        return [{"game_id": gid, "selected": self._chosen(state, gid, options)["key"],
                 "name": state["names"].get(gid, self._chosen(state, gid, options)["definition"]["name"]),
                 "options": list(options.values())}
                for gid, options in sorted(self._options(state).items())]

    def definitions(self):
        state = self._state()
        definitions = []
        for gid, options in self._options(state).items():
            chosen = self._chosen(state, gid, options)
            definition = dict(chosen["definition"])
            definition["name"] = state["names"].get(gid, definition["name"])
            definition["_source_file"] = str(chosen["path"])
            definition["_repository_source"] = chosen["source_id"]
            definitions.append(definition)
        return definitions

    def selected_handler(self, game_id):
        state = self._state()
        options = self._options(state).get(game_id)
        return self._chosen(state, game_id, options) if options else None

    def select(self, game_id, key):
        with self._lock():
            state = self._state()
            options = self._options(state).get(game_id, {})
            if key not in options:
                raise ValueError("This handler source is no longer available.")
            changed = self._chosen(state, game_id, options)["key"] != key
            state["names"].setdefault(game_id, self._chosen(state, game_id, options)["definition"]["name"])
            state["selected"][game_id] = key
            self._write(state)
            return changed

    def save_local(self, definition, *, check=None):
        raw = json.dumps({key: value for key, value in definition.items() if not key.startswith("_")},
                         indent=2, ensure_ascii=False)
        gid, _ = _definition(raw)
        filename = gid + ".json"
        if not safe_filename(filename):
            raise ValueError("Invalid local handler filename.")
        with self._lock():
            if check is not None:
                check()
            state = self._state()
            path = self.handlers / filename
            if path.is_symlink():
                raise ValueError("Cannot replace a symlinked handler.")
            previous = path.read_text() if path.exists() else None
            write_atomic_text(path, raw)
            state["origins"].pop(filename, None)
            state["selected"][gid] = "local:" + filename
            state["names"][gid] = definition["name"]
            try:
                self._write(state)
            except Exception:
                if previous is None:
                    path.unlink(missing_ok=True)
                else:
                    write_atomic_text(path, previous)
                raise
            return path

    def delete_game(self, game_id, *, check=None):
        with self._lock():
            if check is not None:
                check()
            state = self._state()
            for path in self.handlers.glob("*.json"):
                try:
                    gid, _ = _definition(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if gid == game_id:
                    path.unlink()
                    state["origins"].pop(path.name, None)
            for sid, entry in state["catalog"].pop(game_id, {}).items():
                self._candidate_path(sid, entry["filename"]).unlink(missing_ok=True)
            state["selected"].pop(game_id, None)
            state["names"].pop(game_id, None)
            self._write(state)

    def write_handler(self, source, filename, raw, *, filenames=None):
        if not safe_filename(filename):
            raise ValueError("Unsafe handler filename.")
        gid, definition = _definition(raw)
        with self._lock():
            state = self._state()
            if not any(s == source and s["enabled"] for s in state["sources"]):
                raise ValueError("Handler repository is no longer enabled.")
            options = self._options(state).get(gid, {})
            previous = self._chosen(state, gid, options) if options else None
            state["names"].setdefault(gid, previous["definition"]["name"] if previous else definition["name"])
            sid = source["id"]
            candidates = state["catalog"].setdefault(gid, {})
            existing = candidates.get(sid)
            if (existing and existing["filename"] != filename
                    and (filenames is None or existing["filename"] in filenames)):
                raise ValueError(f"Repository contains multiple files for game ID {gid}.")
            for other_gid, other_candidates in state["catalog"].items():
                entry = other_candidates.get(sid)
                if other_gid != gid and entry and entry["filename"] == filename:
                    raise ValueError("Changing a downloaded handler's game ID is not supported.")
            legacy = self.handlers / filename
            if (gid not in state["selected"] and filename not in state["origins"]
                    and sid == "builtin" and legacy.is_file()):
                try:
                    legacy_gid, legacy_definition = _definition(legacy.read_text(encoding="utf-8"))
                    if (legacy_gid == gid and (legacy_definition == definition
                                               or "version" in legacy_definition and "editable" in legacy_definition)):
                        state["origins"][filename] = sid
                except (OSError, ValueError):
                    pass
            replacing_legacy = (previous and previous["path"].parent == self.handlers
                                and state["origins"].get(previous["filename"]) == sid)
            if gid not in state["selected"]:
                state["selected"][gid] = (sid if not previous or replacing_legacy else previous["key"])
            path = self._candidate_path(sid, filename)
            if not path.is_file() or path.read_text(encoding="utf-8") != raw:
                write_atomic_text(path, raw)
            candidates[sid] = {"filename": filename, "name": definition["name"],
                               "repository": source["repository"], "branch": source["branch"],
                               "folder": source["folder"]}
            self._write(state)
            current_options = self._options(state)[gid]
            current = self._chosen(state, gid, current_options)
            return previous != current
