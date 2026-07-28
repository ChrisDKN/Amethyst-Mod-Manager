"""
jagged_alliance_3.py
Game handler for Jagged Alliance 3.

Mod structure:
  Mods deploy into the Proton prefix AppData folder:
    drive_c/users/steamuser/AppData/Roaming/Jagged Alliance3/Mods/
  Staged mods live in Profiles/Jagged Alliance 3/mods/

  Every mod must live inside a named subfolder containing metadata.lua
  (or, for some mods, a single .hpk file):  Mods/<ModName>/metadata.lua.
  A few mods ship metadata.lua / the .hpk loose at the staging root — these
  are auto-wrapped into <ModName>/ before the filemap is built
  (mod_staging_requires_subdir + mod_staging_wrap_signals).
"""

from pathlib import Path

from Games.base_game import BaseGame
from Utils.deploy import (
    LinkMode, deploy_filemap, deploy_core, load_per_mod_strip_prefixes,
    load_separator_deploy_paths, expand_separator_deploy_paths,
    cleanup_custom_deploy_dirs, move_to_core, restore_data_core,
)
from Utils.modlist import read_modlist
from Utils.config_paths import get_profiles_dir

_PROFILES_DIR = get_profiles_dir()

# Path inside the Proton prefix where Jagged Alliance 3 reads mods from
_MODS_SUBPATH = Path(
    "drive_c/users/steamuser/AppData/Roaming/Jagged Alliance 3/Mods"
)


class JaggedAlliance3(BaseGame):

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
        return "Jagged Alliance 3"

    @property
    def game_id(self) -> str:
        return "jagged_alliance_3"

    @property
    def exe_name(self) -> str:
        return "JA3.exe"

    @property
    def steam_id(self) -> str:
        return "1084160"

    @property
    def nexus_game_domain(self) -> str:
        return "jaggedalliance3"

    # Profile Groups: disabled for now — only Stardew Valley currently opts
    # in (see base_game.py's profile_groups_supported docstring) pending
    # wider review of the virtual-merge rework. Uncomment to enable once
    # this game's deploy path has been reviewed/tested.
    # @property
    # def profile_groups_supported(self) -> bool:
    #     return True

    @property
    def conflict_ignore_filenames(self) -> set[str]:
        return {"*read*.txt"}
    
    @property
    def plugin_extensions(self) -> list[str]:
        return []

    @property
    def loot_sort_enabled(self) -> bool:
        return False

    @property
    def loot_game_type(self) -> str:
        return ""

    @property
    def loot_masterlist_url(self) -> str:
        return ""

    @staticmethod
    def _load_ja3_module(stem: str):
        """Load a sibling JA3 module (ja3_packs) by file path.

        The handler is imported via spec_from_file_location, so its folder is
        not on sys.path and a plain ``import`` fails.
        """
        import importlib.util
        sibling = Path(__file__).resolve().parent / f"{stem}.py"
        spec = importlib.util.spec_from_file_location(f"{stem}_ja3", str(sibling))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    # -- Mod structure -------------------------------------------------------

    @property
    def mod_staging_requires_subdir(self) -> bool:
        # Every mod must be Mods/<ModName>/metadata.lua (or <ModName>/<mod>.hpk).
        return True

    @property
    def mod_staging_wrap_signals(self) -> "tuple[set[str], set[str]]":
        # A loose metadata.lua (a genuinely flat mod) or a loose ModContent.hpk
        # (AppData mod shipping only that pack) signals a mod that must be
        # wrapped into <ModName>/.  A non-ModContent .hpk is NOT a wrap signal —
        # it is a Packs/ sibling routed elsewhere (see ja3_packs).
        return ({"metadata.lua", "modcontent.hpk"}, set())

    @property
    def mod_staging_already_structured_markers(self) -> "set[str]":
        # If a subdir already holds metadata.lua the mod is correctly built;
        # a loose .hpk at root is a Packs sibling, not a flat mod — don't wrap.
        return {"metadata.lua"}

    # -----------------------------------------------------------------------
    # Paths
    # -----------------------------------------------------------------------

    def get_game_path(self) -> Path | None:
        return self._game_path

    def get_mod_data_path(self) -> Path | None:
        """Mods deploy into the Proton prefix AppData Mods folder."""
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
          1. Move everything currently in Mods/ → Mods_Core/  (vanilla backup)
          2. Transfer every file listed in filemap.txt into Mods/
          3. Fill gaps with vanilla files from Mods_Core/
        (Root Folder deployment is handled by the GUI after this returns.)
        """
        _log = log_fn or (lambda _: None)

        if self._prefix_path is None:
            raise RuntimeError("Prefix path is not configured.")

        mods_dir = self._prefix_path / _MODS_SUBPATH
        filemap  = self.get_effective_filemap_path()
        staging  = self.get_effective_mod_staging_path()

        mods_dir.mkdir(parents=True, exist_ok=True)

        if not filemap.is_file():
            raise RuntimeError(
                f"filemap.txt not found: {filemap}\n"
                "Run 'Build Filemap' before deploying."
            )

        _log("Step 1: Moving Mods/ → Mods_Core/ ...")
        move_to_core(mods_dir, log_fn=_log)
        _log("  Backed up existing files → Mods_Core/.")

        _log(f"Step 2: Transferring mod files into Mods/ ({mode.name}) ...")
        profile_dir = self.get_profile_root() / "profiles" / profile
        per_mod_strip = load_per_mod_strip_prefixes(profile_dir)
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
                                            core_dir=mods_dir.parent / (mods_dir.name + "_Core"))
        _log(f"  Transferred {linked_mod} mod file(s).")

        _log("Step 3: Filling gaps with vanilla files from Mods_Core/ ...")
        linked_core = deploy_core(mods_dir, placed, mode=mode, log_fn=_log)
        _log(f"  Transferred {linked_core} vanilla file(s).")

        # Step 4: route loose .hpk pack/map files into the game install Packs/
        # tree (backing up the vanilla files they replace).
        if self._game_path is not None:
            _log("Step 4: Routing loose .hpk pack files into Packs/ ...")
            self._load_ja3_module("ja3_packs").deploy_packs(
                filemap, staging, self._game_path, mode=mode,
                log_fn=_log, appdata_mods_dir=mods_dir)
        else:
            _log("Step 4: Skipped Packs/ routing — game path not configured.")

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
        cleanup_custom_deploy_dirs(_profile_dir, _entries, log_fn=_log)

        # Restore any vanilla Packs/ files that loose .hpk mods replaced.
        if self._game_path is not None:
            self._load_ja3_module("ja3_packs").restore_packs(
                self.get_effective_filemap_path(), self._game_path, log_fn=_log)

        _log("Restore: clearing Mods/ and moving Mods_Core/ back ...")
        restored = restore_data_core(mods_dir, overwrite_dir=self.get_effective_overwrite_path(), log_fn=_log)
        _log(f"  Restored {restored} file(s). Mods_Core/ removed.")

        _log("Restore complete.")
