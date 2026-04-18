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

from gui.path_utils import _to_wine_path


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
# noise exes are identified — the change ships with the application.
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

    # Bethesda script extender loaders — users should launch the game via
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
    *,
    update: bool = False,
    output_mod: "Path | None" = None,
    pfx: "Path | None" = None,
) -> None:
    """
    Write cfg/settings.json next to PGPatcher.exe.

    When update=False (default): only seeds the file if it does not exist yet.
    When update=True: always overwrites game.dir and output.dir, preserving all
    other user-configured keys — used at launch time so profile switches are
    reflected correctly (all profiles share the same PGPatcher config file).

    - exe_path   : full path to PGPatcher.exe
    - game_path  : game install root (Linux path)
    - staging_path : mods staging folder (Linux path)
    - output_mod : explicit output folder path; defaults to staging_path / "PGPatcher"
    """
    if game_path is None or staging_path is None:
        log_fn("PGPatcher: game path not configured; skipping settings.json generation")
        return

    cfg_dir = exe_path.parent / "cfg"
    settings_file = cfg_dir / "settings.json"

    if settings_file.exists() and not update:
        return  # already seeded — runtime launch will keep it up to date

    # Ensure the output mod folder exists so PGPatcher can write there
    output_mod_dir = output_mod if output_mod is not None else staging_path / "PGPatcher"
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
    its outputPath points at <staging>/Pandora.

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

    output_mod_dir = staging_path / "Pandora"
    output_mod_dir.mkdir(parents=True, exist_ok=True)

    try:
        settings: dict = json.loads(settings_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        settings = {}

    games = settings.setdefault("games", {})
    entry = games.setdefault(pandora_game_key, {})
    entry["outputPath"] = _to_wine_path(output_mod_dir, pfx)
    if game_path is not None:
        entry.setdefault("gameDataPath", _to_wine_path(game_path / "Data", pfx))

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
    prefix_path: Path | None = (
        game.get_prefix_path() if hasattr(game, "get_prefix_path") else None
    )
    pfx = (prefix_path / "pfx") if prefix_path is not None else None

    # Determine whether we're writing to a profile-local or global exe_args.json
    from Utils.config_paths import get_profile_exe_args_path
    target_file = _EXE_ARGS_FILE
    effective_staging_path = staging_path  # output path for the default args
    try:
        active_dir = getattr(game, "_active_profile_dir", None)
        if active_dir is not None:
            from gui.game_helpers import profile_uses_specific_mods  # type: ignore
            if profile_uses_specific_mods(active_dir):
                target_file = get_profile_exe_args_path(Path(active_dir))
                effective_staging_path = game.get_effective_mod_staging_path()
    except Exception:
        pass

    # Load existing json (never overwrite existing entries)
    try:
        existing: dict[str, str] = json.loads(
            target_file.read_text(encoding="utf-8")
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
                _bootstrap_pgpatcher_settings(exe_path, game_path, effective_staging_path, _log, pfx=pfx)
            if name == "WitcherScriptMerger.exe":
                update_witcher3_script_merger_config(game_path, exe_path) # type: ignore
            if name == "NPC Plugin Chooser 2.exe":
                _bootstrap_npc_plugin_chooser_settings(exe_path, game_path, effective_staging_path, _log, pfx=pfx)
            if name == "Pandora Behaviour Engine+.exe":
                _bootstrap_pandora_settings(
                    getattr(game, "game_id", None),
                    game_path,
                    effective_staging_path,
                    prefix_path,
                    _log,
                )
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
            parts.append(f'{profile.game_flag}{sep}"{_to_wine_path(target, pfx)}"')

        # Output argument — defaults to the effective overwrite folder
        if profile.output_flag:
            sep = _flag_sep(profile.output_flag)
            overwrite_path = (
                effective_staging_path.parent / "overwrite"
                if effective_staging_path else None
            )
            if overwrite_path:
                parts.append(f'{profile.output_flag}{sep}"{_to_wine_path(overwrite_path, pfx)}"')
            else:
                parts.append(f'{profile.output_flag}{sep}"<select output folder>"')

        default_args = " ".join(parts)
        existing[name] = default_args
        changed = True
        _log(f"exe_args: added default args for {name}")

    if changed:
        try:
            target_file.parent.mkdir(parents=True, exist_ok=True)
            target_file.write_text(
                json.dumps(existing, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            _log(f"exe_args: could not write {target_file}: {exc}")
