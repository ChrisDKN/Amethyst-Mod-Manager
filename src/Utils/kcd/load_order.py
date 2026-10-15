from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from Utils.atomic_write import write_atomic, write_atomic_text
from Utils.kcd.mods import child_path, game_version, inspect_mod, mod_folders
from Utils.mods.modlist import read_modlist

STATE_FILE = "kcd_mod_order_state.json"


def workshop_folders(game) -> list[Path]:
    if game.steam_id != "1771300":
        return []
    from Utils.launchers.steam import all_steamapps_dirs, owning_steamapps_dir
    libraries = all_steamapps_dirs()
    owner = owning_steamapps_dir(game.steam_id, game.get_game_path())
    if owner is not None:
        libraries.insert(0, owner)
    found = {}
    for library in libraries:
        root = library / "workshop" / "content" / game.steam_id
        if root.is_dir():
            for folder in sorted(root.iterdir(), key=lambda p: p.name):
                if folder.is_dir() and folder.name.isdigit():
                    found.setdefault(folder.name, folder)
    return list(found.values())


def folder_priorities(game, profile_dir: Path) -> dict[str, int]:
    from Utils.filegraph.deploy import absolute_destination, require_active
    names = [entry.name for entry in read_modlist(profile_dir / "modlist.txt")
             if not entry.is_separator]
    ranks = {name: rank for rank, name in enumerate(reversed(names))}
    ranks["[Overwrite]"] = len(names)
    ranks["[Root_Folder]"] = len(names) + 1
    prefix = os.path.abspath(game.get_mod_data_path()) + os.sep
    priorities = {}
    for entry in require_active().plan.entries:
        destination = absolute_destination(game, entry)
        if destination is None:
            continue
        path = os.path.abspath(destination)
        if not path.casefold().startswith(prefix.casefold()):
            continue
        folder, sep, _ = path[len(prefix):].partition(os.sep)
        if sep:
            key = folder.casefold()
            priorities[key] = max(priorities.get(key, -1), ranks.get(entry.mod_name, 0))
    return priorities


def build_order(game, mods_dir: Path, profile_dir: Path, log_fn,
                *, workshop: list[Path] | None = None) -> str:
    sequel = game.steam_id == "1771300"
    priorities = folder_priorities(game, profile_dir)
    version = game_version(game.get_game_path())
    manual = [inspect_mod(folder, sequel=sequel, version=version)
              for folder in mod_folders(mods_dir)]
    external = []
    if sequel:
        external = [inspect_mod(folder, sequel=True, version=version)
                    for folder in (workshop_folders(game) if workshop is None else workshop)]
    managed = [mod for mod in manual if mod.folder.name.casefold() in priorities]
    unmanaged = external + [mod for mod in manual if mod not in managed]
    managed.sort(key=lambda mod: (priorities[mod.folder.name.casefold()],
                                 mod.folder.name.casefold()))
    original = child_path(mods_dir, "mod_order.txt")
    old_order = (original.read_text(encoding="utf-8-sig").splitlines()
                 if original.is_file() else None)
    old_ids = [line.strip() for line in old_order or []
               if line.strip() and not line.lstrip().startswith(("#", ";"))]
    active_ids = set(old_ids)
    selected = managed + [mod for mod in unmanaged
                          if old_order is None or mod.identifier in active_ids]
    seen = {}
    for mod in manual + external:
        for warning in mod.warnings:
            log_fn(f"KCD: {warning}")
            game.add_deploy_warning(warning)
        if not mod.identifier:
            if mod in selected or (old_order is None and mod in unmanaged):
                raise RuntimeError(
                    f"Cannot determine the KCD2 mod ID for {mod.folder}. "
                    "Correct its mod.manifest or disable the mod before deploying. "
                    "No load-order file was written.")
            continue
        if mod in selected:
            previous = seen.setdefault(mod.identifier, mod.folder)
            if previous != mod.folder:
                warning = (f"Duplicate KCD mod ID {mod.identifier!r}: {previous} and "
                           f"{mod.folder}. The game cannot load both independently.")
                log_fn(f"KCD: {warning}")
                game.add_deploy_warning(warning)
    managed_ids = {mod.identifier for mod in managed}
    order = (old_ids if old_order is not None else
             [mod.identifier for mod in unmanaged])
    order = [identifier for identifier in order if identifier not in managed_ids]
    order.extend(mod.identifier for mod in managed)
    return "".join(identifier + "\n" for identifier in dict.fromkeys(order) if identifier)


def write_order(game, mods_dir: Path, profile_dir: Path, log_fn, *, physical: bool) -> None:
    text = build_order(game, mods_dir, profile_dir, log_fn)
    path = child_path(mods_dir, "mod_order.txt")
    if physical:
        state_path = profile_dir / STATE_FILE
        if state_path.exists():
            raise RuntimeError("KCD load-order recovery state already exists. Restore before deploying again.")
        original = child_path(child_path(mods_dir.parent, mods_dir.name + "_Core"),
                              "mod_order.txt")
        state = {
            "target": str(path),
            "generated_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "original_sha256": (hashlib.sha256(original.read_bytes()).hexdigest()
                                if original.is_file() else None),
            "restoring": False,
        }
        write_atomic_text(state_path, json.dumps(state))
    write_atomic_text(path, text)
    log_fn(f"KCD: wrote {len(text.splitlines())} entries to {path} in mod priority order.")


def remove_generated_order(mods_dir: Path, profile_dir: Path, log_fn) -> None:
    state_path = profile_dir / STATE_FILE
    if not state_path.exists():
        return
    state = json.loads(state_path.read_text(encoding="utf-8"))
    target = Path(state["target"])
    current = child_path(mods_dir, "mod_order.txt")
    if (target.parent != mods_dir or target.name.casefold() != "mod_order.txt"
            or (current.exists() and target != current)):
        raise RuntimeError("KCD load-order recovery target does not match this game folder.")
    core = child_path(mods_dir.parent, mods_dir.name + "_Core")
    if not core.is_dir():
        digest = hashlib.sha256(target.read_bytes()).hexdigest() if target.is_file() else None
        if state.get("restoring") and digest == state["original_sha256"]:
            state_path.unlink()
            return
        raise RuntimeError("KCD load-order recovery is missing the Mods_Core backup; recovery state retained.")
    state["restoring"] = True
    write_atomic_text(state_path, json.dumps(state))
    if target.is_file():
        content = target.read_bytes()
        if hashlib.sha256(content).hexdigest() != state["generated_sha256"]:
            recovery = profile_dir / "kcd_mod_order_runtime.txt"
            index = 1
            while recovery.exists():
                recovery = profile_dir / f"kcd_mod_order_runtime.{index}.txt"
                index += 1
            write_atomic(recovery, content)
            log_fn(f"KCD: preserved changed load order at {recovery}.")
        target.unlink()
