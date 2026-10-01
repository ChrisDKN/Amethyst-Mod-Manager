from __future__ import annotations

from Utils.executables.arguments import XLODGEN_GAME_FLAGS
from Utils.wizards.payload import PayloadInstallError


NEXUS_URL = "https://www.nexusmods.com/skyrimspecialedition/mods/68518?tab=files"
XLODGEN_GITHUB_API = "https://api.github.com/repos/sheson/xLODGen/releases/latest"
TOOLS = {
    "texgen": ("TexGen", "TexGenx64.exe", "TexGen_Output", "DynDOLOD", "dyndolod", False),
    "dyndolod": ("DynDOLOD", "DynDOLODx64.exe", "DynDOLOD_Output", "DynDOLOD", "dyndolod", False),
    "xlodgen": ("xLODGen", "xLODGenx64.exe", "xLODGen_Output", "xLODGen", "xlodgen", True),
}
WIZARD_CLASSES = {
    "wizards.dyndolod.TexGenWizard": "texgen",
    "wizards.dyndolod.DynDOLODWizard": "dyndolod",
    "wizards.dyndolod.xLODGenWizard": "xlodgen",
}


def options(game, tool):
    if tool not in TOOLS:
        raise PayloadInstallError("lod_unavailable", "Choose a built-in LOD-generation tool.")
    flag = XLODGEN_GAME_FLAGS.get(game.game_id)
    if flag is None or (tool != "xlodgen" and flag not in ("-sse", "-tes5", "-tes5vr", "-enderal", "-enderalse")):
        raise PayloadInstallError("lod_unavailable", "This LOD tool does not support the selected game.")
    label, exe, output, directory, keyword, automatic = TOOLS[tool]
    source = ({"kind": "github_release", "url": XLODGEN_GITHUB_API,
               "manual_url": "https://github.com/sheson/xLODGen/releases"}
              if automatic else {"kind": "manual", "url": NEXUS_URL,
                                 "domain": "skyrimspecialedition", "mod_id": 68518})
    return {"tool": tool, "label": label, "exe_name": exe, "app_dir": directory,
            "game_flag": flag, "output_dir": output, "archive_keywords": [keyword],
            "source_tool_id": "xlodgen" if automatic else "dyndolod", "source": source}


def tool_options(game, tool):
    kind = WIZARD_CLASSES.get(tool.dialog_class_path)
    if kind is None:
        raise PayloadInstallError("lod_unavailable", "Choose a built-in LOD-generation tool.")
    return options(game, kind)
