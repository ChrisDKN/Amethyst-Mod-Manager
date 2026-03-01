"""
exe_args_builder.py
Auto-populate ~/.config/AmethystModManager/exe_args.json with sensible default argument prefixes
whenever a known tool executable is detected.

Rules:
  - Only adds NEW entries; existing entries are never modified.
  - The game-root and output portions are pre-filled with their flag prefixes
    so the user only needs to pick an output folder via the Configure dialog.
  - PGPatcher is handled separately: its cfg/settings.json is generated
    automatically so the user does not have to configure paths through its GUI.

To add support for a new tool, add a single entry to EXE_PROFILES below.
"""

from __future__ import annotations

import json
from pathlib import Path
import xml.etree.ElementTree as ET
from typing import Callable, NamedTuple

# ---------------------------------------------------------------------------
# Wine path helper (mirrors gui.py::_to_wine_path)
# ---------------------------------------------------------------------------

def _to_wine_path(linux_path: "Path | str") -> str:
    r"""Convert a Linux absolute path to a Proton/Wine Z:\ path."""
    return "Z:" + str(linux_path).replace("/", "\\")


# ---------------------------------------------------------------------------
# Profile definition
# ---------------------------------------------------------------------------

class _ExeProfile(NamedTuple):
    """
    Defines how to build a default argument string for one tool executable.

    Fields
    ------
    game_flag : str
        The flag that precedes the game-root path (e.g. ``"--tesv:"``) or
        an empty string if the tool does not take a game-root argument.
    game_path_suffix : str
        Sub-path appended to the game root when building the game-root arg.
        Use ``""`` for the root itself or ``"Data"`` for the Data sub-folder.
    output_flag : str
        The flag that precedes the output path (e.g. ``"--output:"``).
    """
    game_flag: str
    game_path_suffix: str   # appended to game root; "" = game root itself
    output_flag: str


# ---------------------------------------------------------------------------
# Known tool profiles
# Add new executables here — one entry per exe name (case-sensitive).
# ---------------------------------------------------------------------------

_DATA_PROFILE = _ExeProfile(game_flag="-d:", game_path_suffix="Data", output_flag="-o:")

EXE_PROFILES: dict[str, _ExeProfile] = {
    # Pandora Behaviour Engine+ ------------------------------------------------
    "Pandora Behaviour Engine+.exe": _ExeProfile(
        game_flag="--tesv:",
        game_path_suffix="",        # points at game root
        output_flag="--output:",
    ),

    # xEdit / DynDOLOD / TexGen / xLODGen family --------------------
    **{name: _DATA_PROFILE for name in (
        "SSEEdit64.exe", "SSEEdit.exe", "SSEEditQuickAutoClean.exe",
        "TES5Edit.exe", "TES5EditQuickAutoClean.exe", "TES5Edit64.exe", 
        "DynDOLODx64.exe", "DynDOLOD.exe",
        "TexGenx64.exe", "TexGen.exe",
        "xLODGenx64.exe", "xLODGen.exe",
        "FO4Edit.exe","FO4Edit64.exe","FO4EditQuickAutoClean.exe",
        "TES4Edit.exe","TES4EditQuickAutoClean.exe","TES4Edit64.exe",
        "FNVEdit.exe","FNVEdit64.exe","FNVEditQuickAutoClean.exe"
    )},
}

# game_id → xLODGen game selection flag
_XLODGEN_GAME_FLAGS: dict[str, str] = {
    "Fallout3":     "-fo3",
    "Fallout3GOTY": "-fo3",
    "FalloutNV":    "-fnv",
    "Fallout4":     "-fo4",
    "Fallout4VR":   "-fo4vr",
    "skyrim":       "-tes5",
    "skyrimvr":     "-tes5vr",
    "skyrim_se":    "-sse",
    "Starfield":    "-sf1",
}

# Executables whose entries are intentionally left blank (handled separately).
EXE_SKIP: frozenset[str] = frozenset({
    "PGPatcher.exe",
    "WitcherScriptMerger.exe",
    "Wrye Bash.exe",       # -o path injected at runtime from active game
})

# ---------------------------------------------------------------------------
# PGPatcher settings.json bootstrap
# ---------------------------------------------------------------------------

# Default settings template — all values except game.dir and output.dir are
# fixed defaults.  Only generated when cfg/settings.json does not exist yet.
_PGPATCHER_SETTINGS_TEMPLATE: dict = {
    "params": {
        "game": {
            "dir": "",   # filled in at runtime
            "type": 0,
        },
        "globalpatcher": {
            "fixeffectlightingcs": False,
        },
        "modmanager": {
            "mo2instancedir": "",
            "mo2useloosefileorder": True,
            "type": 0,
        },
        "output": {
            "dir": "",   # filled in at runtime
            "pluginlang": "English",
            "zip": False,
        },
        "postpatcher": {
            "disableprepatchedmaterials": True,
            "fixsss": False,
            "hairflowmap": False,
        },
        "prepatcher": {
            "fixmeshlighting": False,
        },
        "processing": {
            "allowedmodelrecordtypes": [
                "ACTI", "AMMO", "ANIO", "ARMO", "ARMA", "ARTO", "BPTD",
                "BOOK", "CAMS", "CLMT", "CONT", "DOOR", "EXPL", "FLOR",
                "FURN", "GRAS", "HAZD", "HDPT", "IDLM", "IPCT", "ALCH",
                "INGR", "KEYM", "LVLN", "LIGH", "MATO", "MISC", "MSTT",
                "PROJ", "SCRL", "SLGM", "STAT", "TACT", "TREE", "WEAP",
            ],
            "allowlist": [],
            "blocklist": [
                "*\\cameras\\*",
                "*\\dyndolod\\*",
                "*\\lod\\*",
                "*_lod_*",
                "*_lod.*",
                "*\\markers\\*",
            ],
            "devmode": False,
            "enabledebuglogging": False,
            "enabletracelogging": False,
            "multithread": True,
            "pluginesmify": False,
            "texturemaps": {},
            "vanillabsalist": [
                "Skyrim - Textures0.bsa",
                "Skyrim - Textures1.bsa",
                "Skyrim - Textures2.bsa",
                "Skyrim - Textures3.bsa",
                "Skyrim - Textures4.bsa",
                "Skyrim - Textures5.bsa",
                "Skyrim - Textures6.bsa",
                "Skyrim - Textures7.bsa",
                "Skyrim - Textures8.bsa",
            ],
        },
        "shaderpatcher": {
            "complexmaterial": True,
            "parallax": True,
            "truepbr": False,
        },
        "shadertransforms": {
            "parallaxtocm": False,
        },
    }
}


def _bootstrap_pgpatcher_settings(
    exe_path: Path,
    game_path: "Path | None",
    staging_path: "Path | None",
    log_fn: "Callable[[str], None]",
) -> None:
    """
    Generate cfg/settings.json next to PGPatcher.exe if it does not exist.

    - exe_path   : full path to PGPatcher.exe
    - game_path  : game install root (Linux path)
    - staging_path : mods staging folder (Linux path)
    """
    cfg_dir = exe_path.parent / "cfg"
    settings_file = cfg_dir / "settings.json"

    if settings_file.exists():
        return  # already configured — never overwrite

    if game_path is None or staging_path is None:
        log_fn("PGPatcher: game path not configured; skipping settings.json generation")
        return

    # Ensure the output mod folder exists so PGPatcher can write there
    output_mod_dir = staging_path / "PGPatcher"
    output_mod_dir.mkdir(parents=True, exist_ok=True)

    # Build the settings dict from the template
    import copy
    settings = copy.deepcopy(_PGPATCHER_SETTINGS_TEMPLATE)
    settings["params"]["game"]["dir"] = _to_wine_path(game_path)
    settings["params"]["output"]["dir"] = _to_wine_path(output_mod_dir)

    # Write cfg/settings.json (create cfg/ if needed)
    try:
        cfg_dir.mkdir(parents=True, exist_ok=True)
        settings_file.write_text(json.dumps(settings, indent=2), encoding="utf-8")
        log_fn(f"PGPatcher: generated {settings_file}")
    except OSError as exc:
        log_fn(f"PGPatcher: could not write settings.json: {exc}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


from Utils.config_paths import get_exe_args_path  # noqa: E402
_EXE_ARGS_FILE = get_exe_args_path()

def update_witcher3_script_merger_config(game_root: Path, exe_path: Path) -> bool:
    """
    Update the WitcherScriptMerger.exe.config file to set the GameDirectory key.
    Returns True if the file was updated, False otherwise.
    """
    
    config_path = exe_path.parent / "WitcherScriptMerger.exe.config"
    if config_path.exists() and game_root is not None:
        wine_path = _to_wine_path(game_root)
        tree = ET.parse(config_path)
        root = tree.getroot()
        app_settings = root.find('appSettings')
        if app_settings is None:
            return False
        updated = False
        for add in app_settings.findall('add'):
            if add.attrib.get('key') == 'GameDirectory':
                if add.attrib.get('value') != wine_path:
                    add.set('value', wine_path)
                    updated = True
        if updated:
            tree.write(config_path, encoding='utf-8', xml_declaration=True)
    return updated

def build_default_exe_args(
    detected_exes: list[Path],
    game,
    log_fn: Callable[[str], None] | None = None,
) -> None:
    """
    For each detected exe that has a known profile and is not already in
    exe_args.json, write a default argument prefix entry.

    The output flag is written with just the flag prefix and a placeholder
    path (the staging folder) so the user can easily identify what to change
    via the Configure dialog.  The game-root flag is fully resolved.

    Parameters
    ----------
    detected_exes:
        Full paths of every .exe found by the exe scanner.
    game:
        The active BaseGame instance (used to resolve game_path / staging).
    log_fn:
        Optional callable for status messages; pass None to suppress output.
    """
    def _log(msg: str) -> None:
        if log_fn:
            log_fn(msg)

    # Resolve game paths (may be None if not configured)
    game_path: Path | None = (
        game.get_game_path() if hasattr(game, "get_game_path") else None
    )
    staging_path: Path | None = (
        game.get_mod_staging_path() if hasattr(game, "get_mod_staging_path") else None
    )

    # Load existing json (never overwrite existing entries)
    try:
        existing: dict[str, str] = json.loads(
            _EXE_ARGS_FILE.read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        existing = {}

    changed = False



    for exe_path in detected_exes:
        name = exe_path.name

        # Tools in EXE_SKIP get a blank exe_args entry and any bespoke setup
        if name in EXE_SKIP:
            if name not in existing:
                existing[name] = ""
                changed = True
            if name == "PGPatcher.exe":
                _bootstrap_pgpatcher_settings(exe_path, game_path, staging_path, _log)
            if name == "WitcherScriptMerger.exe":
                update_witcher3_script_merger_config(game_path, exe_path) # type: ignore
            continue

        # Skip unknowns and already-configured entries
        if name not in EXE_PROFILES or name in existing:
            continue

        profile = EXE_PROFILES[name]
        parts: list[str] = []

        def _flag_sep(flag: str) -> str:
            """Return '' if flag ends with ':' (e.g. '-o:'), else ' '."""
            return "" if flag.endswith(":") else " "

        # Game-root argument
        if profile.game_flag and game_path:
            target = (
                game_path / profile.game_path_suffix
                if profile.game_path_suffix
                else game_path
            )
            sep = _flag_sep(profile.game_flag)
            parts.append(f'{profile.game_flag}{sep}"{_to_wine_path(target)}"')

        # Output argument — defaults to the overwrite folder
        if profile.output_flag:
            sep = _flag_sep(profile.output_flag)
            overwrite_path = staging_path.parent / "overwrite" if staging_path else None
            if overwrite_path:
                parts.append(f'{profile.output_flag}{sep}"{_to_wine_path(overwrite_path)}"')
            else:
                parts.append(f'{profile.output_flag}{sep}"<select output folder>"')

        default_args = " ".join(parts)
        existing[name] = default_args
        changed = True
        _log(f"exe_args: added default args for {name}")

    if changed:
        try:
            _EXE_ARGS_FILE.write_text(
                json.dumps(existing, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            _log(f"exe_args: could not write {_EXE_ARGS_FILE}: {exc}")
