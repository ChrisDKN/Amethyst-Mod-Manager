"""
oblivion_remastered.py
Game handler for Oblivion Remastered (Unreal Engine 5).

Mod structure
-------------
Mods ship files destined for multiple locations in the game root:

  .esp / .esm         → OblivionRemastered/Content/Dev/ObvData/Data/
  .pak / .utoc / .ucas → OblivionRemastered/Content/Paks/
  ue4ss/ folder        → OblivionRemastered/Binaries/Win64/
  .lua files           → OblivionRemastered/Binaries/Win64/ue4ss/Mods/
  OBSE/ folder         → OblivionRemastered/Binaries/Win64/
  .bk2 cutscenes       → OblivionRemastered/Content/Movies/Modern/
  Loose files (no rule) → game root (OblivionRemastered/)

The game root itself lives inside the Steam install directory:
  <steam_install>/OblivionRemastered/

Plugins.txt is managed by the plugin panel (extensions: .esp, .esm).
"""

from __future__ import annotations

from pathlib import Path

from Games.ue5_game import UE5Game, UE5Rule
from Utils.deploy import CustomRule, LinkMode
from Utils.config_paths import get_profiles_dir

# Plugins.txt lives here inside the game root (OblivionRemastered/)
_PLUGINS_TXT_GAME_REL = Path("Content/Dev/ObvData/Data/Plugins.txt")

_PROFILES_DIR = get_profiles_dir()

# Game root subfolder inside the Steam install directory
_GAME_SUBDIR = "OblivionRemastered"


class OblivionRemastered(UE5Game):

    vanilla_plugins = [
        "Oblivion.esm",
        "DLCBattlehornCastle.esp", "DLCFrostcrag.esp", "DLCHorseArmor.esp",
        "DLCMehrunesRazor.esp", "DLCOrrery.esp", "DLCShiveringIsles.esp",
        "DLCSpellTomes.esp", "DLCThievesDen.esp", "DLCVileLair.esp",
        "Knights.esp",
        "AltarESPMain.esp", "AltarDeluxe.esp", "AltarESPLocal.esp",
    ]

    # Data-folder mods (often ported from original Oblivion) can still be
    # authored as BAIN packages, so keep the picker despite the UE5 base.
    @property
    def supports_bain(self) -> bool:
        return True

    # -----------------------------------------------------------------------
    # Identity
    # -----------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "Oblivion Remastered"

    @property
    def game_id(self) -> str:
        return "oblivion_remastered"

    @property
    def exe_name(self) -> str:
        return "OblivionRemastered/Binaries/Win64/OblivionRemastered-Win64-Shipping.exe"

    @property
    def steam_id(self) -> str:
        return "2623190"

    @property
    def nexus_game_domain(self) -> str:
        return "oblivionremastered"

    @property
    def plugin_extensions(self) -> list[str]:
        return [".esp", ".esm"]

    @property
    def plugins_use_star_prefix(self) -> bool:
        return False

    @property
    def plugins_include_vanilla(self) -> bool:
        return True
    
    @property
    def mod_required_top_level_folders(self) -> set[str]:
        return {"OblivionRemastered", "Content", "Paks", "~mods", "Binaries", "Win64","ue4ss","Mods"}

    @property
    def mod_auto_strip_until_required(self) -> bool:
        return True

    @property
    def mod_install_as_is_if_no_match(self) -> bool:
        return True

    @property
    def mod_folder_strip_prefixes_post(self) -> set[str]:
        return {"OblivionRemastered", "Content", "Paks", "~mods", "Binaries", "Win64","ue4ss","Mods"}
    
    @property
    def conflict_ignore_filenames(self) -> set[str]:
        return {"*.html","*.md","*.jpeg","*.png","*.jpg","*.rar","*read*.txt","*.docx","*install*.txt","*guide*.txt"}
    
    @property
    def loot_sort_enabled(self) -> bool:
        return True
    
    @property
    def loot_game_type(self) -> str:
        return "OblivionRemastered"
    
    @property
    def frameworks(self) -> dict[str, str]:
        return {
            "Script Extender": "Binaries/Win64/obse64_loader.exe",
            "UE4SS": "Binaries/Win64/dwmapi.dll"
            }

    @property
    def wine_dll_overrides(self) -> dict[str, str]:
        return {"dwmapi": "native,builtin", "winmm": "native,builtin"}

    @property
    def preferred_launch_exe(self) -> str:
        # obse64_loader.exe must be launched to play with mods active, but
        # replacing OblivionRemastered.exe with it causes errors.  When OBSE
        # is installed we show it first in the dropdown as the launch exe.
        return "Binaries/Win64/obse64_loader.exe"
    
    @property
    def loot_masterlist_repo(self) -> str:
        return "oblivion-remastered"

    # -----------------------------------------------------------------------
    # Routing rules
    # -----------------------------------------------------------------------
    # Game-root file routing is declared via ``custom_routing_rules``
    # (CustomRule), the same mechanism Marvel Rivals uses.  Rules are evaluated
    # in order; the first match wins.

    @property
    def custom_routing_rules(self) -> list[CustomRule]:
        return [
            CustomRule(dest="Unused",
                        folders=["wingdk", "src", "True Oblivion.ini Merger"]),

            CustomRule(dest="Unused", 
                        filenames=["Altar.ini"],
                        flatten=True),
            
            # Required as our strip prefix rules do not apply to fomods
            CustomRule(dest="", 
                        folders=["Content, Binaries"], 
                        flatten=True),

            CustomRule(dest="Binaries/Win64", 
                        filenames=["*obse64_*.*"],
                        flatten=True),

            CustomRule(dest="Content/Paks", 
                        folders=["LogicMods"], 
                        flatten=True),

            CustomRule(dest="Binaries/Win64",
                        folders=["ue4ss", "obse", "GameSettings",
                                "MadConfigs", "SkipMessages"],
                        flatten=True),

            CustomRule(dest="Content/Paks/~mods", 
                        extensions=[".pak"],
                        companion_extensions=[".ucas", ".utoc"],
                        include_siblings=True),

            CustomRule(dest="Binaries/Win64/ue4ss/Mods",
                        folders=["scripts", "dlls"], 
                        include_siblings=True),
        
            CustomRule(dest="Binaries/Win64/ue4ss/Mods",
                        filenames=["enabled.txt"], 
                        include_siblings=True),

            CustomRule(dest="Binaries/Win64/ue4ss/Mods",
                        filenames=["mods.txt"], 
                        flatten=True),

            CustomRule(dest="Content/Dev/ObvData/Data",
                        extensions=[".esm", ".esp"], 
                        flatten=True),

            CustomRule(dest="Content/Dev/ObvData/Data",
                        folders=["MagicLoader", "bashtags","SyncMap","sound"], 
                        flatten=True),

            CustomRule(dest="Content/Movies/Modern", 
                        extensions=[".bk2"],
                        flatten=True),

            CustomRule(dest="Binaries/Win64/ue4ss/Mods",
                        folders=["shared", "NPCAppearanceManager"], 
                        flatten=True),

            CustomRule(dest="Unused",
                        extensions=[".txt"], 
                        loose_only=True),
        ]

    @property
    def _ue5_post_passthrough_rules(self) -> list[UE5Rule]:
        # Trailing UE5 fallbacks (same as Ue5CustomGame's defaults) — applied
        # before the custom_routing_rules - Used to apply the ue4ss base structure
        return [
            UE5Rule(
                dest="Binaries/Win64",
                filenames=["dwmapi.dll"],
            ),
            UE5Rule(
                dest="Binaries/Win64/ue4ss",
                filenames=["ue4ss.dll", "ue4ss.pdb", "ue4ss-settings.ini"],
            ),
        ]

    # -----------------------------------------------------------------------
    # Paths
    # -----------------------------------------------------------------------

    def get_vanilla_plugins_path(self) -> Path | None:
        game_path = self.get_game_path()
        if game_path is None:
            return None
        return game_path / _PLUGINS_TXT_GAME_REL.parent

    def get_game_path(self) -> Path | None:
        """The actual game root is the OblivionRemastered/ subfolder inside
        the Steam install directory.  Looked up case-insensitively so it works
        on both Windows (via Proton) and Linux test setups."""
        if self._game_path is None:
            return None
        # Exact match first
        sub = self._game_path / _GAME_SUBDIR
        if sub.is_dir():
            return sub
        # Case-insensitive scan (handles lowercase 'oblivionremastered' on Linux)
        needle = _GAME_SUBDIR.lower()
        try:
            for child in self._game_path.iterdir():
                if child.is_dir() and child.name.lower() == needle:
                    return child
        except OSError:
            pass
        # Fallback: user pointed directly at the subfolder
        return self._game_path

    def get_mod_staging_path(self) -> Path:
        if self._staging_path is not None:
            return self._staging_path / "mods"
        return _PROFILES_DIR / self.name / "mods"

    # -----------------------------------------------------------------------
    # Deploy / restore (adds Plugins.txt symlink)
    # -----------------------------------------------------------------------

    def _plugins_txt_target(self) -> Path | None:
        game_path = self.get_game_path()
        if game_path is None:
            return None
        return game_path / _PLUGINS_TXT_GAME_REL

    def _symlink_plugins_txt(self, profile: str, log_fn) -> None:
        _log = log_fn
        target = self._plugins_txt_target()
        if target is None:
            return
        source = self.get_profile_root() / "profiles" / profile / "plugins.txt"
        if not source.is_file():
            _log(f"  WARN: plugins.txt not found at {source} — skipping deploy.")
            return
        from Utils.plugins import deploy_plugins_copy
        content = source.read_text(encoding="utf-8")
        deploy_plugins_copy(target.parent, target.name, content, _log)

    def _remove_plugins_txt_symlink(self, log_fn) -> None:
        from Utils.plugins import remove_plugins_copy
        target = self._plugins_txt_target()
        if target is not None:
            remove_plugins_copy(target.parent, target.name, log_fn)

    def deploy(self, log_fn=None, mode: LinkMode = LinkMode.HARDLINK,
               profile: str = "default", progress_fn=None) -> None:
        super().deploy(log_fn=log_fn, mode=mode, profile=profile, progress_fn=progress_fn)
        _log = log_fn or (lambda _: None)
        _log("Symlinking Plugins.txt ...")
        self._symlink_plugins_txt(profile, _log)

    def restore(self, log_fn=None, progress_fn=None) -> None:
        super().restore(log_fn=log_fn, progress_fn=progress_fn)
        self._remove_plugins_txt_symlink(log_fn or (lambda _: None))
