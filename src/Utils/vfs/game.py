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

    supports_vfs_deploy = True
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
    vfs_root_payload_targets_data = False

    def get_vfs_game_root(self) -> Path | None:
        """Outer install directory materialized and virtualized at launch."""
        return self.get_game_path()

    def get_vfs_data_root(self) -> Path | None:
        """Primary deployment directory inside :meth:`get_vfs_game_root`."""
        return self.get_mod_data_path()

    def vfs_relative_path(self, relative: str | Path) -> Path:
        """Translate a handler-relative path to the outer VFS root."""
        return Path(relative)

    def get_deploy_active(self) -> bool:
        """Include every profile's VFS state in the deployed-state guard."""
        try:
            from Utils.vfs import has_any_deployment_state
            if has_any_deployment_state(self):
                return True
        except Exception:
            pass
        parent = getattr(super(), "get_deploy_active", None)
        return bool(parent()) if callable(parent) else False

    def get_last_deployed_profile(self) -> str:
        """Direct Restore to a failed profile even before its first success."""
        parent = getattr(super(), "get_last_deployed_profile", None)
        recorded = str(parent() or "default") if callable(parent) else "default"
        try:
            from Utils.vfs import deployment_state_profiles
            profiles = deployment_state_profiles(self)
        except Exception:
            profiles = ()
        if not profiles:
            return recorded

        # Prefer the currently selected profile when it owns real state, then
        # the successful deploy record, then the newest discovered marker.
        active = getattr(self, "_active_profile_dir", None)
        active_name = Path(active).name if active is not None else ""
        if active_name in profiles:
            return active_name
        if recorded in profiles:
            return recorded
        return profiles[0]

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
        # Same-profile VFS redeploys are handled separately by
        # deploy_incremental.plan_vfs_redeploy: the pipeline retains the old
        # shadow so build_layers can capture its runtime writes and replace it
        # atomically without a preliminary Restore.
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
        if (not getattr(self, "supports_script_extender_swap", True)
                or not getattr(self, "script_extender_swap", False)
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
        game_root = self.get_vfs_game_root()
        if game_root is None:
            return None
        game_root = Path(game_root).resolve()
        from Utils.vfs import virtual_file_path

        def _virtual_candidate(relative: str) -> Path | None:
            if not relative:
                return None
            return virtual_file_path(self, relative)

        extender = self._vfs_script_extender()
        candidate = _virtual_candidate(extender)
        if candidate is not None:
            return candidate

        preferred = getattr(self, "preferred_launch_exe", "") or ""
        candidate = _virtual_candidate(preferred)
        if candidate is not None:
            return candidate

        launcher_resolver = getattr(self, "_launcher_name", None)
        launcher_names = []
        if callable(launcher_resolver):
            launcher_names.append(launcher_resolver())
        else:
            launcher_names.append(getattr(self, "exe_name", ""))
            launcher_names.extend(getattr(self, "exe_name_alts", None) or ())
        for launcher_name in launcher_names:
            candidate = _virtual_candidate(launcher_name)
            if candidate is not None:
                return candidate

        # Some handlers expose a nested project root while their configured
        # executable is relative to the outer install. Reuse the normal
        # recursive resolver, then ensure the result belongs to this view.
        try:
            from Utils.executables.launch import resolve_game_exe
            resolved = resolve_game_exe(self)
            if resolved is not None:
                relative = resolved.resolve(strict=False).relative_to(game_root)
                candidate = virtual_file_path(self, relative)
                if candidate is not None:
                    return candidate
        except (OSError, ValueError):
            pass
        return None

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

    def _prepare_vfs_passthrough_command(
        self, vanilla_command: list[str],
    ) -> list[str]:
        """Apply loader selection/default args without choosing a namespace."""
        from Utils.vfs import prefer_virtual_executable, virtual_file
        command = list(vanilla_command)

        # Launcher handoffs execute this exact argv rather than going through
        # the manager's normal game-launch argument path. Carry the handler's
        # required defaults here too (notably Cyberpunk's -modded), while
        # preserving any copy the launcher/user already supplied.
        selected_name = ""
        declared_names = [getattr(self, "exe_name", "")]
        declared_names.extend(getattr(self, "exe_name_alts", None) or ())
        declared_names.extend(getattr(self, "direct_launch_exes", None) or ())
        framework_launch_exes = (
            getattr(self, "framework_launch_exes", None) or {})
        if isinstance(framework_launch_exes, dict):
            declared_names.extend(framework_launch_exes.values())
        declared_basenames = {
            Path(name).name.casefold() for name in declared_names if name
        }
        for token in reversed(command):
            candidate = Path(token.strip('"').replace("\\", "/")).name
            if candidate.casefold() in declared_basenames:
                selected_name = candidate
                break
        try:
            defaults = self.default_launch_args_for_exe(selected_name)
        except AttributeError:
            defaults = getattr(self, "default_launch_args", []) or []
        command.extend(str(arg) for arg in defaults if str(arg) not in command)

        extender = self._vfs_script_extender()
        if extender:
            command = prefer_virtual_executable(self, command, extender)
        else:
            preferred = getattr(self, "preferred_launch_exe", "") or ""
            if preferred and virtual_file(self, preferred):
                command = prefer_virtual_executable(self, command, preferred)
        return command

    def get_vfs_passthrough_command(self, vanilla_command: list[str]) -> list[str]:
        """Wrap a launcher's command, preferring a configured script extender."""
        from Utils.vfs import wrap_command
        command = self._prepare_vfs_passthrough_command(vanilla_command)
        return wrap_command(self, command)

    def get_vfs_sandbox_passthrough_command(
        self, vanilla_command: list[str],
    ) -> tuple[list[str], Path, dict[str, str]]:
        """Build argv which a Flatpak launcher can execute in-place."""
        from Utils.vfs import sandbox_passthrough_command
        command = self._prepare_vfs_passthrough_command(vanilla_command)
        return sandbox_passthrough_command(self, command)

    def get_vfs_steam_command(self, vanilla_command: list[str]) -> list[str]:
        """Compatibility alias for the original Steam-only CLI contract."""
        return self.get_vfs_passthrough_command(vanilla_command)

    def _deploy_vfs(self, *, profile: str, filemap: Path, staging: Path,
                    log_fn, progress_fn=None) -> None:
        """Build a private game view and run compatible handler hooks."""
        from Utils.deployment import (
            expand_separator_deploy_paths,
            expand_separator_link_modes,
            expand_separator_raw_deploy,
            LinkMode,
            load_per_mod_strip_prefixes,
            load_separator_deploy_paths,
        )
        from Utils.mods.files import excluded_raw_by_mod
        from Utils.mods.modlist import read_modlist
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

        data_root_getter = getattr(self, "get_vfs_data_root", None)
        data_root = (
            data_root_getter() if callable(data_root_getter)
            else self.get_mod_data_path()
        )
        data_name = Path(data_root).name if data_root is not None else "data"
        log_fn(f"VFS deploy: resolving a private {self.name} game view ...")
        try:
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
        except BaseException:
            # Generic VFS handlers may deliberately place separator payloads
            # or prefix-routed files outside the private game view. Reverse
            # those physical side effects immediately when the view fails to
            # build. UE5 supplies its own richer transactional callback.
            if not callable(getattr(self, "_vfs_populate_data_layer", None)):
                try:
                    from Utils.deployment import cleanup_custom_deploy_dirs
                    cleanup_custom_deploy_dirs(
                        profile_dir,
                        sep_entries,
                        log_fn=log_fn,
                        filemap_path=filemap,
                        game=self,
                    )
                except Exception as cleanup_exc:
                    log_fn(
                        "  WARN: failed to roll back external VFS separator "
                        f"targets: {cleanup_exc}"
                    )
                try:
                    from Utils.deployment import restore_custom_rules
                    game_root_getter = getattr(self, "get_vfs_game_root", None)
                    game_root = (
                        game_root_getter()
                        if callable(game_root_getter)
                        else self.get_game_path()
                    )
                    if game_root is not None:
                        restore_custom_rules(
                            filemap,
                            Path(game_root),
                            rules=[],
                            log_fn=log_fn,
                            prefix_root=self.get_prefix_path(),
                        )
                except Exception as cleanup_exc:
                    log_fn(
                        "  WARN: failed to roll back prefix-routed VFS "
                        f"targets: {cleanup_exc}"
                    )
            raise

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

        # Some handlers need the fully resolved shadow before they can create
        # generated game metadata (for example Cyberpunk's archive modlist).
        # Keep this hook immediately before the deployment snapshot so those
        # generated files are treated as deploy artifacts, not runtime writes.
        post_view_hook = getattr(self, "_vfs_post_view_build", None)
        if callable(post_view_hook):
            from Utils.vfs import effective_shadow_root
            post_view_hook(
                view_root=effective_shadow_root(self),
                profile=profile,
                filemap=filemap,
                staging=staging,
                log_fn=log_fn,
            )

        # Game-specific hooks above may write generated files into the private
        # view. Snapshot only after they finish so restore can distinguish
        # those deploy artifacts from files created while the game is running.
        from Utils.vfs import finalize_deployment
        finalize_deployment(self, log_fn=log_fn)
        physical_note = str(
            getattr(self, "vfs_physical_game_mutation_note", "") or ""
        ).strip()
        completion = (
            physical_note
            if physical_note
            else "the real game directory was not modified"
        )
        log_fn(
            f"VFS deploy complete: {data_count} {data_name} + "
            f"{root_count} root file(s); {completion}."
        )
