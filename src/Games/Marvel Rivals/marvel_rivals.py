"""
marvel_rivals.py
Game handler for Marvel Rivals (Unreal Engine 5).

Mod structure
-------------
Mods ship files destined for multiple locations inside the MarvelGame/Marvel/
subfolder (the UE5 game root):

  .pak / .utoc / .ucas  → MarvelGame/Marvel/Content/Paks/~mods/
  ue4ss/ folder         → MarvelGame/Marvel/Binaries/Win64/ue4ss/
  .lua files            → MarvelGame/Marvel/Binaries/Win64/ue4ss/Mods/
  .dll / .pdb files     → MarvelGame/Marvel/Binaries/Win64/
  Binaries/ or Content/ already present → game root (path preserved as-is)
  Loose files (no rule) → game root (MarvelGame/Marvel/)

The game root lives at:
  <steam_install>/MarvelRivals/MarvelGame/Marvel/

There are no plugin files (.esp/.esm/.esl) — this is a pure UE5 game.
"""

from __future__ import annotations

from pathlib import Path

from Games.ue5_game import UE5Game, UE5Rule
from Utils.config_paths import get_profiles_dir

_PROFILES_DIR = get_profiles_dir()

# Path from the Steam install dir to the UE5 game root
_GAME_SUBDIR_PARTS = ["MarvelGame", "Marvel"]


def _find_subdir(base: Path, name: str) -> Path | None:
    """Case-insensitive lookup of a single child directory."""
    exact = base / name
    if exact.is_dir():
        return exact
    needle = name.lower()
    try:
        for child in base.iterdir():
            if child.is_dir() and child.name.lower() == needle:
                return child
    except OSError:
        pass
    return None


class MarvelRivals(UE5Game):

    # -----------------------------------------------------------------------
    # Identity
    # -----------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "Marvel Rivals"

    @property
    def game_id(self) -> str:
        return "marvel_rivals"

    @property
    def exe_name(self) -> str:
        return "MarvelRivals_Launcher.exe"

    @property
    def steam_id(self) -> str:
        return "2767030"

    @property
    def nexus_game_domain(self) -> str:
        return "marvelrivals"
    
    @property
    def wine_dll_overrides(self) -> dict[str, str]:
        return {"dsound": "native,builtin"}
    
    @property
    def conflict_ignore_filenames(self) -> set[str]:
        return {"*.txt","*.html","*.md","*.jpeg","*.png","*.jpg","*.rar"}
    
    @property
    def frameworks(self) -> dict[str, str]:
        return {"UTOC Bypass": "Binaries/Win64/plugins/MarvelRivalsUTOCSignatureBypass.asi"}

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
            # LogicMods folder → Content/Paks/LogicMods/ (preserved as a folder
            # under Paks). Must come before the .pak extension rule so files
            # inside LogicMods don't get routed to ~mods/.
            UE5Rule(dest="Content/Paks", prefix="Content/Paks/LogicMods",
                    strip=["Content/Paks"], flatten=True),
            UE5Rule(dest="Content/Paks", prefix="Paks/LogicMods",
                    strip=["Paks"], flatten=True),
            UE5Rule(dest="Content/Paks", folder="LogicMods", flatten=True),
            # Pak / streaming files → Content/Paks/~mods/  (checked before the
            # generic folder="content" catch-all so mods shipped as
            # Content/Paks/… are routed here rather than to the game root as-is)
            UE5Rule(
                dest="Content/Paks/~mods",
                extensions=[".pak", ".utoc", ".ucas"],
                strip=["Content/Paks/~mods", "Content/Paks/~Mods", "Content/Paks", "Paks", "Content", "~mods", "~Mods"],
                flatten=True,
            ),
            # Files already inside Content/Paks/~Mods (any casing) → normalise
            # to lowercase ~mods dest so only one folder is created on disk.
            UE5Rule(
                dest="Content/Paks/~mods",
                prefix="Content/Paks/~Mods",
                strip=["Content/Paks/~Mods", "Content/Paks/~mods"],
                flatten=True,
            ),
            # Mods shipping Binaries/Win64/UE4SS/… → normalise to lowercase
            # ue4ss dest so only one folder is ever created on disk.
            UE5Rule(
                dest="Binaries/Win64/ue4ss",
                prefix="Binaries/Win64/UE4SS",
                strip=["Binaries/Win64/UE4SS", "Binaries/Win64/ue4ss"],
                flatten=True,
            ),
            # ue4ss/ or UE4SS/ top-level folder → Binaries/Win64/ue4ss/
            # (catches loose ue4ss files like UE4SS-settings.ini before the
            # extension rules can misroute them)
            UE5Rule(
                dest="Binaries/Win64/ue4ss",
                folder="ue4ss",
                strip=["ue4ss", "UE4SS"],
                flatten=True,
            ),
            # Paths already starting with Binaries/ or Content/ → game root,
            # path preserved as-is.  These mods ship the full correct structure.
            UE5Rule(dest="", folder="binaries"),
            UE5Rule(dest="", folder="content"),
            # Lua UE4SS scripts and companion files (config.ini, data .json,
            # enabled.txt) → Binaries/Win64/ue4ss/Mods/
            UE5Rule(
                dest="Binaries/Win64/ue4ss/Mods",
                extensions=[".lua", ".ini", ".json"],
                filenames=["enabled.txt"],
                strip=[
                    "Binaries/Win64/ue4ss/Mods",
                    "Binaries/Win64/ue4ss",
                    "ue4ss/Mods",
                    "UE4SS/Mods",
                    "UE4SS",
                    "ue4ss",
                    "Mods",
                ],
                flatten=True,
            ),
            # Loose UE4SS proxy/runtime files (dwmapi.dll, UE4SS.dll, etc.) → Binaries/Win64/
            UE5Rule(
                dest="Binaries/Win64",
                extensions=[".dll", ".pdb"],
            ),
            UE5Rule(
                dest="Binaries/Win64/plugins",
                extensions=[".asi"],
                strip=["plugins"],
                flatten=True,
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
        # Strip common top-level wrappers mod authors include.
        return {"marvelrivals", "marvelgame", "marvel"}

    # -----------------------------------------------------------------------
    # Paths
    # -----------------------------------------------------------------------

    def get_game_path(self) -> Path | None:
        """The UE5 game root is MarvelRivals/MarvelGame/Marvel/ inside the
        Steam install directory.  Each segment is looked up case-insensitively
        so it works on both Windows (via Proton) and Linux."""
        if self._game_path is None:
            return None

        current = self._game_path
        for part in _GAME_SUBDIR_PARTS:
            found = _find_subdir(current, part)
            if found is None:
                # Fallback: user may have pointed directly at a deeper folder
                return self._game_path
            current = found
        return current

    def get_mod_staging_path(self) -> Path:
        if self._staging_path is not None:
            return self._staging_path / "mods"
        return _PROFILES_DIR / self.name / "mods"
