from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

from Games.Custom.custom_game import StandardCustomGame
from Utils.atomic_write import write_atomic
from Utils.mods.modlist import read_modlist


_DEFINITION = {
    "name": "Rimworld",
    "game_id": "Rimworld",
    "exe_name": "start_RimWorld.sh",
    "exe_name_alts": ["RimWorldWin64.exe", "RimWorldWin.exe"],
    "deploy_type": "standard",
    "mod_data_path": "Mods",
    "steam_id": "294100",
    "nexus_game_domain": "rimworld",
    "mod_folder_strip_prefixes": ["Mods"],
    "restore_before_deploy": True,
    "normalize_folder_case": True,
    "editable": False,
}

_CONFIG_NAMES = ("RimWorld by Ludeon Studios", "RimWorld")
_SYNC_STATE = "rimworld_managed_ids.json"


def _package_id(about_xml: Path) -> str | None:
    try:
        root = ET.parse(about_xml).getroot()
    except (OSError, ET.ParseError):
        return None
    for child in root:
        if child.tag.casefold() == "packageid" and child.text:
            return child.text.strip() or None
    return None


class RimWorld(StandardCustomGame):
    post_deploy_failure_is_fatal = True

    def __init__(self) -> None:
        super().__init__(dict(_DEFINITION))

    @property
    def is_custom(self) -> bool:
        return False

    @property
    def mod_staging_requires_subdir(self) -> bool:
        return True

    @property
    def mod_staging_wrap_signals(self) -> tuple[set[str], set[str]]:
        return ({"About/About.xml"}, set())

    @property
    def mod_staging_already_structured_markers(self) -> set[str]:
        return {"About/About.xml"}

    @property
    def additional_install_logic(self) -> list:
        return [self._flatten_bundle_wrapper]

    @staticmethod
    def _flatten_bundle_wrapper(mod_dir: Path, _name: str, log_fn) -> bool:
        children = list(mod_dir.iterdir())
        folders = [child for child in children if child.is_dir()]
        if len(folders) != 1 or (mod_dir / "About" / "About.xml").is_file():
            return False
        wrapper = folders[0]
        if (wrapper / "About" / "About.xml").is_file():
            return False
        if not any((child / "About" / "About.xml").is_file()
                   for child in wrapper.iterdir() if child.is_dir()):
            return False
        contents = list(wrapper.iterdir())
        if any((mod_dir / child.name).exists() for child in contents):
            return False
        for child in contents:
            child.rename(mod_dir / child.name)
        wrapper.rmdir()
        log_fn(f"RimWorld: removed archive wrapper '{wrapper.name}/'.")
        return True

    def prepare_mod_staging(self, staging: Path, log_fn) -> bool:
        if not staging.is_dir():
            return False
        from Utils.mods.install import wrap_flat_mod_dir

        changed = False
        for mod_dir in staging.iterdir():
            if not mod_dir.is_dir() or mod_dir.is_symlink():
                continue
            flattened = self._flatten_bundle_wrapper(mod_dir, mod_dir.name, log_fn)
            wrapped = wrap_flat_mod_dir(
                mod_dir, {"about/about.xml"}, set(), {"about/about.xml"})
            changed = flattened or wrapped or changed
        if changed:
            log_fn("RimWorld: normalized staged mod folders before building the filemap.")
        return changed

    def _mods_config_path(self) -> Path | None:
        from Utils.executables.launch import resolve_game_exe

        executable = resolve_game_exe(self)
        if executable is None:
            return None
        roots: list[Path] = []
        if executable.suffix.casefold() == ".exe":
            prefix = self.get_prefix_path()
            if prefix is None:
                return None
            drive_c = prefix / "drive_c"
            if not drive_c.is_dir():
                drive_c = prefix / "pfx" / "drive_c"
            users = drive_c / "users"
            if not users.is_dir():
                return None
            candidates = sorted(p for p in users.iterdir() if p.is_dir())
            candidates.sort(key=lambda p: p.name != "steamuser")
            roots = [p / "AppData" / "LocalLow" / "Ludeon Studios"
                     for p in candidates]
        else:
            roots = [Path.home() / ".config" / "unity3d" / "Ludeon Studios"]
        for root in roots:
            for name in _CONFIG_NAMES:
                path = root / name / "Config" / "ModsConfig.xml"
                if path.is_file():
                    return path
        return None

    def _package_ids_by_mod(self, staging: Path, *, deployed: bool = False,
                            legacy: bool = False
                            ) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        if not staging.is_dir():
            return result
        mods_dir = (self.get_mod_data_path()
                    if deployed and not self.vfs_launch_enabled else None)
        for mod_dir in staging.iterdir():
            if not mod_dir.is_dir():
                continue
            ids: list[str] = []
            for unit in mod_dir.iterdir():
                if not unit.is_dir():
                    continue
                about = unit / "About" / "About.xml"
                if not about.is_file():
                    continue
                package_id = unit.name if legacy else _package_id(about)
                if package_id:
                    if mods_dir is not None:
                        installed_about = mods_dir / unit.name / "About" / "About.xml"
                        installed = (unit.name if legacy and installed_about.is_file()
                                     else _package_id(installed_about))
                        if (installed is None
                                or installed.casefold() != package_id.casefold()):
                            continue
                    ids.append(package_id)
            result[mod_dir.name] = ids
        return result

    def post_deploy(self, log_fn=None) -> None:
        log = log_fn or (lambda _message: None)
        config_path = self._mods_config_path()
        if config_path is None:
            log("RimWorld load order: ModsConfig.xml not found; launch the "
                "selected native or Proton installation once, then deploy again.")
            return

        profile_dir = self._active_profile_dir or (
            self.get_profile_root() / "profiles" / "default")
        modlist_path = profile_dir / "modlist.txt"
        if not modlist_path.is_file():
            raise RuntimeError(f"RimWorld modlist not found: {modlist_path}")
        entries = read_modlist(modlist_path)

        parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
        root = ET.parse(config_path, parser=parser).getroot()
        if root.tag != "ModsConfigData":
            raise ValueError(f"Unexpected RimWorld config root: {root.tag}")
        active = root.find("activeMods")
        if active is None:
            raise ValueError("RimWorld ModsConfig.xml has no activeMods element")
        version = (root.findtext("version") or "").strip()
        legacy = version.startswith(("1.0.", "0.")) or (
            not version and root.find("buildNumber") is not None)

        packages = self._package_ids_by_mod(
            self.get_effective_mod_staging_path(), deployed=True, legacy=legacy)
        missing = [entry.name for entry in entries
                   if entry.enabled and not entry.is_separator
                   and not packages.get(entry.name)]
        if missing:
            identifier = "About/About.xml" if legacy else "a packageId"
            log(f"RimWorld load order: no direct mod folder with {identifier} "
                "for " + ", ".join(missing[:5])
                + (f" (+{len(missing) - 5} more)" if len(missing) > 5 else "")
                + "; these entries were not added to ModsConfig.xml.")
        managed_now = {package_id.casefold() for ids in packages.values()
                       for package_id in ids}
        state_path = self.get_profile_root() / _SYNC_STATE
        try:
            previous = json.loads(state_path.read_text(encoding="utf-8"))
            previously_managed = {str(value).casefold() for value in previous}
        except (OSError, ValueError, TypeError):
            previously_managed = set()

        current = [item.text.strip() for item in active.findall("li")
                   if item.text and item.text.strip()]
        unmanaged = [package_id for package_id in current
                     if package_id.casefold()
                     not in managed_now | previously_managed]

        ordered: list[str] = []
        seen: set[str] = set()
        for entry in reversed(entries):
            if entry.is_separator or not entry.enabled:
                continue
            for package_id in packages.get(entry.name, ()):
                key = package_id.casefold()
                if key not in seen:
                    ordered.append(package_id)
                    seen.add(key)
        wanted = unmanaged + ordered
        if current != wanted:
            for item in list(active):
                if item.tag == "li":
                    active.remove(item)
            for package_id in wanted:
                ET.SubElement(active, "li").text = package_id
            write_atomic(config_path, ET.tostring(
                root, encoding="utf-8", xml_declaration=True))
        write_atomic(state_path, json.dumps(sorted(managed_now)).encode("utf-8"))
        log(f"RimWorld load order: synced {len(ordered)} Amethyst mod(s) to {config_path}.")
