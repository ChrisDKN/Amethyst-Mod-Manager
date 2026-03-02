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
from Utils.deploy import LinkMode
from Utils.config_paths import get_profiles_dir

# Plugins.txt lives here inside the game root (OblivionRemastered/)
_PLUGINS_TXT_GAME_REL = Path("Content/Dev/ObvData/Data/Plugins.txt")

_PROFILES_DIR = get_profiles_dir()

# Game root subfolder inside the Steam install directory
_GAME_SUBDIR = "OblivionRemastered"


class OblivionRemastered(UE5Game):

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
        # The launcher lives one level above the OblivionRemastered/ subfolder
        return "OblivionRemastered.exe"

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
    def loot_sort_enabled(self) -> bool:
        return True

    @property
    def loot_game_type(self) -> str:
        return "OblivionRemastered"

    @property
    def loot_masterlist_url(self) -> str:
        return "https://raw.githubusercontent.com/loot/oblivion-remastered/v0.26/masterlist.yaml"

    # -----------------------------------------------------------------------
    # UE5 routing rules
    # -----------------------------------------------------------------------
    # Rules are evaluated in order; first match wins.
    # ``folder`` matches on the first path segment of the staged relative path.
    # ``extensions`` matches on file extension.
    # ``strip`` lists prefixes to remove so files don't get double-nested.

    @property
    def ue5_routing_rules(self) -> list[UE5Rule]:
        return [
            # Paths already starting with Binaries/ or Content/ → game root,
            # path preserved as-is.  These mods ship the full correct structure.
            UE5Rule(dest="", folder="binaries"),
            UE5Rule(dest="", folder="content"),
            # ue4ss/ folder → Binaries/Win64/ue4ss/
            UE5Rule(
                dest="Binaries/Win64/ue4ss",
                folder="ue4ss",
                strip=["ue4ss"],
            ),
            # OBSE/ folder → Binaries/Win64/OBSE/
            UE5Rule(
                dest="Binaries/Win64/OBSE",
                folder="obse",
                strip=["obse", "OBSE"],
            ),
            # Pak / streaming files → Content/Paks/
            UE5Rule(
                dest="Content/Paks",
                extensions=[".pak", ".utoc", ".ucas"],
                strip=["Content/Paks", "Paks"],
            ),
            # Data/ folder → Content/Dev/ObvData/Data/ (path preserved under Data/).
            # Covers mods shipped as Data/MyMod.esp, Data/SyncMap/MyMod.ini, etc.
            UE5Rule(
                dest="Content/Dev/ObvData/Data",
                folder="data",
                strip=["Content/Dev/ObvData/Data", "Data"],
            ),
            # Loose esp/esm plugins (no Data/ wrapper) → Content/Dev/ObvData/Data/
            UE5Rule(
                dest="Content/Dev/ObvData/Data",
                extensions=[".esp", ".esm"],
                strip=["Content/Dev/ObvData/Data", "Data"],
            ),
            # Lua UE4SS scripts → Binaries/Win64/ue4ss/Mods/
            UE5Rule(
                dest="Binaries/Win64/ue4ss/Mods",
                extensions=[".lua"],
                strip=[
                    "Binaries/Win64/ue4ss/Mods",
                    "Binaries/Win64/ue4ss",
                    "ue4ss/Mods",
                    "UE4SS/Mods",
                    "UE4SS",
                    "ue4ss",
                ],
            ),
            # Loose UE4SS proxy/runtime files (dwmapi.dll, UE4SS.dll, UE4SS.pdb) → Binaries/Win64/
            UE5Rule(
                dest="Binaries/Win64",
                extensions=[".dll", ".pdb"],
            ),
            # Bink video replacers → Content/Movies/Modern/
            UE5Rule(
                dest="Content/Movies/Modern",
                extensions=[".bk2"],
                strip=["Content/Movies/Modern", "Content/Movies"],
            ),
        ]

    @property
    def ue5_default_dest(self) -> str:
        """Files that match no rule land at the game root."""
        return ""

    # -----------------------------------------------------------------------
    # Mod install hints
    # -----------------------------------------------------------------------

    @property
    def mod_folder_strip_prefixes(self) -> set[str]:
        # Some mod authors wrap their entire mod in a top-level folder that
        # mirrors the game's install root.  Strip it so the routing rules
        # see the correct first segment.
        return {"oblivionremastered"}


    # -----------------------------------------------------------------------
    # Paths
    # -----------------------------------------------------------------------

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
            _log(f"  WARN: plugins.txt not found at {source} — skipping symlink.")
            return
        if target.exists() or target.is_symlink():
            target.unlink()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to(source)
        _log(f"  Linked Plugins.txt → {target}")

    def _remove_plugins_txt_symlink(self, log_fn) -> None:
        target = self._plugins_txt_target()
        if target is not None and target.is_symlink():
            target.unlink()
            log_fn("  Removed Plugins.txt symlink.")

    def deploy(self, log_fn=None, mode: LinkMode = LinkMode.HARDLINK,
               profile: str = "default", progress_fn=None) -> None:
        super().deploy(log_fn=log_fn, mode=mode, profile=profile, progress_fn=progress_fn)
        _log = log_fn or (lambda _: None)
        _log("Symlinking Plugins.txt ...")
        self._symlink_plugins_txt(profile, _log)

    def restore(self, log_fn=None, progress_fn=None) -> None:
        super().restore(log_fn=log_fn, progress_fn=progress_fn)
        self._remove_plugins_txt_symlink(log_fn or (lambda _: None))
