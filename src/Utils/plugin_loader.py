"""
plugin_loader.py
Discovers and loads external wizard plugin scripts from the Plugins directory.

Plugin files are plain Python scripts placed in ~/.config/AmethystModManager/Plugins/.
Each must define a module-level ``PLUGIN_INFO`` dict and a dialog class that follows
the standard wizard dialog signature::

    PLUGIN_INFO = {
        "id":           "my_tool",
        "label":        "My Tool",
        "description":  "One-line description.",
        "game_ids":     ["skyrim_se"],      # list of supported game_ids
        "all_games":    False,              # True = show for every game
        "dialog_class": "MyToolDialog",     # class name in this file
        "category":     "Patchers and Cleanup",  # optional: picker group header.
                                                # Omit to auto-infer; a new name
                                                # not in CATEGORY_ORDER is shown
                                                # automatically, before "Other".
    }

    class MyToolDialog(ctk.CTkFrame):
        def __init__(self, parent, game, log_fn=None, *, on_close=None, **extra):
            ...

Bad or incomplete plugin files are silently skipped so one broken plugin
doesn't affect the rest of the application.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from Games.base_game import BaseGame, WizardTool
from Utils.config_paths import get_plugins_dir

# ---------------------------------------------------------------------------
# Module-level cache
# ---------------------------------------------------------------------------

_plugins_cache: list[dict] = []
_plugins_dir_mtime: float = 0.0

_REQUIRED_KEYS = {"id", "label", "dialog_class"}


# ---------------------------------------------------------------------------
# Built-in wizard tools (ported former external plugins)
# ---------------------------------------------------------------------------
#
# These were originally distributed as external ``PLUGIN_INFO`` scripts (see the
# discovery machinery below) but are now shipped inside the app as first-class
# wizard tools with Qt views registered in ``wizards_qt.REGISTRY``.  We keep the
# game-id-based attachment (rather than moving them onto each game class) because
# some target games are JSON custom games (e.g. Slime Rancher, My Summer Car),
# which have no Python game class to edit - matching on ``game.game_id`` covers
# both built-in and custom games uniformly.
#
# Each entry mirrors the old PLUGIN_INFO shape but carries a *real*
# ``dialog_class`` (used as the ``dialog_class_path`` that keys ``get_spec`` in
# the Qt wizard registry), not a class object.

_BETHESDA_GAME_IDS = [
    "skyrim_se", "Fallout3", "Fallout3GOTY", "FalloutNV", "Fallout4",
    "Fallout4VR", "Oblivion", "skyrim", "skyrimvr", "Starfield",
    "enderal", "enderalse",
]

BUILTIN_WIZARD_TOOLS: list[dict] = [
    {
        "id": "bethesda_register_game_path",
        "label": "Register Game Path in Wine Registry",
        "description": ("Write the game's install path to the Bethesda Softworks "
                        "registry keys in the game's Proton prefix."),
        "game_ids": _BETHESDA_GAME_IDS,
        "all_games": False,
        "dialog_class": "wizards.bethesda_register_game_path.RegisterGamePathWizard",
        "category": "Setup and Installers",
    },
    {
        "id": "bethesda_synthesis",
        "label": "Run Synthesis",
        "description": "Install and run Mutagen Synthesis patcher in its own prefix.",
        "game_ids": [g for g in _BETHESDA_GAME_IDS if g != "FalloutNV"],
        "all_games": False,
        "dialog_class": "wizards.bethesda_synthesis.SynthesisWizard",
        "category": "Patchers and Cleanup",
    },
    {
        "id": "nif_viewer",
        "label": "NIF Viewer",
        "description": ("Browse every mesh in this profile and the vanilla game "
                        "- loose files and BSA/BA2 contents - and preview it in 3D."),
        "game_ids": _BETHESDA_GAME_IDS,
        "all_games": False,
        # Qt-only tool: no Tk class exists behind this path, it is a registry key.
        "dialog_class": "wizards.nif_viewer.NifViewerWizard",
        "category": "Other",
    },
    {
        "id": "bg3_import_modlist_json",
        "label": "Import BG3MM Load Order (.json)",
        "description": ("Convert a BG3 Mod Manager modlist.json into this "
                        "profile's load order and apply it."),
        "game_ids": ["baldurs_gate_3"],
        "all_games": False,
        "dialog_class": "wizards.bg3_import.BG3ImportWizard",
        "category": "Load Order and Config",
    },
    {
        "id": "sdv_smapi",
        "label": "Install SMAPI",
        "description": ("Download and install SMAPI (mod loader) for Stardew "
                        "Valley, into the game folder or as a managed mod."),
        "game_ids": ["Stardew_Valley"],
        "all_games": False,
        "dialog_class": "wizards.sdv_smapi.SmapiWizard",
        "category": "Setup and Installers",
    },
    {
        "id": "sr_srml",
        "label": "Install SRML",
        "description": "Download and install SRML (Slime Rancher Mod Loader).",
        "game_ids": ["Slime_Rancher"],
        "all_games": False,
        "dialog_class": "wizards.sr_srml.SRMLWizard",
        "category": "Setup and Installers",
    },
    {
        "id": "msc_mscloader",
        "label": "Install MSCLoader",
        "description": "Download and install MSCLoader (mod loader for My Summer Car).",
        "game_ids": ["My_Summer_Car"],
        "all_games": False,
        "dialog_class": "wizards.msc_mscloader.MSCLoaderWizard",
        "category": "Setup and Installers",
    },
]


def get_builtin_wizard_tools_for_game(game_id: str) -> list[WizardTool]:
    """Return built-in ported-plugin :class:`WizardTool` entries for *game_id*.

    Attaches by ``game_id`` so JSON custom games are covered too.  Unlike the
    external plugins, these carry a real ``dialog_class_path`` (the key
    ``wizards_qt.get_spec`` looks up), so no ``extra['_dialog_class']`` route.
    """
    tools: list[WizardTool] = []
    for info in BUILTIN_WIZARD_TOOLS:
        if info.get("all_games") or game_id in info.get("game_ids", []):
            tools.append(WizardTool(
                id=info["id"],
                label=info["label"],
                description=info.get("description", ""),
                dialog_class_path=info["dialog_class"],
                category=info.get("category", ""),
            ))
    return tools


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def discover_plugins(*, force: bool = False) -> list[dict]:
    """Scan the Plugins directory and return validated plugin descriptors.

    Each descriptor is the plugin's ``PLUGIN_INFO`` dict augmented with:
      - ``_resolved_class``: the actual dialog class object
      - ``_source_file``:    path to the ``.py`` file

    Results are cached and only re-scanned when the directory's mtime changes,
    unless *force* is ``True``.
    """
    global _plugins_cache, _plugins_dir_mtime

    plugins_dir = get_plugins_dir()

    try:
        current_mtime = plugins_dir.stat().st_mtime
    except OSError:
        return _plugins_cache

    if not force and current_mtime == _plugins_dir_mtime and _plugins_cache:
        return _plugins_cache

    plugins: list[dict] = []
    seen_ids: set[str] = set()

    for py_file in sorted(plugins_dir.glob("*.py")):
        if py_file.name.startswith("_"):
            continue
        try:
            plugin = _load_plugin_file(py_file)
        except Exception as exc:
            _warn(f"Plugin '{py_file.name}': skipped - {exc}")
            continue

        if plugin is None:
            continue

        pid = plugin["id"]
        if pid in seen_ids:
            _warn(f"Plugin '{py_file.name}': duplicate id '{pid}', skipped.")
            continue

        seen_ids.add(pid)
        plugins.append(plugin)

    _plugins_cache = plugins
    _plugins_dir_mtime = current_mtime
    return plugins


def _load_plugin_file(py_file: Path) -> dict | None:
    """Load a single plugin file and return a validated descriptor, or *None*."""
    module_name = f"_amm_plugins.{py_file.stem}"

    spec = importlib.util.spec_from_file_location(module_name, str(py_file))
    if spec is None or spec.loader is None:
        return None

    # Remove previously cached module so updated files are re-executed
    sys.modules.pop(module_name, None)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)

    info = getattr(module, "PLUGIN_INFO", None)
    if not isinstance(info, dict):
        _warn(f"Plugin '{py_file.name}': missing or invalid PLUGIN_INFO dict.")
        return None

    missing = _REQUIRED_KEYS - info.keys()
    if missing:
        _warn(f"Plugin '{py_file.name}': PLUGIN_INFO missing keys: {missing}")
        return None

    class_name = info["dialog_class"]
    cls = getattr(module, class_name, None)
    if cls is None or not isinstance(cls, type):
        _warn(f"Plugin '{py_file.name}': dialog_class '{class_name}' not found or not a class.")
        return None

    descriptor = dict(info)
    descriptor["_resolved_class"] = cls
    descriptor["_source_file"] = py_file
    descriptor.setdefault("description", "")
    descriptor.setdefault("game_ids", [])
    descriptor.setdefault("all_games", False)
    return descriptor


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------

def get_plugin_tools_for_game(game_id: str) -> list[WizardTool]:
    """Return :class:`WizardTool` entries from loaded plugins that match *game_id*."""
    tools: list[WizardTool] = []
    for plugin in discover_plugins():
        if plugin.get("all_games") or game_id in plugin.get("game_ids", []):
            tools.append(WizardTool(
                id=plugin["id"],
                label=plugin["label"],
                description=plugin.get("description", ""),
                dialog_class_path="",
                category=plugin.get("category", ""),
                extra={"_dialog_class": plugin["_resolved_class"]},
            ))
    return tools


def get_all_wizard_tools(game: BaseGame) -> list[WizardTool]:
    """Return a game's own wizard tools merged with the built-in ported-plugin
    tools and any external plugin tools for *game*."""
    return (list(game.wizard_tools)
            + get_builtin_wizard_tools_for_game(game.game_id)
            + get_plugin_tools_for_game(game.game_id))


# ---------------------------------------------------------------------------
# Exe → wizard mapping
# ---------------------------------------------------------------------------
#
# When a user runs one of these executables from the exe dropdown, the matching
# wizard tool is opened instead of launching the exe directly through Proton.
# The wizards handle install/deploy/prefix setup that a bare Proton launch skips.
#
# Keyed by the tool's ``dialog_class_path``; the value is the set of exe
# basenames (lowercase) the wizard launches.  Tools whose exe name varies per
# game (the xEdit family) are handled dynamically below via ``extra`` instead.

_WIZARD_CLASS_EXES: dict[str, set[str]] = {
    "wizards.pandora.PandoraWizard": {"pandora behaviour engine+.exe"},
    "wizards.bodyslide.BodySlideWizard": {"bodyslide.exe", "bodyslide x64.exe"},
    "wizards.bodyslide.OutfitStudioWizard": {"outfitstudio.exe", "outfitstudio x64.exe"},
    "wizards.pgpatcher.PGPatcherWizard": {"pgpatcher.exe"},
    "wizards.eslifier.ESLifierWizard": {"eslifier.exe"},
    "wizards.dyndolod.TexGenWizard": {"texgenx64.exe"},
    "wizards.dyndolod.DynDOLODWizard": {"dyndolodx64.exe"},
    "wizards.dyndolod.xLODGenWizard": {"xlodgenx64.exe", "xlodgen.exe"},
    "wizards.bethini.BethINIWizard": {"bethini.exe"},
    "wizards.wrye_bash.WryeBashWizard": {"wrye bash.exe"},
    "wizards.script_merger_tw3.ScriptMergerWizard": {"witcherscriptmerger.exe"},
}


def _tool_exe_names(tool: WizardTool) -> set[str]:
    """Return the lowercase exe basenames that *tool* launches, if any.

    Covers the static class→exe registry plus the parametrised xEdit family,
    whose exe name is supplied per-game via ``extra['xedit_exe']`` (with a
    ``QuickAutoClean`` variant for the QAC wizard).
    """
    path = tool.dialog_class_path
    if path in _WIZARD_CLASS_EXES:
        return _WIZARD_CLASS_EXES[path]
    if path in ("wizards.sseedit.SSEEditWizard", "wizards.sseedit.SSEEditQACWizard"):
        base = (tool.extra.get("xedit_exe") or "SSEEdit.exe")
        if path.endswith("QACWizard") and base.lower().endswith(".exe"):
            base = base[: -len(".exe")] + "QuickAutoClean.exe"
        return {base.lower()}
    if path in ("wizards.sseedit.XEditDiscordWizard",
                "wizards.sseedit.XEditDiscordQACWizard"):
        # The Discord build's QAC uses the same launcher (a -quickautoclean
        # switch), so both wizard classes map to the plain exe name.
        base = (tool.extra.get("xedit_exe") or "xTESEdit.exe")
        return {base.lower()}
    return set()


def wizard_tool_for_exe(game: BaseGame, exe_name: str) -> WizardTool | None:
    """Return the wizard tool that should open when *exe_name* is run, or None.

    *exe_name* is matched case-insensitively against the exe basenames each of
    *game*'s available wizard tools launches.  The plain xEdit wizard is
    preferred over its QuickAutoClean sibling when both could match.
    """
    target = exe_name.lower()
    qac_match: WizardTool | None = None
    for tool in get_all_wizard_tools(game):
        if target in _tool_exe_names(tool):
            if tool.dialog_class_path.endswith("QACWizard"):
                qac_match = tool
            else:
                return tool
    return qac_match


# ---------------------------------------------------------------------------
# Logging helper
# ---------------------------------------------------------------------------

def _warn(msg: str) -> None:
    """Log a plugin warning via the app log if available, otherwise print."""
    try:
        from Utils.app_log import app_log
        app_log(msg)
    except Exception:
        print(f"[plugin_loader] {msg}")
