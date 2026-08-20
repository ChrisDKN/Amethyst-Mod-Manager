"""Reusable game-handler adapter for the profile VFS.

Handlers with one stable game root and one primary mod-data directory can mix
this class in, expose their normal deployment metadata, and branch to
``_deploy_vfs`` from their deploy method. Game-specific post-deploy behavior
remains in the handler.
"""

from __future__ import annotations

from pathlib import Path


class ProfileVFSGameMixin:
    """Opt-in settings, deployment, and launch hooks for ``Utils.vfs``."""

    supports_profile_vfs = True
    launch_passthrough_supported = True
    # Compatibility for launch-option integrations written before launcher-
    # aware handoffs were added.
    steam_launch_passthrough_supported = True
    vfs_profile_setting_keys = ("vfs_enabled",)
    vfs_legacy_setting_keys: tuple[str, ...] = ()
    vfs_prefers_script_extender = False
    # Native handlers can opt into using the complete materialized view as
    # their real working directory when they do not require the install path.
    vfs_direct_shadow_launch = False
    vfs_physical_supports_incremental_deploy = False

    @property
    def vfs_enabled(self) -> bool:
        settings = self._load_settings()
        if "vfs_enabled" in settings:
            return bool(settings["vfs_enabled"])
        return any(bool(settings.get(key, False))
                   for key in self.vfs_legacy_setting_keys)

    def set_vfs_enabled(self, value: bool) -> None:
        settings = self._load_settings()
        settings["vfs_enabled"] = bool(value)
        self._save_settings(settings)

    @property
    def vfs_launch_enabled(self) -> bool:
        return self.supports_profile_vfs and self.vfs_enabled

    @property
    def virtualizes_game_root(self) -> bool:
        return self.vfs_launch_enabled

    @property
    def supports_incremental_deploy(self) -> bool:
        # VFS publishes a fresh resolved layer and has no physical diff target.
        return (self.vfs_physical_supports_incremental_deploy
                and not self.vfs_launch_enabled)

    @property
    def native_launch_required(self) -> bool:
        return self.vfs_launch_enabled

    def native_launch_blocked_reason(self) -> str:
        if not self.vfs_launch_enabled:
            return ""
        return (
            f"{self.name}'s profile VFS needs either Amethyst's Play button "
            "or the generated launcher wrapper so Proton starts inside its "
            "private game view."
        )

    def _vfs_script_extender(self) -> str:
        if (not getattr(self, "script_extender_swap", False)
                or not self.vfs_prefers_script_extender):
            return ""
        try:
            if "Script Extender" not in self.frameworks:
                return ""
            return self._script_extender_exe
        except Exception:
            return ""

    def get_vfs_launch_exe(self) -> Path | None:
        """Executable seen inside the active profile's private game view."""
        game_root = self.get_game_path()
        if game_root is None:
            return None
        game_root = Path(game_root)
        from Utils.vfs import virtual_file
        extender = self._vfs_script_extender()
        if extender and virtual_file(self, extender):
            return game_root / extender
        launcher_resolver = getattr(self, "_launcher_name", None)
        launcher_name = (
            launcher_resolver() if callable(launcher_resolver)
            else getattr(self, "exe_name", "")
        )
        if not launcher_name:
            return None
        launcher = game_root / launcher_name
        return launcher if launcher.is_file() else None

    def vfs_file_exists(self, relative: str) -> bool:
        if not self.vfs_launch_enabled:
            return False
        from Utils.vfs import virtual_file
        return virtual_file(self, relative)

    def wrap_launch_command(self, command: list[str], *,
                            env: dict[str, str] | None = None) -> list[str]:
        if not self.vfs_launch_enabled:
            return command
        from Utils.vfs import wrap_command
        return wrap_command(self, command, env=env)

    def get_vfs_passthrough_command(self, vanilla_command: list[str]) -> list[str]:
        """Wrap a launcher's command, preferring a configured script extender."""
        from Utils.vfs import prefer_virtual_executable, wrap_command
        command = list(vanilla_command)
        extender = self._vfs_script_extender()
        if extender:
            command = prefer_virtual_executable(self, command, extender)
        return wrap_command(self, command)

    def get_vfs_steam_command(self, vanilla_command: list[str]) -> list[str]:
        """Compatibility alias for the original Steam-only CLI contract."""
        return self.get_vfs_passthrough_command(vanilla_command)

    def _deploy_vfs(self, *, profile: str, filemap: Path, staging: Path,
                    log_fn, progress_fn=None) -> None:
        """Build a private game view and run compatible handler hooks."""
        from Utils.deploy import (
            expand_separator_deploy_paths,
            expand_separator_link_modes,
            expand_separator_raw_deploy,
            LinkMode,
            load_per_mod_strip_prefixes,
            load_separator_deploy_paths,
        )
        from Utils.mod_files import excluded_raw_by_mod
        from Utils.modlist import read_modlist
        from Utils.vfs import build_layers

        profile_dir = self.get_profile_root() / "profiles" / profile
        per_mod_strip = load_per_mod_strip_prefixes(profile_dir)
        sep_deploy = load_separator_deploy_paths(profile_dir)
        sep_entries = (read_modlist(profile_dir / "modlist.txt")
                       if sep_deploy else [])
        per_mod_deploy = expand_separator_deploy_paths(
            sep_deploy, sep_entries) or {}
        per_mod_link_modes = expand_separator_link_modes(
            sep_deploy, sep_entries) or {}
        per_mod_raw = expand_separator_raw_deploy(
            sep_deploy, sep_entries) or None
        subdir_builder = getattr(self, "_vfs_per_mod_subdirs", None)
        per_mod_subdirs = (
            subdir_builder(profile_dir, staging, log_fn=log_fn)
            if callable(subdir_builder) else None
        )
        root_folder_enabled = bool(
            getattr(self, "_pipeline_root_folder_enabled", True))
        get_deploy_mode = getattr(self, "get_deploy_mode", None)
        external_deploy_mode = (
            get_deploy_mode() if callable(get_deploy_mode)
            else LinkMode.HARDLINK
        )
        if external_deploy_mode not in (LinkMode.HARDLINK, LinkMode.SYMLINK):
            external_deploy_mode = LinkMode.HARDLINK

        file_exclude: set[str] = set()
        prepare_filemap = getattr(self, "_vfs_prepare_filemap", None)
        if callable(prepare_filemap):
            prepared = prepare_filemap(filemap, staging, log_fn=log_fn)
            if prepared:
                file_exclude.update(
                    str(path).replace("\\", "/").lower()
                    for path in prepared
                )

        data_root = self.get_mod_data_path()
        data_name = Path(data_root).name if data_root is not None else "data"
        log_fn(f"VFS deploy: resolving a private {self.name} game view ...")
        data_count, root_count = build_layers(
            self,
            profile=profile,
            filemap=filemap,
            staging=staging,
            per_mod_strip=per_mod_strip,
            per_mod_deploy=per_mod_deploy,
            raw_mods=per_mod_raw,
            excluded_raw=excluded_raw_by_mod(profile_dir) or None,
            root_folder_enabled=root_folder_enabled,
            per_mod_subdirs=per_mod_subdirs,
            per_mod_link_modes=per_mod_link_modes,
            external_deploy_mode=external_deploy_mode,
            file_exclude=file_exclude or None,
            log_fn=log_fn,
            progress_fn=progress_fn,
        )

        if getattr(self, "uses_plugins_txt", False):
            log_fn("VFS deploy: linking plugins.txt into the Proton prefix ...")
            self._symlink_plugins_txt(profile, log_fn)
        for message, hook_name, args in (
            ("linking profile INI files", "_symlink_profile_ini_files",
             (profile, log_fn)),
            ("linking profile saves", "_symlink_profile_saves",
             (profile, log_fn)),
            ("applying archive invalidation", "apply_archive_invalidation",
             (log_fn,)),
            ("applying INI overrides", "apply_ini_overrides", (log_fn,)),
        ):
            hook = getattr(self, hook_name, None)
            if callable(hook):
                log_fn(f"VFS deploy: {message} ...")
                hook(*args)
        orders_by_mtime = getattr(self, "_orders_plugins_by_mtime", None)
        if callable(orders_by_mtime) and orders_by_mtime():
            log_fn("VFS deploy: setting plugin mtimes to match load order ...")
            self.stamp_plugin_load_order(profile, log_fn)

        # Game-specific hooks above may write generated files into the private
        # view. Snapshot only after they finish so restore can distinguish
        # those deploy artifacts from files created while the game is running.
        from Utils.vfs import finalize_deployment
        finalize_deployment(self, log_fn=log_fn)
        log_fn(
            f"VFS deploy complete: {data_count} {data_name} + "
            f"{root_count} root file(s); the real game directory was not modified."
        )
