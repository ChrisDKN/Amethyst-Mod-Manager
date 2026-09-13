from __future__ import annotations

import os
import posixpath
import re
import threading
import weakref
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path


_snapshot_indexes = weakref.WeakKeyDictionary()
_snapshot_indexes_lock = threading.Lock()


def data_subpath(game_type: str) -> str:
    return {
        "Morrowind": "Data Files",
        "OpenMW": "resources/vfs",
        "OblivionRemastered": "OblivionRemastered/Content/Dev/ObvData/Data",
    }.get(game_type, "Data")


def _normalise(path: str) -> str:
    value = posixpath.normpath(path.replace("\\", "/"))
    if value.startswith("/") or value == ".." or value.startswith("../"):
        raise ValueError(f"Path leaves the LOOT game view: {path}")
    return value


def _relative(path: str, root: str) -> str | None:
    if path.lower() == root.lower():
        return "."
    prefix = root.rstrip("/") + "/"
    return path[len(prefix):] if path.lower().startswith(prefix.lower()) else None


def _resolve_case(root: Path, relative: str) -> Path:
    current = root
    for part in Path(relative).parts:
        direct = current / part
        if direct.exists():
            current = direct
            continue
        try:
            current = next((p for p in current.iterdir() if p.name.lower() == part.lower()), direct)
        except OSError:
            return direct
    return current


def condition_directories(db, plugin_names: list[str], data_relative: str) -> set[str]:
    directories = set()
    metadata = [db.plugin_metadata(name, True, False) for name in plugin_names]
    items = list(db.general_messages(True, False))
    for meta in metadata:
        if meta is not None:
            for field in ("messages", "tags", "requirements", "incompatibilities",
                          "load_after_files", "dirty_info", "clean_info"):
                items.extend(getattr(meta, field))
    for item in items:
        paths = re.findall(r'"([^"\n]+)"', item.condition or "")
        if hasattr(item, "constraint"):
            paths.append(str(item.name))
        for path in paths:
            try:
                directories.add(_normalise(data_relative + "/" + posixpath.dirname(path)))
                if "\\" not in path:
                    directories.add(_normalise(data_relative + "/" + path))
            except ValueError:
                continue
    return directories


@dataclass(frozen=True)
class ProfileSources:
    snapshot: object
    game_root: Path
    data_root: Path
    mod_data_root: Path
    staging_root: Path
    overwrite_root: Path
    root_folder: Path
    deployed_profile: object = None
    _directory_indexes: dict = field(default_factory=dict, init=False, repr=False, compare=False)

    @classmethod
    def from_game(cls, snapshot, game):
        deployed = None
        if game.get_deploy_active() and game.get_last_deploy_mode() != "VFS":
            from Utils.filegraph.service import FileGraphService
            profile_dir = game.get_profile_root() / "profiles" / game.get_last_deployed_profile()
            deployed = FileGraphService.open_library(game, profile_dir).open_profile(profile_dir)
        return cls(
            snapshot, Path(game.get_game_path()),
            Path(game.get_vanilla_plugins_path()), Path(game.get_mod_data_path()),
            Path(game.get_effective_mod_staging_path()),
            Path(game.get_effective_overwrite_path()),
            Path(game.get_effective_root_folder_path()),
            deployed,
        )

    def destination(self, entry) -> Path | None:
        if entry.target == "game":
            return self.game_root / entry.destination
        if entry.target.startswith("custom:"):
            return Path(entry.target[7:]) / entry.destination
        return None

    @cached_property
    def deployed_paths(self) -> frozenset[str]:
        if self.deployed_profile is None:
            return frozenset()
        return frozenset(
            str(path).lower() for entry in self.deployed_profile.deployed_entries()
            if (path := self.destination(entry)) is not None)

    def original(self, path: Path) -> Path | None:
        path = Path(os.path.abspath(path))
        if str(path).lower() not in self.deployed_paths:
            return path
        root = self.deployed_profile.library.root
        relative = _relative(str(path), str(self.game_root))
        if relative is not None:
            for folder in ("Root_Backup", "filemap_backup", "custom_rules_backup"):
                backup = _resolve_case(root / folder, relative)
                if backup.is_file():
                    return backup
        backup = _resolve_case(root / "custom_deploy_backup", str(path).lstrip("/"))
        return backup if backup.is_file() else None

    def _logical_path(self, target: str, destination: str, data_relative: str) -> str | None:
        if target == "game":
            path = self.game_root / destination
        elif target.startswith("custom:"):
            path = Path(target[7:]) / destination
        else:
            return None
        relative = _relative(str(path), str(self.data_root))
        if relative is None and _relative(str(path), str(self.game_root)) is None:
            relative = _relative(str(path), str(self.mod_data_root))
        if relative is None:
            if _relative(str(path), str(self.game_root)) is None:
                return None
            relative = os.path.relpath(path, self.data_root)
        try:
            logical = _normalise(data_relative + "/" + relative)
        except ValueError:
            return None
        return "" if logical == "." else logical

    def _directory_index(self, data_relative: str):
        cached = self._directory_indexes.get(data_relative)
        if cached is not None:
            return cached
        routing = self.game_root, self.data_root, self.mod_data_root, data_relative
        with _snapshot_indexes_lock:
            try:
                shared = _snapshot_indexes.setdefault(self.snapshot, {})
            except TypeError:
                shared = {}
            cached = shared.get(routing)
        if cached is not None:
            self._directory_indexes[data_relative] = cached
            return cached
        parents = {}
        for candidate_id, _owner, target, destination, _conflict in self.snapshot.data_entries():
            parents.setdefault((target, posixpath.dirname(destination)), []).append(candidate_id)
        files = {}
        spellings = {}
        for (target, parent), ids in parents.items():
            logical = self._logical_path(target, parent, data_relative)
            if logical is None:
                continue
            files.setdefault(logical.lower(), []).extend(ids)
            while logical and logical.lower() not in spellings:
                spellings[logical.lower()] = logical
                logical = posixpath.dirname(logical)
        cached = files, spellings
        self._directory_indexes[data_relative] = cached
        with _snapshot_indexes_lock:
            shared[routing] = cached
        return cached

    def files(self, directories: set[str], data_relative: str):
        files, spellings = self._directory_index(data_relative)
        ids = set()
        for directory in directories:
            if directory in spellings:
                yield spellings[directory], None
            ids.update(files.get(directory, ()))
        winners = self.snapshot.deployment_entries(ids)
        for winner in sorted(winners, key=lambda entry: entry.candidate_id):
            logical = self._logical_path(winner.target, winner.destination, data_relative)
            if logical is None:
                continue
            if winner.mod_key == "[overwrite]":
                source_root = self.overwrite_root
            elif winner.mod_key == "[root_folder]":
                source_root = self.root_folder
            else:
                source_root = self.staging_root / winner.mod_name
            source = bytes(winner.source_rel).decode("utf-8", "surrogateescape")
            yield logical, source_root / _normalise(source)


class GameView:
    """Private files used by LOOT's loaders and metadata conditions."""

    def __init__(self, root: Path, game_type: str, game_root: Path,
                 data_root: Path, sources: ProfileSources | None = None):
        self.root = root / "game"
        self.local = root / "user/AppData/Local" / game_type
        self.data_relative = data_subpath(game_type)
        self.data = self.root / self.data_relative
        self.game_type = game_type
        self.game_root = game_root
        self.data_root = data_root
        self.sources = sources
        self._paths: dict[str, Path] = {}
        self._linked_sources: dict[str, Path] = {}
        self._source_state: dict[Path, tuple] = {}
        self._reserved: set[str] = set()
        self._populated_dirs: set[str] = set()
        self.root.mkdir(parents=True)
        self.data = self._directory(self.data_relative)
        self.local.mkdir(parents=True)

    def _directory(self, relative: str) -> Path:
        current = self.root
        parts = []
        for part in Path(relative).parts:
            parts.append(part)
            key = "/".join(parts).lower()
            existing = self._paths.get(key)
            if existing is None:
                existing = current / part
                existing.mkdir(exist_ok=True)
                self._paths[key] = existing
            elif existing.name != part and not (current / part).exists():
                (current / part).symlink_to(existing.name, target_is_directory=True)
            current = existing
        return current

    def link(self, relative: str, source: Path) -> None:
        relative = _normalise(relative)
        key = relative.lower()
        if key in self._reserved or self._linked_sources.get(key) == source:
            return
        previous = self._paths.get(key)
        if previous is not None:
            if previous.is_dir():
                raise ValueError(f"File conflicts with a directory in the LOOT view: {relative}")
            previous.unlink(missing_ok=True)
        if not source.is_file():
            raise FileNotFoundError(f"LOOT source is no longer available: {source}")
        dest = self._directory(posixpath.dirname(relative)) / Path(relative).name
        dest.symlink_to(source.absolute())
        self._paths[key] = dest
        self._linked_sources[key] = source
        self.track_source(source)

    def write_config(self, path: Path, text: str, encoding: str = "utf-8") -> None:
        relative = path.relative_to(self.root.parent)
        if path.is_symlink():
            path.unlink()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding=encoding)
        if relative.parts[0] == self.root.name:
            self._reserved.add(path.relative_to(self.root).as_posix().lower())

    def track_source(self, source: Path) -> None:
        if source not in self._source_state:
            self._source_state[source] = self._signature(source)

    @staticmethod
    def _signature(path: Path) -> tuple:
        stat = path.stat()
        return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns

    def check_sources(self) -> None:
        for path, signature in self._source_state.items():
            if self._signature(path) != signature:
                raise RuntimeError(f"LOOT source changed during sorting: {path.name}. Run LOOT again.")

    def populate(self, db, plugin_names: list[str], plugin_paths: list[str],
                 condition_paths=()) -> None:
        directories = {"", self.data_relative.lower()}
        directory_spellings = set(condition_paths)
        if db is not None:
            directory_spellings.update(condition_directories(db, plugin_names, self.data_relative))
        directories.update(path.lower() if path != "." else "" for path in directory_spellings)
        new_directories = directories - self._populated_dirs
        for directory in sorted(new_directories):
            relative = _relative(directory, self.data_relative)
            if relative is not None:
                core = self.data_root.with_name(self.data_root.name + "_Core")
                base = core if core.is_dir() else self.data_root
                source_dir = _resolve_case(base, relative)
            else:
                source_dir = _resolve_case(
                    self.data_root, posixpath.relpath(directory or ".", self.data_relative.lower()))
            if not source_dir.is_dir():
                continue
            self.track_source(source_dir)
            self._directory(directory)
            with os.scandir(source_dir) as entries:
                for entry in entries:
                    if entry.is_file() and not entry.name.lower().endswith((".esp", ".esm", ".esl", ".ghost")):
                        source = Path(entry.path)
                        if self.sources is not None:
                            source = self.sources.original(source)
                        if source is not None:
                            self.link(posixpath.join(directory, entry.name), source)
        if self.sources is not None:
            for relative, source in self.sources.files(new_directories, self.data_relative):
                if source is None:
                    self._directory(relative)
                else:
                    self.link(relative, source)
        for path in plugin_paths:
            source = Path(path)
            self.link(self.data_relative + "/" + source.name, source)
        for directory in directory_spellings:
            existing = self._paths.get(directory.lower())
            if existing is not None and existing.is_dir():
                self._directory(directory)
        self._populated_dirs.update(new_directories)

    def write_active_plugins(self, path: Path, names: list[str], enabled: set[str]) -> None:
        enabled = {name.lower() for name in enabled}
        active = [name for name in names if name.lower() in enabled]
        if self.game_type == "Morrowind":
            text = "[Game Files]\n" + "".join(f"GameFile{i}={name}\n" for i, name in enumerate(active))
        elif self.game_type == "OpenMW":
            text = "replace=data\n" + f'data="{self.data}"\n' + "".join(f"content={name}\n" for name in active)
        elif self.game_type in {"SkyrimSE", "SkyrimVR", "Fallout4", "Fallout4VR", "Starfield"}:
            text = "".join(("*" if name.lower() in enabled else "") + name + "\n" for name in names)
        else:
            text = "".join(name + "\n" for name in active)
        from Utils.plugins import _plugins_txt_encoding
        self.write_config(path, text, "utf-8" if self.game_type == "OpenMW" else _plugins_txt_encoding(text))
        self.write_config(self.local / "loadorder.txt", "\n".join(names) + "\n")
