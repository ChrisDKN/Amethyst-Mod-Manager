from __future__ import annotations

import configparser
import json
import threading
import uuid
from pathlib import Path

from Utils.atomic_write import atomic_writer
from Utils.mods.metadata import meta_file_lock


def read_ownership(meta_path: Path) -> dict:
    cp = configparser.ConfigParser()
    try:
        with Path(meta_path).open(encoding="utf-8-sig") as stream:
            cp.read_file(stream)
        value = json.loads(cp.get("General", "collectionOwnership", fallback="{}"))
        if (value.get("version") == 1 and isinstance(value.get("mod_id"), str)
                and value["mod_id"] and isinstance(value.get("introduced"), bool)
                and isinstance(value.get("installations"), list)
                and all(isinstance(item, str) and item for item in value["installations"])):
            expected = value.get("identity")
            if expected is not None and expected != _ini_identity(cp):
                return {}
            return value
    except (OSError, ValueError, AttributeError, configparser.Error):
        pass
    return {}


def _ini_identity(cp) -> list:
    from Nexus.nexus_meta import normalise_game_domain
    try:
        mod_id = cp.getint("General", "modid", fallback=0)
    except ValueError:
        mod_id = 0
    if mod_id:
        return ["nexus", normalise_game_domain(cp.get("General", "gameName", fallback="")), mod_id]
    return ["archive", cp.get("General", "installationFile", fallback="").casefold()]


def write_ownership(meta_path: Path, value: dict) -> None:
    with meta_file_lock(meta_path):
        cp = configparser.ConfigParser()
        if meta_path.exists():
            with meta_path.open(encoding="utf-8-sig") as stream:
                cp.read_file(stream)
        if not cp.has_section("General"):
            cp.add_section("General")
        value = {**value, "identity": _ini_identity(cp)}
        cp.set("General", "collectionOwnership", json.dumps(value, separators=(",", ":")).replace("%", "%%"))
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        with atomic_writer(meta_path) as stream:
            cp.write(stream)


def mod_identity(meta) -> tuple:
    from Nexus.nexus_meta import normalise_game_domain
    if meta.mod_id:
        return ("nexus", normalise_game_domain(meta.game_domain), int(meta.mod_id))
    return ("archive", str(meta.installation_file or "").casefold())


def carry_ownership(previous, current) -> str:
    raw = getattr(previous, "collection_ownership", "") if previous else ""
    if raw and mod_identity(previous) == mod_identity(current):
        identity = mod_identity(current)
        if identity[0] == "nexus" or identity[1]:
            return raw
    return ""


class CollectionOwnership:
    def __init__(self, staging: Path, record: dict):
        from Nexus.nexus_meta import read_meta
        self.staging = Path(staging)
        self.record = record
        self.install_id = record["install_id"]
        self._lock = threading.RLock()
        self._before = {}
        self.members = dict(record.get("members") or {})
        if self.staging.exists():
            for folder in self.staging.iterdir():
                if folder.is_dir() and not folder.name.startswith("."):
                    try:
                        identity = mod_identity(read_meta(folder / "meta.ini"))
                    except (OSError, ValueError, configparser.Error):
                        identity = None
                    self._before[folder.name.casefold()] = (
                        read_ownership(folder / "meta.ini"), identity)

    def track(self, name: str) -> None:
        from Nexus.nexus_meta import read_meta
        if not name or Path(name).name != name:
            raise ValueError("Invalid collection mod folder")
        folder = self.staging / name
        if not folder.is_dir():
            return
        with self._lock, meta_file_lock(folder / "meta.ini"):
            value = read_ownership(folder / "meta.ini")
            before = self._before.get(name.casefold())
            tracked = value.get("mod_id") in self.members
            if not tracked and before:
                previous, identity = before
                value = (dict(previous) if identity is not None
                         and identity == mod_identity(read_meta(folder / "meta.ini")) else {})
            elif not tracked:
                value = {}
            if not value:
                value = {"version": 1, "mod_id": uuid.uuid4().hex,
                         "introduced": before is None and not folder.is_symlink(),
                         "installations": []}
            value["installations"] = sorted(set(value["installations"]) | {self.install_id})
            write_ownership(folder / "meta.ini", value)
            self.members[value["mod_id"]] = name

    def finish(self, status: str, *, report=None) -> None:
        from Utils.collections.installed import save_record, _read_record
        with self._lock:
            try:
                latest = _read_record(Path(self.record["path"]))
            except (OSError, ValueError):
                latest = {}
            if latest.get("install_id") == self.install_id:
                self.record.pop("pending_delete", None)
                if latest.get("pending_delete"):
                    self.record["pending_delete"] = latest["pending_delete"]
            self.record["status"] = status
            if status == "complete":
                self.record.pop("previous", None)
            self.record["members"] = dict(self.members)
            if report is not None:
                self.record["report"] = report
            save_record(self.record)
