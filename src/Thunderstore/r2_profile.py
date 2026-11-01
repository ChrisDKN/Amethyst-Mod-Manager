from __future__ import annotations

from dataclasses import dataclass, replace
import base64
import binascii
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from urllib.error import HTTPError
from urllib.request import urlopen
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

    def manifest(self, community, versions=None, *, communities=None):
        mods, omitted = [], []
        for mod in self.mods:
            if mod.source not in {"Thunderstore", "Hexium"}:
                omitted.append(f"{mod.full_name}: unsupported source {mod.source}")
                continue
            if versions is not None and (mod.source.lower(), mod.full_name) not in versions:
                omitted.append(f"{mod.full_name}: version unavailable in {community}")
                continue
            data = (versions or {}).get((mod.source.lower(), mod.full_name), {})
            mods.append({
                "name": mod.package_id, "version": mod.version,
                "enabled": mod.enabled, "optional": False,
                "source": {
                    "type": mod.source.lower(), "namespace": mod.namespace,
                    "name": mod.name, "version": mod.version,
                    "fullName": mod.full_name,
                    "community": (communities or {}).get(mod.source.lower(), community),
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
        if communities is not None:
            details["communities"] = communities
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
        identity_parts = match.groups() if match is not None else ()
        if row.get("source") == "Hexium" and isinstance(identity, str):
            from Thunderstore.repositories import split_full_name
            namespace, package, _ = split_full_name(identity + "-0.0.0")
            identity_parts = (namespace, package) if namespace else ()
        if not identity_parts:
            raise ValueError(f"Invalid package name in entry {index}.")
        version = row.get("version", row.get("versionNumber"))
        if isinstance(version, str):
            from Thunderstore.repositories import version_key
            if version_key(version)[0] < 0:
                raise ValueError(f"Invalid version for {identity}.")
            version_string = version
        elif isinstance(version, dict) and all(type(version.get(key)) is int and version[key] >= 0 for key in ("major", "minor", "patch")):
            version_string = ".".join(str(version[key]) for key in ("major", "minor", "patch"))
            prerelease = version.get("preRelease", version.get("prerelease", ""))
            if prerelease:
                version_string += "-" + str(prerelease)
                from Thunderstore.repositories import version_key
                if version_key(version_string)[0] < 0:
                    raise ValueError(f"Invalid version for {identity}.")
        else:
            raise ValueError(f"Invalid version for {identity}.")
        enabled = row.get("enabled", True)
        source = row.get("source", "Thunderstore")
        if type(enabled) is not bool or not isinstance(source, str) or not source.strip():
            raise ValueError(f"Invalid enabled state or source for {identity}.")
        mod = R2Mod(*identity_parts,
                    version_string, enabled, source)
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


def normalise_share_code(code: str) -> str:
    code = code.strip()
    if not re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", code):
        raise ValueError("Enter a Thunderstore or Hexium share code in the form xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx.")
    return code.lower()


def download_share_profile(code: str, destination, source="thunderstore") -> R2Profile:
    from Thunderstore.thunderstore_download import build_request

    code = normalise_share_code(code)
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    if source not in {"thunderstore", "hexium"}:
        raise ValueError("Unsupported share-code repository")
    sources = (source, "hexium" if source == "thunderstore" else "thunderstore")
    limit = 256 * 1024 ** 2
    with TemporaryDirectory(prefix=".thunderstore-profile-", dir=destination) as temporary:
        archive_path = Path(temporary) / "profile.r2z"
        try:
            for repository in sources:
                host = "hexium.gg" if repository == "hexium" else "thunderstore.io"
                url = f"https://{host}/api/experimental/legacyprofile/get/{code}/"
                try:
                    response = urlopen(build_request(url), timeout=60)
                except HTTPError as exc:
                    status = exc.code
                    exc.close()
                    if status == 404:
                        continue
                    if status == 429:
                        raise ValueError(f"{repository.title()} rate limited this request. Wait a moment and try again.") from exc
                    raise
                source = repository
                break
            else:
                raise ValueError("The share code was not found on Thunderstore or Hexium. It may have expired or contain a typo.")
            with response, archive_path.open("wb") as writer:
                if response.readline(32).rstrip(b"\r\n") != b"#r2modman":
                    raise ValueError("The share code did not return an r2modman profile.")
                pending = b""
                received = written = 0
                while chunk := response.read(256 * 1024):
                    received += len(chunk)
                    if received > (limit + 2) // 3 * 4 + 1024 ** 2:
                        raise ValueError("The shared profile exceeds the download size limit (256 MiB).")
                    pending += b"".join(chunk.split())
                    length = max(0, (len(pending) - 4) // 4 * 4)
                    if b"=" in pending[:length]:
                        raise ValueError("The shared profile contains invalid Base64 data.")
                    decoded = base64.b64decode(pending[:length], validate=True)
                    pending = pending[length:]
                    written += len(decoded)
                    if written > limit:
                        raise ValueError("The shared profile exceeds the download size limit (256 MiB).")
                    writer.write(decoded)
                decoded = base64.b64decode(pending, validate=True)
                if written + len(decoded) > limit:
                    raise ValueError("The shared profile exceeds the download size limit (256 MiB).")
                writer.write(decoded)
        except binascii.Error as exc:
            raise ValueError("The shared profile contains invalid Base64 data.") from exc
        profile = read_profile(archive_path)
        final_path = destination / f"{source}-profile-{code}.r2z"
        archive_path.replace(final_path)
        return replace(profile, archive_path=str(final_path.resolve()))


def check_community(profile: R2Profile, community: str):
    if not community:
        raise ValueError("Select a configured Thunderstore game first.")
    if profile.community and profile.community.casefold() != community.casefold():
        raise ValueError(f"This profile targets '{profile.community}', but the selected "
                         f"game uses '{community}'. Switch games first, then import.")


def check_import_game(details, game):
    communities = details.get("communities")
    if communities is None:
        if details.get("community") not in {
                getattr(game, "thunderstore_community", ""), getattr(game, "hexium_community", "")}:
            raise ValueError("The profile belongs to another game.")
        return
    if any(not community or community != getattr(game, f"{source}_community", "")
           for source, community in communities.items()):
        raise ValueError("The profile contains packages for another game's repository community.")


def import_manifest(profile: R2Profile, community: str, *, hexium_community=None) -> dict:
    communities = {source: (hexium_community if source == "hexium" and hexium_community is not None else community)
                   for source in {mod.source.lower() for mod in profile.mods if mod.source in {"Hexium", "Thunderstore"}}}
    if not communities:
        check_community(profile, community or hexium_community)
        return profile.manifest(community or hexium_community)
    if any(not value for value in communities.values()):
        raise ValueError("The selected game does not support every repository in this profile.")
    if profile.community and profile.community.casefold() not in {slug.casefold() for slug in communities.values()}:
        raise ValueError("The profile belongs to another game's repository community.")
    selected = next((slug for slug in communities.values() if slug.casefold() == profile.community.casefold()),
                    community or hexium_community)
    return profile.manifest(selected, communities=communities)


def prepare_import(profile: R2Profile, community: str, *, hexium_community=None) -> dict:
    manifest = import_manifest(profile, community, hexium_community=hexium_community)
    communities = manifest["r2Import"].get("communities", {})
    from Thunderstore.repositories import catalogue
    versions = {}
    for source, slug in communities.items():
        for package in catalogue(slug, force=True, source=source).values():
            for version in package["versions"]:
                versions[(source, version["full_name"])] = version
    return profile.manifest(manifest["r2Import"]["community"], versions, communities=communities)
