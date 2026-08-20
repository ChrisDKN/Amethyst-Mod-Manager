"""
BepInEx.py
Game handler for BepInEx-based games.

Mod structure:
  Mods install into <game_path>/BepInEx/Plugins/
  Staged mods live in Profiles/Subnautica/mods/

  Root_Folder/ files deploy straight to the game install root (handled by GUI).
"""

from pathlib import Path
import stat

from Games.base_game import BaseGame, WizardTool
from Utils.deploy import LinkMode, deploy_core, deploy_custom_rules, deploy_filemap, load_per_mod_strip_prefixes, load_separator_deploy_paths, expand_separator_deploy_paths, expand_separator_link_modes, expand_separator_raw_deploy, cleanup_custom_deploy_dirs, move_to_core, restore_custom_rules, restore_data_core
from Utils.modlist import read_modlist
from Utils.config_paths import get_profiles_dir

_PROFILES_DIR = get_profiles_dir()


def _thunderstore_plugin_subdirs(staging_root: Path, entries,
                                 log_fn=None) -> dict[str, str]:
    """Return enabled mod -> Thunderstore package-ID deployment folder.

    Risk of Thunder's preloader deliberately removes the legacy direct child
    ``BepInEx/plugins/RoR2BepInExPack``.  r2modman avoids that path (and keeps
    packages isolated generally) by installing plugin payloads below the full,
    versionless package ID.  Amethyst keeps its staging names user-facing, so
    apply the same namespace only when deploying ordinary plugin files.
    """
    from Thunderstore.thunderstore_meta import read_meta

    _log = log_fn or (lambda _message: None)
    subdirs: dict[str, str] = {}
    for entry in entries:
        if entry.is_separator or not entry.enabled:
            continue
        package_id = read_meta(staging_root / entry.name / "meta.ini").package_id
        package_id = package_id.strip()
        if not package_id:
            continue
        if (package_id in (".", "..") or "/" in package_id
                or "\\" in package_id or "\x00" in package_id):
            _log(f"  WARN: ignoring unsafe Thunderstore package ID "
                 f"{package_id!r} for {entry.name!r}")
            continue
        subdirs[entry.name] = package_id
    return subdirs


class Subnautica(BaseGame):

    def __init__(self):
        self._game_path: Path | None = None
        self._prefix_path: Path | None = None
        self._deploy_mode: LinkMode = LinkMode.HARDLINK
        self._staging_path: Path | None = None
        self._saved_heroic_app_name: str | None = None
        self.load_paths()

    # -----------------------------------------------------------------------
    # Identity
    # -----------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "Subnautica"

    @property
    def game_id(self) -> str:
        return "Subnautica"

    @property
    def exe_name(self) -> str:
        return "Subnautica.exe"

    @property
    def steam_id(self) -> str:
        return "264710"

    @property
    def default_deploy_mode(self) -> str:
        return "symlink"

    def set_heroic_app_name(self, app_name: str | None) -> None:
        self._saved_heroic_app_name = app_name or None
        self.save_paths()
    
    @property
    def conflict_ignore_filenames(self) -> set[str]:
        return {
            "*.md",
            "icon.png",
            "manifest.json",
            "LocalizationExample.zip",
            "*read*.txt",
            "changelog*.txt",
            "steam_appid.txt",
            }

    @property
    def nexus_game_domain(self) -> str:
        return "subnautica"

    @property
    def thunderstore_community(self) -> str:
        return "subnautica"
    
    @property
    def extra_mod_folder_strip_prefixes(self) -> set[str]:
        """Per-game wrapper folders to strip on top of the shared BepInEx set."""
        return set()

    @property
    def mod_folder_strip_prefixes(self) -> set[str]:
        return {"plugins", "bepinex", "BepInExPack"} | self.extra_mod_folder_strip_prefixes

    @property
    def mods_dir(self) -> str:
        return "BepInEx/plugins"

    def runtime_snapshot_exclude_dirs(self) -> set[str] | None:
        # plugins/ is reverted via its _Core backup; capture everything else
        # (BepInEx/config, caches, root loader files) into Root_Folder/.
        return {self.mods_dir}
    
    @property
    def plugin_extensions(self) -> list[str]:
        return []

    @property
    def wine_dll_overrides(self) -> dict[str, str]:
        return {
            "winhttp": "native,builtin",
            "version": "native,builtin"
            }

    @property
    def frameworks(self) -> "dict[str, tuple[str, ...]]":
        # Windows builds proxy-load via winhttp.dll; native Linux builds ship
        # run_bepinex.sh instead - either one means BepInEx is present.
        return {"BepInEx": ("winhttp.dll", "run_bepinex.sh")}

    @property
    def custom_routing_rules(self) -> list:
        from Utils.deploy import CustomRule
        return [
            CustomRule(dest="", filenames=[
                "winhttp.dll",
                "version.dll",
                "run_bepinex.sh",
                "libdoorstop.so",
                "libdoorstop.dylib",
                "doorstop_config.ini",
                "*.doorstop_version",
                "qmodmanager-config.json",
                "qmodmanager_log*.txt",
                "start_game_bepinex.sh",
                "start_server_bepinex.sh",
            ], flatten=True, loose_only=True),
            CustomRule(dest="BepInEx", folders=[
                "config",
                "core",
                "patchers",
                "plugins",
                "monomod",
            ], flatten=True, loose_only=True),
            CustomRule(dest="BepInEx/monomod", extensions=[
                ".mm.dll"
            ], loose_only=True),
            CustomRule(dest="BepInEx/plugins", folders=[
                "Tobey"
            ], flatten=True, loose_only=True),
            CustomRule(dest="", folders=[
                "qmods",
                "doorstop_libs"
            ], flatten=True, loose_only=True),
        ]
    
    @property
    def reshade_dll(self) -> str:
        return "dxgi.dll"

    # -----------------------------------------------------------------------
    # Paths
    # -----------------------------------------------------------------------

    def get_game_path(self) -> Path | None:
        return self._game_path

    def get_mod_data_path(self) -> Path | None:
        """Mods go into BepInEx/Plugins/ inside the game directory."""
        if self._game_path is None:
            return None
        return self._game_path / self.mods_dir

    def get_mod_staging_path(self) -> Path:
        if self._staging_path is not None:
            return self._staging_path / "mods"
        return _PROFILES_DIR / self.name / "mods"

    def _load_paths_extra(self, data: dict) -> None:
        self._saved_heroic_app_name = data.get("heroic_app_name") or None

    def _save_paths_extra(self) -> dict:
        return {"heroic_app_name": self._saved_heroic_app_name or ""}

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
        """Deploy staged mods into BepInEx/Plugins/.

        Workflow:
          1. Move BepInEx/Plugins/ → BepInEx/Plugins_Core/  (vanilla backup)
          2. Transfer mod files listed in filemap.txt into BepInEx/Plugins/
          3. Fill gaps with vanilla files from Plugins_Core/
        (Root Folder deployment is handled by the GUI after this returns.)
        """
        _log = log_fn or (lambda _: None)

        if self._game_path is None:
            raise RuntimeError("Game path is not configured.")

        plugins_dir = self._game_path / self.mods_dir
        filemap     = self.get_effective_filemap_path()
        staging     = self.get_effective_mod_staging_path()
        core        = self.mods_dir + "_Core"

        plugins_dir.mkdir(parents=True, exist_ok=True)
        if not filemap.is_file():
            raise RuntimeError(
                f"filemap.txt not found: {filemap}\n"
                "Run 'Build Filemap' before deploying."
            )

        _log(f"Step 1: Moving {plugins_dir.name}/ → {core}/ ...")
        move_to_core(plugins_dir, log_fn=_log)
        _log(f"  Backed up existing files → {core}/.")

        profile_dir = self.get_profile_root() / "profiles" / profile
        per_mod_strip = load_per_mod_strip_prefixes(profile_dir)
        entries = read_modlist(profile_dir / "modlist.txt")
        package_subdirs = _thunderstore_plugin_subdirs(
            staging, entries, log_fn=_log)

        # Separator overrides - loaded from the real profile_dir and passed
        # explicitly so shared-staging layouts get the right link modes.
        _sep_deploy = load_separator_deploy_paths(profile_dir)
        _sep_entries = entries if _sep_deploy else []
        per_mod_deploy = expand_separator_deploy_paths(_sep_deploy, _sep_entries) or None
        per_mod_modes = expand_separator_link_modes(_sep_deploy, _sep_entries) or None
        per_mod_raw = expand_separator_raw_deploy(_sep_deploy, _sep_entries) or None

        custom_rules = self.custom_routing_rules
        custom_exclude: set[str] = set()
        if custom_rules:
            _log("Step 2a: Routing BepInEx root files via custom rules ...")
            custom_exclude = deploy_custom_rules(
                filemap, self._game_path, staging,
                rules=custom_rules,
                mode=mode,
                strip_prefixes=self.mod_folder_strip_prefixes,
                per_mod_strip_prefixes=per_mod_strip,
                per_mod_link_modes=per_mod_modes,
                log_fn=_log,
                progress_fn=progress_fn,
                raw_mods=per_mod_raw,
            )

        _log(f"Step 2: Transferring mod files into {plugins_dir} ({mode.name}) ...")
        linked_mod, placed = deploy_filemap(filemap, plugins_dir, staging,
                                            mode=mode,
                                            strip_prefixes=self.mod_folder_strip_prefixes,
                                            per_mod_strip_prefixes=per_mod_strip,
                                            per_mod_deploy_dirs=per_mod_deploy,
                                            per_mod_link_modes=per_mod_modes,
                                            log_fn=_log,
                                            progress_fn=progress_fn,
                                            exclude=custom_exclude or None,
                                            core_dir=plugins_dir.parent / (plugins_dir.name + "_Core"),
                                            per_mod_subdirs=package_subdirs)
        _log(f"  Transferred {linked_mod} mod file(s).")

        _log(f"Step 3: Filling gaps with vanilla files from {core}/ ...")
        linked_core = deploy_core(plugins_dir, placed, mode=mode, log_fn=_log)
        _log(f"  Transferred {linked_core} vanilla file(s).")

        _log(
            f"Deploy complete. "
            f"{linked_mod} mod + {linked_core} vanilla "
            f"= {linked_mod + linked_core} total file(s) in {plugins_dir.name}/."
        )

        # Capture runtime files generated outside plugins/ on the next restore.
        self.snapshot_root_for_runtime_capture(log_fn=_log)

    def restore(self, log_fn=None, progress_fn=None) -> None:
        """Restore BepInEx/Plugins/ to its vanilla state."""
        _log = log_fn or (lambda _: None)

        if self._game_path is None:
            raise RuntimeError("Game path is not configured.")

        plugins_dir = self._game_path / self.mods_dir
        core = self.mods_dir + "_Core"
        core_dir = self._game_path / core
        
        _profile_dir = self._active_profile_dir
        _entries = read_modlist(_profile_dir / "modlist.txt") if _profile_dir else []
        cleanup_custom_deploy_dirs(_profile_dir, _entries, log_fn=_log)

        custom_rules = self.custom_routing_rules
        if custom_rules and self._game_path:
            _log("Restore: removing custom-routed BepInEx root files ...")
            restore_custom_rules(
                self.get_effective_filemap_path(),
                self._game_path,
                rules=custom_rules,
                log_fn=_log,
            )

        if core_dir.is_dir():
            _log(f"Restore: clearing {plugins_dir.name}/ and moving {core}/ back ...")
            restored = restore_data_core(plugins_dir, core_dir=core_dir, overwrite_dir=self.get_effective_overwrite_path(), log_fn=_log)
            _log(f"  Restored {restored} file(s). {core}/ removed.")

        # Sweep runtime files generated outside plugins/ (BepInEx/config, caches,
        # root loader files) into Root_Folder/ so they re-deploy next time.  Runs
        # after the plugins dir and custom-routed files have been restored/removed.
        moved = self.capture_runtime_files_to_root_folder(log_fn=_log)
        if moved:
            _log(f"  Moved {moved} runtime file(s) to Root_Folder/.")

        # BepInEx/plugins (and BepInEx itself) are mod-introduced folders, not
        # vanilla like Skyrim's Data.  A truly vanilla state has no BepInEx dir,
        # so prune any now-empty folder from plugins_dir up to (but not
        # including) the game root.  os.rmdir only succeeds when empty, so a dir
        # that still holds vanilla/runtime files is left untouched.
        _dir = plugins_dir
        while _dir != self._game_path and _dir.is_dir():
            try:
                _dir.rmdir()
            except OSError:
                break  # not empty (or gone) - stop climbing
            _log(f"  Removed empty folder {_dir.relative_to(self._game_path)}/.")
            _dir = _dir.parent

        _log("Restore complete.")
        
class Subnautica_Below_Zero(Subnautica):
    
    @property
    def name(self) -> str:
        return "Subnautica: Below Zero"

    @property
    def game_id(self) -> str:
        return "Subnautica_Below_Zero"

    @property
    def exe_name(self) -> str:
        return "SubnauticaZero.exe"

    @property
    def steam_id(self) -> str:
        return "848450"

    @property
    def nexus_game_domain(self) -> str:
        return "subnauticabelowzero"

    @property
    def thunderstore_community(self) -> str:
        return "subnautica-below-zero"

class TCG_Card_Shop_Simulator(Subnautica):

    @property
    def name(self) -> str:
        return "TCG Card Shop Simulator"

    @property
    def game_id(self) -> str:
        return "TCG_Card_Shop_Simulator"

    @property
    def exe_name(self) -> str:
        return "Card Shop Simulator.exe"

    @property
    def steam_id(self) -> str:
        return "3070070"

    @property
    def nexus_game_domain(self) -> str:
        return "tcgcardshopsimulator"

    @property
    def thunderstore_community(self) -> str:
        return "tcg-card-shop-simulator"

    @property
    def default_deploy_mode(self) -> str:
        return "symlink"

class Lethal_Company(Subnautica):

    @property
    def name(self) -> str:
        return "Lethal Company"

    @property
    def game_id(self) -> str:
        return "Lethal_Company"

    @property
    def exe_name(self) -> str:
        return "Lethal Company.exe"

    @property
    def steam_id(self) -> str:
        return "1966720"

    @property
    def nexus_game_domain(self) -> str:
        return "lethalcompany"

    @property
    def thunderstore_community(self) -> str:
        return "lethal-company"

    @property
    def default_deploy_mode(self) -> str:
        return "symlink"

class Valheim(Subnautica):
    @property
    def name(self) -> str:
        return "Valheim"

    @property
    def game_id(self) -> str:
        return "Valheim"

    @property
    def exe_name(self) -> str:
        return "valheim.exe"

    @property
    def exe_name_alts(self) -> list[str]:
        return ["valheim.x86_64"]
    
    @property
    def steam_id(self) -> str:
        return "892970"

    @property
    def nexus_game_domain(self) -> str:
        return "valheim"

    @property
    def thunderstore_community(self) -> str:
        return "valheim"

    @property
    def extra_mod_folder_strip_prefixes(self) -> set[str]:
        return {"BepInExPack_Valheim"}

    @property
    def default_deploy_mode(self) -> str:
        return "symlink"

    def deploy(self, log_fn=None, mode: LinkMode = LinkMode.HARDLINK,
                profile: str = "default", progress_fn=None) -> None:
        """Deploy staged mods into BepInEx/Plugins/ for Valheim, with extra steps."""
        super().deploy(log_fn=log_fn, mode=mode, profile=profile, progress_fn=progress_fn)

        """Run after all deployment steps, including Root_Folder moves."""
        _log = log_fn or (lambda _: None)
        game_path = self.get_game_path()
        root_folder = self.get_effective_root_folder_path()
        candidates = []
        if game_path is not None:
            candidates.append(game_path / "start_game_bepinex.sh")
        candidates.append(root_folder / "start_game_bepinex.sh")
        found = False
        for launcher in candidates:
            if launcher.exists():
                current_mode = launcher.stat().st_mode
                launcher.chmod(current_mode | stat.S_IXUSR)
                _log(f"Set executable bit (u+x) on {launcher}.")
                found = True
                break
        if not found:
            _log("Warning: start_game_bepinex.sh not found in game folder or Root_Folder; skipping chmod.")

        # Log the Steam launch argument
        _log(
            "To launch Valheim with BepInEx on Linux, set the following as your Steam launch option:\n"
            "    ./start_game_bepinex.sh %command%\n"
            "You must add this manually in Steam (right-click Valheim > Properties > Launch Options)."
        )
        
class HNSS(Subnautica):
    @property
    def name(self) -> str:
        return "Hollow Knight: Silksong"

    @property
    def game_id(self) -> str:
        return "Hollow_Knight_Silksong"

    @property
    def exe_name(self) -> str:
        return "Hollow Knight Silksong.exe"

    @property
    def steam_id(self) -> str:
        return "1030300"

    @property
    def nexus_game_domain(self) -> str:
        return "hollowknightsilksong"

    @property
    def thunderstore_community(self) -> str:
        return "hollow-knight-silksong"
    
    @property
    def exe_name_alts(self) -> list[str]:
        return ["Hollow Knight Silksong"]

    @property
    def default_deploy_mode(self) -> str:
        return "symlink"

class Peak(Subnautica):
    @property
    def name(self) -> str:
        return "Peak"

    @property
    def game_id(self) -> str:
        return "peak"

    @property
    def exe_name(self) -> str:
        return "PEAK.exe"

    @property
    def steam_id(self) -> str:
        return "3527290"

    @property
    def nexus_game_domain(self) -> str:
        return "peak"

    @property
    def thunderstore_community(self) -> str:
        return "peak"

    @property
    def extra_mod_folder_strip_prefixes(self) -> set[str]:
        return {"BepInExPack_Peak"}

class ROR2(Subnautica):
    @property
    def name(self) -> str:
        return "Risk of Rain 2"

    @property
    def game_id(self) -> str:
        return "riskofrain2"

    @property
    def exe_name(self) -> str:
        return "Risk of Rain 2.exe"

    @property
    def steam_id(self) -> str:
        return "632360"

    @property
    def nexus_game_domain(self) -> str:
        # Thunderstore-only game. MUST be overridden to "" - without it the
        # class inherits Subnautica's domain and the Nexus browser would list
        # Subnautica mods for Risk of Rain 2.
        return ""

    @property
    def thunderstore_community(self) -> str:
        return "riskofrain2"

class Inscryption(Subnautica):
    @property
    def name(self) -> str:
        return "Inscryption"

    @property
    def game_id(self) -> str:
        return "inscryption"

    @property
    def exe_name(self) -> str:
        return "Inscryption.exe"

    @property
    def exe_name_alts(self) -> list[str]:
        return ["Inscryption.x86_64"]

    @property
    def steam_id(self) -> str:
        return "1092790"

    @property
    def nexus_game_domain(self) -> str:
        return ""

    @property
    def thunderstore_community(self) -> str:
        return "inscryption"

    @property
    def extra_mod_folder_strip_prefixes(self) -> set[str]:
        return {"BepInExPack_Inscryption"}

class repo(Subnautica):
    @property
    def name(self) -> str:
        return "R.E.P.O"

    @property
    def game_id(self) -> str:
        return "repo"

    @property
    def exe_name(self) -> str:
        return "REPO.exe"

    @property
    def steam_id(self) -> str:
        return "3241660"

    @property
    def nexus_game_domain(self) -> str:
        return "repo"

    @property
    def thunderstore_community(self) -> str:
        return "repo"

class DysonSphereProgram(Subnautica):
    @property
    def name(self) -> str:
        return "Dyson Sphere Program"

    @property
    def game_id(self) -> str:
        return "dysonsphereprogram"

    @property
    def exe_name(self) -> str:
        return "DSPGAME.exe"

    @property
    def steam_id(self) -> str:
        return "1366540"

    @property
    def nexus_game_domain(self) -> str:
        return "dysonsphereprogram"

    @property
    def thunderstore_community(self) -> str:
        return "dyson-sphere-program"

class SupermarketSimulator(Subnautica):
    @property
    def name(self) -> str:
        return "Supermarket Simulator"

    @property
    def game_id(self) -> str:
        return "Supermarket_Simulator"

    @property
    def exe_name(self) -> str:
        return "Supermarket Simulator.exe"

    @property
    def steam_id(self) -> str:
        return "2670630"

    @property
    def nexus_game_domain(self) -> str:
        return "supermarketsimulator"

    @property
    def thunderstore_community(self) -> str:
        return ""
