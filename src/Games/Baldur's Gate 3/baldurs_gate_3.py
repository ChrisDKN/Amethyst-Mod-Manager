"""
baldurs_gate_3.py
Game handler for Baldur's Gate 3.

Mod structure:
  Mods install into the Larian AppData Mods folder.  Two layouts are
  supported:
    - Proton prefix:  <prefix>/drive_c/users/steamuser/AppData/Local/
                      Larian Studios/Baldur's Gate 3/Mods/
    - Native Linux:   ~/.local/share/Larian Studios/Baldur's Gate 3/Mods/
  The prefix is preferred when configured; otherwise the native Linux
  root is used if it exists.
  Staged mods live in Profiles/Baldur's Gate 3/mods/

  Only .pak files are deployed to the Mods folder — other files (readmes,
  images, etc.) are excluded from the filemap via mod_install_extensions.

  Mods that contain a bin/ folder have those files deployed to the game's
  root install directory instead (via filemap_root.txt), since BG3 native
  mods expect bin/ to sit alongside the game executable.

  After deploying .pak files, modsettings.lsx is generated automatically so
  BG3 recognises the installed mods.  Mod load order follows the modlist
  priority, with dependencies topologically sorted to appear before the
  mods that require them.
"""

import json
from pathlib import Path

from Games.base_game import BaseGame
from Utils.deploy import (
    CustomRule, LinkMode, deploy_filemap, deploy_core, move_to_core, restore_data_core,
    deploy_custom_rules, load_per_mod_strip_prefixes,
    load_separator_deploy_paths, expand_separator_deploy_paths,
    cleanup_custom_deploy_dirs, restore_custom_rules,
)
from Utils.modlist import read_modlist
from Utils.config_paths import get_profiles_dir
from Utils.modsettings import write_modsettings, write_vanilla_modsettings
from Utils.steam_finder import find_prefix

_PROFILES_DIR = get_profiles_dir()

# Path inside the Proton prefix where the Larian data folder lives
_PREFIX_LARIAN_SUBPATH = Path(
    "drive_c/users/steamuser/AppData/Local/Larian Studios/Baldur's Gate 3"
)

# Native Linux (Steam Deck) build stores its data here
_NATIVE_LARIAN_ROOT = (
    Path.home() / ".local/share/Larian Studios/Baldur's Gate 3"
)

# Subpaths within the Larian root
_MODS_REL = Path("Mods")
_MODSETTINGS_REL = Path("PlayerProfiles/Public/modsettings.lsx")


class BaldursGate3(BaseGame):

    def __init__(self):
        self._game_path: Path | None = None
        self._prefix_path: Path | None = None
        self._deploy_mode: LinkMode = LinkMode.HARDLINK
        self._staging_path: Path | None = None
        self._patch_version: int = 8
        self.load_paths()

    # -----------------------------------------------------------------------
    # Identity
    # -----------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "Baldur's Gate 3"

    @property
    def game_id(self) -> str:
        return "baldurs_gate_3"

    @property
    def exe_name(self) -> str:
        return "bin/bg3.exe"

    @property
    def exe_name_alts(self) -> list[str]:
        # Native Linux build ships a bare ELF binary at bin/bg3
        return ["bin/bg3"]

    @property
    def steam_id(self) -> str:
        return "1086940"

    @property
    def nexus_game_domain(self) -> str:
        return "baldursgate3"
    
    @property
    def mod_required_top_level_folders(self) -> set[str]:
        return {"data","bin","generated","public","video","mods"}
    
    @property
    def mod_required_file_types(self) -> set[str]:
        return {".pak"}
    
    @property
    def mod_auto_strip_until_required(self) -> bool:
        return True
    
    @property
    def mod_install_as_is_if_no_match(self) -> bool:
        return True

    @property
    def custom_routing_rules(self) -> list:
        return [
            CustomRule(dest="Data", folders=["generated"], flatten=True),
            CustomRule(dest="Data", folders=["public"], flatten=True),
            CustomRule(dest="Data", folders=["video"], flatten=True),
            CustomRule(dest="Data", folders=["mods"], flatten=True),
            CustomRule(dest="Data", folders=["Cursors"], flatten=True),
            CustomRule(dest="bin", filenames=["DWrite.dll"], flatten=True),
            CustomRule(dest="", folders=["bin"], flatten=True),
            CustomRule(dest="", folders=["data"], flatten=True),
        ]
    
    @property
    def plugin_extensions(self) -> list[str]:
        return []
    
    @property
    def conflict_ignore_filenames(self) -> set[str]:
        return {"info.json","*.txt"}
    
    @property
    def frameworks(self) -> dict[str, str]:
        return {
                "Script Extender": "bin/DWrite.dll",
                "Native Mod Loader":"bin/bink2w64.dll"
            }

    @property
    def wine_dll_overrides(self) -> dict[str, str]:
        return {"DWrite": "native,builtin"}

    # -----------------------------------------------------------------------
    # Paths
    # -----------------------------------------------------------------------

    def get_game_path(self) -> Path | None:
        return self._game_path

    def _larian_root(self) -> Path | None:
        """Return the Larian data root, preferring Proton prefix, then native Linux."""
        if self._prefix_path is not None:
            return self._prefix_path / _PREFIX_LARIAN_SUBPATH
        if _NATIVE_LARIAN_ROOT.is_dir():
            return _NATIVE_LARIAN_ROOT
        return None

    def get_mod_data_path(self) -> Path | None:
        """Mods deploy into the Larian AppData Mods folder (prefix or native)."""
        root = self._larian_root()
        return root / _MODS_REL if root is not None else None

    def get_mod_staging_path(self) -> Path:
        if self._staging_path is not None:
            return self._staging_path / "mods"
        return _PROFILES_DIR / self.name / "mods"

    def get_hardlink_deploy_targets(self) -> list[tuple[str, "Path | None"]]:
        if self._prefix_path is None and _NATIVE_LARIAN_ROOT.is_dir():
            data_target: Path | None = _NATIVE_LARIAN_ROOT
            label = "Larian data (native Linux)"
        else:
            data_target = self._prefix_path
            label = "Proton prefix"
        return [
            ("Game directory", self._game_path),
            (label, data_target),
        ]

    # -----------------------------------------------------------------------
    # Configuration persistence
    # -----------------------------------------------------------------------

    def load_paths(self) -> bool:
        self._migrate_old_config()
        if not self._paths_file.exists():
            self._game_path = None
            self._prefix_path = None
            self._staging_path = None
            return False
        try:
            data = json.loads(self._paths_file.read_text(encoding="utf-8"))
            raw = data.get("game_path", "")
            if raw:
                self._game_path = Path(raw)
            raw_pfx = data.get("prefix_path", "")
            if raw_pfx:
                self._prefix_path = Path(raw_pfx)
            raw_mode = data.get("deploy_mode", "hardlink")
            self._deploy_mode = {
                "symlink": LinkMode.SYMLINK,
                "copy":    LinkMode.SYMLINK,
            }.get(raw_mode, LinkMode.HARDLINK)
            raw_staging = data.get("staging_path", "")
            if raw_staging:
                self._staging_path = Path(raw_staging)
            try:
                pv = int(data.get("patch_version", 8))
            except (TypeError, ValueError):
                pv = 8
            self._patch_version = pv if pv in (6, 7, 8) else 8
            self._validate_staging()
            if not self._prefix_path or not self._prefix_path.is_dir():
                if not _NATIVE_LARIAN_ROOT.is_dir():
                    found = find_prefix(self.steam_id)
                    if found:
                        self._prefix_path = found
                        self.save_paths()
            return bool(self._game_path)
        except (json.JSONDecodeError, OSError):
            pass
        self._game_path = None
        self._prefix_path = None
        return False

    def save_paths(self) -> None:
        self._paths_file.parent.mkdir(parents=True, exist_ok=True)
        mode_str = {
            LinkMode.SYMLINK: "symlink",
            LinkMode.COPY:    "copy",
        }.get(self._deploy_mode, "hardlink")
        data = {
            "game_path":    str(self._game_path)    if self._game_path    else "",
            "prefix_path":  str(self._prefix_path)  if self._prefix_path  else "",
            "deploy_mode":  mode_str,
            "staging_path": str(self._staging_path) if self._staging_path else "",
            "patch_version": self._patch_version,
        }
        self._paths_file.write_text(
            json.dumps(data, indent=2), encoding="utf-8"
        )

    def set_game_path(self, path: Path | str | None) -> None:
        self._game_path = Path(path) if path else None
        self.save_paths()

    def set_staging_path(self, path: "Path | str | None") -> None:
        self._staging_path = Path(path) if path else None
        self.save_paths()

    def get_prefix_path(self) -> Path | None:
        return self._prefix_path

    def get_deploy_mode(self) -> LinkMode:
        return self._deploy_mode

    def set_deploy_mode(self, mode: LinkMode) -> None:
        self._deploy_mode = mode
        self.save_paths()

    def set_prefix_path(self, path: Path | str | None) -> None:
        self._prefix_path = Path(path) if path else None
        self.save_paths()

    def get_patch_version(self) -> int:
        return self._patch_version

    def set_patch_version(self, version: int) -> None:
        if version not in (6, 7, 8):
            version = 8
        self._patch_version = version
        self.save_paths()

    # -----------------------------------------------------------------------
    # Deployment
    # -----------------------------------------------------------------------

    def deploy(self, log_fn=None, mode: LinkMode = LinkMode.HARDLINK,
               profile: str = "default", progress_fn=None) -> None:
        """Deploy staged .pak mods into the Proton prefix Mods folder.

        Workflow:
          1. Move everything currently in the Mods folder → Mods_Core/
          2. Hard-link every .pak listed in filemap.txt into the Mods folder
          3. Hard-link vanilla .pak files from Mods_Core/ for anything not
             provided by a mod
          4. Generate modsettings.lsx so BG3 recognises the installed mods
        (Root Folder deployment is handled by the GUI after this returns.)
        """
        _log = log_fn or (lambda _: None)

        larian_root = self._larian_root()
        if larian_root is None:
            raise RuntimeError(
                "No Larian data folder found. Configure the Proton prefix, "
                f"or install the native Linux build so {_NATIVE_LARIAN_ROOT} exists."
            )

        mods_dir = larian_root / _MODS_REL
        filemap  = self.get_effective_filemap_path()
        staging  = self.get_effective_mod_staging_path()
        modlist  = self.get_profile_root() / "profiles" / profile / "modlist.txt"

        mods_dir.mkdir(parents=True, exist_ok=True)

        if not filemap.is_file():
            raise RuntimeError(
                f"filemap.txt not found: {filemap}\n"
                "Run 'Build Filemap' before deploying."
            )

        profile_dir = self.get_profile_root() / "profiles" / profile
        per_mod_strip = load_per_mod_strip_prefixes(profile_dir)

        custom_rules = self.custom_routing_rules
        custom_exclude: set[str] = set()
        if custom_rules and self._game_path:
            _log("Step 1a: Routing bin/ and generated/ files via custom rules ...")
            custom_exclude = deploy_custom_rules(
                filemap, self._game_path, staging,
                rules=custom_rules,
                mode=mode,
                strip_prefixes=self.mod_folder_strip_prefixes,
                per_mod_strip_prefixes=per_mod_strip,
                log_fn=_log,
            )
            _log(f"  Routed {len(custom_exclude)} file(s) to Data/.")

        _log("Step 1: Moving Mods/ → Mods_Core/ ...")
        moved = move_to_core(mods_dir, log_fn=_log)
        _log(f"  Moved {moved} file(s) to Mods_Core/.")

        _log(f"Step 2: Transferring mod .pak files into Mods/ ({mode.name}) ...")
        _sep_deploy = load_separator_deploy_paths(profile_dir)
        _sep_entries = read_modlist(profile_dir / "modlist.txt") if _sep_deploy else []
        per_mod_deploy = expand_separator_deploy_paths(_sep_deploy, _sep_entries) or None
        linked_mod, placed = deploy_filemap(filemap, mods_dir, staging,
                                            mode=mode,
                                            strip_prefixes=self.mod_folder_strip_prefixes,
                                            per_mod_strip_prefixes=per_mod_strip,
                                            per_mod_deploy_dirs=per_mod_deploy,
                                            log_fn=_log,
                                            progress_fn=progress_fn,
                                            exclude=custom_exclude or None,
                                            core_dir=mods_dir.parent / (mods_dir.name + "_Core"))
        _log(f"  Transferred {linked_mod} mod file(s).")

        _log("Step 3: Filling gaps with vanilla files from Mods_Core/ ...")
        linked_core = deploy_core(mods_dir, placed, mode=mode, log_fn=_log)
        _log(f"  Transferred {linked_core} vanilla file(s).")

        _log("Step 4: Generating modsettings.lsx ...")
        modsettings = larian_root / _MODSETTINGS_REL
        game_data = self._game_path / "Data" if self._game_path else None
        mod_count = write_modsettings(modsettings, modlist, staging,
                                      log_fn=_log,
                                      game_data_path=game_data,
                                      patch_version=self._patch_version)

        _log(
            f"Deploy complete. "
            f"{linked_mod} mod + {linked_core} vanilla "
            f"= {linked_mod + linked_core} total file(s) in Mods/. "
            f"modsettings.lsx written with {mod_count} mod(s)."
        )

    def restore(self, log_fn=None, progress_fn=None) -> None:
        """Remove deployed mods and restore the vanilla Mods folder."""
        _log = log_fn or (lambda _: None)

        larian_root = self._larian_root()
        if larian_root is None:
            raise RuntimeError(
                "No Larian data folder found. Configure the Proton prefix, "
                f"or install the native Linux build so {_NATIVE_LARIAN_ROOT} exists."
            )

        mods_dir = larian_root / _MODS_REL

        # Undo custom-routed files (bin/ and generated/ → game root / Data/)
        if self._game_path:
            custom_rules = self.custom_routing_rules
            if custom_rules:
                _log("Restore: removing custom-routed files (bin/, generated/) ...")
                restore_custom_rules(
                    self.get_effective_filemap_path(),
                    self._game_path,
                    rules=custom_rules,
                    log_fn=_log,
                )

        _profile_dir = self._active_profile_dir
        _entries = read_modlist(_profile_dir / "modlist.txt") if _profile_dir else []
        cleanup_custom_deploy_dirs(_profile_dir, _entries, log_fn=_log)

        _log("Restore: clearing Mods/ and moving Mods_Core/ back ...")
        restored = restore_data_core(mods_dir, overwrite_dir=self.get_effective_overwrite_path(), log_fn=_log)
        _log(f"  Restored {restored} file(s). Mods_Core/ removed.")

        _log("Restore: resetting modsettings.lsx to vanilla ...")
        modsettings = larian_root / _MODSETTINGS_REL
        write_vanilla_modsettings(modsettings, log_fn=_log,
                                  patch_version=self._patch_version)

        _log("Restore complete.")

    def post_clean_game_folder(self, log_fn=None) -> None:
        """Reset modsettings.lsx to vanilla after Clean Game Folder."""
        larian_root = self._larian_root()
        if larian_root is None:
            return
        modsettings = larian_root / _MODSETTINGS_REL
        write_vanilla_modsettings(modsettings, log_fn=log_fn,
                                  patch_version=self._patch_version)
