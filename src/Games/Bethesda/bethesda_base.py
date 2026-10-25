"""Shared Bethesda Data-folder deployment and profile support."""

import json
import shutil
from pathlib import Path

from Games.base_game import BaseGame, MODERN_DIRECTX_DEPS, WizardTool
from Games.Bethesda.bethesda_ini import _read_ini_key, _set_ini_key
from Utils.atomic_write import write_atomic_text
from Utils.config_paths import get_profiles_dir
from Utils.deployment import (
    LinkMode,
    cleanup_custom_deploy_dirs,
    deploy_core,
    deploy_custom_rules,
    deploy_filemap,
    expand_separator_deploy_paths,
    expand_separator_link_modes,
    expand_separator_raw_deploy,
    load_per_mod_strip_prefixes,
    load_separator_deploy_paths,
    move_to_core,
    remove_case_alias_links,
    remove_probe_stub_dirs,
    restore_custom_rules,
    restore_data_core,
)
from Utils.mods.modlist import read_modlist
from Utils.vfs import ProfileVFSGameMixin

_PROFILES_DIR = get_profiles_dir()


class BethesdaBase(ProfileVFSGameMixin, BaseGame):
    vfs_legacy_setting_keys = ("skyrim_vfs_enabled",)
    vfs_physical_supports_incremental_deploy = True

    profile_overridable_settings = (
        *BaseGame.profile_overridable_settings,
        "disable_creations_menu",
        *ProfileVFSGameMixin.vfs_profile_setting_keys,
        # Read old Skyrim profiles without forcing users to toggle/re-save.
        *vfs_legacy_setting_keys,
    )

    # Compatibility aliases for legacy Skyrim VFS settings.
    @property
    def skyrim_vfs_enabled(self) -> bool:
        return self.vfs_enabled

    def set_skyrim_vfs_enabled(self, value: bool) -> None:
        self.set_vfs_enabled(value)

    # Enderal's own launcher already bootstraps its extender and overrides this.
    vfs_prefers_script_extender = True
    supports_script_extender_swap = True

    plugins_use_star_prefix = False
    plugins_include_vanilla = True
    vanilla_plugins: list[str] = []
    vanilla_dlc_plugins: list[str] = []
    synthesis_registry_name = ""

    _APPDATA_SUBPATH: Path
    _APPDATA_SUBPATH_GOG: Path | None = None
    _MYGAMES_SUBPATH: Path
    _MYGAMES_SUBPATH_GOG: Path | None = None
    _ARCHIVE_INI_FILENAME: str
    _ARCHIVE_PREFS_INI_FILENAME: str | None = None
    _CUSTOM_INI_FILENAME = ""
    _invalidation_bsa_name: str | None = None
    _invalidation_bsa_version: int | None = None
    _archive_list_needs_mod_bsas = False
    _archive_list_fix_name: str | None = None
    _archive_list_fix_path: str | None = None
    _extra_auto_install_deps: tuple[str, ...] = ()

    @property
    def _script_extender_exe(self) -> str:
        return ""

    @property
    def auto_install_deps(self) -> list[str]:
        return [*MODERN_DIRECTX_DEPS, *self._extra_auto_install_deps]

    profile_overridable_paths_extras = (
        "script_extender_swap",
        "profile_ini_files",
        "profile_saves",
        "plugins_txt_filename",
    )

    @property
    def supports_bain(self) -> bool:
        return True

    def __init__(self):
        self._game_path: Path | None = None
        self._prefix_path: Path | None = None
        self._deploy_mode: LinkMode = LinkMode.HARDLINK
        self._staging_path: Path | None = None
        self._script_extender_swap: bool = True
        self._profile_ini_files: bool = False
        self._profile_saves: bool = False
        # None follows the game default; an override controls in-prefix casing.
        self._plugins_txt_filename_override: str | None = None
        self.load_paths()

    _launcher_alts: list[str] = []

    @property
    def exe_name_alts(self) -> list[str]:
        return list(self._launcher_alts)

    @property
    def plugin_extensions(self) -> list[str]:
        return [".esp", ".esl", ".esm"]

    # Avoid Wine directory scans for repeated Data/Data probes.
    @property
    def probe_stub_dirs(self) -> "list[str]":
        return ["Data/Data"]

    # Match engine-requested casing to avoid Wine directory scans.
    @property
    def case_alias_dirs(self) -> "list[str]":
        return [
            "Data",
            "Data/Textures",
            "Data/Meshes",
            "Data/Sound",
            "Data/Interface",
            "Data/Scripts",
            "Data/Music",
            "Data/Strings",
            "Data/Grass",
            "Data/Data",
        ]

    @property
    def mods_dir(self) -> str:
        return "Data"

    def runtime_snapshot_exclude_dirs(self) -> set[str] | None:
        # Data/ is reverted via Data_Core; capture only files outside it.
        return {self.mods_dir}

    @property
    def mod_folder_strip_prefixes(self) -> set[str]:
        return {"Data", "oblivion"}

    @property
    def mod_required_top_level_folders(self) -> set[str]:
        return {
            "skse", "sfse", "f4se", "nvse", "fose", "obse", "textures", "sound", "meshes",
            "mcm", "scripts", "interface", "lightplacer", "mapmarkers", "music",
            "nemesis_engine", "seq", "shadercache", "shaders", "reshade-shaders",
            "shadersfx", "grass", "video", "source", "calientetools", "data", "materials",
            "tools", "config", "menus", "distantlod", "fonts", "facegen", "lodsettings",
            "lsdata", "strings", "trees", "asi", "geometries", "bashtags",
            "dialogueviews", "terrain", "vis", "programs", "misc", "particles",
            "planetdata", "dyndolod", "netscriptframework", "skyproc patchers", "src",
        }

    @property
    def mod_auto_strip_until_required(self) -> bool:
        return True

    @property
    def mod_required_file_types(self) -> set[str]:
        return {".esp", ".esl", ".esm", ".ini", ".bsa", ".ba2"}

    @property
    def mod_install_as_is_if_no_match(self) -> bool:
        return True

    @property
    def conflict_ignore_filenames(self) -> set[str]:
        return {"info.xml", "*read*.txt", "*.md", "*.url", "*.zip"}

    @property
    def conflict_ignore_foldernames(self) -> set[str]:
        return {"src"}

    @property
    def archive_extensions(self) -> frozenset[str]:
        return frozenset({".bsa"})

    @property
    def loot_sort_enabled(self) -> bool:
        return True

    @property
    def reshade_dll(self) -> str:
        return "d3d9.dll"

    @property
    def reshade_arch(self) -> int:
        return 32

    def _saves_routing_rule(self, extensions: list[str]):
        from Utils.deployment import CustomRule
        gog_sub = self._MYGAMES_SUBPATH_GOG or Path(f"{self._MYGAMES_SUBPATH} GOG")
        mirrors: list[str] = []
        if self._prefix_path is not None and (self._prefix_path / self._MYGAMES_DOCS / gog_sub).is_dir():
            mirrors.append(str(self._MYGAMES_DOCS / gog_sub / "Saves"))
        # Keep the persisted rule ID across the base-handler extraction.
        return CustomRule(rule_id='fallout_3:7dd628b5eb3e',
            dest=str(self._MYGAMES_DOCS / self._MYGAMES_SUBPATH / "Saves"),
            extensions=extensions, flatten=True, to_prefix=True,
            mirror_dests=mirrors,
        )

    @staticmethod
    def _xlodgen_wizard_tool(id_suffix: str) -> WizardTool:
        return WizardTool(
            id=f"run_xlodgen_{id_suffix}",
            label="Run xLODGen",
            description="Install xLODGen, deploy mods, and run xLODGenx64.exe.",
            dialog_class_path="wizards.dyndolod.xLODGenWizard",
        )

    _DISCORD_XEDIT_EXES: dict[str, str] = {
        "fo": "xFOEdit.exe",     # Fallout 3 / New Vegas / 4 / VR
        "sf": "xSFEdit.exe",     # Starfield
        "tes": "xTESEdit.exe",   # Oblivion / Skyrim / SSE / VR / Enderal
    }

    # Discord launchers require an explicit game-mode argument.
    _DISCORD_XEDIT_MODES: dict[str, str] = {
        "fo3edit": "FO3", "fnvedit": "FNV", "fo4edit": "FO4",
        "fo4vredit": "FO4VR", "fo76edit": "FO76", "sf1edit": "SF1",
        "tes4edit": "TES4", "tes5edit": "TES5", "tes5vredit": "TES5VR",
        "sseedit": "SSE",
    }

    @staticmethod
    def _discord_xedit_exe(build: str) -> str:
        b = build.lower()
        if b.startswith("sf"):
            return BethesdaBase._DISCORD_XEDIT_EXES["sf"]
        if b.startswith(("fo", "fnv")):
            return BethesdaBase._DISCORD_XEDIT_EXES["fo"]
        return BethesdaBase._DISCORD_XEDIT_EXES["tes"]

    @staticmethod
    def _discord_xedit_mode(build: str) -> str:
        """Default game-mode arg for a *build* (e.g. FO4Edit -> 'FO4')."""
        return BethesdaBase._DISCORD_XEDIT_MODES.get(build.lower(), build)

    @staticmethod
    def _xedit_wizard_tools(
        build: str, id_suffix: str, nexus_url: str, nexus_file_id: int = 0,
        qac: bool = True,
        discord_only: bool = False, discord_mode: str | None = None,
    ) -> list[WizardTool]:
        exe = f"{build}.exe"
        tools: list[WizardTool] = []
        if not discord_only:
            nexus_extra = {"xedit_exe": exe, "nexus_url": nexus_url,
                           "nexus_file_id": nexus_file_id,
                           "display_name": build}
            tools.append(
                WizardTool(
                    id=f"run_{build.lower()}_{id_suffix}",
                    label="Run {0}", label_args=(build,),
                    description="Install {0}, deploy mods, and run {1}.",
                    description_args=(build, exe),
                    dialog_class_path="wizards.sseedit.SSEEditWizard",
                    extra=dict(nexus_extra),
                )
            )
            if qac:
                tools.append(
                    WizardTool(
                        id=f"run_{build.lower()}_qac_{id_suffix}",
                        label="Run {0} QAC", label_args=(build,),
                        description="Deploy mods and run {0}QuickAutoClean.exe.",
                        description_args=(build,),
                        dialog_class_path="wizards.sseedit.SSEEditQACWizard",
                        extra=dict(nexus_extra),
                    )
                )

        discord_exe = BethesdaBase._discord_xedit_exe(build)
        mode = discord_mode or BethesdaBase._discord_xedit_mode(build)
        discord_extra = {
            "xedit_exe": discord_exe,
            "display_name": "xEdit",
            "app_dir": "xEdit (Discord)",
            "discord": True,
            "discord_mode": mode,
        }
        tools.append(
            WizardTool(
                id=f"run_xedit_discord_{id_suffix}",
                label="Run xEdit (Discord version)",
                description=(
                    "Deploy mods and run {0} -{1} from the latest "
                    "xEdit build, released through the xEdit Discord."),
                description_args=(discord_exe, mode),
                dialog_class_path="wizards.sseedit.XEditDiscordWizard",
                extra=dict(discord_extra),
            )
        )
        tools.append(
            WizardTool(
                id=f"run_xedit_discord_qac_{id_suffix}",
                label="Run xEdit QAC (Discord version)",
                description=(
                    "Deploy mods and run {0} -{1} -quickautoclean "
                    "from the latest xEdit build, released through the xEdit Discord."),
                description_args=(discord_exe, mode),
                dialog_class_path="wizards.sseedit.XEditDiscordQACWizard",
                extra=dict(discord_extra),
            )
        )
        return tools

    def get_game_path(self) -> Path | None:
        return self._game_path

    def get_mod_data_path(self) -> Path | None:
        """Mods go into the Data/ subfolder of the game root directory."""
        if self._game_path is None:
            return None
        return self._game_path / "Data"

    def get_mod_staging_path(self) -> Path:
        if self._staging_path is not None:
            return self._staging_path / "mods"
        return _PROFILES_DIR / self.name / "mods"

    def _load_paths_extra(self, data: dict) -> None:
        self._script_extender_swap = data.get("script_extender_swap", True)
        self._profile_ini_files = data.get("profile_ini_files", False)
        self._profile_saves = data.get("profile_saves", False)
        raw_pfname = data.get("plugins_txt_filename")
        # A saved default must not become a profile-specific casing override.
        pfname = str(raw_pfname) if raw_pfname else ""
        self._plugins_txt_filename_override = (
            pfname if pfname and pfname != self._PLUGINS_TXT_FILENAME else None)

    def _save_paths_extra(self) -> dict:
        # Persist the default too, so resetting casing clears stale overrides.
        return {
            "script_extender_swap": self._script_extender_swap,
            "profile_ini_files":    self._profile_ini_files,
            "profile_saves":        self._profile_saves,
            "plugins_txt_filename": self.plugins_txt_filename,
        }

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

    @property
    def script_extender_swap(self) -> bool:
        return self._script_extender_swap

    def set_script_extender_swap(self, value: bool) -> None:
        self._script_extender_swap = value
        self.save_paths()

    @property
    def profile_ini_files(self) -> bool:
        return self._profile_ini_files

    def set_profile_ini_files(self, value: bool) -> None:
        self._profile_ini_files = value
        self.save_paths()
        if value:
            self._ensure_profile_ini_dirs()

    # Name of the subfolder inside each profile that holds the user's INIs.
    _PROFILE_INI_SUBDIR = "ini files"

    def _profile_ini_dir(self, profile: str) -> Path:
        """Folder inside a profile that the user drops per-profile INIs into."""
        return self.get_profile_root() / "profiles" / profile / self._PROFILE_INI_SUBDIR

    def _ensure_profile_ini_dirs(self) -> None:
        """Create the empty 'ini files' folder for every existing profile."""
        profiles_root = self.get_profile_root() / "profiles"
        if not profiles_root.is_dir():
            return
        for profile_dir in profiles_root.iterdir():
            if profile_dir.is_dir():
                (profile_dir / self._PROFILE_INI_SUBDIR).mkdir(parents=True, exist_ok=True)

    @property
    def profile_saves(self) -> bool:
        return self._profile_saves and self.supports_profile_saves

    def set_profile_saves(self, value: bool) -> None:
        self._profile_saves = value and self.supports_profile_saves
        self.save_paths()
        if self._profile_saves:
            self._ensure_profile_saves_dirs()

    def _ensure_profile_saves_dirs(self) -> None:
        """Create an empty ``Saves`` folder in each existing profile folder."""
        try:
            profiles_root = self.get_profile_root() / "profiles"
        except Exception:
            return
        names: set[str] = set()
        if self._active_profile_dir is not None:
            names.add(self._active_profile_dir.name)
        if profiles_root.is_dir():
            names.update(p.name for p in profiles_root.iterdir() if p.is_dir())
        for name in names:
            try:
                (profiles_root / name / self._SAVES_FOLDER_NAME).mkdir(
                    parents=True, exist_ok=True)
            except OSError:
                pass

    def set_prefix_path(self, path: Path | str | None) -> None:
        self._prefix_path = Path(path) if path else None
        self.save_paths()

    _archive_list_in_prefs_ini: bool = True
    archive_invalidation_enabled = True
    _archive_invalidation_extra_keys: tuple[tuple[str, str], ...] = ()

    _invalidation_archive_list_key: str = "SArchiveList"

    @property
    def frameworks(self) -> dict[str, str]:
        fw = {"Script Extender": self._script_extender_exe} if self._script_extender_exe else {}
        if (self._archive_list_needs_mod_bsas
                and self._archive_list_fix_name and self._archive_list_fix_path):
            fw[self._archive_list_fix_name] = self._archive_list_fix_path
        return fw

    @property
    def framework_launch_exes(self) -> dict[str, str]:
        if "Script Extender" in self.frameworks:
            return {"Script Extender": self._script_extender_exe}
        return {}

    _PLUGINS_TXT_FILENAME = "plugins.txt"

    uses_plugins_txt = True

    @property
    def plugins_txt_filename(self) -> str:
        return self._plugins_txt_filename_override or self._PLUGINS_TXT_FILENAME

    def set_plugins_txt_filename(self, value: str) -> None:
        value = (value or "").strip() or self._PLUGINS_TXT_FILENAME
        if value == self._PLUGINS_TXT_FILENAME:
            self._plugins_txt_filename_override = None
        else:
            self._plugins_txt_filename_override = value
        self.save_paths()

    # GOG builds require a real plugins.txt copy rather than a symlink.
    def _plugins_txt_targets(self, prefix_root: "Path | None" = None) -> list[Path]:
        root = prefix_root if prefix_root is not None else self._prefix_path
        if root is None:
            return []
        fname = self.plugins_txt_filename
        steam_dir = root / self._APPDATA_SUBPATH
        targets: list[Path] = []
        if steam_dir.is_dir():
            targets.append(steam_dir / fname)
        if self._APPDATA_SUBPATH_GOG is not None:
            gog_dir = root / self._APPDATA_SUBPATH_GOG
            if gog_dir.is_dir():
                targets.append(gog_dir / fname)
        if not targets:
            targets.append(steam_dir / fname)
        return targets

    def _plugins_txt_target(self) -> Path | None:
        """Return the primary in-prefix plugins.txt path (back-compat shim)."""
        targets = self._plugins_txt_targets()
        return targets[0] if targets else None

    def _symlink_plugins_txt(self, profile: str, log_fn, prefix_root: "Path | None" = None) -> None:
        from Utils.plugins import deploy_plugins_copy
        _log = log_fn
        targets = self._plugins_txt_targets(prefix_root)
        if not targets:
            _log("  WARN: Prefix path not set - skipping plugins.txt deploy.")
            return

        source = self.get_profile_root() / "profiles" / profile / "plugins.txt"
        if not source.is_file():
            _log(f"  WARN: plugins.txt not found at {source} - skipping deploy.")
            return

        content = source.read_text(encoding="utf-8")
        for target in targets:
            deploy_plugins_copy(target.parent, target.name, content, _log)
            if self._lock_plugins_txt:
                try:
                    target.chmod(0o444)
                except OSError as exc:
                    _log(f"  WARN: could not set {target.name} read-only: {exc}")

    def _remove_plugins_txt_symlink(self, log_fn) -> None:
        """Remove the deployed plugins.txt copy (or legacy symlink) on restore."""
        from Utils.plugins import remove_plugins_copy
        _log = log_fn
        for target in self._plugins_txt_targets():
            remove_plugins_copy(target.parent, target.name, _log)

    _plugin_load_order_by_mtime: bool = True

    # Every Bethesda engine loads master-flagged plugins before non-masters.
    plugins_master_block = True

    _lock_plugins_txt = False

    def _orders_plugins_by_mtime(self) -> bool:
        return self._plugin_load_order_by_mtime and not self.plugins_use_star_prefix

    def stamp_plugin_load_order(self, profile: str, log_fn=None) -> None:
        """Set ascending mtimes on deployed plugins to match the profile's load order."""
        _log = log_fn or (lambda _: None)
        if self._game_path is None or not self._orders_plugins_by_mtime():
            return
        from Utils.plugins import read_loadorder, read_plugins
        profile_dir = self.get_profile_root() / "profiles" / profile
        ordered = read_loadorder(profile_dir / "loadorder.txt")
        if not ordered:
            ordered = [
                e.name for e in read_plugins(
                    profile_dir / "plugins.txt",
                    star_prefix=self.plugins_use_star_prefix,
                )
            ]
        if not ordered:
            return
        from Utils.plugins.mtimes import stamp_plugin_load_order
        data_dir = self._game_path / "Data"
        if self.vfs_launch_enabled:
            from Utils.vfs import virtual_data_write_path
            data_dir = virtual_data_write_path(self, ".")
        stamped = stamp_plugin_load_order(
            ordered,
            data_dir,
            staging_root=self.get_effective_mod_staging_path(),
            overwrite_dir=self.get_effective_overwrite_path(),
            log_fn=_log,
        )
        if stamped:
            _log(f"  Set mtimes on {stamped} plugin(s) to enforce load order.")

    _MYGAMES_DOCS = Path("drive_c/users/steamuser/Documents/My Games")

    @staticmethod
    def _resolve_ini_path(directory: Path, filename: str) -> Path:
        """Keep an existing INI's on-disk casing, or use the configured name."""
        target = directory / filename
        if target.is_file() or target.is_symlink():
            return target
        try:
            for entry in directory.iterdir():
                if (entry.name.casefold() == filename.casefold()
                        and (entry.is_file() or entry.is_symlink())):
                    return entry
        except OSError:
            pass
        return target

    def _get_archive_ini_path(self) -> "Path | None":
        """Return the primary INI used for archive invalidation (back-compat)."""
        mygames = self._mygames_path()
        if mygames is None:
            return None
        return self._resolve_ini_path(mygames, self._ARCHIVE_INI_FILENAME)

    def _get_archive_ini_paths(self) -> list[Path]:
        mygames = self._mygames_path()
        if mygames is None:
            return []
        paths = [self._resolve_ini_path(mygames, self._ARCHIVE_INI_FILENAME)]
        if self._ARCHIVE_PREFS_INI_FILENAME:
            paths.append(self._resolve_ini_path(
                mygames, self._ARCHIVE_PREFS_INI_FILENAME))
        return paths

    def _mygames_paths(self) -> list[Path]:
        if self._prefix_path is None:
            return []
        steam_dir = self._prefix_path / self._MYGAMES_DOCS / self._MYGAMES_SUBPATH
        paths: list[Path] = []
        if steam_dir.is_dir():
            paths.append(steam_dir)
        if self._MYGAMES_SUBPATH_GOG is not None:
            gog_dir = self._prefix_path / self._MYGAMES_DOCS / self._MYGAMES_SUBPATH_GOG
            if gog_dir.is_dir():
                paths.append(gog_dir)
        if not paths:
            paths.append(steam_dir)
        return paths

    def _mygames_path(self) -> "Path | None":
        """Return the primary My Games folder (back-compat shim)."""
        paths = self._mygames_paths()
        return paths[0] if paths else None

    def _profile_ini_filename(self, filename: str) -> str:
        prefs = self._ARCHIVE_PREFS_INI_FILENAME
        stem = Path(prefs or self._ARCHIVE_INI_FILENAME).stem.removesuffix("Prefs")
        names = [self._ARCHIVE_INI_FILENAME, prefs, self._CUSTOM_INI_FILENAME,
                 f"{stem}Custom.ini", *self._PROFILE_SAVES_INI_FILES]
        return next((name for name in names if name and name.casefold() == filename.casefold()), filename)

    @staticmethod
    def _profile_ini_link_dir(target: Path) -> "Path | None":
        if not target.is_symlink():
            return None
        try:
            source = target.readlink()
            if not source.is_absolute():
                source = target.parent / source
            return source.parent.resolve()
        except (OSError, RuntimeError):
            return None

    def _is_managed_profile_ini_link(self, target: Path) -> bool:
        link_dir = self._profile_ini_link_dir(target)
        if link_dir is None:
            return False
        try:
            relative = link_dir.relative_to(
                (self.get_profile_root() / "profiles").resolve())
        except (OSError, RuntimeError, ValueError):
            return False
        return (
            len(relative.parts) == 2
            and relative.parts[1].casefold()
            == self._PROFILE_INI_SUBDIR.casefold()
        )

    @staticmethod
    def _restore_profile_ini_link(target: Path, log_fn) -> None:
        target.unlink()
        log_fn(f"  Removed profile INI symlink: {target.name}")
        backup = target.with_suffix(".bak")
        if backup.exists() or backup.is_symlink():
            backup.rename(target)
            log_fn(f"  Restored {target.name} from .bak")

    def _symlink_profile_ini_files(self, profile: str, log_fn) -> None:
        """Link profile INIs using the game's filenames, preserving prefix backups."""
        _log = log_fn
        if self._PROFILE_SAVES_INI_FILES:
            from Games.Bethesda.profile_saves import restore_profile_saves
            restore_profile_saves(self, _log)
        if not self._profile_ini_files:
            return
        mygames_dirs = self._mygames_paths()
        if not mygames_dirs:
            _log("  WARN: Prefix path not set - skipping profile INI symlinks.")
            return
        ini_dir = self._profile_ini_dir(profile)
        ini_dir.mkdir(parents=True, exist_ok=True)
        ini_files = {}
        for src in sorted(ini_dir.iterdir()):
            if src.suffix.casefold() != ".ini" or not src.is_file():
                continue
            key = src.name.casefold()
            if key in ini_files:
                import filecmp
                if not filecmp.cmp(src, ini_files[key], shallow=False):
                    raise RuntimeError(f"Conflicting profile INIs: {ini_files[key].name} and {src.name}. Keep one version in {ini_dir}.")
                _log(f"  Ignoring identical INI casing duplicate: {src.name}")
                continue
            ini_files[key] = src
        if not ini_files:
            _log(f"  No *.ini files found in '{ini_dir.name}' folder - skipping.")
            return
        for mygames in mygames_dirs:
            mygames.mkdir(parents=True, exist_ok=True)
            for target in mygames.iterdir():
                if (target.name.casefold() in ini_files
                        and self._is_managed_profile_ini_link(target)):
                    self._restore_profile_ini_link(target, _log)
            for src in ini_files.values():
                target = self._resolve_ini_path(mygames, self._profile_ini_filename(src.name))
                if target.exists() or target.is_symlink():
                    backup = target.with_suffix(".bak")
                    if backup.exists() or backup.is_symlink():
                        number = 1
                        saved = backup.with_name(f"{backup.name}.{number}")
                        while saved.exists() or saved.is_symlink():
                            number += 1
                            saved = backup.with_name(f"{backup.name}.{number}")
                        backup.rename(saved)
                        _log(f"  Preserved earlier backup: {saved.name}")
                    target.rename(backup)
                    _log(f"  Backed up {target.name} → {backup.name}")
                target.symlink_to(src)
                _log(f"  Linked {src.name} → {target}")

    def _remove_profile_ini_symlinks(self, _profile: str, log_fn) -> None:
        """Remove profile INI symlinks from My Games and restore any backups."""
        _log = log_fn
        if self._PROFILE_SAVES_INI_FILES:
            from Games.Bethesda.profile_saves import restore_profile_saves
            restore_profile_saves(self, _log)
        mygames_dirs = [p for p in self._mygames_paths() if p.is_dir()]
        if not mygames_dirs:
            return
        for mygames in mygames_dirs:
            # Scan the directory too, to remove orphaned profile INI symlinks.
            for target in mygames.iterdir():
                if target.suffix.casefold() != ".ini":
                    continue
                if not self._is_managed_profile_ini_link(target):
                    continue
                self._restore_profile_ini_link(target, _log)

    supports_profile_saves = True
    _SAVES_FOLDER_NAME = "Saves"
    _SAVES_BACKUP_SUFFIX = "_backup_amm"
    _PROFILE_SAVES_INI_FILES: tuple[str, ...] = ()
    _PROFILE_SAVES_PARENT_RELATIVE = False

    def _profile_saves_ini_files(self, directory: Path) -> tuple[str, ...]:
        return self._PROFILE_SAVES_INI_FILES

    def _saves_link_targets(self) -> list[Path]:
        return self._mygames_paths()

    def _profile_saves_dir(self, profile: str) -> Path:
        """Path to the profile-specific saves folder (created on demand)."""
        return self.get_profile_root() / "profiles" / profile / self._SAVES_FOLDER_NAME

    def _symlink_profile_saves(self, profile: str, log_fn) -> None:
        _log = log_fn
        if self._PROFILE_SAVES_INI_FILES:
            from Games.Bethesda.profile_saves import deploy_profile_saves
            deploy_profile_saves(self, profile, _log)
            return
        if not self.profile_saves:
            return
        targets = self._saves_link_targets()
        if not targets:
            _log("  WARN: No save-link target - skipping profile saves.")
            return
        profile_saves = self._profile_saves_dir(profile)
        profile_saves.mkdir(parents=True, exist_ok=True)
        for target_dir in targets:
            target_dir.mkdir(parents=True, exist_ok=True)
            link = target_dir / self._SAVES_FOLDER_NAME
            if link.is_symlink():
                link.unlink()
            elif link.exists():
                backup = target_dir / (self._SAVES_FOLDER_NAME + self._SAVES_BACKUP_SUFFIX)
                if backup.exists():
                    _log(f"  WARN: {backup.name} already exists - leaving "
                         f"{link.name} in place, skipping.")
                    continue
                link.rename(backup)
                _log(f"  Backed up existing {link.name} → {backup.name}")
            link.symlink_to(profile_saves)
            _log(f"  Linked {link} → {profile_saves}")

    def _remove_profile_saves_symlinks(self, profile: str, log_fn) -> None:
        """Remove profile saves symlinks and restore any backed-up Saves folder."""
        _log = log_fn
        if self._PROFILE_SAVES_INI_FILES:
            from Games.Bethesda.profile_saves import restore_profile_saves
            restore_profile_saves(self, _log)
            return
        if not self.profile_saves:
            return
        profile_saves = self._profile_saves_dir(profile)
        for target_dir in self._saves_link_targets():
            if not target_dir.is_dir():
                continue
            link = target_dir / self._SAVES_FOLDER_NAME
            if link.is_symlink() and Path(link.resolve()) == profile_saves.resolve():
                link.unlink()
                _log(f"  Removed profile saves symlink: {link}")
            elif link.exists():
                # Leave foreign symlinks and their backups untouched.
                continue
            backup = target_dir / (self._SAVES_FOLDER_NAME + self._SAVES_BACKUP_SUFFIX)
            if backup.exists() and not link.exists():
                backup.rename(link)
                _log(f"  Restored {link.name} from {backup.name}")

    def apply_archive_invalidation(self, log_fn) -> None:
        _log = log_fn
        if not self.archive_invalidation_enabled:
            return
        # Revert existing invalidation when the setting is disabled.
        if not self.archive_invalidation:
            self.revert_archive_invalidation(_log)
            return
        ini_paths = self._get_archive_ini_paths()
        if not ini_paths:
            _log("  WARN: Prefix path not set - skipping archive invalidation.")
            return

        # Compute one BSA delta for all INIs and write the sidecar once.
        prev_mod_bsas: list[str] = []
        new_mod_bsas: list[str] = []
        if self._archive_list_needs_mod_bsas:
            prev_mod_bsas = self._tracked_mod_bsas()
            new_mod_bsas = self._deployed_mod_bsas()

        self._write_dummy_bsa_file(_log)
        primary_ini = ini_paths[0]
        longest_list = ""
        for ini_path in ini_paths:
            ini_path.parent.mkdir(parents=True, exist_ok=True)
            _set_ini_key(ini_path, "Archive", "bInvalidateOlderFiles", "1")
            for key, value in self._archive_invalidation_extra_keys:
                if _read_ini_key(ini_path, "Archive", key) is not None:
                    continue
                _set_ini_key(ini_path, "Archive", key, value)
            # Remove legacy Prefs archive lists on engines that do not support them.
            if ini_path != primary_ini and not self._archive_list_in_prefs_ini:
                self._strip_archive_list_keys(ini_path)
                continue
            written = self._apply_dummy_bsa_invalidation_ini(
                ini_path, prev_mod_bsas, new_mod_bsas)
            if len(written) > len(longest_list):
                longest_list = written

        if self._archive_list_needs_mod_bsas:
            self._save_tracked_mod_bsas(new_mod_bsas)
            if new_mod_bsas:
                _log(f"  Registered {len(new_mod_bsas)} mod BSA(s) in "
                     f"{self._invalidation_archive_list_key}.")
            self._sync_archive_list_custom_ini(ini_paths, longest_list, _log)

        names = ", ".join(p.name for p in ini_paths)
        _log(f"  Archive invalidation enabled in {names}.")

    def _sync_archive_list_custom_ini(
        self, ini_paths: "list[Path]", list_str: str, _log,
    ) -> None:
        key = self._invalidation_archive_list_key
        ini_dirs = {p.parent for p in ini_paths}
        over = len(list_str) > 255
        if over and self._archive_list_fix_installed():
            for d in ini_dirs:
                _set_ini_key(self._resolve_ini_path(d, self._CUSTOM_INI_FILENAME), "Archive",
                             key, list_str)
            _log(f"  {key} is {len(list_str)} chars (engine limit 255) - "
                 f"wrote full list to {self._CUSTOM_INI_FILENAME} "
                 f"({self._archive_list_fix_name} installed).")
            return
        for d in ini_dirs:
            custom_ini = self._resolve_ini_path(d, self._CUSTOM_INI_FILENAME)
            if custom_ini.is_file():
                _set_ini_key(custom_ini, "Archive", key, None)
        if over:
            fix = (f" Install {self._archive_list_fix_name} to fix this."
                   if self._archive_list_fix_name else "")
            _log(f"  WARN: {key} is {len(list_str)} characters - the engine "
                 "reads only the first 255 and some mod BSAs will not load."
                 f"{fix}")
            self.add_deploy_warning(
                f"{key} exceeds the engine's 255-character limit - some mod "
                f"BSAs will not load.{fix}")

    def revert_archive_invalidation(self, log_fn) -> None:
        _log = log_fn
        if not self.archive_invalidation_enabled:
            return
        ini_paths = [p for p in self._get_archive_ini_paths() if p.is_file()]
        if not ini_paths:
            return

        for ini_path in ini_paths:
            self._revert_dummy_bsa_invalidation_ini(ini_path)
            _set_ini_key(ini_path, "Archive", "bInvalidateOlderFiles", None)
            for key, value in self._archive_invalidation_extra_keys:
                current = _read_ini_key(ini_path, "Archive", key)
                if current is None or current != value:
                    continue
                _set_ini_key(ini_path, "Archive", key, None)

        self._delete_dummy_bsa_file(_log)
        if self._archive_list_needs_mod_bsas:
            self._save_tracked_mod_bsas([])
            for d in {p.parent for p in ini_paths}:
                custom_ini = self._resolve_ini_path(d, self._CUSTOM_INI_FILENAME)
                if custom_ini.is_file():
                    _set_ini_key(custom_ini, "Archive",
                                 self._invalidation_archive_list_key, None)
        names = ", ".join(p.name for p in ini_paths)
        _log(f"  Archive invalidation reverted in {names}.")

    _ini_override_keys: tuple[tuple[str, str, str], ...] = ()
    disable_creations_menu_default = False

    @property
    def supports_creations_menu(self) -> bool:
        return bool(self._ini_override_keys)

    @property
    def disable_creations_menu(self) -> bool:
        return self._load_settings().get(
            "disable_creations_menu", self.disable_creations_menu_default)

    @disable_creations_menu.setter
    def disable_creations_menu(self, value: bool) -> None:
        data = self._load_settings()
        data["disable_creations_menu"] = bool(value)
        self._save_settings(data)

    @staticmethod
    def _ini_override_backup_path(ini_path: Path) -> Path:
        target = ini_path.resolve()
        return target.with_name(f".{target.name}.amethyst-overrides.json")

    def apply_ini_overrides(self, log_fn) -> None:
        """Apply the optional Creations platform override on deploy."""
        if not self._ini_override_keys:
            return
        if not self.disable_creations_menu:
            self.revert_ini_overrides(log_fn)
            return
        _log = log_fn
        ini_paths = self._get_archive_ini_paths()
        if not ini_paths:
            return
        for ini_path in ini_paths:
            ini_path.parent.mkdir(parents=True, exist_ok=True)
            backup_path = self._ini_override_backup_path(ini_path)
            backup = (json.loads(backup_path.read_text(encoding="utf-8"))
                      if backup_path.is_file() else {})
            for section, key, value in self._ini_override_keys:
                current = _read_ini_key(ini_path, section, key)
                if current == value:
                    continue
                originals = backup.setdefault(section, {})
                if key not in originals:
                    originals[key] = current
                    write_atomic_text(backup_path, json.dumps(backup))
                _set_ini_key(ini_path, section, key, value)
        names = ", ".join(p.name for p in ini_paths)
        _log(f"  Applied INI overrides in {names}.")

    def revert_ini_overrides(self, log_fn) -> None:
        """Restore overridden values, preserving subsequent user edits."""
        if not self._ini_override_keys:
            return
        _log = log_fn
        ini_paths = [p for p in self._get_archive_ini_paths() if p.is_file()]
        if not ini_paths:
            return
        for ini_path in ini_paths:
            backup_path = self._ini_override_backup_path(ini_path)
            backup = (json.loads(backup_path.read_text(encoding="utf-8"))
                      if backup_path.is_file() else {})
            for section, key, value in self._ini_override_keys:
                if _read_ini_key(ini_path, section, key) != value:
                    continue
                originals = backup.get(section, {})
                if key in originals:
                    _set_ini_key(ini_path, section, key, originals[key])
                elif self.disable_creations_menu_default:
                    # Older Fallout 4 deployments did not save the original value.
                    _set_ini_key(ini_path, section, key, None)
            backup_path.unlink(missing_ok=True)
        names = ", ".join(p.name for p in ini_paths)
        _log(f"  Reverted INI overrides in {names}.")

    def _write_dummy_bsa_file(self, _log) -> None:
        """Write the dummy BSA into the game's Data folder, if configured."""
        bsa_name = self._invalidation_bsa_name
        bsa_version = self._invalidation_bsa_version
        if bsa_name is None or bsa_version is None:
            return
        if self._game_path is None:
            _log("  WARN: Game path not set - skipping dummy BSA write.")
            return
        from Utils.bsa.invalidation import write_dummy_bsa
        try:
            if self.vfs_launch_enabled:
                from Utils.vfs import virtual_data_write_path
                target = virtual_data_write_path(self, bsa_name)
            else:
                target = self._game_path / "Data" / bsa_name
            target.parent.mkdir(parents=True, exist_ok=True)
            write_dummy_bsa(target, bsa_version)
        except OSError as exc:
            _log(f"  WARN: Could not write {bsa_name}: {exc}")

    def _delete_dummy_bsa_file(self, _log) -> None:
        """Remove the dummy BSA from the game's Data folder, if present."""
        bsa_name = self._invalidation_bsa_name
        if bsa_name is None or self._game_path is None:
            return
        if self.vfs_launch_enabled:
            try:
                from Utils.vfs import virtual_data_write_path
                bsa_path = virtual_data_write_path(self, bsa_name)
            except RuntimeError:
                return
        else:
            bsa_path = self._game_path / "Data" / bsa_name
        if not bsa_path.is_file():
            return
        try:
            bsa_path.unlink()
            _log(f"  Removed dummy {bsa_name}.")
        except OSError as exc:
            _log(f"  WARN: Could not remove {bsa_name}: {exc}")

    def _apply_dummy_bsa_invalidation_ini(
        self, ini_path: Path,
        prev_mod_bsas: "list[str] | None" = None,
        new_mod_bsas: "list[str] | None" = None,
    ) -> str:
        bsa_name = self._invalidation_bsa_name
        if bsa_name is None:
            return ""
        from Utils.bsa.invalidation import (
            ensure_in_archive_list, append_to_archive_list,
            remove_many_from_archive_list,
        )
        key = self._invalidation_archive_list_key
        current = _read_ini_key(ini_path, "Archive", key) or ""
        updated = ensure_in_archive_list(current, bsa_name)
        if self._archive_list_needs_mod_bsas:
            prev = self._tracked_mod_bsas() if prev_mod_bsas is None else prev_mod_bsas
            mod_bsas = self._deployed_mod_bsas() if new_mod_bsas is None else new_mod_bsas
            updated = remove_many_from_archive_list(updated, prev)
            updated = append_to_archive_list(updated, mod_bsas)
        if updated != current:
            _set_ini_key(ini_path, "Archive", key, updated)
        _set_ini_key(ini_path, "Archive", "SInvalidationFile", "")
        return updated

    def _strip_archive_list_keys(self, ini_path: Path) -> None:
        if _read_ini_key(ini_path, "Archive",
                         self._invalidation_archive_list_key) is not None:
            _set_ini_key(ini_path, "Archive",
                         self._invalidation_archive_list_key, None)
        if _read_ini_key(ini_path, "Archive", "SInvalidationFile") == "":
            _set_ini_key(ini_path, "Archive", "SInvalidationFile", None)

    def _revert_dummy_bsa_invalidation_ini(self, ini_path: Path) -> None:
        bsa_name = self._invalidation_bsa_name
        if bsa_name is None:
            return
        from Utils.bsa.invalidation import (
            remove_from_archive_list, remove_many_from_archive_list,
        )
        key = self._invalidation_archive_list_key
        current = _read_ini_key(ini_path, "Archive", key)
        if current is not None:
            updated = remove_from_archive_list(current, bsa_name)
            if self._archive_list_needs_mod_bsas:
                updated = remove_many_from_archive_list(
                    updated, self._tracked_mod_bsas())
            if updated != current:
                _set_ini_key(ini_path, "Archive", key, updated or None)
        # Restore the default so SInvalidationFile is not left empty.
        if _read_ini_key(ini_path, "Archive", "SInvalidationFile") == "":
            _set_ini_key(ini_path, "Archive", "SInvalidationFile",
                         "ArchiveInvalidation.txt")

    def _archive_list_fix_installed(self) -> bool:
        if self._archive_list_fix_path is None or self._game_path is None:
            return False
        if self.vfs_launch_enabled:
            from Utils.vfs import virtual_file
            return virtual_file(self, self._archive_list_fix_path)
        current = self._game_path
        for part in Path(self._archive_list_fix_path).parts:
            try:
                entries = {e.name.lower(): e for e in current.iterdir()}
            except OSError:
                return False
            match = entries.get(part.lower())
            if match is None:
                return False
            current = match
        return current.is_file()

    def _mod_bsa_tracking_path(self) -> "Path | None":
        try:
            return self.get_effective_filemap_path().parent / "managed_archives.txt"
        except Exception:
            return None

    def _tracked_mod_bsas(self) -> list[str]:
        """Mod BSA names we previously appended to SArchiveList, if any."""
        path = self._mod_bsa_tracking_path()
        if path is None or not path.is_file():
            return []
        try:
            return [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()
                    if ln.strip()]
        except OSError:
            return []

    def _save_tracked_mod_bsas(self, names: list[str]) -> None:
        path = self._mod_bsa_tracking_path()
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("\n".join(names) + ("\n" if names else ""),
                            encoding="utf-8")
        except OSError:
            pass

    def _deployed_mod_bsas(self) -> list[str]:
        from Utils.filegraph.deploy import current, legacy_rows
        if current() is None:
            return []
        names: list[str] = []
        seen: set[str] = set()
        try:
            for rel, _owner in legacy_rows():
                rel = rel.strip()
                if not rel or "/" in rel or "\\" in rel:
                    continue  # only top-level Data/ entries are loadable archives
                low = rel.lower()
                if not (low.endswith(".bsa") or low.endswith(".ba2")):
                    continue
                if low in seen:
                    continue
                seen.add(low)
                names.append(rel)
        except OSError:
            return []
        return self._order_mod_bsas_by_plugins(names)

    def _plugin_load_order(self) -> list[str]:
        if self._active_profile_dir is None:
            return []
        # Profile plugins.txt stays lowercase regardless of in-prefix casing.
        path = self._active_profile_dir / "plugins.txt"
        if not path.is_file():
            return []
        order: list[str] = []
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                name = line.strip()
                if not name or name.startswith("#"):
                    continue
                if name.startswith("*"):
                    name = name[1:].strip()
                if name:
                    order.append(name.lower())
        except OSError:
            return []
        return order

    def _order_mod_bsas_by_plugins(self, bsa_names: list[str]) -> list[str]:
        order = self._plugin_load_order()
        if not order:
            return bsa_names
        # plugin stem (no extension) -> load index
        stem_rank: dict[str, int] = {}
        for i, plugin in enumerate(order):
            stem = plugin.rsplit(".", 1)[0]
            stem_rank[stem] = i
        stems = sorted(stem_rank, key=len, reverse=True)

        def rank(bsa: str) -> int:
            stem = bsa.rsplit(".", 1)[0].lower()
            for pstem in stems:
                if stem == pstem:
                    return stem_rank[pstem]
                if (stem.startswith(pstem)
                        and stem[len(pstem)] in " -_0123456789"):
                    return stem_rank[pstem]
            return -1

        ranked = [(rank(b), i, b) for i, b in enumerate(bsa_names)]
        # Later plugins win: place their matching BSAs first.
        matched = sorted((t for t in ranked if t[0] >= 0),
                         key=lambda t: (-t[0], t[1]))
        unmatched = [t for t in ranked if t[0] < 0]
        return [b for _, _, b in matched] + [b for _, _, b in unmatched]

    def _launcher_name(self) -> str:
        candidates = [self.exe_name, *self.exe_name_alts]
        if self._game_path is not None:
            for name in candidates:
                stem = Path(name).stem
                if (self._game_path / name).is_file() or \
                   (self._game_path / (stem + ".bak")).is_file():
                    return name
        return self.exe_name

    @property
    def _script_extender_runtime_ini(self) -> "Path | None":
        return None

    def _script_extender_runtime_ini_path(self) -> "Path | None":
        rel_path = self._script_extender_runtime_ini
        if self._game_path is None or rel_path is None:
            return None
        from Utils.games.frameworks import resolve_file_ci
        return (resolve_file_ci(self._game_path, rel_path)
                or self._game_path / rel_path)

    def _write_script_extender_runtime_override(self, backup_name: str,
                                                log_fn) -> None:
        ini_path = self._script_extender_runtime_ini_path()
        if ini_path is None:
            return
        if ini_path.is_symlink():
            from Utils.atomic_write import write_atomic
            write_atomic(ini_path, ini_path.read_bytes())
        _set_ini_key(ini_path, "Loader", "RuntimeName", backup_name,
                     case_insensitive=True)
        rel_path = ini_path.relative_to(self._game_path)
        log_fn(f"  Wrote {rel_path} (RuntimeName={backup_name}).")

    def _remove_script_extender_runtime_override(self, log_fn) -> None:
        ini_path = self._script_extender_runtime_ini_path()
        if ini_path is None or self._game_path is None:
            return
        backup_name = Path(self._launcher_name()).stem + ".bak"
        backup = self._game_path / backup_name
        if not backup.is_file() or _read_ini_key(
                ini_path, "Loader", "RuntimeName",
                case_insensitive=True) != backup_name:
            return
        _set_ini_key(ini_path, "Loader", "RuntimeName", None,
                     case_insensitive=True)
        rel_path = ini_path.relative_to(self._game_path)
        log_fn(f"  Removed Amethyst RuntimeName from {rel_path}.")

    def swap_launcher(self, log_fn) -> None:
        """Replace the game launcher with the script extender if present."""
        _log = log_fn
        if self._game_path is None:
            return
        if not self.supports_script_extender_swap:
            _log("  Launcher swap is not used for this game - skipping.")
            return
        if self.vfs_launch_enabled:
            _log("  VFS launch: launcher swap is virtual - game files unchanged.")
            return
        if not self._script_extender_swap:
            _log("  Script extender / launcher swap disabled - skipping.")
            return
        se = self._game_path / self._script_extender_exe
        if not se.is_file():
            _log(f"  {self._script_extender_exe} not found - skipping launcher swap.")
            return
        exe_name = self._launcher_name()
        launcher = self._game_path / exe_name
        backup   = self._game_path / (Path(exe_name).stem + ".bak")
        if launcher.is_file():
            launcher.rename(backup)
            _log(f"  Renamed {exe_name} → {backup.name}.")
        shutil.copy2(se, launcher)
        _log(f"  Copied {self._script_extender_exe} → {exe_name}.")
        if backup.is_file():
            self._write_script_extender_runtime_override(backup.name, _log)

    def _restore_launcher(self, log_fn) -> None:
        """Reverse the script extender launcher swap if a backup exists."""
        _log = log_fn
        if self._game_path is None:
            return
        exe_name = self._launcher_name()
        backup   = self._game_path / (Path(exe_name).stem + ".bak")
        launcher = self._game_path / exe_name
        if not backup.is_file():
            return
        if launcher.is_file():
            launcher.unlink()
        backup.rename(launcher)
        _log(f"  Restored {exe_name} from {backup.name}.")

    def deploy(self, log_fn=None, mode: LinkMode = LinkMode.HARDLINK,
               profile: str = "default", progress_fn=None) -> None:
        _log = log_fn or (lambda _: None)

        if self._game_path is None:
            raise RuntimeError("Game path is not configured.")

        data_dir = self._game_path / "Data"
        filemap  = self.get_effective_filemap_path()
        staging  = self.get_effective_mod_staging_path()

        if not data_dir.is_dir():
            raise RuntimeError(f"Data directory not found: {data_dir}")
        from Utils.filegraph.deploy import input_ready
        if not input_ready():
            raise RuntimeError(
                f"filemap.txt not found: {filemap}\n"
                "Run 'Build Filemap' before deploying."
            )

        from Utils.bethesda.registry import repair_game_registry_path
        if not repair_game_registry_path(self, self._game_path, _log):
            _log("  WARN: Bethesda registry game path could not be repaired.")

        if self.vfs_launch_enabled:
            return self._deploy_vfs(
                profile=profile, filemap=filemap, staging=staging,
                log_fn=_log, progress_fn=progress_fn)

        profile_dir = self.get_profile_root() / "profiles" / profile
        per_mod_strip = load_per_mod_strip_prefixes(profile_dir)

        # Read separator overrides from the active profile, not the shared filemap root.
        _sep_deploy = load_separator_deploy_paths(profile_dir)
        _sep_entries = read_modlist(profile_dir / "modlist.txt") if _sep_deploy else []
        per_mod_deploy = expand_separator_deploy_paths(_sep_deploy, _sep_entries) or None
        per_mod_modes = expand_separator_link_modes(_sep_deploy, _sep_entries) or None
        per_mod_raw = expand_separator_raw_deploy(_sep_deploy, _sep_entries) or None

        custom_rules = self.effective_custom_routing_rules
        _log("Step 1: Moving Data/ → Data_Core/ ...")
        move_to_core(data_dir, log_fn=_log)
        _log("  Backed up existing files → Data_Core/.")

        custom_exclude: set[str] = set()
        if custom_rules:
            _log("Step 1b: Routing files via custom rules ...")
            custom_exclude = deploy_custom_rules(
                filemap, self._game_path, staging,
                rules=custom_rules,
                mode=mode,
                strip_prefixes=self.mod_folder_strip_prefixes,
                per_mod_strip_prefixes=per_mod_strip,
                per_mod_link_modes=per_mod_modes,
                raw_mods=per_mod_raw,
                log_fn=_log,
                progress_fn=progress_fn,
                prefix_root=self.get_prefix_path(),
            )

        _log(f"Step 2: Transferring mod files into Data/ ({mode.name}) ...")
        linked_mod, placed = deploy_filemap(filemap, data_dir, staging,
                                            mode=mode,
                                            strip_prefixes=self.mod_folder_strip_prefixes,
                                            per_mod_strip_prefixes=per_mod_strip,
                                            per_mod_deploy_dirs=per_mod_deploy,
                                            per_mod_link_modes=per_mod_modes,
                                            log_fn=_log,
                                            progress_fn=progress_fn,
                                            exclude=custom_exclude or None,
                                            core_dir=data_dir.parent / (data_dir.name + "_Core"))
        _log(f"  Transferred {linked_mod} mod file(s).")

        _log("Step 3: Filling gaps with vanilla files from Data_Core/ ...")
        linked_core = deploy_core(data_dir, placed, mode=mode, log_fn=_log,
                                  manifest_dir=filemap.parent)
        _log(f"  Transferred {linked_core} vanilla file(s).")

        _log("Step 4: Symlinking plugins.txt into Proton prefix ...")
        self._symlink_plugins_txt(profile, _log)

        _log("Step 5: Symlinking profile INI files ...")
        self._symlink_profile_ini_files(profile, _log)

        _log("Step 6: Symlinking profile saves ...")
        self._symlink_profile_saves(profile, _log)

        _log("Step 7: Applying archive invalidation ...")
        self.apply_archive_invalidation(_log)

        self.apply_ini_overrides(_log)

        if self._orders_plugins_by_mtime():
            _log("Step 8: Setting plugin mtimes to match load order ...")
            self.stamp_plugin_load_order(profile, _log)

        _log(
            f"Deploy complete. "
            f"{linked_mod} mod + {linked_core} vanilla "
            f"= {linked_mod + linked_core} total file(s) in Data/."
        )

        # Capture runtime files generated outside Data/ on the next restore.
        self.snapshot_root_for_runtime_capture(log_fn=_log)

    def post_clean_game_folder(self, log_fn=None) -> None:
        _log = log_fn or (lambda _: None)
        self._remove_script_extender_runtime_override(_log)
        self._restore_launcher(_log)

    def restore(self, log_fn=None, progress_fn=None) -> None:
        """Restore Data/ to its vanilla state by moving Data_Core/ back."""
        _log = log_fn or (lambda _: None)

        if self._game_path is None:
            raise RuntimeError("Game path is not configured.")

        from Utils.deployment.custom_rules import validate_custom_rules_restore
        validate_custom_rules_restore(
            self.get_effective_filemap_path(), self._game_path,
            self.get_prefix_path(), log_fn=_log)

        data_dir = self._game_path / "Data"

        default_game_path = self.get_global_game_path()
        if default_game_path is None:
            _log("  WARN: Default-profile game path is not configured; "
                 "Bethesda registry path was not restored.")
        else:
            from Utils.bethesda.registry import repair_game_registry_path
            if not repair_game_registry_path(self, default_game_path, _log):
                _log("  WARN: Bethesda registry game path could not be restored "
                     "to the default profile.")

        self._remove_script_extender_runtime_override(_log)

        _log("Restore: removing case-alias symlinks ...")
        remove_case_alias_links(self._game_path, self.case_alias_dirs,
                                log_fn=_log)
        remove_probe_stub_dirs(self._game_path, self.probe_stub_dirs,
                               log_fn=_log)

        _log("Restore: reverting archive invalidation ...")
        self.revert_archive_invalidation(_log)

        self.revert_ini_overrides(_log)

        _profile_dir = self._active_profile_dir
        _entries = read_modlist(_profile_dir / "modlist.txt") if _profile_dir else []
        cleanup_custom_deploy_dirs(
            _profile_dir, _entries, log_fn=_log,
            filemap_path=self.get_effective_filemap_path(),
            game=self,
        )

        if self._game_path:
            _log("Restore: removing custom-routed files ...")
            restore_custom_rules(
                self.get_effective_filemap_path(),
                self._game_path,
                rules=[],
                log_fn=_log,
                prefix_root=self.get_prefix_path(),
            )

        core_dir = data_dir.parent / (data_dir.name + "_Core")
        physical_deploy = core_dir.exists()
        from Utils.vfs import cleanup_deployment, has_deployment_state
        vfs_deploy = has_deployment_state(self)
        if vfs_deploy:
            cleanup_deployment(self, preserve_upper=True, log_fn=_log)
        if physical_deploy:
            # A physical deploy may still be active after VFS was toggled on.
            _log("Restore: clearing Data/ and moving Data_Core/ back ...")
            restored = restore_data_core(
                data_dir,
                overwrite_dir=self.get_effective_overwrite_path(),
                staging_root=self.get_effective_mod_staging_path(),
                strip_prefixes=self.mod_folder_strip_prefixes,
                log_fn=_log,
                restore_whitelist=self.restore_whitelist_matcher(
                    rel_prefix="data/"),
                game=self, profile_dir=self._active_profile_dir,
            )
            _log(f"  Restored {restored} file(s). Data_Core/ removed.")
        elif not vfs_deploy:
            cleanup_deployment(self, preserve_upper=True, log_fn=_log)

        self._remove_plugins_txt_symlink(_log)
        self._restore_launcher(_log)

        # Restore the launcher first so its backup is not captured as a runtime file.
        if physical_deploy:
            moved = self.capture_runtime_files_to_root_folder(log_fn=_log)
            if moved:
                _log(f"  Moved {moved} runtime file(s) to Root_Folder/.")

        _active = self._active_profile_dir
        if _active is not None:
            _log("Restore: removing profile INI symlinks ...")
            self._remove_profile_ini_symlinks(_active.name, _log)
            _log("Restore: removing profile saves symlinks ...")
            self._remove_profile_saves_symlinks(_active.name, _log)

        _log("Restore complete.")
