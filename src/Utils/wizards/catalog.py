"""
GUI-neutral wizard-tool category grouping.

Shared by the Tk wizard picker (gui/wizard_dialog.py) and the Qt Wizards
header menu.  A tool may declare its own ``category`` on its WizardTool; if
it doesn't, one is inferred from its id below.  ``CATEGORY_ORDER`` fixes the
display order of the headers; anything not listed falls under "Other" at the
bottom.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from Games.base_game import WizardTool

CATEGORY_ORDER = [
    "Setup and Installers",
    "Install Modlist",
    "Body and Outfits",
    "Animation and Physics",
    "DynDOLOD",
    "RSuite (experimental)",
    "Patchers and Cleanup",
    "xEdit",
    "Load Order and Config",
    "INI Tweaks",
    "NIF Viewer",
    "Other",
]

# Ordered (substring-in-id, category) rules.  First match wins.  ids are the
# stable machine keys (e.g. "run_dyndolod_skyrimse") so these survive label
# wording changes.
_CATEGORY_RULES: list[tuple[tuple[str, ...], str]] = [
    # Body and outfits
    (("bodyslide", "outfitstudio", "outfit_studio"), "Body and Outfits"),
    # Animation and physics
    (("pandora",), "Animation and Physics"),
    # RSuite (experimental) - checked before LOD so these don't fall into it
    (("vramr", "bendr", "parallaxr"), "RSuite (experimental)"),
    # DynDOLOD (LOD & textures)
    (("texgen", "dyndolod", "xlodgen"), "DynDOLOD"),
    # xEdit
    #   xEdit ships under many build names (SSEEdit, FO4Edit, FNVEdit, TES5Edit,
    #   SF1Edit, …) whose wizard ids share the "<build>edit_<suffix>" shape, so
    #   match the generic "edit_" infix to catch the whole family - not just
    #   SSEEdit.  Trailing "_" keeps it from matching unrelated ids like
    #   "editor"/"credits".  Checked before Patchers so the xEdit family lands
    #   in its own category for every game.
    (("edit_",), "xEdit"),
    # Patchers and cleanup
    (("pgpatcher", "eslifier", "skygen", "plugin_audit",
      "script_merger", "gpak"), "Patchers and Cleanup"),
    # Load order and config
    (("wrye_bash", "bethini"), "Load Order and Config"),
    # Setup and installers (script extenders, downgraders, patches, framework installs)
    (("install_se", "install_reshade", "install_bepinex", "install_mgexe",
      "install_mcp", "downgrade", "4gb_patch", "dtkit", "_patch"),
     "Setup and Installers"),
]


def infer_category(tool: "WizardTool") -> str:
    """Return the display category for *tool* (explicit if set, else inferred)."""
    if getattr(tool, "category", ""):
        return tool.category
    key = (tool.id or "").lower()
    for needles, cat in _CATEGORY_RULES:
        if any(n in key for n in needles):
            return cat
    return "Other"


def group_by_category(tools: list["WizardTool"]) -> list[tuple[str, list["WizardTool"]]]:
    """Group *tools* into ``[(category, [tools…]), …]`` in CATEGORY_ORDER.

    Tools within a category keep their incoming (alphabetical) order.  Empty
    categories are omitted; unknown categories are appended after the known
    ones, before falling through to "Other".
    """
    buckets: dict[str, list["WizardTool"]] = {}
    for tool in tools:
        buckets.setdefault(infer_category(tool), []).append(tool)

    order = list(CATEGORY_ORDER)
    for cat in buckets:
        if cat not in order:
            order.insert(len(order) - 1, cat)  # before trailing "Other"

    return [(cat, buckets[cat]) for cat in order if buckets.get(cat)]
