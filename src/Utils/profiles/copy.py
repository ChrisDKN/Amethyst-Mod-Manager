from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from Utils.atomic_write import filename_limit, write_atomic_text
from Utils.deployment.locking import game_mutation_lock
from Utils.profiles.state import merge_profile_settings, read_profile_settings


COPY_TEMP_PREFIX = ".profile-copy-"

# Generated projections and recovery records belong to the source deployment.
_GENERATED = frozenset({
    ".amethyst-vfs", "filemap.txt", "filemap_root.txt", "filemap_deployed.txt",
    "filemap_backup", "deploy_snapshot.txt", "deploy_stats.txt",
    "deploy_stats_delta.txt", "vanilla_deployed.txt", "Root_Backup",
    "root_folder_deployed.txt", "root_deploy_identities.json",
    "custom_deploy_backup", "custom_deploy_log.txt", "custom_rules_backup",
    "custom_rules_prefix_backup", "custom_rules_deployed.txt",
    "custom_rules_roots.json", "modlist.txt.lock", ".amethyst-operation.lock",
})


@dataclass(frozen=True)
class ProfileCopySize:
    required_bytes: int
    file_bytes: int


class InsufficientCopySpace(OSError):
    def __init__(self, required: int, available: int):
        self.required = required
        self.available = available
        super().__init__("Not enough free disk space to copy this profile")


def _check_stop(stop):
    if stop is not None and stop.is_set():
        raise InterruptedError("Profile copy cancelled")


def _validate_source(source: Path):
    if source.is_symlink() or not source.is_dir():
        raise ValueError("The source profile is missing or is a symbolic link")
    settings = read_profile_settings(source, None)
    if not settings.get("profile_specific_mods") or settings.get("is_group"):
        raise ValueError("Only profiles with their own profile-specific mods can be copied")


def validate_copy_name(source: Path, name: str) -> Path:
    if (not name or name != name.strip() or name.startswith(".")
            or any(c in '/\\' or ord(c) < 32 or ord(c) == 127 for c in name)
            or len(os.fsencode(name)) > filename_limit(source.parent)
            or name.casefold() == "default"):
        raise ValueError("Enter a valid profile folder name")
    destination = source.parent / name
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"Profile '{name}' already exists")
    return destination


def _entries(source: Path, stop=None):
    stack = [source]
    while stack:
        _check_stop(stop)
        directory = stack.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                _check_stop(stop)
                if directory == source and (
                        entry.name in _GENERATED
                        or entry.name.startswith(("filegraph.", ".filegraph"))):
                    continue
                info = entry.stat(follow_symlinks=False)
                path = Path(entry.path)
                yield path, info
                if stat.S_ISDIR(info.st_mode):
                    stack.append(path)


def estimate_profile_copy(source: Path, *, stop=None) -> ProfileCopySize:
    source = Path(source)
    _validate_source(source)
    block = os.statvfs(source.parent).f_frsize or 4096
    required, file_bytes = block, 0
    for path, info in _entries(source, stop):
        if stat.S_ISREG(info.st_mode):
            file_bytes += info.st_size
            required += ((info.st_size + block - 1) // block) * block
        elif stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            required += block
        else:
            raise ValueError(f"Cannot copy a special file: {path}")
    return ProfileCopySize(required, file_bytes)


def _rebase(value, source: Path, destination: Path):
    if isinstance(value, str):
        for old, new, sep in ((str(source), str(destination), "/"),
                              (str(source).replace("/", "\\"),
                               str(destination).replace("/", "\\"), "\\")):
            value = new if value == old else value.replace(old + sep, new + sep)
        return value
    if isinstance(value, list):
        return [_rebase(item, source, destination) for item in value]
    if isinstance(value, dict):
        return {_rebase(key, source, destination): _rebase(item, source, destination)
                for key, item in value.items()}
    return value


def _copy_metadata(stage: Path, source: Path, destination: Path):
    for name in ("profile_state.json", "profile_settings.json", "exe_args.json"):
        path = stage / name
        if path.is_symlink():
            # Never write through a copied link back into the original profile.
            path.unlink()
            shutil.copy2(source / name, path)
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            updated = _rebase(data, source, destination)
            if updated != data:
                write_atomic_text(path, json.dumps(updated, indent=2))
    settings = read_profile_settings(stage, None)
    updates = {"original_default": None, "profile_locked": None,
               "hide_from_profile_dropdown": None}
    if settings.get("collection_identity") or settings.get("collection_url"):
        updates["collection_custom_name"] = True
    merge_profile_settings(stage, updates)


def copy_profile(game, source: Path, name: str, *, stop=None,
                 progress_fn=None) -> Path:
    source = Path(source).absolute()
    profiles = (Path(game.get_profile_root()) / "profiles").absolute()
    if source.parent != profiles:
        raise ValueError("The source profile does not belong to this game")
    with game_mutation_lock(game):
        destination = validate_copy_name(source, name)
        size = estimate_profile_copy(source, stop=stop)
        available = shutil.disk_usage(profiles).free
        if size.required_bytes > available:
            raise InsufficientCopySpace(size.required_bytes, available)
        with tempfile.TemporaryDirectory(prefix=COPY_TEMP_PREFIX, dir=profiles) as temporary:
            stage = Path(temporary) / "profile"
            stage.mkdir()
            copied, last_progress = 0, 0.0
            directories = [(source, stage)]
            for path, info in _entries(source, stop):
                target = stage / path.relative_to(source)
                if stat.S_ISDIR(info.st_mode):
                    target.mkdir()
                    directories.append((path, target))
                elif stat.S_ISLNK(info.st_mode):
                    link = os.readlink(path)
                    if os.path.isabs(link):
                        link = _rebase(link, source, destination)
                    target.symlink_to(link)
                    shutil.copystat(path, target, follow_symlinks=False)
                else:
                    if not stat.S_ISREG(info.st_mode):
                        raise ValueError(f"Cannot copy a special file: {path}")
                    shutil.copy2(path, target)
                    copied += info.st_size
                now = time.monotonic()
                if progress_fn is not None and now - last_progress >= 0.1:
                    progress_fn(copied, size.file_bytes)
                    last_progress = now
            _copy_metadata(stage, source, destination)
            for path, target in reversed(directories):
                shutil.copystat(path, target)
            _check_stop(stop)
            validate_copy_name(source, name)
            stage.rename(destination)
        return destination
