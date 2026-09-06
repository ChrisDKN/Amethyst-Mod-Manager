from __future__ import annotations

import configparser
import json
import re
import zipfile
from dataclasses import replace
from pathlib import Path

from .hashes import canonical_hash, package_hash, XXHash
from .models import Archive, Directive, Package
from .paths import WabbajackError, relative_path

DIRECTIVES = {"FromArchive", "PatchedFromArchive", "MergedPatch", "InlineFile",
              "RemappedInlineFile", "PropertyFile", "ArchiveMeta", "CreateBSA", "TransformedTexture"}


def qvalue(value):
    value = str(value)
    byte_array = value.startswith("@ByteArray(") and value.endswith(")")
    if byte_array:
        value = value[11:-1]
    if value.startswith('"') and value.endswith('"'):
        value = value[1:-1]
    escapes = {"\\": "\\", '"': '"', "n": "\n", "r": "\r", "t": "\t"}
    value = re.sub(r'\\(x[0-9a-fA-F]{1,4}|[\\"nrt])',
                   lambda m: chr(int(m[1][1:], 16)) if m[1].startswith("x") else escapes[m[1]], value)
    if byte_array:
        try:
            return value.encode("latin-1").decode("utf-8")
        except UnicodeError:
            pass
    return value


def stock_folder(package):
    value = package.game_path.replace("\\", "/")
    for style in ("BACK", "DOUBLE_BACK", "FORWARD"):
        token = "{--||MO2_PATH_MAGIC_" + style + "||--}"
        if value.startswith(token + "/"):
            return relative_path(value[len(token):].lstrip("/"))
    return ""


def type_name(raw) -> str:
    name = str(raw or "").split(",", 1)[0].split("+")[0].split(".")[-1]
    return name.removesuffix("Downloader")


def archive_path(data: dict) -> tuple[str, list[str]]:
    value = data.get("ArchiveHashPath", [])
    if isinstance(value, str):
        value = value.split("|")
    if not isinstance(value, list) or not value:
        raise WabbajackError("Missing ArchiveHashPath")
    return canonical_hash(value[0]), [relative_path(p) for p in value[1:]]


def dependency_paths(directive):
    if directive.kind == "CreateBSA":
        base = "TEMP_BSA_FILES/" + relative_path(directive.data["TempID"])
        return [f"{base}/{relative_path(f['Path'])}" for f in directive.data["FileStates"]]
    if directive.kind == "MergedPatch":
        return [relative_path(s["RelativePath"]) for s in directive.data["Sources"]]
    return []


def required_directives(package, reusable):
    by_path = {d.path.casefold(): d for d in package.directives}
    pending = [d for d in package.directives if d.path.split("/")[0].casefold() != "temp_bsa_files"]
    required = set()
    while pending:
        directive = pending.pop()
        if directive.path in required:
            continue
        required.add(directive.path)
        if directive.path not in reusable:
            pending.extend(by_path[p.casefold()] for p in dependency_paths(directive))
    return required


def inspect_package(path: Path) -> Package:
    path = Path(path)
    with zipfile.ZipFile(path) as archive:
        names = {}
        for info in archive.infolist():
            name = relative_path(info.filename.rstrip("/"))
            if name in names:
                raise WabbajackError(f"Duplicate package member: {name}")
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise WabbajackError(f"Symbolic link in package: {name}")
            names[name] = info
        if "modlist" not in names:
            raise WabbajackError("Package does not contain a modlist manifest")
        if names["modlist"].file_size > 512 * 1024 * 1024:
            raise WabbajackError("Modlist manifest exceeds 512 MiB")
        raw = json.loads(archive.read(names["modlist"]))
        sources = {}
        for item in raw.get("Archives", []):
            key = canonical_hash(item["Hash"])
            size = int(item["Size"])
            name = relative_path(item["Name"])
            if "/" in name or size < 0:
                raise WabbajackError(f"Invalid archive: {name}")
            state = item.get("State") or {}
            source = Archive(key, name, size, type_name(state.get("$type")), state)
            if key in sources and sources[key].size != size:
                raise WabbajackError(f"Conflicting archive identity: {name}")
            sources.setdefault(key, source)
        directives, destinations, profiles = [], set(), []
        directory_spelling = {}
        selected, game_path = "", ""
        for index, item in enumerate(raw.get("Directives", [])):
            kind = type_name(item.get("$type"))
            if kind not in DIRECTIVES:
                raise WabbajackError(f"Unsupported directive: {kind}")
            required = {
                "FromArchive": {"ArchiveHashPath"},
                "PatchedFromArchive": {"ArchiveHashPath", "PatchID"},
                "TransformedTexture": {"ArchiveHashPath", "ImageState"},
                "MergedPatch": {"Sources", "PatchID"},
                "CreateBSA": {"TempID", "State", "FileStates"},
            }.get(kind, {"SourceDataID"})
            missing = required - item.keys()
            if missing:
                raise WabbajackError(f"{kind} is missing required fields: {', '.join(sorted(missing))}")
            target = relative_path(item["To"])
            parts = target.split("/")
            if len(parts) > 1 and parts[0].casefold() in {"mods", "profiles"}:
                parts[0] = parts[0].casefold()
            for n in range(1, len(parts)):
                key = "/".join(parts[:n]).casefold()
                spelling = directory_spelling.setdefault(key, parts[n - 1])
                parts[n - 1] = spelling
            target = "/".join(parts)
            folded = target.casefold()
            if folded in destinations:
                raise WabbajackError(f"Conflicting output destination: {target}")
            destinations.add(folded)
            size = int(item.get("Size", 0))
            if size < 0:
                raise WabbajackError(f"Negative output size: {target}")
            expected = canonical_hash(item["Hash"]) if item.get("Hash") is not None else ""
            directive = Directive(index, kind, target, expected, size, item)
            if directive.deterministic and not expected:
                raise WabbajackError(f"Missing output hash: {target}")
            for key in ("PatchID", "SourceDataID"):
                if key in item:
                    member = relative_path(item[key])
                    if member not in names:
                        raise WabbajackError(f"Missing package member: {member}")
            if "PatchID" in item:
                with archive.open(item["PatchID"]) as patch:
                    if patch.read(10) != b"OCTODELTA\x01":
                        raise WabbajackError(f"Unsupported patch format: {target}")
            if item.get("FromHash") is not None:
                canonical_hash(item["FromHash"])
            if kind in {"FromArchive", "PatchedFromArchive", "TransformedTexture"}:
                key, _ = archive_path(item)
                if key not in sources:
                    raise WabbajackError(f"Unknown archive for {target}: {key}")
            parts = target.split("/")
            if len(parts) == 3 and parts[0].casefold() == "profiles" and parts[2].casefold() == "modlist.txt":
                if parts[1] not in profiles:
                    profiles.append(parts[1])
                if kind == "InlineFile":
                    member = names[relative_path(item["SourceDataID"])]
                    if member.file_size > 8 * 1024 * 1024:
                        raise WabbajackError(f"Profile modlist exceeds 8 MiB: {target}")
                    content = archive.read(member)
                    if any(line and not line.startswith(("+", "-", "#")) for line in content.decode("utf-8-sig").splitlines()):
                        raise WabbajackError(f"Invalid authored profile modlist: {target}")
                    digest = XXHash()
                    digest.update(content)
                    if digest.digest() != expected or len(content) != size:
                        directive = replace(directive, embedded_hash=digest.digest(), embedded_size=len(content))
            if folded == "modorganizer.ini" and kind in {"InlineFile", "RemappedInlineFile"}:
                cp = configparser.ConfigParser(interpolation=None, strict=False)
                try:
                    cp.read_string(archive.read(item["SourceDataID"]).decode("utf-8-sig"))
                    selected = qvalue(cp.get("General", "selected_profile", fallback=""))
                    game_path = qvalue(cp.get("General", "gamePath", fallback=""))
                except (ValueError, configparser.Error, UnicodeError):
                    pass
            directives.append(directive)
        if not directives:
            raise WabbajackError("Modlist has no installation directives")
        for target in destinations:
            parts = target.split("/")
            if any("/".join(parts[:n]) in destinations for n in range(1, len(parts))):
                raise WabbajackError(f"File/directory output collision: {target}")
        by_path = {d.path.casefold(): d for d in directives}
        dependencies = {}
        for d in directives:
            deps = []
            if d.kind == "MergedPatch":
                for source in d.data.get("Sources", []):
                    rel = relative_path(source["RelativePath"])
                    if rel.casefold() not in by_path:
                        raise WabbajackError(f"Missing merge source: {rel}")
                    canonical_hash(source["Hash"])
                    deps.append(by_path[rel.casefold()].index)
            if d.kind == "CreateBSA":
                temp = relative_path(d.data["TempID"])
                for file in d.data.get("FileStates", []):
                    rel = relative_path(file["Path"])
                    source = by_path.get(f"temp_bsa_files/{temp}/{rel}".casefold())
                    if source is None:
                        raise WabbajackError(f"Missing archive-building source: {temp}/{rel}")
                    deps.append(source.index)
            dependencies[d.index] = set(deps)
        done = set()
        while dependencies:
            ready = [i for i, deps in dependencies.items() if deps <= done]
            if not ready:
                raise WabbajackError("Installation directive dependency cycle")
            done.update(ready)
            for i in ready:
                del dependencies[i]
    return Package(path, package_hash(path), raw.get("Name") or path.stem,
                   str(raw.get("Version", "")), str(raw.get("GameType", "")),
                   sources, directives, raw, profiles, selected, game_path)


def inline_stream(package: Package, member: str):
    archive = zipfile.ZipFile(package.path)
    try:
        return archive, archive.open(relative_path(member))
    except BaseException:
        archive.close()
        raise
