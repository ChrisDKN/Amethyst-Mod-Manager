from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
import re
from urllib.parse import quote
from zipfile import ZipFile

import yaml


@dataclass(frozen=True)
class R2Mod:
    namespace: str
    name: str
    version: str
    enabled: bool
    source: str

    @property
    def package_id(self):
        return f"{self.namespace}-{self.name}"

    @property
    def full_name(self):
        return f"{self.package_id}-{self.version}"


@dataclass(frozen=True)
class R2Profile:
    name: str
    community: str
    mods: tuple[R2Mod, ...]
    archived: bool = False
    archive_path: str = ""
    archive_signature: str = ""
    config_count: int = 0
    skipped_files: tuple[str, ...] = ()

    def manifest(self, community, versions=None):
        mods, omitted = [], []
        for mod in self.mods:
            if mod.source != "Thunderstore":
                omitted.append(f"{mod.full_name}: unsupported source {mod.source}")
                continue
            if versions is not None and mod.full_name not in versions:
                omitted.append(f"{mod.full_name}: version unavailable in {community}")
                continue
            data = (versions or {}).get(mod.full_name, {})
            mods.append({
                "name": mod.package_id, "version": mod.version,
                "enabled": mod.enabled, "optional": False,
                "source": {
                    "type": "thunderstore", "namespace": mod.namespace,
                    "name": mod.name, "version": mod.version,
                    "fullName": mod.full_name, "community": community,
                    "logicalFilename": mod.package_id,
                    "fileSize": data.get("file_size", 0),
                    "versionInfo": ({
                        "full_version_name": mod.full_name,
                        "download_url": data.get("download_url", ""),
                        "description": data.get("description", ""),
                        "website_url": data.get("website_url", ""),
                        "icon_url": data.get("icon", ""),
                        "size": data.get("file_size", 0),
                        "dependencies": data.get("dependencies", []),
                    } if data else None),
                },
            })
        omitted.extend(f"Bundled file {name}: not a supported configuration file"
                       for name in self.skipped_files)
        details = {"community": community, "omitted": omitted}
        if self.archived:
            details["archive"] = {"path": self.archive_path, "signature": self.archive_signature}
        return {"info": {"name": self.name}, "mods": mods, "r2Import": details}


def parse_profile(text: str) -> R2Profile:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid r2x YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("The r2x manifest must contain a profileName and mods list.")
    name = data.get("profileName")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("The r2x profileName must be a non-empty string.")
    community = data.get("community", "")
    if community is None:
        community = ""
    if not isinstance(community, str) or (community and not re.fullmatch(r"[a-zA-Z0-9_-]+", community)):
        raise ValueError("Invalid r2x community.")
    rows = data.get("mods")
    if not isinstance(rows, list) or not rows or len(rows) > 10000:
        raise ValueError("The r2x mods list must contain between 1 and 10000 entries.")
    mods = {}
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise ValueError(f"Invalid mod entry {index}.")
        identity = row.get("name")
        match = (re.fullmatch(r"([A-Za-z0-9_]+)-([A-Za-z0-9_][A-Za-z0-9_-]*)", identity)
                 if isinstance(identity, str) else None)
        if match is None:
            raise ValueError(f"Invalid package name in entry {index}.")
        version = row.get("version", row.get("versionNumber"))
        if not isinstance(version, dict) or any(
                type(version.get(key)) is not int or version[key] < 0
                for key in ("major", "minor", "patch")):
            raise ValueError(f"Invalid version for {identity}.")
        enabled = row.get("enabled", True)
        source = row.get("source", "Thunderstore")
        if type(enabled) is not bool or not isinstance(source, str) or not source.strip():
            raise ValueError(f"Invalid enabled state or source for {identity}.")
        mod = R2Mod(*match.groups(),
                    ".".join(str(version[key]) for key in ("major", "minor", "patch")),
                    enabled, source)
        key = mod.package_id.casefold()
        if key in mods and mods[key] != mod:
            raise ValueError(f"Conflicting duplicate package: {identity}.")
        mods[key] = mod
    return R2Profile(name.strip(), community, tuple(mods.values()))


def read_profile(path) -> R2Profile:
    path = Path(path)
    archived = path.suffix.lower() == ".r2z"
    limit = 4 * 1024 * 1024
    configs, skipped, signature = [], [], ""
    if archived:
        from Thunderstore.r2_archive import inspect_archive
        with ZipFile(path) as archive:
            data, configs, skipped, signature = inspect_archive(archive)
    elif path.suffix.lower() in (".r2x", ".txt"):
        with path.open("rb") as stream:
            data = stream.read(limit + 1)
    else:
        raise ValueError("Choose an .r2z archive, .r2x manifest or its renamed .txt copy.")
    if len(data) > limit:
        raise ValueError("The r2x manifest exceeds 4 MiB.")
    return replace(parse_profile(data.decode("utf-8-sig")), archived=archived,
                   archive_path=str(path.resolve()) if archived else "",
                   archive_signature=signature, config_count=len(configs),
                   skipped_files=tuple(skipped))


def check_community(profile: R2Profile, community: str):
    if not community:
        raise ValueError("Select a configured Thunderstore game first.")
    if profile.community and profile.community.casefold() != community.casefold():
        raise ValueError(f"This profile targets '{profile.community}', but the selected "
                         f"game uses '{community}'. Switch games first, then import.")


def prepare_import(profile: R2Profile, community: str) -> dict:
    from Thunderstore.thunderstore_download import fetch_json

    check_community(profile, community)
    data = fetch_json(
        f"https://thunderstore.io/c/{quote(community, safe='')}/api/v1/package/",
        timeout=60)
    if not isinstance(data, list) or any(
            not isinstance(package, dict) or not isinstance(package.get("versions"), list)
            for package in data):
        raise ValueError("Could not load the Thunderstore catalogue. Retry to check availability.")
    versions = {}
    for package in data:
        for version in package["versions"]:
            if not isinstance(version, dict) or not isinstance(version.get("full_name"), str):
                raise ValueError("Invalid Thunderstore catalogue response. Retry to check availability.")
            if type(version.get("file_size", 0)) is not int or version.get("file_size", 0) < 0:
                raise ValueError("Invalid package size in the Thunderstore catalogue. Retry to check availability.")
            versions[version["full_name"]] = version
    return profile.manifest(community, versions)
