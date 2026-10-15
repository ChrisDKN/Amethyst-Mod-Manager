from __future__ import annotations

import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET

from Utils.atomic_write import write_atomic, write_atomic_text
from Utils.mods.modlist import read_modlist

STATE_FILE = "rdr2_lml_order_state.json"


def _order_path(folder: Path) -> Path:
    matches = [p for p in folder.iterdir() if p.name.casefold() == "mods.xml"] if folder.is_dir() else []
    if len(matches) > 1:
        raise RuntimeError(f"Multiple mods.xml files exist in {folder}.")
    return matches[0] if matches else folder / "mods.xml"


def _key(folder: str) -> str:
    return folder.replace("\\", "/").strip("/").casefold()


def build_order(game, mods_dir: Path, profile_dir: Path) -> bytes:
    from Utils.filegraph.deploy import entry_relative_to, require_active

    names = [entry.name for entry in read_modlist(profile_dir / "modlist.txt")
             if entry.enabled and not entry.is_separator]
    ranks = {name: rank for rank, name in enumerate(reversed(names))}
    ranks["[Overwrite]"] = len(names)
    ranks["[Root_Folder]"] = len(names) + 1
    folders = {}
    target_roots = {}
    for entry in require_active().plan.entries:
        relative = entry_relative_to(game, entry, game.get_mod_data_path(), target_roots)
        if relative is None:
            continue
        path = Path(relative)
        if path.name.casefold() != "install.xml" or path.parent == Path("."):
            continue
        if not (mods_dir / path).is_file():
            continue
        folder = path.parent.as_posix()
        folders[_key(folder)] = (ranks.get(entry.mod_name, -1), folder)

    original = _order_path(mods_dir)
    root = ET.parse(original).getroot() if original.is_file() else ET.Element("ModsManager")
    if root.tag != "ModsManager":
        raise RuntimeError(f"Unexpected LML load-order format in {original}.")
    mods = root.find("Mods")
    if mods is None:
        mods = ET.SubElement(root, "Mods")
    order = root.find("LoadOrder")
    if order is None:
        order = ET.SubElement(root, "LoadOrder")
    old_ranks = {_key(node.text or ""): index for index, node in enumerate(order.findall("Mod"))}
    existing = {}
    for node in mods.findall("Mod"):
        key = _key(node.get("folder", ""))
        if key in folders:
            mods.remove(node)
            existing.setdefault(key, node)
    for node in list(order):
        if node.tag == "Mod" and _key(node.text or "") in folders:
            order.remove(node)
    for key in sorted(folders, key=lambda key: (folders[key][0], old_ranks.get(key, -1), key)):
        _rank, folder = folders[key]
        node = existing.get(key)
        if node is None:
            node = ET.Element("Mod")
            ET.SubElement(node, "Name").text = folder
        node.set("folder", folder)
        for tag, value in (("Enabled", "true"), ("Overwrite", "false")):
            child = node.find(tag)
            if child is None:
                child = ET.SubElement(node, tag)
            child.text = value
        if node.find("DisabledGroups") is None:
            ET.SubElement(node, "DisabledGroups")
        mods.append(node)
        ET.SubElement(order, "Mod").text = folder
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True) + b"\n"


def write_order(game, mods_dir: Path, profile_dir: Path, log_fn, *, physical: bool) -> None:
    if mods_dir.is_symlink():
        raise RuntimeError(f"Cannot generate LML load order through a directory symlink: {mods_dir}")
    content = build_order(game, mods_dir, profile_dir)
    path = _order_path(mods_dir)
    if physical:
        state_path = profile_dir / STATE_FILE
        if state_path.exists():
            raise RuntimeError("LML load-order recovery state already exists. Restore before deploying again.")
        core = mods_dir.parent / (mods_dir.name + "_Core")
        if not core.is_dir():
            raise RuntimeError("LML load-order generation requires the lml_Core backup.")
        original = _order_path(core)
        state = {
            "target": str(path),
            "generated_sha256": hashlib.sha256(content).hexdigest(),
            "original_sha256": (hashlib.sha256(original.read_bytes()).hexdigest()
                                if original.is_file() else None),
            "restoring": False,
        }
        write_atomic_text(state_path, json.dumps(state))
    write_atomic(path, content)
    log_fn(f"LML: wrote {path} in mod priority order (highest priority loads last).")


def remove_generated_order(mods_dir: Path, profile_dir: Path, log_fn) -> None:
    state_path = profile_dir / STATE_FILE
    if not state_path.exists():
        return
    state = json.loads(state_path.read_text(encoding="utf-8"))
    target = Path(state["target"])
    current = _order_path(mods_dir)
    if (target.parent != mods_dir or target.name.casefold() != "mods.xml"
            or (current.exists() and target != current) or mods_dir.is_symlink()):
        raise RuntimeError("LML load-order recovery target does not match this game folder.")
    core = mods_dir.parent / (mods_dir.name + "_Core")
    if not core.is_dir():
        digest = hashlib.sha256(current.read_bytes()).hexdigest() if current.is_file() else None
        if state.get("restoring") and digest == state["original_sha256"]:
            state_path.unlink()
            return
        raise RuntimeError("LML load-order recovery is missing lml_Core; recovery state retained.")
    state["restoring"] = True
    write_atomic_text(state_path, json.dumps(state))
    if target.is_file():
        content = target.read_bytes()
        if hashlib.sha256(content).hexdigest() != state["generated_sha256"]:
            recovery = profile_dir / "rdr2_lml_order_runtime.xml"
            index = 1
            while recovery.exists():
                recovery = profile_dir / f"rdr2_lml_order_runtime.{index}.xml"
                index += 1
            write_atomic(recovery, content)
            log_fn(f"LML: preserved changed load order at {recovery}.")
        target.unlink()
