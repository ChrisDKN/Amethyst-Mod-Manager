"""Shared Sims prefix deployment and restoration."""

from pathlib import Path

from Games.base_game import BaseGame
from Utils.config_paths import get_profiles_dir
from Utils.deployment import (
    LinkMode,
    cleanup_custom_deploy_dirs,
    deploy_core,
    deploy_custom_rules,
    deploy_filemap,
    expand_separator_deploy_paths,
    load_per_mod_strip_prefixes,
    load_separator_deploy_paths,
    move_to_core,
    restore_custom_rules,
    restore_data_core,
)
from Utils.mods.modlist import read_modlist

_PROFILES_DIR = get_profiles_dir()


class SimsBase(BaseGame):
    def __init__(self):
        self._game_path: Path | None = None
        self._prefix_path: Path | None = None
        self._deploy_mode: LinkMode = LinkMode.HARDLINK
        self._staging_path: Path | None = None
        self.load_paths()

    @property
    def user_subpath(self) -> Path:
        return Path("drive_c/users/steamuser/Documents/Electronic Arts") / self.name

    @property
    def mods_subpath(self) -> Path:
        return self.user_subpath / "Mods"

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

    def get_game_path(self) -> Path | None:
        return self._game_path

    def get_mod_data_path(self) -> Path | None:
        if self._prefix_path is None:
            return None
        return self._prefix_path / self.mods_subpath

    def get_mod_staging_path(self) -> Path:
        if self._staging_path is not None:
            return self._staging_path / "mods"
        return _PROFILES_DIR / self.name / "mods"

    def get_hardlink_deploy_targets(self) -> list[tuple[str, Path | None]]:
        return [("Proton prefix", self._prefix_path)]

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

    def prepare_mod_directory(self, mods_dir: Path, log_fn) -> None:
        mods_dir.mkdir(parents=True, exist_ok=True)

    def deploy(self, log_fn=None, mode: LinkMode = LinkMode.HARDLINK,
               profile: str = "default", progress_fn=None) -> None:
        _log = log_fn or (lambda _: None)
        if self._prefix_path is None:
            raise RuntimeError("Prefix path is not configured.")

        mods_dir = self.get_mod_data_path()
        filemap = self.get_effective_filemap_path()
        staging = self.get_effective_mod_staging_path()

        from Utils.filegraph.deploy import require_active
        require_active()
        self.prepare_mod_directory(mods_dir, _log)

        profile_dir = self.get_profile_root() / "profiles" / profile
        per_mod_strip = load_per_mod_strip_prefixes(profile_dir)
        sep_deploy = load_separator_deploy_paths(profile_dir)
        sep_entries = read_modlist(profile_dir / "modlist.txt") if sep_deploy else []
        per_mod_deploy = expand_separator_deploy_paths(sep_deploy, sep_entries) or None

        custom_rules = self.effective_custom_routing_rules
        custom_exclude: set[str] = set()
        if custom_rules:
            _log("Step 0: Routing files to their custom destinations ...")
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

        core_name = mods_dir.name + "_Core"
        _log(f"Step 1: Moving {mods_dir.name}/ → {core_name}/ ...")
        move_to_core(mods_dir, log_fn=_log)

        _log(f"Step 2: Transferring mod files into {mods_dir.name}/ ({mode.name}) ...")
        linked_mod, placed = deploy_filemap(
            filemap, mods_dir, staging,
            mode=mode,
            strip_prefixes=self.mod_folder_strip_prefixes,
            per_mod_strip_prefixes=per_mod_strip,
            per_mod_deploy_dirs=per_mod_deploy,
            log_fn=_log,
            progress_fn=progress_fn,
            exclude=custom_exclude or None,
            core_dir=mods_dir.parent / core_name,
        )

        _log(f"Step 3: Filling gaps with existing files from {core_name}/ ...")
        linked_core = deploy_core(mods_dir, placed, mode=mode, log_fn=_log)
        _log(f"Deploy complete. {linked_mod} mod + {linked_core} existing "
             f"= {linked_mod + linked_core} total file(s) in {mods_dir.name}/.")

    def restore(self, log_fn=None, progress_fn=None) -> None:
        _log = log_fn or (lambda _: None)
        if self._prefix_path is None:
            raise RuntimeError("Prefix path is not configured.")

        mods_dir = self.get_mod_data_path()
        profile_dir = self._active_profile_dir
        entries = read_modlist(profile_dir / "modlist.txt") if profile_dir else []
        cleanup_custom_deploy_dirs(profile_dir, entries, log_fn=_log, game=self)

        _log("Restore: removing custom-routed files ...")
        restore_custom_rules(
            self.get_effective_filemap_path(),
            self._game_path or self._prefix_path,
            rules=[],
            log_fn=_log,
            prefix_root=self._prefix_path,
        )

        _log(f"Restore: clearing {mods_dir.name}/ and restoring existing files ...")
        restored = restore_data_core(
            mods_dir, overwrite_dir=self.get_effective_overwrite_path(),
            log_fn=_log, game=self, profile_dir=profile_dir,
        )
        _log(f"Restore complete. Restored {restored} file(s).")
