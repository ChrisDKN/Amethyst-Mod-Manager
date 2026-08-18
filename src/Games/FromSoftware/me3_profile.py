"""
me3_profile.py
ModProfile (.me3) generation for FROMSOFTWARE games driven by the me3 loader.

FROMSOFTWARE engines read their assets out of encrypted DVDBND archives
(.bhd/.bdt), never from loose files on disk, so nothing a mod ships can be
deployed into the game folder the way every other Amethyst handler works.  The
me3 loader injects a DLL that hooks the archive layer and serves overrides from
an in-memory VFS instead.  Its mod list is a TOML file - so "deploying" for
these games means writing a .me3 profile that points at the staged mod folders.

Two things about the format drive the code here:

  - Every path in a .me3 is resolved RELATIVE TO THE .me3 FILE ITSELF.  We
    write the profile as a sibling of mods/, so each entry is a tidy
    "mods/<ModName>/..." and the profile survives the staging tree being moved.

  - me3 is LAST-WINS.  VfsOverrideMapping.scan_directories() walks packages in
    order and inserts each file into a HashMap, so a later package overwrites an
    earlier one's entry for the same path.  Amethyst is the opposite: modlist
    index 0 is the HIGHEST priority.  build_packages() therefore emits packages
    in REVERSED modlist order, and pins the result with an explicit load_after
    chain (older me3 builds had no deterministic order without those keys).
    Getting this backwards silently inverts every conflict, and it is invisible
    anywhere outside the running game.

Mods come in two flavours, and one mod can be both:
  [[packages]]  a directory of asset overrides mirroring the DVDBND layout
  [[natives]]   a .dll to load into the process
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# Top-level directory names that appear inside the DVDBND.  A folder carrying
# one of these is an asset package, which also makes this the marker set used to
# find the real mod root inside an archive that nested it a few levels deep.
ACCEPTABLE_FOLDERS = frozenset({
    "_backup", "_unknown", "action", "asset", "chr", "cutscene", "event",
    "font", "map", "material", "menu", "movie", "msg", "other", "param",
    "parts", "script", "sd", "sfx", "shader", "sound",
})

# A loose regulation.bin (the game's param bundle) is a package on its own.
_PACKAGE_MARKER_FILES = frozenset({"regulation.bin"})

# Support libraries and rival loaders that must never be loaded as a mod: they
# are either dependencies other DLLs pull in themselves, or - for me3's own host
# and ModEngine2 - a second loader fighting the one that is running.
IGNORED_DLLS = frozenset({
    "modengine2.dll", "mod_loader.dll", "lua.dll",
    "zlib1.dll", "hooklibraryx64.dll", "minhook.x64.dll", "me3_mod_host.dll",
})

# Proxy loaders: DLLs named after a system library so the GAME loads them via
# its import table (Elden Mod Loader ships as dinput8.dll).  me3 cannot load one
# as a [[natives]] entry - it has to sit next to the exe for the game's import
# table to pick it up - so these are deployed into the game folder instead, and
# the DLL mods they own go with them (see the handler's EML support).
PROXY_LOADER_DLLS = frozenset({
    "dinput8.dll", "dxgi.dll", "d3d11.dll", "winmm.dll", "version.dll",
})

# EML's own config, shipped beside its dinput8.dll.
PROXY_LOADER_SIDECARS = frozenset({"mod_loader_config.ini"})

# Folder EML loads its DLL mods from, relative to the game executable.
PROXY_LOADER_MODS_DIR = "mods"

# How far down to look for a package root.  Nexus archives nest a level or two
# ("MyMod-1234-1-0/MyMod/parts/"); anything deeper is not a layout convention.
_MAX_ROOT_DEPTH = 4

PROFILE_VERSION = "v1"

# Kinds returned by classify_mod().
KIND_PACKAGE = "package"
KIND_NATIVE = "native"
KIND_ME3_PROFILE = "me3_profile"
KIND_UNKNOWN = "unknown"


@dataclass
class Me3ModEntry:
    """What one staged mod folder contributes to a .me3 profile."""

    name: str
    kind: str
    package_root: "Path | None" = None
    natives: list[Path] = field(default_factory=list)
    # Proxy-loader DLLs found in the mod (see PROXY_LOADER_DLLS) - reported to
    # the user, never emitted.
    proxies: list[Path] = field(default_factory=list)
    # A .me3 the mod shipped: we cannot merge another profile, so it is
    # reported while the mod's own assets/DLLs still load.
    bundled_profile: "Path | None" = None
    # True when Mod Files has per-file exclusions the .me3 format cannot
    # express (a package is a whole directory).
    has_unexpressable_exclusions: bool = False

    @property
    def loadable(self) -> bool:
        """Whether this entry contributes anything to the profile."""
        return self.package_root is not None or bool(self.natives)


def _iter_children(folder: Path) -> list[Path]:
    """Return folder's direct children, or [] if it cannot be read."""
    try:
        return sorted(folder.iterdir())
    except OSError:
        return []


def _is_package_dir(folder: Path) -> bool:
    """Whether folder holds DVDBND asset dirs or a package marker file."""
    for child in _iter_children(folder):
        if child.is_dir() and child.name.lower() in ACCEPTABLE_FOLDERS:
            return True
        if child.is_file() and child.name.lower() in _PACKAGE_MARKER_FILES:
            return True
    return False


def find_package_root(mod_dir: Path) -> "Path | None":
    """Return the shallowest folder under mod_dir holding DVDBND assets."""
    # Breadth-first so "MyMod-1234/MyMod/parts/" resolves to ".../MyMod" and not
    # to some deeper folder that happens to carry a matching name.
    level = [mod_dir]
    for _ in range(_MAX_ROOT_DEPTH):
        if not level:
            return None
        for folder in level:
            if _is_package_dir(folder):
                return folder
        level = [c for f in level for c in _iter_children(f) if c.is_dir()]
    return None


def _rel_key(path: Path, mod_dir: Path) -> str:
    """Lowercase mod-relative key, matching Mod Files' exclusion keys."""
    try:
        return path.relative_to(mod_dir).as_posix().lower()
    except ValueError:
        return path.name.lower()


def _mod_natives(mod_dir: Path,
                 excluded: "set[str] | None" = None
                 ) -> "tuple[list[Path], list[Path]]":
    """Return (loadable DLLs, proxy-loader DLLs) under mod_dir.

    Support libraries and me3's own host are dropped outright; proxy loaders are
    returned separately so the caller can tell the user rather than silently
    losing the mod. Files disabled in Mod Files are honoured here.
    """
    try:
        found = sorted(p for p in mod_dir.rglob("*.dll") if p.is_file())
    except OSError:
        return [], []
    natives: list[Path] = []
    proxies: list[Path] = []
    for p in found:
        if excluded and _rel_key(p, mod_dir) in excluded:
            continue
        low = p.name.lower()
        if low in PROXY_LOADER_DLLS:
            proxies.append(p)
        elif low not in IGNORED_DLLS:
            natives.append(p)
    return natives, proxies


def classify_mod(mod_dir: Path, name: "str | None" = None,
                 excluded: "set[str] | None" = None) -> Me3ModEntry:
    """Classify a staged mod folder into packages and/or native DLLs."""
    entry_name = name if name is not None else mod_dir.name

    natives, proxies = _mod_natives(mod_dir, excluded)
    package_root = find_package_root(mod_dir)

    # A mod that ships its own .me3 is a profile/modpack. We cannot merge
    # another profile into ours, but its assets and DLLs are still real mod
    # content - load those and flag the bundled profile rather than dropping
    # the whole mod on the floor.
    bundled = None
    try:
        bundled = next(mod_dir.rglob("*.me3"), None)
    except OSError:
        pass

    if package_root is not None:
        # Both is common - Seamless Co-op ships a DLL alongside its assets - so
        # emit the package AND every native rather than forcing one kind.
        kind = KIND_PACKAGE
    elif natives:
        kind = KIND_NATIVE
    elif bundled is not None or proxies:
        # Nothing loadable, but there IS content worth explaining.
        kind = KIND_ME3_PROFILE if bundled is not None else KIND_UNKNOWN
    else:
        kind = KIND_UNKNOWN
    return Me3ModEntry(entry_name, kind, package_root, natives,
                       proxies=proxies, bundled_profile=bundled)


def _sanitize_id(name: str, taken: set[str]) -> str:
    """Return a TOML-safe, unique package id derived from a mod name."""
    ident = re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_") or "mod"
    if ident not in taken:
        taken.add(ident)
        return ident
    n = 2
    while f"{ident}_{n}" in taken:
        n += 1
    ident = f"{ident}_{n}"
    taken.add(ident)
    return ident


def _rel_path(path: Path, profile_dir: Path) -> str:
    """Return path relative to the .me3 location, with forward slashes."""
    try:
        rel = path.relative_to(profile_dir)
    except ValueError:
        # Staging outside the profile dir (a user-set staging path) can't be
        # expressed relatively - me3 accepts absolute paths too.
        return path.as_posix()
    return rel.as_posix()


def build_entries(mod_dirs: list[tuple[str, Path]],
                  excluded_by_mod: "dict[str, set[str]] | None" = None
                  ) -> list[Me3ModEntry]:
    """Classify staged mods in modlist order.

    Every existing mod folder yields an entry, including ones with nothing
    loadable: the caller reports those instead of dropping them silently.
    *excluded_by_mod* carries Mod Files' per-file exclusions.
    """
    entries: list[Me3ModEntry] = []
    for name, mod_dir in mod_dirs:
        if not mod_dir.is_dir():
            continue
        excluded = (excluded_by_mod or {}).get(name)
        entry = classify_mod(mod_dir, name, excluded)
        # A package is a whole directory in the .me3 format, so an exclusion
        # inside one cannot be expressed - flag it rather than pretend.
        if excluded and entry.package_root is not None:
            entry.has_unexpressable_exclusions = True
        entries.append(entry)
    return entries


def _toml_str(value: str) -> str:
    r"""Return value as a TOML basic string, escaping \ and " only."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def render_profile(entries: list[Me3ModEntry], *, game: str, profile_dir: Path,
                   savefile: "str | None" = None, start_online: bool = False,
                   disable_arxan: bool = True, mem_patch: bool = False,
                   mem_patch_heap_size: "int | None" = None) -> str:
    """Render a profileVersion v1 .me3 document for entries.

    Packages are emitted lowest-priority first because me3 is last-wins while
    the incoming list is in Amethyst order (index 0 = highest priority).
    """
    lines: list[str] = [
        "# Generated by Amethyst - edits are overwritten on the next deploy.",
        f"profileVersion = {_toml_str(PROFILE_VERSION)}",
    ]

    if savefile:
        lines.append(f"savefile = {_toml_str(savefile)}")
    lines.append(f"start_online = {'true' if start_online else 'false'}")
    lines.append(f"disable_arxan = {'true' if disable_arxan else 'false'}")
    # ALWAYS emit mem_patch, never omit it when false. me3 maps an absent value
    # to no_mem_patch=None and then computes `mem_patch = !no_mem_patch
    # .unwrap_or(false)`, i.e. omitted means ENABLED - so leaving it out when
    # the user turned it off would silently switch it on.
    lines.append(f"mem_patch = {'true' if mem_patch else 'false'}")
    if mem_patch and mem_patch_heap_size:
        lines.append(f"mem_patch_heap_size = {int(mem_patch_heap_size)}")

    lines += ["", "[[supports]]", f"game = {_toml_str(game)}"]

    # Reverse into me3's precedence: highest priority scanned last so its files
    # win the HashMap insert. Entries with nothing loadable are reported by the
    # caller, not written here.
    packages = [e for e in entries if e.package_root is not None]
    taken: set[str] = set()
    ids = {id(e): _sanitize_id(e.name, taken) for e in packages}

    previous_id: str | None = None
    for entry in reversed(packages):
        pkg_id = ids[id(entry)]
        lines += [
            "",
            "[[packages]]",
            f"id = {_toml_str(pkg_id)}",
            f"path = {_toml_str(_rel_path(entry.package_root, profile_dir))}",
        ]
        if previous_id is not None:
            # Belt and braces: array order alone was not deterministic in older
            # me3 builds, so chain each package behind the previous one.
            lines.append(
                f"load_after = [{{ id = {_toml_str(previous_id)}, "
                "optional = false }]"
            )
        previous_id = pkg_id

    # Natives stay in modlist order; me3 loads them all and DLL precedence is a
    # runtime concern rather than a file-override one.
    for entry in entries:
        for dll in entry.natives:
            lines += [
                "",
                "[[natives]]",
                f"path = {_toml_str(_rel_path(dll, profile_dir))}",
            ]

    return "\n".join(lines) + "\n"
