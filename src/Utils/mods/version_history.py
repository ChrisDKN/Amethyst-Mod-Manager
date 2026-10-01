from __future__ import annotations

import configparser
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from Utils.atomic_write import atomic_writer
from Utils.mods.metadata import locked_meta_write, meta_file_lock

if TYPE_CHECKING:
    from Nexus.nexus_meta import NexusModMeta
    from Thunderstore.thunderstore_meta import ThunderstoreModMeta


SECTION = "AmethystRollback"


@dataclass(frozen=True)
class PreviousVersion:
    source: str = ""
    game_domain: str = ""
    mod_id: int = 0
    file_id: int = 0
    community: str = ""
    namespace: str = ""
    name: str = ""
    version: str = ""
    installation_file: str = ""
    file_size: int = 0
    mod_name: str = ""

    @property
    def identity(self):
        if self.source == "nexus":
            return self.source, self.game_domain, self.mod_id, self.file_id
        return self.source, self.namespace, self.name, self.version

    @property
    def valid(self):
        if self.source == "nexus":
            return bool(self.game_domain and self.mod_id > 0 and self.file_id > 0)
        return bool(self.source == "thunderstore" and self.namespace
                    and self.name and self.version)

    def nexus_meta(self):
        from Nexus.nexus_meta import NexusModMeta
        return NexusModMeta(
            game_domain=self.game_domain, mod_id=self.mod_id,
            file_id=self.file_id, version=self.version,
            installation_file=self.installation_file, file_size=self.file_size)

    def thunderstore_meta(self):
        from Thunderstore.thunderstore_meta import ThunderstoreModMeta
        return ThunderstoreModMeta(
            community=self.community, namespace=self.namespace, name=self.name,
            version=self.version, file_size=self.file_size)


def previous_version(meta, thunderstore_meta, source):
    if source == "nexus":
        from Nexus.nexus_meta import normalise_game_domain
        return PreviousVersion(
            source=source, game_domain=normalise_game_domain(meta.game_domain),
            mod_id=meta.mod_id, file_id=meta.file_id, version=meta.version,
            installation_file=meta.installation_file, file_size=meta.file_size,
            mod_name=meta.mod_name)
    ts = thunderstore_meta
    return PreviousVersion(
        source=source, community=ts.community, namespace=ts.namespace,
        name=ts.name, version=ts.version,
        installation_file=meta.installation_file,
        file_size=meta.file_size or ts.file_size, mod_name=meta.mod_name)


def read_previous_version(cp):
    if not cp.has_section(SECTION):
        return None
    values = {}
    try:
        for item in fields(PreviousVersion):
            raw = cp.get(SECTION, item.name, fallback="") or ""
            values[item.name] = (int(raw or 0) if item.name in
                                 {"mod_id", "file_id", "file_size"} else raw)
        result = PreviousVersion(**values)
        return result if result.valid else None
    except (ValueError, configparser.Error):
        return None


@locked_meta_write
def write_update_state(meta_path, updated, previous):
    cp = configparser.ConfigParser()
    cp.read(meta_path, encoding="utf-8")
    if not cp.has_section("General"):
        cp.add_section("General")
    cp.set("General", "updated", updated)
    cp.remove_section(SECTION)
    if previous is not None and previous.valid:
        cp.add_section(SECTION)
        for item in fields(previous):
            cp.set(SECTION, item.name,
                   str(getattr(previous, item.name)).replace("%", "%%"))
    with atomic_writer(meta_path) as stream:
        cp.write(stream)


@dataclass
class VersionChangeContext:
    source: str
    mod_name: str
    profile_dir: Path
    previous_path: Path
    previous_meta: NexusModMeta
    previous_thunderstore: ThunderstoreModMeta
    game_name: str
    operation: str = "update"
    target_thunderstore: ThunderstoreModMeta | None = None
    origin_profile_dir: Path | None = None
    dependency_changes: dict = field(default_factory=dict)

    @property
    def rollback_name(self):
        previous = self.previous_meta.previous_version
        name = (previous.mod_name if self.operation == "rollback"
                and previous is not None else "") or self.mod_name
        if (name in {".", ".."} or Path(name).name != name
                or any(char in name for char in "\\\x00\r\n")):
            raise ValueError(f"Invalid saved mod name for '{self.mod_name}'.")
        return name

    @classmethod
    def capture(cls, game, profile_dir, mod_name, source, operation="update"):
        from Utils.mods.copy import resolve_target_staging
        from Utils.profiles.groups import entry_owner_profile, is_group
        from Nexus.nexus_meta import read_meta
        from Thunderstore.thunderstore_meta import read_meta as read_ts_meta
        profile_dir = Path(profile_dir)
        origin_profile_dir = profile_dir
        if is_group(profile_dir):
            owner = entry_owner_profile(profile_dir, mod_name)
            if owner is None:
                raise ValueError(f"Could not find the profile owning '{mod_name}'.")
            profile_dir, mod_name = owner
        path = Path(resolve_target_staging(game, profile_dir)) / mod_name
        meta = read_meta(path / "meta.ini")
        if source == "nexus" and not meta.game_domain:
            meta.game_domain = getattr(game, "nexus_game_domain", "") or ""
        return cls(source, mod_name, profile_dir, path, meta,
                   read_ts_meta(path / "meta.ini"), game.name, operation,
                   origin_profile_dir=origin_profile_dir)

    def validate(self):
        from Nexus.nexus_meta import read_meta
        from Thunderstore.thunderstore_meta import read_meta as read_ts_meta
        if not self.previous_path.is_dir():
            raise ValueError(f"The previous installation of '{self.mod_name}' is missing.")
        current = read_meta(self.previous_path / "meta.ini")
        current.game_domain = current.game_domain or self.previous_meta.game_domain
        actual = previous_version(current, read_ts_meta(
            self.previous_path / "meta.ini"), self.source)
        expected = previous_version(self.previous_meta,
                                    self.previous_thunderstore, self.source)
        if actual.identity != expected.identity:
            raise ValueError(f"The installed version of '{self.mod_name}' changed; try again.")
        if self.operation == "rollback" and (
                current.previous_version != self.previous_meta.previous_version):
            raise ValueError(f"The rollback target of '{self.mod_name}' changed; try again.")
        if self.operation == "rollback" and self.rollback_name != self.mod_name:
            target = self.previous_path.parent / self.rollback_name
            if target.exists() or target.is_symlink():
                raise ValueError(f"Cannot restore the name '{target.name}'; another mod already uses it.")
        self.previous_meta = current
        self.previous_thunderstore = read_ts_meta(self.previous_path / "meta.ini")

    def commit(self, meta_path):
        with meta_file_lock(meta_path):
            self._commit(meta_path)

    def _commit(self, meta_path):
        from Nexus.nexus_meta import read_meta, write_meta
        from Thunderstore.thunderstore_meta import read_meta as read_ts_meta
        old = self.previous_meta
        new = read_meta(meta_path)
        if self.target_thunderstore is not None:
            import copy
            from Thunderstore.thunderstore_meta import write_meta as write_ts_meta
            ts = copy.copy(self.target_thunderstore)
            ts.ignore_update = self.previous_thunderstore.ignore_update
            ts.ignored_version = self.previous_thunderstore.ignored_version
            ts.installed = self.previous_thunderstore.installed or ts.installed
            ts.community = ts.community or self.previous_thunderstore.community
            write_ts_meta(meta_path, ts)
            new.version = ts.version
        new.installed = old.installed or new.installed
        new.root_folder = old.root_folder
        new.ignore_update = old.ignore_update
        new.ignored_version = old.ignored_version
        if (self.operation == "rollback" and self.source == "nexus"
                and old.previous_version is not None):
            new.version = old.previous_version.version or new.version
        write_meta(meta_path, new)
        prior = previous_version(old, self.previous_thunderstore, self.source)
        current = previous_version(new, read_ts_meta(meta_path), self.source)
        if not current.valid:
            raise ValueError(f"Could not save the installed version of '{self.mod_name}'.")
        if self.operation == "rollback" and (
                old.previous_version is None
                or current.identity != old.previous_version.identity):
            raise ValueError(f"The installed archive does not match the rollback target of '{self.mod_name}'.")
        if self.operation != "rollback" and prior.identity == current.identity:
            write_update_state(meta_path, old.updated, old.previous_version)
            return
        target = prior if prior.valid else None
        if (self.operation == "rollback" or old.from_collection_patched
                or old.wabbajack_patched):
            target = None
        write_update_state(meta_path, datetime.now(timezone.utc).isoformat(
            timespec="seconds"), target)
