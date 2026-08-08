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

from Utils.wine_paths import to_wine_path as _to_wine_path


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
# Add new executables here - one entry per exe name (case-sensitive).
# ---------------------------------------------------------------------------

_DATA_PROFILE = _ExeProfile(game_flag="-d:", game_path_suffix="Data", output_flag="-o:")

EXE_PROFILES: dict[str, _ExeProfile] = {
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
    "NPC Plugin Chooser 2.exe",
    "Pandora Behaviour Engine+.exe",  # output path configured via Settings.json
})

# Exe names (lowercase) hidden from the dropdown by default.  These are
# helper / sub-process / redistributable executables that are commonly
# bundled inside mod tool archives but are never meant to be launched
# directly by the user.  Custom EXEs added via '+ Add custom EXE…'
# always bypass this filter.  Extend this list here in source when new
# noise exes are identified - the change ships with the application.
EXE_FILTER_DEFAULTS: frozenset[str] = frozenset({
    # DirectXTex utilities (ship with many texture tools / Creation Kit)
    "texconv.exe",
    "bc7.exe",
    "bendr.exe",
    "optimise.exe",
    "loose.exe",
    "layerprep.exe",
    "filter.exe",
    "extract.exe",
    "exclude.exe",
    "bsa.exe",
    "prepparallax.exe",
    "outputqc.exe",
    "makeunpack.exe",
    "loosecopy.exe",
    "extractbsa.exe",
    "exclusions.exe",
    "convert.exe",
    "bendrfilter.exe",
    "alphanormalsql.exe",
    "pgtools.exe",
    "userguide.exe",
    "lodgen.exe",
    "lodgenx64.exe",
    "ssedump.exe",
    "ssedump64.exe",
    "texconvx64.exe",
    "merge.bat",
    "re-uv.bat",
    "treelod.exe",
    "kdiff3.exe",
    "quickbms.exe",
    "quickbms_4gb_files.exe",
    "wcc_lite.exe",
    "fo4dump.exe",
    "fo4dump64.exe",
    "reimport.bat",
    "reimport2.bat",
    "reimport2_4gb_files.bat",
    "reimport3_localizations.bat",
    "reimport_4gb_files.bat",
    "fnvdump.exe",
    "fnvdump64.exe",
    "fo3dump.exe",
    "fo3dump64.exe",
    "tes4dump.exe",
    "tes4dump64.exe",
    "tes5dump.exe",
    "tes5dump64.exe",
    "7z.exe",
    "ffdec_orig.bat",
    "xdelta.exe",
    "hkxcmd.exe",
    "fetch_macholib.bat",
    "wininst-10.0-amd64.exe",
    "wininst-10.0.exe",
    "wininst-14.0-amd64.exe",
    "wininst-14.0.exe",
    "wininst-6.0.exe",
    "wininst-7.1.exe",
    "wininst-8.0.exe",
    "wininst-9.0-amd64.exe",
    "wininst-9.0.exe",
    "idle.bat",
    "activate.bat",
    "deactivate.bat",
    "nemesis compiler version.bat",
    "papyrusassembler.exe",
    "papyruscompiler.exe",
    "hybrid.bat",
    "script.bat",
    "lodgenx64win.exe",
    "dip.exe",
    "lodgenx64win10.exe",
    "lodgenx64win6.exe",
    "bsarch.exe",
    "sseedit.exe",
    "xlodgen.exe",
    

    # Bethesda script extender loaders - users should launch the game via
    # Steam (with the extender wired up through launch options / proxy),
    # not by running these EXEs directly through the mod manager.
    "skse_loader.exe",          # Skyrim (Oldrim) SKSE
    "skse64_loader.exe",        # Skyrim Special Edition / AE
    "sksevr_loader.exe",        # Skyrim VR
    "f4se_loader.exe",          # Fallout 4
    "f4sevr_loader.exe",        # Fallout 4 VR
    "fose_loader.exe",          # Fallout 3 FOSE
    "nvse_loader.exe",          # Fallout: New Vegas NVSE
    "obse_loader.exe",          # Oblivion OBSE
    "sfse_loader.exe",          # Starfield SFSE
    "mwse-launcher.exe",        # Morrowind MWSE
    
    "synthesis.exe", # Only works via the wizard menu

    # ReSaver (Fallout 4 / Skyrim save editor) - only ReSavor.bat should be
    # launched.  The folder also ships an older ReSaver.bat/.exe and a raw
    # ReSavor.exe, none of which should appear in the dropdown.
    "resaver.bat",
    "resaver.exe",
    "resavor.exe",

    # Bundled JRE binaries (ReSaver and other Java tools ship a private jre/) -
    # never launched directly by the user.
    "jabswitch.exe",
    "java.exe",
    "java-rmi.exe",
    "javaw.exe",
    "jfr.exe",
    "jjs.exe",
    "keytool.exe",
    "kinit.exe",
    "klist.exe",
    "ktab.exe",
    "orbd.exe",
    "pack200.exe",
    "policytool.exe",
    "rmid.exe",
    "rmiregistry.exe",
    "servertool.exe",
    "tnameserv.exe",
    "unpack200.exe",
})

# ---------------------------------------------------------------------------
# PGPatcher settings.json bootstrap
# ---------------------------------------------------------------------------

# Default settings template - all values except game.dir and output.dir are
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
    *,
    update: bool = False,
    output_mod: "Path | None" = None,
    pfx: "Path | None" = None,
    mo2_instance_dir: "Path | None" = None,
    game_type: "int | None" = None,
) -> None:
    """
    Write cfg/settings.json next to PGPatcher.exe.

    When update=False (default): only seeds the file if it does not exist yet.
    When update=True: always overwrites game.dir and output.dir, preserving all
    other user-configured keys - used at launch time so profile switches are
    reflected correctly (all profiles share the same PGPatcher config file).

    - exe_path   : full path to PGPatcher.exe
    - game_path  : game install root (Linux path)
    - staging_path : mods staging folder (Linux path)
    - output_mod : explicit output folder path; defaults to staging_path / "PGPatcher_output"
    - mo2_instance_dir : if given, enable MO2 conflict-resolution mode
      (modmanager.type=2) pointed at this dummy MO2 instance; if None, force
      modmanager.type=0 (None).
    """
    if game_path is None or staging_path is None:
        log_fn("PGPatcher: game path not configured; skipping settings.json generation")
        return

    cfg_dir = exe_path.parent / "cfg"
    settings_file = cfg_dir / "settings.json"

    if settings_file.exists() and not update:
        return  # already seeded - runtime launch will keep it up to date

    # Ensure the output mod folder exists so PGPatcher can write there
    output_mod_dir = output_mod if output_mod is not None else staging_path / "PGPatcher_output"
    output_mod_dir.mkdir(parents=True, exist_ok=True)

    # Load existing settings (preserve user changes) or start from template
    import copy
    if settings_file.exists():
        try:
            settings = json.loads(settings_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            settings = copy.deepcopy(_PGPATCHER_SETTINGS_TEMPLATE)
    else:
        settings = copy.deepcopy(_PGPATCHER_SETTINGS_TEMPLATE)

    # Update the two profile-dependent paths
    settings.setdefault("params", {}).setdefault("game", {})["dir"] = _to_wine_path(game_path, pfx)
    settings["params"].setdefault("output", {})["dir"] = _to_wine_path(output_mod_dir, pfx)

    # Game type (BethesdaGame::GameType: SE=0, GOG=1, VR=2, Enderal=3).  Set when
    # known so GOG installs read the right game; left untouched otherwise.
    if game_type is not None:
        settings["params"]["game"]["type"] = game_type

    # Mod-manager conflict-resolution mode: MO2 (2) against the dummy instance,
    # or None (0).  type 1 would be Vortex.
    mm = settings["params"].setdefault("modmanager", {})
    if mo2_instance_dir is not None:
        mm["type"] = 2
        mm["mo2instancedir"] = _to_wine_path(mo2_instance_dir, pfx)
    else:
        mm["type"] = 0

    # Write cfg/settings.json (create cfg/ if needed)
    try:
        cfg_dir.mkdir(parents=True, exist_ok=True)
        settings_file.write_text(json.dumps(settings, indent=2), encoding="utf-8")
        log_fn(f"PGPatcher: {'updated' if update else 'generated'} {settings_file}")
    except OSError as exc:
        log_fn(f"PGPatcher: could not write settings.json: {exc}")


# ---------------------------------------------------------------------------
# NPC Plugin Chooser 2 settings.json bootstrap
# ---------------------------------------------------------------------------

def _bootstrap_npc_plugin_chooser_settings(
    exe_path: Path,
    game_path: "Path | None",
    staging_path: "Path | None",
    log_fn: "Callable[[str], None]",
    pfx: "Path | None" = None,
) -> None:
    """
    Write or update settings.json next to "NPC Plugin Chooser 2.exe".

    Always updates ModsFolder and SkyrimGamePath regardless of whether the
    file already exists, so that profile switches are reflected immediately.
    """
    settings_file = exe_path.parent / "settings.json"

    if game_path is None or staging_path is None:
        log_fn("NPC Plugin Chooser 2: game/staging path not configured; skipping settings.json")
        return

    # Load existing settings if present
    try:
        settings: dict = json.loads(settings_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        settings = {}

    settings["ModsFolder"] = _to_wine_path(staging_path, pfx)
    settings["SkyrimGamePath"] = _to_wine_path(game_path / "Data", pfx)

    try:
        settings_file.write_text(json.dumps(settings, indent=2), encoding="utf-8")
        log_fn(f"NPC Plugin Chooser 2: updated {settings_file}")
    except OSError as exc:
        log_fn(f"NPC Plugin Chooser 2: could not write settings.json: {exc}")


# ---------------------------------------------------------------------------
# Pandora Behaviour Engine+ Settings.json bootstrap
# ---------------------------------------------------------------------------

# Maps Mod Manager game_id → Pandora's Settings.json game key.
_PANDORA_GAME_KEYS: dict[str, str] = {
    "skyrim_se": "SkyrimSE",
    "skyrim":    "SkyrimLE",
    "skyrimvr":  "SkyrimVR",
}


def _bootstrap_pandora_settings(
    game_id: "str | None",
    game_path: "Path | None",
    staging_path: "Path | None",
    prefix_path: "Path | None",
    log_fn: "Callable[[str], None]",
) -> None:
    """
    Update Pandora Behaviour Engine's Settings.json inside the Wine prefix so
    its outputPath points at <staging>/Pandora_output.

    Pandora's newer builds read the output folder from Settings.json rather
    than the ``--output:`` CLI flag, so we have to rewrite it at launch time
    to follow the active profile's staging folder.

    prefix_path : Path to the compatdata folder (containing ``pfx/``).
    """
    if staging_path is None or prefix_path is None:
        log_fn("Pandora: staging or prefix path missing; skipping Settings.json update")
        return

    pandora_game_key = _PANDORA_GAME_KEYS.get(game_id or "", "SkyrimSE")

    pfx = prefix_path / "pfx" if prefix_path.name != "pfx" else prefix_path
    settings_file = (
        pfx / "drive_c" / "users" / "steamuser" / "AppData" / "Local"
        / "Pandora Behaviour Engine" / "Settings.json"
    )

    output_mod_dir = staging_path / "Pandora_output"
    output_mod_dir.mkdir(parents=True, exist_ok=True)

    try:
        settings: dict = json.loads(settings_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        settings = {}

    # Default to the dark theme (theme 2) on first setup; leave the user's
    # choice alone if they've already picked one.
    app = settings.setdefault("app", {})
    app.setdefault("theme", 2)

    games = settings.setdefault("games", {})
    entry = games.setdefault(pandora_game_key, {})
    entry["outputPath"] = _to_wine_path(output_mod_dir, pfx)
    if game_path is not None:
        # Force-set (not setdefault): the tool prefix is shared across
        # profiles, so a first-run path must not stick when the active
        # profile points at a different game install.
        entry["gameDataPath"] = _to_wine_path(game_path / "Data", pfx)

    try:
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        settings_file.write_text(json.dumps(settings, indent=2), encoding="utf-8")
        log_fn(f"Pandora: updated {settings_file}")
    except OSError as exc:
        log_fn(f"Pandora: could not write Settings.json: {exc}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


from Utils.config_paths import get_exe_args_path  # noqa: E402
_EXE_ARGS_FILE = get_exe_args_path()

def update_witcher3_script_merger_config(game_root: Path, exe_path: Path) -> bool:
    """
    Update the WitcherScriptMerger config file to set the GameDirectory key.
    Handles both WitcherScriptMerger.exe.config (legacy) and WitcherScriptMerger.dll.config (newer).
    Returns True if any file was updated, False otherwise.
    """
    if game_root is None:
        return False
    wine_path = _to_wine_path(game_root)
    any_updated = False
    for config_name in ("WitcherScriptMerger.exe.config", "WitcherScriptMerger.dll.config"):
        config_path = exe_path.parent / config_name
        if not config_path.exists():
            continue
        tree = ET.parse(config_path)
        root = tree.getroot()
        app_settings = root.find('appSettings')
        if app_settings is None:
            continue
        updated = False
        for add in app_settings.findall('add'):
            if add.attrib.get('key') == 'GameDirectory':
                if add.attrib.get('value') != wine_path:
                    add.set('value', wine_path)
                    updated = True
        if updated:
            tree.write(config_path, encoding='utf-8', xml_declaration=True)
            any_updated = True
    return any_updated
