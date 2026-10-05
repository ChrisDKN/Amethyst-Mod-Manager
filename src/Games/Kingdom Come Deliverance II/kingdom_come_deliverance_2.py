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
from Utils.kcd.mods import child_path, mods_path, normalise_layout
from Utils.kcd.load_order import STATE_FILE, remove_generated_order, write_order

_PROFILES_DIR = get_profiles_dir()


class KingdomComeDeliverance2(ProfileVFSGameMixin, BaseGame):

    filegraph_routing_revision = 1
    archive_name_ordering = False
    post_deploy_failure_is_fatal = True

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
        return mods_path(self._game_path).name if self._game_path is not None else "mods"

    @property
    def archive_extensions(self) -> frozenset[str]:
        return frozenset({".pak"})

    @property
    def archive_plugin_ordering(self) -> bool:
        return False

    def archive_member_path(self, archive: str, member: str) -> str:
        from Utils.kcd.archives import member_path
        return member_path(archive, member)

    @property
    def additional_install_logic(self) -> list:
        return [self._normalise_mod]

    def _normalise_mod(self, root: Path, mod_name: str, log_fn) -> bool:
        return normalise_layout(root, mod_name, log_fn, sequel=self.steam_id == "1771300")

    def prepare_mod_staging(self, staging: Path, log_fn) -> bool:
        changed = False
        if staging.is_dir():
            for folder in staging.iterdir():
                if (folder.name.lower() in {"root_folder", "overwrite"}
                        or folder.name.endswith("_separator")):
                    continue
                changed = self._normalise_mod(folder, folder.name, log_fn) or changed
        return changed

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

    def post_deploy(self, log_fn=None) -> None:
        if self.vfs_launch_enabled:
            return
        profile_dir = (self._active_profile_dir
                       or self.get_profile_root() / "profiles" / "default")
        write_order(self, self.get_mod_data_path(), profile_dir,
                    log_fn or (lambda _: None), physical=True)

    def _vfs_post_view_build(self, *, view_root: Path, profile: str,
                             filemap: Path, staging: Path, log_fn) -> None:
        write_order(self, view_root / self.mods_dir,
                    self.get_profile_root() / "profiles" / profile, log_fn, physical=False)

    def post_clean_game_folder(self, log_fn=None) -> None:
        profile_dir = (self._active_profile_dir
                       or self.get_profile_root() / "profiles" / "default")
        if (profile_dir / STATE_FILE).exists():
            self.restore(log_fn=log_fn)

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
        core_dir = child_path(self._game_path, core)

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
        move_to_core(plugins_dir, core_dir=core_dir, log_fn=_log)
        _log(f"  Backed up existing files → {core}/.")
        plugins_dir.mkdir(parents=True, exist_ok=True)

        _log(f"Step 2: Transferring mod files into {plugins_dir} ({mode.name}) ...")
        profile_dir = self.get_profile_root() / "profiles" / profile
        per_mod_strip = load_per_mod_strip_prefixes(profile_dir)
        _sep_deploy = load_separator_deploy_paths(profile_dir)
        _sep_entries = read_modlist(profile_dir / "modlist.txt") if _sep_deploy else []
        per_mod_deploy = expand_separator_deploy_paths(_sep_deploy, _sep_entries) or None
        custom_exclude = self._deploy_custom_routing_rules(mode, log_fn)
        linked_mod, placed = deploy_filemap(
            filemap, plugins_dir, staging,
            exclude=custom_exclude,
            mode=mode,
            strip_prefixes=self.mod_folder_strip_prefixes,
            per_mod_strip_prefixes=per_mod_strip,
            per_mod_deploy_dirs=per_mod_deploy,
            log_fn=_log,
            progress_fn=progress_fn,
            core_dir=core_dir,
        )
        _log(f"  Transferred {linked_mod} mod file(s).")
        placed.update(self._custom_routing_destinations_under(
            custom_exclude, plugins_dir))

        _log(f"Step 3: Filling gaps with vanilla files from {core}/ ...")
        linked_core = deploy_core(
            plugins_dir, placed, core_dir=core_dir, mode=mode, log_fn=_log)
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
        self._restore_custom_routing_rules(log_fn)
        _log = log_fn or (lambda _: None)

        if self._game_path is None:
            raise RuntimeError("Game path is not configured.")

        plugins_dir = self._game_path / self.mods_dir
        core = self.mods_dir + "_Core"
        core_dir = child_path(self._game_path, core)

        _profile_dir = self._active_profile_dir
        if _profile_dir is None:
            _profile_dir = self.get_profile_root() / "profiles" / "default"
        _entries = read_modlist(_profile_dir / "modlist.txt") if _profile_dir else []
        cleanup_custom_deploy_dirs(_profile_dir, _entries, log_fn=_log, game=self)

        from Utils.vfs import cleanup_deployment, has_deployment_state
        if has_deployment_state(self):
            cleanup_deployment(self, preserve_upper=True, log_fn=_log)
            if not core_dir.is_dir() and not (_profile_dir / STATE_FILE).exists():
                _log("Restore complete.")
                return
            _log("Restore: a physical deployment also remains; restoring it now ...")

        _log(f"Restore: clearing {plugins_dir.name}/ and moving {core}/ back if present ...")
        remove_generated_order(plugins_dir, _profile_dir, _log)
        restored = restore_data_core(
            plugins_dir, core_dir=core_dir,
            overwrite_dir=self.get_effective_overwrite_path(), log_fn=_log,
            game=self, profile_dir=_profile_dir,
        )
        if restored > 0:
            _log(f"  Restored {restored} file(s). {core}/ removed.")
        (_profile_dir / STATE_FILE).unlink(missing_ok=True)

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
