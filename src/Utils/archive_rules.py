"""
archive_rules.py
Per-game allowlists for what can go inside a BSA / BA2.

Faithful Python port of bethutil's ``Settings::get(Game)`` allowlists
(see https://github.com/Guekka/bethutil ``include/btu/bsa/settings.hpp``).
We use bethutil's curated allowlist instead of a blocklist because:

  * A blocklist always misses something - bethutil's comments call out
    ``.kf, .mp3, .ini, .txt, .json`` as "purposefully missing" for TES4
    even though some of those look harmless at first glance.
  * The engine only loads specific extension / directory combinations
    from inside an archive.  Files outside the allowlist load fine as
    loose, but if you stuff them into a BSA the engine ignores them
    silently - wasting space at best, breaking the mod at worst (the
    .bk2 video case that prompted this rewrite).

Rule shape:

    (extension, frozenset(first_directory_segments))

A file matches the rule iff:

  1. Its extension (case-insensitive, including the leading dot) equals
     ``extension``.
  2. Its first relative-path segment (lowercased) is in the directory
     set.  The literal sentinel ``"root"`` matches files at the mod
     root (no parent directory).

A file is **packable** when at least one rule across the standard /
texture / incompressible lists for its game matches.  Anything else is
blacklisted (= left loose; not an error).

The "incompressible" list is informational at the moment - bsa_writer's
existing per-extension incompressible bit handling already covers
``.wav / .mp3 / .ogg / .flac / .xwm / .fuz / .lip / .*strings``, which
is a superset of what bethutil flags incompressible.

References:
  * https://github.com/Guekka/bethutil  (settings.hpp, MIT license)
  * tests against vanilla Bethesda BSA / BA2 contents on disk.
"""

from __future__ import annotations


# A single allow rule: (lowercase_extension_with_dot, allowed_first_dirs).
# The ``"root"`` sentinel (singular, lowercase) means "file at the mod
# folder root".  Use ``frozenset`` for O(1) membership.
Rule = tuple[str, frozenset[str]]


# ---------------------------------------------------------------------------
# Skyrim Special Edition / Skyrim VR / Enderal SE - bethutil's tes5_default_sets
# ---------------------------------------------------------------------------

_SSE_STANDARD: tuple[Rule, ...] = (
    (".bgem",         frozenset({"materials"})),
    (".bgsm",         frozenset({"materials"})),
    (".bto",          frozenset({"meshes"})),
    (".btr",          frozenset({"meshes"})),
    (".btt",          frozenset({"meshes"})),
    (".cgid",         frozenset({"grass"})),
    (".dlodsettings", frozenset({"lodsettings"})),
    (".dtl",          frozenset({"meshes"})),
    (".egm",          frozenset({"meshes"})),
    (".jpg",          frozenset({"root"})),
    (".hkb",          frozenset({"meshes"})),
    (".hkx",          frozenset({"meshes"})),
    (".lst",          frozenset({"meshes"})),
    (".nif",          frozenset({"meshes"})),
    (".psc",          frozenset({"scripts", "source"})),
    (".tga",          frozenset({"textures"})),
    (".tri",          frozenset({"meshes"})),
)

_SSE_TEXTURE: tuple[Rule, ...] = (
    (".dds", frozenset({"textures", "interface"})),
    (".png", frozenset({"textures"})),
)

_SSE_INCOMPRESSIBLE: tuple[Rule, ...] = (
    (".dlstrings",   frozenset({"strings"})),
    (".fuz",         frozenset({"sound"})),
    (".fxp",         frozenset({"shadersfx"})),
    (".gid",         frozenset({"grass"})),
    (".gfx",         frozenset({"interface"})),
    (".hkc",         frozenset({"meshes"})),
    (".hkt",         frozenset({"meshes"})),
    (".hkp",         frozenset({"meshes"})),
    (".ilstrings",   frozenset({"strings"})),
    (".ini",         frozenset({"meshes"})),
    (".lip",         frozenset({"sound"})),
    (".lnk",         frozenset({"grass"})),
    (".lod",         frozenset({"lodsettings"})),
    (".ogg",         frozenset({"sound"})),
    (".pex",         frozenset({"scripts"})),
    (".seq",         frozenset({"seq"})),
    (".strings",     frozenset({"strings"})),
    (".swf",         frozenset({"interface"})),
    (".txt",         frozenset({"interface", "meshes", "scripts"})),
    (".wav",         frozenset({"sound"})),
    (".xml",         frozenset({"dialogueviews"})),
    (".xwm",         frozenset({"music", "sound"})),
)


# ---------------------------------------------------------------------------
# Fallout 4 / Fallout 4 VR - bethutil's sets_fo4 (SSE + .png demoted to
# standard, .uvd added to standard, .dds restricted to textures+interface).
# ---------------------------------------------------------------------------

_FO4_STANDARD: tuple[Rule, ...] = _SSE_STANDARD + (
    (".png", frozenset({"textures"})),
    (".uvd", frozenset({"vis"})),
)

_FO4_TEXTURE: tuple[Rule, ...] = (
    (".dds", frozenset({"textures", "interface"})),
)

_FO4_INCOMPRESSIBLE: tuple[Rule, ...] = _SSE_INCOMPRESSIBLE


# ---------------------------------------------------------------------------
# Skyrim Legendary Edition - bethutil's sets_sle (= SSE without the
# texture-version split; same allowlists).
# ---------------------------------------------------------------------------

_SLE_STANDARD       = _SSE_STANDARD
_SLE_TEXTURE        = _SSE_TEXTURE
_SLE_INCOMPRESSIBLE = _SSE_INCOMPRESSIBLE


# ---------------------------------------------------------------------------
# Oblivion (TES4) - bethutil's tes4_default_sets.  Comment in upstream:
# "Purposefully missing: .kf, .mp3, .ini, .txt, .json".
# ---------------------------------------------------------------------------

_TES4_STANDARD: tuple[Rule, ...] = (
    (".nif", frozenset({"meshes"})),
    (".egm", frozenset({"meshes"})),
    (".egt", frozenset({"meshes"})),
    (".tri", frozenset({"meshes"})),
    (".cmp", frozenset({"meshes"})),
    (".lst", frozenset({"meshes"})),
    (".dtl", frozenset({"meshes"})),
    (".spt", frozenset({"trees"})),
)

_TES4_TEXTURE: tuple[Rule, ...] = (
    (".dds", frozenset({"textures"})),
    (".tai", frozenset({"textures"})),
    (".tga", frozenset({"textures"})),
    (".bmp", frozenset({"textures"})),
    (".fnt", frozenset({"textures"})),
    (".tex", frozenset({"textures"})),
)

_TES4_INCOMPRESSIBLE: tuple[Rule, ...] = (
    (".wav",          frozenset({"sound"})),
    (".ogg",          frozenset({"sound"})),
    (".lip",          frozenset({"sound"})),
    (".txt",          frozenset({"menus"})),
    (".vso",          frozenset({"shaders"})),
    (".pso",          frozenset({"shaders"})),
    (".vsh",          frozenset({"shaders"})),
    (".psh",          frozenset({"shaders"})),
    (".lsl",          frozenset({"shaders"})),
    (".h",            frozenset({"shaders"})),
    (".dat",          frozenset({"lsdata"})),
    (".dlodsettings", frozenset({"lodsettings"})),
    (".ctl",          frozenset({"facegen"})),
)


# ---------------------------------------------------------------------------
# Fallout 3 / FNV - bethutil's sets_fnv (= TES4 + tes5 archive version
# tweak; same allowlists).
# ---------------------------------------------------------------------------

_FNV_STANDARD       = _TES4_STANDARD
_FNV_TEXTURE        = _TES4_TEXTURE
_FNV_INCOMPRESSIBLE = _TES4_INCOMPRESSIBLE


# ---------------------------------------------------------------------------
# Game-id → (standard, texture, incompressible) lookup.
#
# Game IDs come from each handler's ``game_id`` property in src/Games/.
# Games we don't yet support (Starfield, Fallout 76, Morrowind v103,
# non-Bethesda) aren't in this map - the helpers below fall through to
# "no rules", which the writer will treat as "no packable files" and
# refuse the operation.
# ---------------------------------------------------------------------------

_GAME_RULES: dict[str, tuple[tuple[Rule, ...], tuple[Rule, ...], tuple[Rule, ...]]] = {
    # Skyrim SE engine family
    "skyrim_se":  (_SSE_STANDARD, _SSE_TEXTURE, _SSE_INCOMPRESSIBLE),
    "skyrimvr":   (_SSE_STANDARD, _SSE_TEXTURE, _SSE_INCOMPRESSIBLE),
    "enderalse":  (_SSE_STANDARD, _SSE_TEXTURE, _SSE_INCOMPRESSIBLE),
    # Skyrim LE
    "skyrim":     (_SLE_STANDARD, _SLE_TEXTURE, _SLE_INCOMPRESSIBLE),
    "enderal":    (_SLE_STANDARD, _SLE_TEXTURE, _SLE_INCOMPRESSIBLE),
    # Oblivion
    "Oblivion":   (_TES4_STANDARD, _TES4_TEXTURE, _TES4_INCOMPRESSIBLE),
    # Fallout 3 / NV
    "Fallout3":     (_FNV_STANDARD, _FNV_TEXTURE, _FNV_INCOMPRESSIBLE),
    "Fallout3GOTY": (_FNV_STANDARD, _FNV_TEXTURE, _FNV_INCOMPRESSIBLE),
    "FalloutNV":    (_FNV_STANDARD, _FNV_TEXTURE, _FNV_INCOMPRESSIBLE),
    # Fallout 4 family
    "Fallout4":   (_FO4_STANDARD, _FO4_TEXTURE, _FO4_INCOMPRESSIBLE),
    "Fallout4VR": (_FO4_STANDARD, _FO4_TEXTURE, _FO4_INCOMPRESSIBLE),
}


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def _file_first_dir(rel_path: str) -> str:
    """Return the first relative-path segment, lowercased, or ``"root"``
    for files at the mod root.

    *rel_path* is forward-slash, any case (the writer's ``_collect_files``
    produces lowercase forward-slash keys but ``is_packable`` should
    not assume that)."""
    rel_path = rel_path.replace("\\", "/")
    if "/" not in rel_path:
        return "root"
    return rel_path.split("/", 1)[0].lower()


def _ext_lower(rel_path: str) -> str:
    name = rel_path.rsplit("/", 1)[-1]
    dot = name.rfind(".")
    if dot < 0:
        return ""
    return name[dot:].lower()


def texture_extensions_for_game(game_id: str | None) -> frozenset[str]:
    """Return the set of file extensions (with leading dot, lowercase)
    that classify as "texture" for *game_id* - i.e. files that should
    end up in the ``- Textures.bsa`` / ``- Textures.ba2`` sibling when
    the user asks for a separate textures archive.

    Empty set when *game_id* is unknown or has no texture rules.
    """
    if game_id is None or game_id not in _GAME_RULES:
        return frozenset()
    _, texture_rules, _ = _GAME_RULES[game_id]
    return frozenset(rule_ext for rule_ext, _ in texture_rules)


def is_packable(rel_path: str, game_id: str | None) -> bool:
    """Return True if *rel_path* should be packed for *game_id*.

    Falls back to a permissive "True for any non-dotfile" if the game is
    unknown - the Pack BSA / Pack BA2 buttons gate on a known game ID
    anyway, so this branch is defensive.

    Always-excluded regardless of allowlist:
      * Plugins (``.esp / .esl / .esm``) - must remain loose.
      * Nested archives (``.bsa / .ba2``).
      * Dotfiles and known mod-manager metadata (``meta.ini`` etc.).
      * SKSE / F4SE plugins (``.dll``) - script extenders refuse to
        load DLLs from inside an archive.
    """
    name = rel_path.rsplit("/", 1)[-1]
    if not name or name.startswith("."):
        return False
    lower = name.lower()
    if lower in _ALWAYS_EXCLUDE_NAME:
        return False
    ext = _ext_lower(rel_path)
    if ext in _ALWAYS_EXCLUDE_EXT:
        return False

    if game_id is None or game_id not in _GAME_RULES:
        # Unknown game - be permissive.  In practice the GUI never
        # invokes Pack on an unknown game, but if a future game is
        # wired up before we add its allowlist we'd rather pack a
        # superset than nothing.
        return True

    standard, texture, incompressible = _GAME_RULES[game_id]
    first_dir = _file_first_dir(rel_path)
    for rule_list in (standard, texture, incompressible):
        for rule_ext, rule_dirs in rule_list:
            if rule_ext == ext and first_dir in rule_dirs:
                return True
    return False


# Always-excluded extensions / names - applied even before the allowlist
# check, to short-circuit the engine-format files (plugins, nested
# archives, SKSE DLLs) and obvious mod-manager metadata.
_ALWAYS_EXCLUDE_EXT: frozenset[str] = frozenset({
    ".esp", ".esl", ".esm",   # Plugins live in plugins.txt, not in archives.
    ".bsa", ".ba2",           # Engine cannot mount nested archives.
    ".dll",                   # Script-extender plugins must be loose.
    ".exe", ".bat", ".cmd",   # No reason for these to be in an archive.
    ".sh", ".lnk", ".url",
})

_ALWAYS_EXCLUDE_NAME: frozenset[str] = frozenset({
    "meta.ini", "info.xml", "modinfo.json", "mod.json",
})


__all__ = [
    "is_packable",
    "texture_extensions_for_game",
]
