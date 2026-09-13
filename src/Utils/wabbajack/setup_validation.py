from __future__ import annotations

import re
import struct

from .paths import WabbajackError


def version_key(value):
    value = str(value).strip().lower().removeprefix("v")
    if not re.fullmatch(r"\d+(?:\.\d+)*", value):
        return ()
    parts = [int(part) for part in value.split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def verify_version(task, version):
    required = task.required_version
    if required and (not version_key(version) or version_key(version) != version_key(required)):
        raise WabbajackError(
            f"{task.label} version mismatch: the list requires {required}; "
            f"selected content reports {version or 'no identifiable version'}")


def mpi_identity(task, manifest):
    package = manifest["Package"]
    title = str(package.get("Title", "")).strip()
    if title.casefold() not in {name.casefold() for name in task.mpi_titles}:
        raise WabbajackError(f"Wrong MPI package: {title}; select {task.label}")
    version = str(package.get("Version", "")).strip()
    if task.id.startswith("ttw:") and not version_key(version):
        raise WabbajackError("The TTW MPI package has no valid version")
    verify_version(task, version)
    return {"version": version, "ttw": version if task.id.startswith("ttw:") else ""}


def plugin_identity(task, path):
    if not task.id.startswith(("ttw:", "yupttw:")):
        return {}
    with path.open("rb") as stream:
        header = stream.read(24)
        if len(header) != 24 or header[:4] != b"TES4":
            raise WabbajackError(f"Invalid Bethesda plugin: {path}")
        size, flags = struct.unpack_from("<II", header, 4)
        if not flags & 1 or not 18 <= size <= 1024 * 1024 or size > path.stat().st_size - 24:
            raise WabbajackError(f"Invalid Bethesda master header: {path}")
        data = stream.read(size)
    fields = {}
    offset = 0
    while offset < size:
        if offset + 6 > len(data):
            raise WabbajackError(f"Truncated plugin header: {path}")
        tag, length = struct.unpack_from("<4sH", data, offset)
        offset += 6
        if tag == b"XXXX":
            if length != 4 or offset + 10 > len(data):
                raise WabbajackError(f"Invalid extended plugin metadata: {path}")
            length = struct.unpack_from("<I", data, offset)[0]
            tag = data[offset + 4:offset + 8]
            offset += 10
        if offset + length > len(data):
            raise WabbajackError(f"Truncated plugin metadata: {path}")
        fields.setdefault(tag, []).append(data[offset:offset + length])
        offset += length
    if len(fields.get(b"HEDR", [b""])[0]) != 12:
        raise WabbajackError(f"Invalid plugin record header: {path}")
    description = fields.get(b"SNAM", [b""])[0].rstrip(b"\0").decode("utf-8", errors="replace")
    author = fields.get(b"CNAM", [b""])[0].decode("utf-8", errors="replace")
    masters = {value.rstrip(b"\0").lower() for value in fields.get(b"MAST", [])}
    if task.id.startswith("ttw:"):
        if "tale of two wastelands" not in author.casefold() and not re.search(r"\bTTW\b", author, re.I):
            raise WabbajackError(f"{path.name} does not identify itself as Tale of Two Wastelands")
        version = description.strip() if version_key(description) else ""
        ttw = version
    else:
        if b"taleoftwowastelands.esm" not in masters:
            raise WabbajackError(f"{path.name} is not a YUPTTW plugin: its TTW master is missing")
        yup = re.search(r"\bYUP(?:TTW)?\s+v?(\d+(?:\.\d+)+)\b", description, re.I)
        compatible = re.search(r"\bTTW\s+v?(\d+(?:\.\d+)+)\b", description, re.I)
        if not yup:
            raise WabbajackError(f"{path.name} does not identify its YUPTTW version")
        version = yup[1]
        ttw = compatible[1] if compatible else ""
    verify_version(task, version)
    return {"version": version, "ttw": ttw}


def version_detail(task, identity):
    version = identity.get("version")
    detail = f"Detected {task.label} {version}" if version else f"Verified {task.label} content"
    if task.id.startswith("yupttw:") and identity.get("ttw"):
        detail += f" for TTW {identity['ttw']}"
    if task.required_version:
        detail += "; matches the list's required version"
    return detail
