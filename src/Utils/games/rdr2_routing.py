from collections import defaultdict
from pathlib import PurePosixPath

from Utils.deployment.custom_rules import compute_routed_destinations
from Utils.deployment.shared import CustomRule


_LOADERS = {
    "dinput8.dll", "version.dll", "scripthookrdr2.dll",
    "scripthookrdrnetapi.dll", "modmanager.core.dll",
    "modmanager.nativeinterop.dll", "vfs.asi",
}


def is_payload(path: str) -> bool:
    parts = path.replace("\\", "/").lower().split("/")
    return not any(
        part in {"modmanager", "examples", "for errors"}
        or part.startswith(("example ", "examples "))
        for part in parts[:-1]
    ) and parts[-1] not in {"__folder_managed_by_vortex", "_place all this in the game root"}


def resolve_package(
    paths: list[str], mod_name: str, rules: list[CustomRule],
) -> tuple[dict[str, list[tuple[bool, str]]], list[list[str]]]:
    paths = [path.replace("\\", "/") for path in paths]
    manifests = {
        str(PurePosixPath(path).parent).lower(): str(PurePosixPath(path).parent)
        for path in paths if PurePosixPath(path).name.lower() == "install.xml"
    }

    manifest_parents = sorted(manifests, key=len, reverse=True)

    def lml_path(path: str) -> str | None:
        parts = path.split("/")
        lower = [part.lower() for part in parts]
        if "lml" in lower[:-1]:
            return "/".join(parts[lower.index("lml") + 1:])
        for parent in manifest_parents:
            if parent == ".":
                return f"{mod_name}/{path}"
            if path.lower().startswith(parent + "/"):
                return manifests[parent].rsplit("/", 1)[-1] + path[len(parent):]
        for folder in ("stream", "replace"):
            if folder in lower[:-1]:
                return "/".join(parts[lower.index(folder):])
        return None

    protected = {path: lml_path(path) for path in paths}
    anchors = {}
    plugins = defaultdict(list)
    for path in paths:
        name = PurePosixPath(path).name
        if protected[path] is not None:
            continue
        if not (name.lower().endswith(".asi") or name.lower() in _LOADERS):
            continue
        claims = compute_routed_destinations([name], rules).get(name.lower())
        if not claims:
            continue
        parent = path.rpartition("/")[0]
        anchors[parent.lower()] = parent
        if name.lower().endswith(".asi"):
            plugins[name.lower()].append(path)

    variants = [sorted(values) for _, values in sorted(plugins.items()) if len(values) > 1]
    ambiguous = {path.rpartition("/")[0].lower() for group in variants for path in group}
    resolved = {}
    groups = defaultdict(list)
    loose_claims = compute_routed_destinations(paths, rules)
    ordered_anchors = sorted(anchors, key=len, reverse=True)
    for path in paths:
        if protected[path] is not None:
            resolved[path] = [(False, "lml/" + protected[path])]
            continue
        parent = next((anchor for anchor in ordered_anchors
                       if not anchor or path.lower().startswith(anchor + "/")), None)
        if parent is not None and parent not in ambiguous:
            relative = path[len(parent) + 1:] if parent else path
            groups[parent].append((path, relative))
        else:
            resolved[path] = ([(False, "lml/" + path)] if parent in ambiguous
                              else loose_claims.get(path.lower(), [(False, "lml/" + path)]))

    for group in groups.values():
        claims = compute_routed_destinations([relative for _, relative in group], rules)
        for path, relative in group:
            resolved[path] = claims.get(relative.lower(), [(False, relative)])
    return resolved, variants
