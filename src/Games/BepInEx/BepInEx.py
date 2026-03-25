"""
BepInEx.py
Game handler for BepInEx-based games.

Mod structure:
  Mods install into <game_path>/BepInEx/Plugins/
  Staged mods live in Profiles/Subnautica/mods/

  Root_Folder/ files deploy straight to the game install root (handled by GUI).
"""

import json
from pathlib import Path
import stat

from Games.base_game import BaseGame, WizardTool
from Utils.deploy import LinkMode, apply_wine_dll_overrides, deploy_core, deploy_filemap, load_per_mod_strip_prefixes, load_separator_deploy_paths, expand_separator_deploy_paths, cleanup_custom_deploy_dirs, move_to_core, restore_data_core
from Utils.modlist import read_modlist
from Utils.config_paths import get_profiles_dir
from Utils.steam_finder import find_prefix

_PROFILES_DIR = get_profiles_dir()


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

    def set_heroic_app_name(self, app_name: str | None) -> None:
        self._saved_heroic_app_name = app_name or None
        self.save_paths()

    @property
    def nexus_game_domain(self) -> str:
        return "subnautica"

    @property
    def mod_folder_strip_prefixes(self) -> set[str]:
        return {"plugins", "bepinex"}
    
    @property
    def mods_dir(self) -> str:
        return "BepInEx/plugins"
    
    @property
    def plugin_extensions(self) -> list[str]:
        return []

    @property
    def wine_dll_overrides(self) -> dict[str, str]:
        return {"winhttp": "native,builtin"}

    @property
    def frameworks(self) -> dict[str, str]:
        return {"BepInEx": "winhttp.dll"}

    @property
    def loot_sort_enabled(self) -> bool:
        return False

    @property
    def loot_game_type(self) -> str:
        return ""

    @property
    def loot_masterlist_url(self) -> str:
        return ""
    
    @property
    def reshade_dll(self) -> str:
        return "dxgi.dll"

    @property
    def wizard_tools(self) -> list[WizardTool]:
        return self._base_wizard_tools() + [
            WizardTool(
                id="install_bepinex_subnautica",
                label="Install BepInEx",
                description="Download and install BepInEx into the game folder.",
                dialog_class_path="wizards.bepinex.BepInExWizard",
                extra={
                    "download_url": "https://www.nexusmods.com/subnautica/mods/1108?tab=files",
                    "archive_keywords": ["bepinex", "subnautica"],
                    "inner_folder": "",
                    "chmod_files": [],
                },
            ),
        ]

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
                "copy":    LinkMode.COPY,
            }.get(raw_mode, LinkMode.HARDLINK)
            raw_staging = data.get("staging_path", "")
            if raw_staging:
                self._staging_path = Path(raw_staging)
            self._saved_heroic_app_name = data.get("heroic_app_name") or None
            self._validate_staging()
            if not self._prefix_path or not self._prefix_path.is_dir():
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
            "game_path":        str(self._game_path)    if self._game_path    else "",
            "prefix_path":      str(self._prefix_path)  if self._prefix_path  else "",
            "deploy_mode":      mode_str,
            "staging_path":     str(self._staging_path) if self._staging_path else "",
            "heroic_app_name":  self._saved_heroic_app_name or "",
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
        moved = move_to_core(plugins_dir, log_fn=_log)
        _log(f"  Moved {moved} file(s) to {core}/.")

        _log(f"Step 2: Transferring mod files into {plugins_dir} ({mode.name}) ...")
        profile_dir = self.get_profile_root() / "profiles" / profile
        per_mod_strip = load_per_mod_strip_prefixes(profile_dir)
        _sep_deploy = load_separator_deploy_paths(profile_dir)
        _sep_entries = read_modlist(profile_dir / "modlist.txt") if _sep_deploy else []
        per_mod_deploy = expand_separator_deploy_paths(_sep_deploy, _sep_entries) or None
        linked_mod, placed = deploy_filemap(filemap, plugins_dir, staging,
                                            mode=mode,
                                            strip_prefixes=self.mod_folder_strip_prefixes,
                                            per_mod_strip_prefixes=per_mod_strip,
                                            per_mod_deploy_dirs=per_mod_deploy,
                                            log_fn=_log,
                                            progress_fn=progress_fn,
                                            core_dir=plugins_dir.parent / (plugins_dir.name + "_Core"))
        _log(f"  Transferred {linked_mod} mod file(s).")

        _log(f"Step 3: Filling gaps with vanilla files from {core}/ ...")
        linked_core = deploy_core(plugins_dir, placed, mode=mode, log_fn=_log)
        _log(f"  Transferred {linked_core} vanilla file(s).")

        _log(
            f"Deploy complete. "
            f"{linked_mod} mod + {linked_core} vanilla "
            f"= {linked_mod + linked_core} total file(s) in {plugins_dir.name}/."
        )

        if self._prefix_path and self.wine_dll_overrides:
            _log("Applying Wine DLL overrides to Proton prefix ...")
            apply_wine_dll_overrides(self._prefix_path, self.wine_dll_overrides, log_fn=_log)

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

        if core_dir.is_dir():
            _log(f"Restore: clearing {plugins_dir.name}/ and moving {core}/ back ...")
            restored = restore_data_core(plugins_dir, core_dir=core_dir, overwrite_dir=self.get_effective_overwrite_path(), log_fn=_log)
            _log(f"  Restored {restored} file(s). {core}/ removed.")

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
    def wizard_tools(self) -> list[WizardTool]:
        return self._base_wizard_tools() + [
            WizardTool(
                id="install_bepinex_subnautica_below_zero",
                label="Install BepInEx",
                description="Download and install BepInEx into the game folder.",
                dialog_class_path="wizards.bepinex.BepInExWizard",
                extra={
                    "download_url": "https://www.nexusmods.com/subnauticabelowzero/mods/344?tab=files",
                    "archive_keywords": ["bepinex", "subnautica", "below"],
                    "inner_folder": "",
                    "chmod_files": [],
                },
            ),
        ]


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
    def wizard_tools(self) -> list[WizardTool]:
        return self._base_wizard_tools() + [
            WizardTool(
                id="install_bepinex_tcg",
                label="Install BepInEx",
                description="Download and install BepInEx into the game folder.",
                dialog_class_path="wizards.bepinex.BepInExWizard",
                extra={
                    "download_url": "https://www.nexusmods.com/tcgcardshopsimulator/mods/2?tab=files",
                    "archive_keywords": ["bepinex", "configuration"],
                    "inner_folder": "",
                    "chmod_files": [],
                },
            ),
        ]

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
    def wizard_tools(self) -> list[WizardTool]:
        return self._base_wizard_tools() + [
            WizardTool(
                id="install_bepinex_lethal_company",
                label="Install BepInEx",
                description="Download and install BepInEx into the game folder.",
                dialog_class_path="wizards.bepinex.BepInExWizard",
                extra={
                    "download_url": "https://www.nexusmods.com/lethalcompany/mods/42?tab=files",
                    "archive_keywords": ["bepinex"],
                    "inner_folder": "",
                    "chmod_files": [],
                },
            ),
        ]

class Valheim(Subnautica):
    @property
    def name(self) -> str:
        return "Valheim"

    @property
    def game_id(self) -> str:
        return "Valheim"

    @property
    def exe_name(self) -> str:
        return "valheim.x86_64"

    @property
    def steam_id(self) -> str:
        return "892970"

    @property
    def nexus_game_domain(self) -> str:
        return "valheim"

    @property
    def wizard_tools(self) -> list[WizardTool]:
        return self._base_wizard_tools() + [
            WizardTool(
                id="install_bepinex_valheim",
                label="Install BepInEx",
                description="Download and install BepInEx into the game folder.",
                dialog_class_path="wizards.bepinex.BepInExWizard",
                extra={
                    "download_url": "https://thunderstore.io/package/download/denikson/BepInExPack_Valheim/5.4.2333/",
                    "archive_keywords": ["denikson-bepinexpack_valheim"],
                    "inner_folder": "BepInExPack_Valheim",
                    "chmod_files": [
                        "start_server_bepinex.sh",
                        "start_game_bepinex.sh",
                    ],
                },
            ),
        ]

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
        