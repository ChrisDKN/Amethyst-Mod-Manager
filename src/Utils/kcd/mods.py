from __future__ import annotations

from dataclasses import dataclass
import fnmatch
from pathlib import Path
import re
import shutil
import xml.etree.ElementTree as ET


def child_path(root: Path, name: str) -> Path:
    matches = [p for p in root.iterdir() if p.name.casefold() == name.casefold()] if root.is_dir() else []
    if len(matches) > 1:
        raise RuntimeError(f"Multiple {name} paths exist in {root}: "
                           + ", ".join(p.name for p in matches))
    return matches[0] if matches else root / name


def mods_path(root: Path) -> Path:
    path = child_path(root, "mods")
    if not path.exists():
        backups = [p for p in root.iterdir()
                   if p.is_dir() and p.name.casefold() == "mods_core"] if root.is_dir() else []
        if len(backups) > 1:
            raise RuntimeError(f"Multiple Mods backups exist in {root}.")
        if backups:
            return root / backups[0].name[:-5]
    return path


def game_version(root: Path) -> str:
    try:
        text = child_path(root, "system.cfg").read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return ""
    match = re.search(r'^\s*wh_sys_version\s*=\s*["\']?([^\s"\';]+)', text, re.M | re.I)
    return match.group(1) if match else ""


@dataclass
class Mod:
    folder: Path
    identifier: str
    warnings: list[str]


def inspect_mod(folder: Path, *, sequel: bool, version: str = "") -> Mod:
    warnings = []
    identifier = folder.name if not sequel else ""
    manifest = child_path(folder, "mod.manifest")
    try:
        root = ET.fromstring(manifest.read_bytes())
        if root.tag != "kcd_mod":
            raise ValueError("expected a kcd_mod root element")
    except (OSError, ET.ParseError, ValueError) as exc:
        warnings.append(f"{folder.name}: missing or invalid mod.manifest ({exc}).")
        return Mod(folder, identifier, warnings)
    if sequel:
        identifier = (root.findtext("info/modid") or "").strip()
        if not identifier:
            name = (root.findtext("info/name") or "").strip()
            identifier = name if re.fullmatch(r"[a-z_]+", name) else ""
            warnings.append(f"{folder.name}: mod.manifest has no explicit modid; "
                            "the game derives an ID from its name.")
        elif not re.fullmatch(r"[a-z_]+", identifier):
            warnings.append(f"{folder.name}: modid {identifier!r} does not follow "
                            "Warhorse's lowercase-letter/underscore format.")
        if identifier and (any(c.isspace() for c in identifier)
                           or any(c in identifier for c in "/\\#;")):
            identifier = ""
    supports = root.find("supports")
    if supports is not None:
        patterns = [(item.text or "").strip() for item in supports
                    if item.tag in {"version", "kcd_version"} and (item.text or "").strip()]
        if patterns and version and not any(fnmatch.fnmatchcase(version, p) for p in patterns):
            warnings.append(f"{folder.name}: supports {', '.join(patterns)}, but the game "
                            f"is {version}; the game may disable this mod.")
    return Mod(folder, identifier, warnings)


def mod_folders(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    result = []
    for folder in sorted(root.iterdir(), key=lambda p: p.name.casefold()):
        if not folder.is_dir() or folder.name.startswith("."):
            continue
        if (child_path(folder, "mod.manifest").is_file()
                or child_path(folder, "mod.cfg").is_file()
                or child_path(folder, "Data").is_dir()
                or child_path(folder, "Localization").is_dir()):
            result.append(folder)
    return result


def normalise_layout(root: Path, mod_name: str, log_fn, *, sequel: bool) -> bool:
    from Nexus.nexus_meta import read_meta
    from Utils.filegraph.paths import EXCLUDE_NAMES

    if not root.is_dir() or root.is_symlink() or read_meta(root / "meta.ini").root_folder:
        return False
    changed = False
    while not mod_folders(root) and not child_path(root, "mod.manifest").is_file():
        directories = [p for p in root.iterdir() if p.is_dir()
                       and p.name.casefold() not in {"fomod", "root_folder"}
                       and not p.name.startswith(".")]
        if len(directories) != 1:
            break
        wrapper = directories[0]
        if wrapper.is_symlink() or not any(p.name.casefold() == "mod.manifest"
                                          for p in wrapper.rglob("*")):
            break
        children = list(wrapper.iterdir())
        if any(child_path(root, p.name).exists() for p in children):
            break
        for path in children:
            shutil.move(str(path), str(root / path.name))
        wrapper.rmdir()
        changed = True
    manifest = child_path(root, "mod.manifest")
    flat = manifest.is_file() or (not mod_folders(root) and (
        child_path(root, "mod.cfg").is_file() or child_path(root, "Data").is_dir()
        or child_path(root, "Localization").is_dir()))
    if flat:
        identifier = inspect_mod(root, sequel=sequel).identifier if manifest.is_file() else ""
        name = identifier if sequel and re.fullmatch(r"[a-z_]+", identifier) else mod_name
        name = re.sub(r"[^a-zA-Z0-9_-]+", "_", name).strip("_") or "mod"
        if sequel:
            name = name.lower()
        target = child_path(root, name)
        index = 1
        while target.exists():
            target = child_path(root, f"{name}_{index}")
            index += 1
        children = list(root.iterdir())
        target.mkdir()
        for path in children:
            if path.name.lower() in EXCLUDE_NAMES or path.name.lower() == "root_folder":
                continue
            shutil.move(str(path), str(target / path.name))
        changed = True
    if changed:
        log_fn(f"KCD: corrected the mod folder structure for '{mod_name}'.")
    return changed
