"""
the_sims_4.py
Game handler for The Sims 4.

Mod structure:
  Mods install into the Proton prefix Documents folder:
    drive_c/users/steamuser/Documents/Electronic Arts/The Sims 4/Mods/
  Staged mods live in Profiles/The Sims 4/mods/

  Only .package/.ts4script and Tray files are deployed - other files
  (readmes, images, etc.) are excluded via mod_install_extensions.

  Tray content (households, lots, rooms shared as .trayitem sets) is routed
  by custom_routing_rules into the sibling Tray/ folder instead of Mods/.
"""

from pathlib import Path

from Games.base_game import BaseGame
from Utils.deploy import CustomRule, LinkMode, deploy_custom_rules, deploy_filemap, deploy_core, load_per_mod_strip_prefixes, load_separator_deploy_paths, expand_separator_deploy_paths, cleanup_custom_deploy_dirs, move_to_core, restore_custom_rules, restore_data_core
from Utils.modlist import read_modlist
from Utils.config_paths import get_profiles_dir

_PROFILES_DIR = get_profiles_dir()

# Path inside the Proton prefix where The Sims 4 reads mods from
_MODS_SUBPATH = Path(
    "drive_c/users/steamuser/Documents/Electronic Arts/The Sims 4/Mods"
)

# Sibling of Mods/ - the game reads shared households/lots/rooms from here.
_TRAY_SUBPATH = _MODS_SUBPATH.parent / "Tray"

# Tray file extensions; the game only reads these from Tray/, never Mods/.
_TRAY_EXTENSIONS = [".householdbinary", ".trayitem", ".sgi", ".hhi"]


class TheSims4(BaseGame):

    def __init__(self):
        self._game_path: Path | None = None
        self._prefix_path: Path | None = None
        self._deploy_mode: LinkMode = LinkMode.HARDLINK
        self._staging_path: Path | None = None
        self.load_paths()

    # -----------------------------------------------------------------------
    # Identity
    # -----------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "The Sims 4"

    @property
    def game_id(self) -> str:
        return "the_sims_4"

    @property
    def exe_name(self) -> str:
        return "Game/Bin/TS4_x64.exe"
    
    @property
    def exe_name_alts(self) -> list[str]:
        return ["Game/Bin/TS4_Launcher_x64.exe"]

    @property
    def steam_id(self) -> str:
        return "1222670"

    @property
    def nexus_game_domain(self) -> str:
        return "thesims4"

    @property
    def mod_install_extensions(self) -> set[str]:
        return {".package", ".ts4script", *_TRAY_EXTENSIONS}

    @property
    def custom_routing_rules(self) -> list[CustomRule]:
        """Tray files go to the prefix Tray/ folder, not Mods/."""
        return [
            CustomRule(dest=str(_TRAY_SUBPATH), extensions=_TRAY_EXTENSIONS,
                       flatten=True, to_prefix=True),
        ]

    @property
    def mod_required_top_level_folders(self) -> set[str]:
        return {"Mods"}
    
    @property
    def mod_folder_strip_prefixes_post(self) -> set[str]:
            return {"Mods"}

    @property
    def mod_auto_strip_until_required(self) -> bool:
        return True

    @property
    def mod_install_as_is_if_no_match(self) -> bool:
        return True
    

    # -----------------------------------------------------------------------
    # Paths
    # -----------------------------------------------------------------------

    def get_game_path(self) -> Path | None:
        return self._game_path

    def get_mod_data_path(self) -> Path | None:
        """Mods deploy into the Proton prefix Documents Mods folder."""
        if self._prefix_path is None:
            return None
        return self._prefix_path / _MODS_SUBPATH

    def get_mod_staging_path(self) -> Path:
        if self._staging_path is not None:
            return self._staging_path / "mods"
        return _PROFILES_DIR / self.name / "mods"

    def get_hardlink_deploy_targets(self) -> list[tuple[str, "Path | None"]]:
        return [("Proton prefix", self._prefix_path)]

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

    # -----------------------------------------------------------------------
    # Deployment
    # -----------------------------------------------------------------------

    def deploy(self, log_fn=None, mode: LinkMode = LinkMode.HARDLINK,
               profile: str = "default", progress_fn=None) -> None:
        """Deploy staged mods into the Proton prefix Mods folder.

        Workflow:
          1. Move everything currently in the Mods folder → Mods_Core/
          2. Hard-link every .package/.ts4script listed in filemap.txt into Mods/
          3. Hard-link vanilla files from Mods_Core/ for anything not
             provided by a mod
        (Root Folder deployment is handled by the GUI after this returns.)
        """
        _log = log_fn or (lambda _: None)

        if self._prefix_path is None:
            raise RuntimeError("Prefix path is not configured.")

        mods_dir = self._prefix_path / _MODS_SUBPATH
        filemap  = self.get_effective_filemap_path()
        staging  = self.get_effective_mod_staging_path()

        mods_dir.mkdir(parents=True, exist_ok=True)

        from Utils.filegraph_deploy import input_ready
        if not input_ready():
            raise RuntimeError(
                f"filemap.txt not found: {filemap}\n"
                "Run 'Build Filemap' before deploying."
            )

        profile_dir = self.get_profile_root() / "profiles" / profile
        per_mod_strip = load_per_mod_strip_prefixes(profile_dir)
        _sep_deploy = load_separator_deploy_paths(profile_dir)
        _sep_entries = read_modlist(profile_dir / "modlist.txt") if _sep_deploy else []
        per_mod_deploy = expand_separator_deploy_paths(_sep_deploy, _sep_entries) or None

        custom_rules = self.custom_routing_rules
        custom_exclude: set[str] = set()
        if custom_rules:
            _log("Step 0: Routing Tray files into the prefix Tray/ folder ...")
            custom_exclude = deploy_custom_rules(
                filemap, self._game_path or self._prefix_path, staging,
                rules=custom_rules,
                mode=mode,
                strip_prefixes=self.mod_folder_strip_prefixes,
                per_mod_strip_prefixes=per_mod_strip,
                log_fn=_log,
                progress_fn=progress_fn,
                prefix_root=self._prefix_path,
            )

        _log("Step 1: Moving Mods/ → Mods_Core/ ...")
        move_to_core(mods_dir, log_fn=_log)
        _log("  Backed up existing files → Mods_Core/.")

        _log(f"Step 2: Transferring mod files into Mods/ ({mode.name}) ...")
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

        _log(
            f"Deploy complete. "
            f"{linked_mod} mod + {linked_core} vanilla "
            f"= {linked_mod + linked_core} total file(s) in Mods/."
        )

    def restore(self, log_fn=None, progress_fn=None) -> None:
        """Remove deployed mods and restore the vanilla Mods folder."""
        _log = log_fn or (lambda _: None)

        if self._prefix_path is None:
            raise RuntimeError("Prefix path is not configured.")

        mods_dir = self._prefix_path / _MODS_SUBPATH

        _profile_dir = self._active_profile_dir
        _entries = read_modlist(_profile_dir / "modlist.txt") if _profile_dir else []
        cleanup_custom_deploy_dirs(_profile_dir, _entries, log_fn=_log, game=self)

        custom_rules = self.custom_routing_rules
        if custom_rules:
            _log("Restore: removing custom-routed Tray files ...")
            restore_custom_rules(
                self.get_effective_filemap_path(),
                self._game_path or self._prefix_path,
                rules=custom_rules,
                log_fn=_log,
                prefix_root=self._prefix_path,
            )

        _log("Restore: clearing Mods/ and moving Mods_Core/ back ...")
        restored = restore_data_core(
            mods_dir, overwrite_dir=self.get_effective_overwrite_path(),
            log_fn=_log, game=self, profile_dir=self._active_profile_dir)
        _log(f"  Restored {restored} file(s). Mods_Core/ removed.")

        _log("Restore complete.")
