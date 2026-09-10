"""Content-badge classification for the modlist Content column (pure, no Qt)."""
from __future__ import annotations

import fnmatch

# Badge ids. These are stable identifiers used for sorting and by the delegate;
# the human-readable labels live in BADGE_LABELS and are translated at paint
# time by the caller.
BADGE_MESHES = "meshes"
BADGE_TEXTURES = "textures"
BADGE_PBR = "pbr"
BADGE_FOMOD = "fomod"
BADGE_BAIN = "bain"
BADGE_ANIMATIONS = "animations"
BADGE_TEXT = "text"
BADGE_MEDIA = "media"
BADGE_PLUGIN = "plugin"
BADGE_ARCHIVE = "archive"
BADGE_INI = "ini"
BADGE_TOML = "toml"

# Fixed draw order, so truncation ("+N") is deterministic and two mods with the
# same content always read the same way. Install-method badges (FOMOD/BAIN) come
# last: they describe how the mod was packaged, not what it ships.
BADGE_ORDER = (
    BADGE_PLUGIN,
    BADGE_ARCHIVE,
    BADGE_MESHES,
    BADGE_TEXTURES,
    BADGE_PBR,
    BADGE_ANIMATIONS,
    BADGE_MEDIA,
    BADGE_INI,
    BADGE_TOML,
    BADGE_TEXT,
    BADGE_FOMOD,
    BADGE_BAIN,
)

# Untranslated labels; the delegate wraps each in tr() at paint time.
BADGE_LABELS = {
    BADGE_MESHES: "Meshes",
    BADGE_TEXTURES: "Textures",
    BADGE_PBR: "PBR",
    BADGE_FOMOD: "Fomod",
    BADGE_BAIN: "Bain",
    BADGE_ANIMATIONS: "Animations",
    BADGE_TEXT: "Text",
    BADGE_MEDIA: "Media",
    BADGE_PLUGIN: "Plugin",
    BADGE_ARCHIVE: "Archive",
    BADGE_INI: "INI",
    BADGE_TOML: "TOML",
}

_MESH_EXTS = frozenset({".nif"})
_TEX_EXTS = frozenset({".dds"})
# .hkx is the Havok animation container; .kf is the older Gamebryo keyframe set.
_ANIM_EXTS = frozenset({".kf", ".hkx"})
_INI_EXTS = frozenset({".ini"})
_TOML_EXTS = frozenset({".toml"})
_TEXT_EXTS = frozenset({
    ".txt", ".json", ".xml", ".md", ".cfg", ".yaml", ".yml", ".log", ".psc",
})
_MEDIA_EXTS = frozenset({
    ".png", ".jpg", ".jpeg", ".bmp", ".tga", ".webp", ".gif",
    ".bik", ".mp4", ".webm", ".avi", ".wmv",
})

# Fallback extension sets, used only when the game exposes no plugin/archive
# extensions of its own. The live values come from game.plugin_extensions /
# game.archive_extensions via the filegraph's FLAG_PLUGIN / FLAG_ARCHIVE, which
# is what makes .omwscripts, BG3 .pak, UE5 .utoc and Cyberpunk .archive work.
_PLUGIN_EXTS = frozenset({".esp", ".esm", ".esl", ".omwscripts", ".omwaddon"})
_ARCHIVE_EXTS = frozenset({".bsa", ".ba2", ".pak", ".utoc", ".archive"})

# Extension → badge for the purely extension-driven badges. Plugin and Archive
# are deliberately absent: those are game-driven (see compute_badges).
_EXT_BADGES: dict[str, str] = {}
for _exts, _badge in (
    (_MESH_EXTS, BADGE_MESHES),
    (_TEX_EXTS, BADGE_TEXTURES),
    (_ANIM_EXTS, BADGE_ANIMATIONS),
    (_TEXT_EXTS, BADGE_TEXT),
    (_INI_EXTS, BADGE_INI),
    (_TOML_EXTS, BADGE_TOML),
    (_MEDIA_EXTS, BADGE_MEDIA),
):
    for _ext in _exts:
        _EXT_BADGES[_ext] = _badge

# Every extension a badge can be derived from. Passed to the filegraph's
# asset_copies() extension filter so only badge-relevant rows cross the FFI
# boundary when resolving which content came out of an archive.
BADGE_EXTENSIONS = frozenset(_EXT_BADGES) | _PLUGIN_EXTS | _ARCHIVE_EXTS


def ignored_filename(filename: str, patterns) -> bool:
    """True when *filename* matches one of the game's conflict-ignore globs.

    Those patterns (e.g. ``*read*.txt``, ``*.md``, ``LICENSE``) mark files the
    game drops from the filemap entirely - documentation and metadata many mods
    ship. They describe nothing about what the mod actually contains, so they
    must not raise a content badge either. Patterns are basename globs and are
    matched case-insensitively, the same way the filegraph applies them.
    """
    if not patterns:
        return False
    name = filename.rsplit("/", 1)[-1].lower()
    return any(fnmatch.fnmatchcase(name, str(pattern).lower())
               for pattern in patterns)


def extensions_from_paths(paths, ignore_patterns=()) -> set[str]:
    """Badge-relevant extensions from an iterable of mod-relative paths,
    skipping any whose filename matches *ignore_patterns*."""
    out = set()
    for path in paths:
        name = str(path).replace("\\", "/").rsplit("/", 1)[-1].lower()
        if not name or ignored_filename(name, ignore_patterns):
            continue
        dot = name.rfind(".")
        if dot <= 0:
            continue
        ext = name[dot:]
        if ext in BADGE_EXTENSIONS:
            out.add(ext)
    return out


def _badges_for_exts(exts) -> set[str]:
    """The extension-driven badges implied by a set of file extensions."""
    out = set()
    for ext in exts:
        badge = _EXT_BADGES.get(ext)
        if badge is not None:
            out.add(badge)
    return out


def compute_badges(
    mod_names,
    mod_filetypes,
    pbr_mods=(),
    plugin_mods=(),
    archive_mods=(),
    fomod_mods=(),
    bain_mods=(),
    archive_sourced=None,
) -> dict[str, tuple[tuple[str, bool], ...]]:
    """Map each mod name to its ordered badges as (badge_id, from_archive).

    *mod_filetypes* is the filegraph's mod -> {extension} facet. *plugin_mods*
    and *archive_mods* are the game-driven FLAG_PLUGIN / FLAG_ARCHIVE sets; when
    a game exposes neither, the extension fallbacks stand in. *archive_sourced*
    maps a mod to the extensions it ships from inside an archive, which only
    changes a badge's colour, never whether it appears.
    """
    pbr_mods = set(pbr_mods or ())
    plugin_mods = set(plugin_mods or ())
    archive_mods = set(archive_mods or ())
    fomod_mods = set(fomod_mods or ())
    bain_mods = set(bain_mods or ())
    archive_sourced = archive_sourced or {}

    result: dict[str, tuple[tuple[str, bool], ...]] = {}
    for name in mod_names:
        exts = set(mod_filetypes.get(name, ()) or ())
        packed = set(archive_sourced.get(name, ()) or ())
        # Files inside an archive are content the mod ships even when the loose
        # facet never saw them, so they contribute badges as well as colour.
        badges = _badges_for_exts(exts | packed)

        if name in plugin_mods or (exts & _PLUGIN_EXTS):
            badges.add(BADGE_PLUGIN)
        if name in archive_mods or (exts & _ARCHIVE_EXTS):
            badges.add(BADGE_ARCHIVE)
        if name in pbr_mods:
            badges.add(BADGE_PBR)
        if name in fomod_mods:
            badges.add(BADGE_FOMOD)
        if name in bain_mods:
            badges.add(BADGE_BAIN)
        if not badges:
            continue

        # A badge reads as archive-sourced only when every extension backing it
        # came out of an archive - content shipped loose is the headline, so
        # loose wins a tie. The Archive badge itself is about the container and
        # is always drawn in the loose tone.
        loose_badges = _badges_for_exts(exts)
        packed_badges = _badges_for_exts(packed)
        result[name] = tuple(
            (badge, badge in packed_badges and badge not in loose_badges)
            for badge in BADGE_ORDER if badge in badges
        )
    return result


def badge_signature(badges) -> tuple:
    """Sort key: richest mods first, then a stable ordering by badge identity.

    Mods with no detected content sort last regardless of direction, matching
    how the Category/Author columns keep unknowns out of the way.
    """
    ids = tuple(b[0] for b in (badges or ()))
    if not ids:
        return (1, 0, ())
    return (0, -len(ids), tuple(BADGE_ORDER.index(b) for b in ids))
