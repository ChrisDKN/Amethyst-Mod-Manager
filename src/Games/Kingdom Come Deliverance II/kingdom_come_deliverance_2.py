"""
kingdom_come_deliverance_2.py
Game handler for Kingdom Come: Deliverance II.

Steam install: library folder is named KingdomComeDeliverance2 under
  steamapps/common/.

Mod structure:
  Mods install into <game root>/mods/
  Staged mods live in Profiles/Kingdom Come: Deliverance II/mods/

  Root_Folder/ files deploy straight to the game install root (handled by GUI).
"""

from pathlib import Path

from Games.base_game import BaseGame
from Utils.vfs import ProfileVFSGameMixin
from Utils.deployment import (
    LinkMode,
    deploy_core,
    deploy_filemap,
    load_per_mod_strip_prefixes,
    load_separator_deploy_paths,
    expand_separator_deploy_paths,
    cleanup_custom_deploy_dirs,
    move_to_core,
    restore_data_core,
)
from Utils.mods.modlist import read_modlist
from Utils.config_paths import get_profiles_dir

_PROFILES_DIR = get_profiles_dir()


class KingdomComeDeliverance2(ProfileVFSGameMixin, BaseGame):

    profile_overridable_settings = (
        *BaseGame.profile_overridable_settings,
        *ProfileVFSGameMixin.vfs_profile_setting_keys,
    )

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
        return "Kingdom Come: Deliverance II"

    @property
    def game_id(self) -> str:
        return "kingdom_come_deliverance_2"

    @property
    def exe_name(self) -> str:
        return "bin/Win64MasterMasterSteamPGO/KingdomCome.exe"

    @property
    def exe_name_alts(self) -> list[str]:
        return ["bin/Win64/KingdomCome.exe"]

    @property
    def steam_id(self) -> str:
        return "1771300"

    @property
    def nexus_game_domain(self) -> str:
        return "kingdomcomedeliverance2"

    @property
    def reshade_dll(self) -> str:
        return "dxgi.dll"

    @property
    def mods_dir(self) -> str:
        return "mods"

    def runtime_snapshot_exclude_dirs(self) -> set[str] | None:
        # mods/ is reverted via its _Core backup; capture only files outside it.
        return {self.mods_dir.split("/")[0]}

    @property
    def mod_folder_strip_prefixes(self) -> set[str]:
        return {"mods"}

    # -----------------------------------------------------------------------
    # Paths
    # -----------------------------------------------------------------------

    def get_game_path(self) -> Path | None:
        return self._game_path

    def get_mod_data_path(self) -> Path | None:
        """Mods go into mods/ inside the game directory."""
        if self._game_path is None:
            return None
        return self._game_path / self.mods_dir

    def get_mod_staging_path(self) -> Path:
        if self._staging_path is not None:
            return self._staging_path / "mods"
        return _PROFILES_DIR / self.name / "mods"

    def set_staging_path(self, path: Path | str | None) -> None:
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
        """Deploy staged mods into <game root>/mods/.

        Workflow:
          1. Move mods/ → mods_Core/  (vanilla backup)
          2. Transfer mod files listed in filemap.txt into mods/
          3. Fill gaps with vanilla files from mods_Core/
        (Root Folder deployment is handled by the GUI after this returns.)
        """
        _log = log_fn or (lambda _: None)

        if self._game_path is None:
            raise RuntimeError("Game path is not configured.")

        plugins_dir = self._game_path / self.mods_dir
        filemap = self.get_effective_filemap_path()
        staging = self.get_effective_mod_staging_path()
        core = self.mods_dir + "_Core"

        from Utils.filegraph.deploy import input_ready
        if not input_ready():
            raise RuntimeError(
                f"filemap.txt not found: {filemap}\n"
                "Run 'Build Filemap' before deploying."
            )

        if self.vfs_launch_enabled:
            return self._deploy_vfs(
                profile=profile,
                filemap=filemap,
                staging=staging,
                log_fn=_log,
                progress_fn=progress_fn,
            )

        _log(f"Step 1: Moving {plugins_dir.name}/ → {core}/ ...")
        move_to_core(plugins_dir, log_fn=_log)
        _log(f"  Backed up existing files → {core}/.")
        plugins_dir.mkdir(parents=True, exist_ok=True)

        _log(f"Step 2: Transferring mod files into {plugins_dir} ({mode.name}) ...")
        profile_dir = self.get_profile_root() / "profiles" / profile
        per_mod_strip = load_per_mod_strip_prefixes(profile_dir)
        _sep_deploy = load_separator_deploy_paths(profile_dir)
        _sep_entries = read_modlist(profile_dir / "modlist.txt") if _sep_deploy else []
        per_mod_deploy = expand_separator_deploy_paths(_sep_deploy, _sep_entries) or None
        linked_mod, placed = deploy_filemap(
            filemap, plugins_dir, staging,
            mode=mode,
            strip_prefixes=self.mod_folder_strip_prefixes,
            per_mod_strip_prefixes=per_mod_strip,
            per_mod_deploy_dirs=per_mod_deploy,
            log_fn=_log,
            progress_fn=progress_fn,
            core_dir=plugins_dir.parent / (plugins_dir.name + "_Core"),
        )
        _log(f"  Transferred {linked_mod} mod file(s).")

        _log(f"Step 3: Filling gaps with vanilla files from {core}/ ...")
        linked_core = deploy_core(plugins_dir, placed, mode=mode, log_fn=_log)
        _log(f"  Transferred {linked_core} vanilla file(s).")

        _log(
            f"Deploy complete. "
            f"{linked_mod} mod + {linked_core} vanilla "
            f"= {linked_mod + linked_core} total file(s) in {plugins_dir.name}/."
        )

        # Capture runtime files generated outside mods/ on the next restore.
        self.snapshot_root_for_runtime_capture(log_fn=_log)

    def restore(self, log_fn=None, progress_fn=None) -> None:
        """Restore mods/ to vanilla: clear deployed mods and, if present, move mods_Core/ back."""
        _log = log_fn or (lambda _: None)

        if self._game_path is None:
            raise RuntimeError("Game path is not configured.")

        plugins_dir = self._game_path / self.mods_dir
        core = self.mods_dir + "_Core"
        core_dir = self._game_path / core

        _profile_dir = self._active_profile_dir
        _entries = read_modlist(_profile_dir / "modlist.txt") if _profile_dir else []
        cleanup_custom_deploy_dirs(_profile_dir, _entries, log_fn=_log, game=self)

        from Utils.vfs import cleanup_deployment, has_deployment_state
        if has_deployment_state(self):
            cleanup_deployment(self, preserve_upper=True, log_fn=_log)
            if not core_dir.is_dir():
                _log("Restore complete.")
                return
            _log("Restore: a physical deployment also remains; restoring it now ...")

        _log(f"Restore: clearing {plugins_dir.name}/ and moving {core}/ back if present ...")
        restored = restore_data_core(
            plugins_dir, core_dir=core_dir,
            overwrite_dir=self.get_effective_overwrite_path(), log_fn=_log,
            game=self, profile_dir=self._active_profile_dir,
        )
        if restored > 0:
            _log(f"  Restored {restored} file(s). {core}/ removed.")

        moved = self.capture_runtime_files_to_root_folder(log_fn=_log)
        if moved:
            _log(f"  Moved {moved} runtime file(s) to Root_Folder/.")

        _log("Restore complete.")
        
class KingdomComeDeliverance(KingdomComeDeliverance2):

    @property
    def name(self) -> str:
        return "Kingdom Come: Deliverance"

    @property
    def game_id(self) -> str:
        return "kingdom_come_deliverance"

    @property
    def exe_name(self) -> str:
        return "bin/win64/KingdomCome.exe"

    @property
    def exe_name_alts(self) -> list[str]:
        return ["bin/Win64MasterMasterEpicPGO/KingdomCome.exe"]

    @property
    def steam_id(self) -> str:
        return "379430"

    @property
    def nexus_game_domain(self) -> str:
        return "kingdomcomedeliverance"
