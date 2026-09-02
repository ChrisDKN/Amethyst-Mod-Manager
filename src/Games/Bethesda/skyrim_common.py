"""Shared mod-install rules for Skyrim and Enderal handlers."""


# These games share Skyrim's Data-folder layout.  Keep the recognised install
# roots in one place so Enderal does not fall back to Fallout_3's broader list
# (which includes unrelated roots such as f4se, nvse, sfse, and config).
SKYRIM_MOD_REQUIRED_TOP_LEVEL_FOLDERS: frozenset[str] = frozenset({
    "skse",
    "textures",
    "sound",
    "meshes",
    "mcm",
    "scripts",
    "interface",
    "lightplacer",
    "mapmarkers",
    "music",
    "nemesis_engine",
    "seq",
    "shadercache",
    "shaders",
    "grass",
    "video",
    "source",
    "calientetools",
    "data",
    "PBRNifPatcher",
    "PBRTextureSets",
    "distantlod",
    "fonts",
    "facegen",
    "menus",
    "lodsettings",
    "lsdata",
    "strings",
    "trees",
    "asi",
    "tools",
    "enbseries",
    "reshade-shaders",
})
