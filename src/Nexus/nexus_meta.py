"""
nexus_meta.py
Read, write, and query Nexus Mods metadata stored in each mod's ``meta.ini``.

The ``meta.ini`` file lives at the root of every mod staging folder and uses
the MO2 ``[General]`` section format::

    [General]
    gameName=skyrimspecialedition
    modid=2014
    fileid=1234
    version=1.5.97
    installationFile=SkyUI_5_2_SE-12604-5-2SE.7z
    installed=2026-02-22T10:30:00
    nexusFileStatus=1

This module provides:

- **NexusModMeta** - data class holding per-mod Nexus info
- **read_meta** / **write_meta** - I/O helpers (non-destructive: preserves
  existing ``meta.ini`` content when writing)
- **scan_installed_mods** - walk a staging root and collect all mods that
  have Nexus metadata (``modid`` > 0)
"""

from __future__ import annotations

import configparser
import copy
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from Utils.app_log import app_log
from Utils.atomic_write import atomic_writer
from Utils.mods.metadata import locked_meta_write


@dataclass
class NexusModMeta:
    """Nexus metadata for a single installed mod."""

    mod_name: str = ""                 # folder name in staging
    game_domain: str = ""              # e.g. "skyrimspecialedition"
    mod_id: int = 0                    # Nexus mod ID
    file_id: int = 0                   # Nexus file ID
    version: str = ""                  # mod version string
    author: str = ""                   # mod author
    uploaded_by: str = ""              # Nexus username that uploaded the mod
    nexus_name: str = ""               # mod name on Nexus (may differ from folder)
    nexus_file_name: str = ""          # per-file display name on Nexus (file_details.name, e.g. "Engine Fixes - Main File")
    installation_file: str = ""        # original archive filename
    file_size: int = 0                 # original archive size in bytes
    installed: str = ""                # ISO-8601 timestamp
    nexus_url: str = ""                # full Nexus mod page URL
    description: str = ""              # short summary
    category_id: int = 0               # Nexus category
    category_name: str = ""            # Category display name (e.g. Armor, Weapons)
    file_category: str = ""            # MAIN / UPDATE / OPTIONAL / etc
    endorsed: bool = False             # whether the user has endorsed this mod
    latest_file_id: int = 0            # newest known file id (for update checking)
    latest_version: str = ""           # newest known version (for update checking)
    has_update: bool = False           # set by the update checker
    ignore_update: bool = False        # user asked to ignore this update
    ignored_version: str = ""          # latest_version at the time ignore was set
    missing_requirements: str = ""     # semicolon-separated "modId:name" pairs
    nexus_requirements: str = ""       # FULL requirements list, "modId:name" pairs (externals as 0:name)
    ignored_requirements: str = ""     # user-ignored requirement ids, "modId:name" pairs (suppress the ⚠ flag; still listed in the panel)
    is_fomod: bool = False             # True if installed via FOMOD installer
    is_bain: bool = False              # True if installed via BAIN sub-package installer
    root_folder: bool = False          # True if files should deploy to game root
    from_collection: str = ""          # slug of the collection that installed this mod
    from_collection_bundled: bool = False  # True for mods extracted from collection bundled/ folder
    from_collection_patched: bool = False  # True for mods that received BSDIFF40 patches from a collection
    collection_source_file_id: int = 0  # fileId authored in collection.json before policy resolution
    collection_install_type: str | None = None  # manifest details.type used for game-specific destinations
    collection_optional: bool = False  # manifest ``optional`` flag at collection-install time
    collection_phase: int = 0          # manifest ``phase`` at collection-install time
    xedit_modified_plugins: str = ""   # semicolon-separated plugin names edited in xEdit (set on restore)
    fomod_pending_deps: str = ""       # ';'-separated '+'-joined AND-clauses of fileDependency plugins on FOMOD options the user did NOT select; flags a rerun when a clause becomes fully present in the load order
    fomod_active_deps: str = ""        # ';'-separated '+'-joined AND-clauses of fileDependency plugins on FOMOD options the user DID select; flags a rerun when a clause is no longer fully present (its mod was removed)
    fomod_active_deps_seen: str = ""   # subset of fomod_active_deps observed SATISFIED at least once; the "orphaned patch" flag only fires for these, so a clause that was never satisfied (a hint pattern recorded by an older installer) can't fire it
    fomod_pending_baselined: bool = False  # True once the rerun-flag refresh has evaluated fomod_pending_deps against the first post-install load order and pruned the clauses that already held (deps the wizard showed and the user declined - an informed choice, not a change); cleared by a (re)install

    @property
    def nexus_page_url(self) -> str:
        domain = normalise_game_domain(self.game_domain)
        if domain and self.mod_id:
            return f"https://www.nexusmods.com/{domain}/mods/{self.mod_id}"
        return self.nexus_url or ""


# MO2 gameName → Nexus API domain.  Keys are lowercase for lookup.
_MO2_DOMAIN_MAP: dict[str, str] = {
    "skyrimse":              "skyrimspecialedition",
    "skyrim special edition": "skyrimspecialedition",
    "skyrim":                "skyrim",
    "fallout4":              "fallout4",
    "fallout 4":             "fallout4",
    "falloutnv":             "newvegas",
    "fallout3":              "fallout3",
    "oblivion":              "oblivion",
    "morrowind":             "morrowind",
    "starfield":             "starfield",
    "enderal":               "enderal",
    "enderalse":             "enderalspecialedition",
    "cyberpunk2077":         "cyberpunk2077",
    "cyberpunk 2077":        "cyberpunk2077",
    "baldursgate3":          "baldursgate3",
    "baldur's gate 3":       "baldursgate3",
    "stardewvalley":         "stardewvalley",
    "stardew valley":        "stardewvalley",
    "witcher3":              "witcher3",
    "the witcher 3":         "witcher3",
    "thesims4":              "thesims4",
    "the sims 4":            "thesims4",
}


def normalise_game_domain(raw: str) -> str:
    """Convert an MO2-style gameName or mixed-case domain to a Nexus URL domain."""
    if not raw:
        return ""
    key = raw.strip().lower()
    return _MO2_DOMAIN_MAP.get(key, key)


# ---------------------------------------------------------------------------
# Read / write helpers
# ---------------------------------------------------------------------------

_SECTION = "General"

# Mapping: meta.ini key → NexusModMeta attribute
_KEY_MAP: dict[str, str] = {
    "gameName":          "game_domain",
    "modid":             "mod_id",
    "fileid":            "file_id",
    "version":           "version",
    "author":            "author",
    "uploadedBy":        "uploaded_by",
    "nexusName":         "nexus_name",
    "nexusFileName":     "nexus_file_name",
    "installationFile":  "installation_file",
    "fileSize":          "file_size",
    "installed":         "installed",
    "nexusUrl":          "nexus_url",
    "description":       "description",
    "categoryId":        "category_id",
    "categoryName":      "category_name",
    "fileCategory":      "file_category",
    "endorsed":          "endorsed",
    "latestFileId":      "latest_file_id",
    "latestVersion":     "latest_version",
    "hasUpdate":         "has_update",
    "ignoreUpdate":      "ignore_update",
    "ignoredVersion":    "ignored_version",
    "missingRequirements": "missing_requirements",
    "nexusRequirements": "nexus_requirements",
    "ignoredRequirements": "ignored_requirements",
    "FOMOD":             "is_fomod",
    "BAIN":              "is_bain",
    "rootFolder":        "root_folder",
    "fromCollection":    "from_collection",
    "fromCollectionBundled": "from_collection_bundled",
    "fromCollectionPatched": "from_collection_patched",
    "collectionSourceFileId": "collection_source_file_id",
    "collectionInstallType": "collection_install_type",
    "collectionOptional": "collection_optional",
    "collectionPhase":   "collection_phase",
    "xeditModifiedPlugins": "xedit_modified_plugins",
    "fomodPendingDeps":  "fomod_pending_deps",
    "fomodActiveDeps":   "fomod_active_deps",
    "fomodActiveDepsSeen": "fomod_active_deps_seen",
    "fomodPendingBaselined": "fomod_pending_baselined",
}

# Attributes that are ints
_INT_FIELDS = {"mod_id", "file_id", "category_id", "latest_file_id", "file_size",
               "collection_source_file_id", "collection_phase"}

# Attributes that are bools
_BOOL_FIELDS = {
    "endorsed", "has_update", "ignore_update", "is_fomod", "is_bain",
    "root_folder", "from_collection_bundled", "from_collection_patched",
    "collection_optional", "fomod_pending_baselined",
}


# Parsed meta.ini cache, keyed by path with the file's mtime as validity.
# Several hot paths parse the SAME metas back-to-back on every reload /
# profile switch (the modlist meta worker, the filemap build's root-flag
# collect, flag refreshes) - hundreds of configparser runs each. A write
# bumps the mtime, so the entry self-invalidates. A COPY is returned/stored:
# callers mutate the result before write_meta, which must never leak into
# the cached instance (all fields are scalars, so a shallow copy is enough).
_META_CACHE: dict[str, tuple[int, NexusModMeta]] = {}
_META_CACHE_MAX = 8192


def read_meta(meta_ini_path: Path) -> NexusModMeta:
    """
    Parse a ``meta.ini`` file and return a :class:`NexusModMeta`.

    Missing keys are silently defaulted. Results are cached by file mtime.
    """
    path_str = str(meta_ini_path)
    try:
        mtime_ns = os.stat(path_str).st_mtime_ns
    except OSError:
        mtime_ns = None   # missing file → parse to the defaults, don't cache
    if mtime_ns is not None:
        entry = _META_CACHE.get(path_str)
        if entry is not None and entry[0] == mtime_ns:
            return copy.copy(entry[1])

    # allow_no_value/strict=False tolerate MO2's meta.ini quirk
    cp = configparser.ConfigParser(allow_no_value=True, strict=False)
    try:
        cp.read(path_str, encoding="utf-8")
    except configparser.Error as e:
        app_log(f"read_meta: tolerating malformed meta.ini {path_str}: {e}")

    meta = NexusModMeta()
    meta.mod_name = meta_ini_path.parent.name

    if cp.has_section(_SECTION):
        for ini_key, attr in _KEY_MAP.items():
            raw = cp.get(_SECTION, ini_key, fallback=None)
            if raw is None:
                continue
            if attr in _INT_FIELDS:
                try:
                    setattr(meta, attr, int(raw))
                except ValueError:
                    pass
            elif attr in _BOOL_FIELDS:
                setattr(meta, attr, raw.lower() in ("true", "1", "yes"))
            else:
                setattr(meta, attr, raw)

    if mtime_ns is not None:
        if len(_META_CACHE) >= _META_CACHE_MAX:
            for k in list(_META_CACHE.keys())[: _META_CACHE_MAX // 4]:
                _META_CACHE.pop(k, None)
        _META_CACHE[path_str] = (mtime_ns, copy.copy(meta))
    return meta


@locked_meta_write
def write_meta(meta_ini_path: Path, meta: NexusModMeta) -> None:
    """
    Write (or update) Nexus metadata into a ``meta.ini`` file.

    Existing content is preserved - only the keys we manage are touched.
    """
    cp = configparser.ConfigParser()
    # Preserve existing content (e.g. FOMOD flags, notes, etc.)
    if meta_ini_path.is_file():
        cp.read(str(meta_ini_path), encoding="utf-8")

    if not cp.has_section(_SECTION):
        cp.add_section(_SECTION)

    for ini_key, attr in _KEY_MAP.items():
        value = getattr(meta, attr, None)
        if value is None:
            continue
        if attr in _BOOL_FIELDS:
            # Never clobber an existing True flag with False for state set by
            # the installer (FOMOD, collection bundle/patch markers) - those
            # come from a one-shot install step and should survive callers
            # that construct fresh ``NexusModMeta`` objects without them.
            if attr in (
                "is_fomod", "is_bain", "from_collection_bundled",
                "from_collection_patched", "collection_optional",
                "fomod_pending_baselined",
            ) and not value:
                continue
            cp.set(_SECTION, ini_key, "true" if value else "false")
        else:
            # Never clobber the xEdit-modified plugin list with an empty value:
            # it is set by restore_data_core (not the installer), so callers
            # that build a fresh NexusModMeta to update other fields would
            # otherwise wipe it.
            if attr == "xedit_modified_plugins" and not value:
                continue
            # Same for the FOMOD pending-deps list: written by the FOMOD
            # installer only. A fresh NexusModMeta from an unrelated update
            # (endorse toggle, update check) must not wipe it - the installer
            # clears it explicitly (removing the ini key) when a rerun finds no
            # unselected deps, so it never needs to write an empty value here.
            if attr == "fomod_pending_deps" and not value:
                continue
            # Same for the FOMOD active-deps list (selected options' deps) and
            # its observed-satisfied subset (maintained by the modlist flag
            # refresh, not the installer).
            if attr in ("fomod_active_deps",
                        "fomod_active_deps_seen") and not value:
                continue
            # Same for the archive size: stamped once at install time; callers
            # that build a fresh NexusModMeta must not zero it.
            if attr == "file_size" and not value:
                continue
            # Collection source/phase are stamped by collection installs. Their
            # zero values must not erase existing metadata from unrelated writers.
            if attr in ("collection_source_file_id", "collection_phase") and not value:
                continue
            # Same for the uploader: stamped by the install lookup / update
            # check; a fresh NexusModMeta without it must not blank the value.
            if attr == "uploaded_by" and not value:
                continue
            cp.set(_SECTION, ini_key, str(value).replace("%", "%%"))

    meta_ini_path.parent.mkdir(parents=True, exist_ok=True)
    with atomic_writer(meta_ini_path) as f:
        cp.write(f)

    app_log(f"Wrote meta.ini: {meta_ini_path}")


def set_meta_key(meta_ini_path: Path, ini_key: str, value: str) -> None:
    """Set one key in a meta.ini's [General] section, leaving the rest untouched.

    Cheaper and safer than a read/modify/write_meta round-trip for callers that
    only maintain a single bookkeeping key.
    """
    set_meta_keys(meta_ini_path, {ini_key: value})


@locked_meta_write
def set_meta_keys(meta_ini_path: Path, values: dict) -> None:
    """Set several keys in a meta.ini's [General] section in ONE write, leaving
    the rest untouched. A value of ``None`` REMOVES that key."""
    if not meta_ini_path.is_file():
        return
    cp = configparser.ConfigParser(allow_no_value=True, strict=False)
    try:
        cp.read(str(meta_ini_path), encoding="utf-8")
    except configparser.Error:
        return
    if not cp.has_section(_SECTION):
        cp.add_section(_SECTION)
    for ini_key, value in values.items():
        if value is None:
            cp.remove_option(_SECTION, ini_key)
        else:
            cp.set(_SECTION, ini_key, str(value).replace("%", "%%"))
    with atomic_writer(meta_ini_path) as f:
        cp.write(f)


@locked_meta_write
def ensure_installed_stamp(meta_ini_path: Path) -> bool:
    """
    If ``installed`` is missing from meta.ini, backfill it from the file's mtime
    in MO2 format (YYYY-MM-DDTHH:MM:SS) for backwards compatibility.  Returns
    True if a stamp was written.
    """
    if not meta_ini_path.is_file():
        return False
    cp = configparser.ConfigParser()
    cp.read(str(meta_ini_path), encoding="utf-8")
    if not cp.has_section(_SECTION):
        cp.add_section(_SECTION)
    if cp.get(_SECTION, "installed", fallback=""):
        return False
    mtime = meta_ini_path.stat().st_mtime
    dt = datetime.fromtimestamp(mtime)
    cp.set(_SECTION, "installed", dt.strftime("%Y-%m-%dT%H:%M:%S"))
    with atomic_writer(meta_ini_path) as f:
        cp.write(f)
    return True


def build_meta_from_download(
    *,
    game_domain: str,
    mod_id: int,
    file_id: int,
    archive_name: str = "",
    mod_info: Optional[object] = None,        # NexusModInfo
    file_info: Optional[object] = None,       # NexusModFile
    from_collection: str = "",
) -> NexusModMeta:
    """
    Create a :class:`NexusModMeta` from download result + optional API data.

    ``mod_info`` and ``file_info`` are NexusModInfo/NexusModFile if the
    caller chose to fetch them; they enrich the metadata but aren't required.

    ``from_collection`` is the collection slug if this mod is being installed
    as part of a collection; leave empty for manual installs.
    """
    meta = NexusModMeta(
        game_domain=game_domain,
        mod_id=mod_id,
        file_id=file_id,
        installation_file=archive_name,
        installed=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
        from_collection=from_collection,
    )

    if mod_info is not None:
        meta.nexus_name = getattr(mod_info, "name", "")
        meta.version = getattr(mod_info, "version", "")
        meta.author = getattr(mod_info, "author", "")
        meta.uploaded_by = getattr(mod_info, "uploaded_by", "") or ""
        meta.description = getattr(mod_info, "summary", "")
        meta.category_id = getattr(mod_info, "category_id", 0)
        meta.category_name = getattr(mod_info, "category_name", "") or ""
        meta.nexus_url = (
            f"https://www.nexusmods.com/{game_domain}/mods/{mod_id}"
        )

    if file_info is not None:
        meta.version = getattr(file_info, "version", "") or meta.version
        meta.file_category = getattr(file_info, "category_name", "")

    return meta


def has_reinstall_carryover(installed: "NexusModMeta | None") -> bool:
    """True when an installed meta carries anything merge_reinstall_metadata
    would preserve. Reinstall callers must NOT hand _write_install_meta a
    prebuilt meta otherwise: an identity-less prebuilt short-circuits its
    filename/MD5 Nexus lookup, so a mod that was never identified at install
    time would stay unidentified forever."""
    return installed is not None and bool(
        getattr(installed, "mod_id", 0)
        or getattr(installed, "root_folder", False)
        or getattr(installed, "from_collection", "")
        or getattr(installed, "collection_install_type", ""))


def merge_reinstall_metadata(
    refreshed: "NexusModMeta | None",
    installed: "NexusModMeta | None",
) -> NexusModMeta:
    """Build metadata for reinstalling the same installed archive.

    API/download metadata remains authoritative when available.  A local
    reinstall can reuse stable package identity from the installed metadata,
    while install-layout and collection ownership must survive either path.
    Transient update-check state is deliberately excluded (re-derived by the
    next check), as is from_collection_patched (a plain reinstall does NOT
    reapply a collection's BSDIFF patches, so the badge would lie).
    """
    meta = copy.copy(refreshed) if refreshed is not None else NexusModMeta()
    if installed is None:
        return meta

    stable_identity = (
        "game_domain",
        "mod_id",
        "file_id",
        "version",
        "author",
        "uploaded_by",
        "nexus_name",
        "nexus_file_name",
        "nexus_url",
        "description",
        "category_id",
        "category_name",
        "file_category",
    )
    for field_name in stable_identity:
        if not getattr(meta, field_name):
            setattr(meta, field_name, getattr(installed, field_name))

    # Layout + ownership come from the INSTALLED meta unconditionally - a
    # fresh API/download meta can never know them (build_meta_from_download
    # leaves them at defaults), and losing root_folder flattens a root
    # package's Data/ tree on reinstall (the bug this merge exists to fix).
    meta.root_folder = installed.root_folder
    meta.from_collection = installed.from_collection
    meta.from_collection_bundled = installed.from_collection_bundled
    meta.collection_source_file_id = installed.collection_source_file_id
    meta.collection_install_type = installed.collection_install_type
    # A dismissed update nag survives reinstalling the SAME file - the update
    # checker un-ignores by itself when a version STRICTLY NEWER than
    # ignored_version appears (nexus_update_checker), so this can't hide a
    # genuinely new update.
    meta.ignore_update = installed.ignore_update
    meta.ignored_version = installed.ignored_version
    return meta


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------

def scan_installed_mods(staging_root: Path) -> list[NexusModMeta]:
    """
    Walk all mod folders under *staging_root* and return metadata for
    those that have a Nexus ``modid`` set (i.e. were installed from Nexus).
    """
    results: list[NexusModMeta] = []
    if not staging_root.is_dir():
        return results

    for folder in sorted(staging_root.iterdir()):
        meta_path = folder / "meta.ini"
        if not meta_path.is_file():
            continue
        meta = read_meta(meta_path)
        if meta.mod_id > 0:
            results.append(meta)

    return results


def parse_req_pairs(raw: str) -> list[tuple[int, str]]:
    """Parse a ``modId:name;modId:name`` requirements string (the format of
    ``missing_requirements`` / ``nexus_requirements``) into (id, name) tuples.
    Malformed parts are skipped; names keep any embedded colons (split-once)."""
    pairs: list[tuple[int, str]] = []
    for part in (raw or "").split(";"):
        part = part.strip()
        if not part:
            continue
        head, _, tail = part.partition(":")
        try:
            rid = int(head.strip())
        except ValueError:
            continue
        pairs.append((rid, tail.strip()))
    return pairs


# (path, mtime) → rootFolder bit, so the per-toggle filemap rebuild doesn't
# re-parse one meta.ini per enabled mod (~500 INI parses ≈ 0.5 s on a big
# modlist just to find a couple of flagged mods). A meta edit bumps the mtime
# and refreshes its entry; entries for removed mods are dropped on rebuild.
_root_flag_cache: dict[str, tuple[float, bool]] = {}


def collect_root_flagged_mods(modlist_path: Path, staging_root: Path,
                              log_fn=None) -> set[str]:
    """Return the set of mods in *modlist_path* whose meta.ini sets
    rootFolder=true - DISABLED mods included. Malformed meta.ini files are
    logged (if *log_fn* is provided) and skipped. Per-file results are
    mtime-cached.

    Disabled mods MUST be included: the set drives rebuild_mod_index /
    rescan_mods_in_index, and the index describes a mod's CONTENT (root mods
    keep their Data/ prefix unstripped) independent of its enabled state.
    Skipping disabled entries meant a root mod that was disabled during a
    Refresh had its index entry rebuilt STRIPPED; enabling it later (no
    rescan on toggle) root-deployed the stripped paths - e.g. a freshly
    wizard-installed SKSE put Scripts/ in the game root instead of Data/.
    For build_filemap the extra names are harmless - it only tests membership
    for mods already in the enabled iteration."""
    from Utils.mods.modlist import read_modlist

    flagged: set[str] = set()
    if not modlist_path.is_file():
        return flagged

    fresh_cache: dict[str, tuple[float, bool]] = {}
    for entry in read_modlist(modlist_path):
        if entry.is_separator:
            continue
        meta_path = staging_root / entry.name / "meta.ini"
        try:
            mtime = meta_path.stat().st_mtime
        except OSError:
            continue   # no meta.ini
        key = str(meta_path)
        cached = _root_flag_cache.get(key)
        if cached is not None and cached[0] == mtime:
            fresh_cache[key] = cached
            if cached[1]:
                flagged.add(entry.name)
            continue
        try:
            is_root = read_meta(meta_path).root_folder
        except Exception as e:
            if log_fn is not None:
                log_fn(f"  WARN: could not read meta.ini for '{entry.name}': {e}")
            else:
                app_log(f"collect_root_flagged_mods: meta.ini unreadable for '{entry.name}': {e}")
            continue
        fresh_cache[key] = (mtime, is_root)
        if is_root:
            flagged.add(entry.name)
    # Replace (don't merge) so entries for removed/renamed mods don't
    # accumulate across profile switches.
    _root_flag_cache.clear()
    _root_flag_cache.update(fresh_cache)
    return flagged


# ---------------------------------------------------------------------------
# Filename parsing
# ---------------------------------------------------------------------------

# Nexus download filenames typically end with: -<mod_id>-<version segments>-<file_id_or_timestamp>
# Example: "A StoryWealth - Caves Of The Commonwealth-60927-1-1-1-1758182764"
#           → mod_id=60927, trailing numbers = [1, 1, 1, 1758182764]
_NEXUS_SUFFIX_RE = re.compile(r"-(\d+(?:-\d+)*)$")


@dataclass
class NexusFilenameInfo:
    """Metadata extracted from a Nexus-style archive filename."""
    mod_id: int = 0
    version_parts: list[int] = field(default_factory=list)
    raw_suffix: str = ""

    @property
    def version(self) -> str:
        """Reconstruct a dotted version string from the numeric parts."""
        if self.version_parts:
            return ".".join(str(p) for p in self.version_parts)
        return ""


def parse_nexus_filename(filename_stem: str) -> Optional[NexusFilenameInfo]:
    """
    Try to extract Nexus mod ID and version from a filename stem.

    Nexus download filenames follow the pattern::

        <mod name>-<mod_id>-<version numbers...>

    For example:
        ``A StoryWealth - Caves Of The Commonwealth-60927-1-1-1-1758182764``
          → mod_id=60927, version_parts=[1, 1, 1, 1758182764]

        ``SkyUI_5_2_SE-12604-5-2SE``
          → mod_id=12604  (trailing part has non-numeric, so just mod_id)

    Returns None if no Nexus-style suffix is found.
    """
    m = _NEXUS_SUFFIX_RE.search(filename_stem)
    if not m:
        return None

    numbers = [int(n) for n in m.group(1).split("-")]
    if not numbers:
        return None

    # First number is always the mod_id, rest are version/timestamp segments
    return NexusFilenameInfo(
        mod_id=numbers[0],
        version_parts=numbers[1:],
        raw_suffix=m.group(0),
    )


# ---------------------------------------------------------------------------
# Auto-detect metadata for manually installed archives
# ---------------------------------------------------------------------------

def resolve_nexus_meta_for_archive(
    archive_path: Path,
    game_domain: str,
    api: Optional[object] = None,
    log_fn: Optional[callable] = None,
) -> Optional[NexusModMeta]:
    """
    Try to identify a Nexus mod from an archive using:

      1. **Filename parsing** - extract mod_id from the Nexus-style filename
         suffix, then query the API for full details.
      2. **MD5 hash lookup** - hash the archive and query
         ``GET /games/{domain}/mods/md5_search/{hash}``.

    Returns a :class:`NexusModMeta` if identified, or ``None``.
    Both strategies require a valid ``api`` (NexusAPI instance).
    """
    _log = log_fn or (lambda m: None)

    archive_name = archive_path.name
    stem = archive_path.stem
    # Handle double extensions like .tar.gz
    if stem.endswith(".tar"):
        stem = Path(stem).stem

    meta: Optional[NexusModMeta] = None

    # --- Strategy 1: parse filename ---
    fn_info = parse_nexus_filename(stem)
    if fn_info and fn_info.mod_id > 0:
        _log(f"Nexus: Detected mod ID {fn_info.mod_id} from filename.")

        if api is None:
            # Offline - save what we can from the filename alone.
            _log("Nexus: Not connected - saving mod ID and install date from filename.")
            now = datetime.now(timezone.utc)
            date_version = now.strftime("d%Y.%-m.%-d.0")
            return NexusModMeta(
                game_domain=game_domain,
                mod_id=fn_info.mod_id,
                installation_file=archive_name,
                installed=now.strftime("%Y-%m-%dT%H:%M:%S"),
                version=date_version,
            )

        try:
            # Use GraphQL for mod info (avoids 2 REST calls: get_mod + get_game_categories)
            mod_info, _ = api.get_mod_and_file_info_graphql(
                game_domain, fn_info.mod_id, file_id=0
            )
            if mod_info is None:
                mod_info = api.get_mod(game_domain, fn_info.mod_id)  # fallback to REST
            meta = build_meta_from_download(
                game_domain=game_domain,
                mod_id=fn_info.mod_id,
                file_id=0,
                archive_name=archive_name,
                mod_info=mod_info,
            )
            # Try to find the exact file_id from the mod's file list
            try:
                files_resp = api.get_mod_files(game_domain, fn_info.mod_id)
                # Match by archive filename
                for f in files_resp.files:
                    if f.file_name and f.file_name.lower() == archive_name.lower():
                        meta.file_id = f.file_id
                        meta.version = f.version or f.mod_version or meta.version
                        meta.file_category = f.category_name
                        meta.nexus_file_name = f.name or ""
                        break
                # If no filename match, check if the version matches
                if meta.file_id == 0 and fn_info.version:
                    for f in files_resp.files:
                        if f.version == fn_info.version or f.mod_version == fn_info.version:
                            meta.file_id = f.file_id
                            meta.file_category = f.category_name
                            meta.nexus_file_name = f.name or ""
                            break
            except Exception:
                pass

            _log(f"Nexus: Identified as '{mod_info.name}' "
                 f"(mod {meta.mod_id}, file {meta.file_id}).")
            return meta
        except Exception as exc:
            _log(f"Nexus: Filename had mod ID {fn_info.mod_id} but API lookup "
                 f"failed ({exc}). Trying MD5...")

    if api is None:
        return None

    # --- Strategy 2: MD5 hash lookup ---
    _log(f"Nexus: Computing MD5 hash of {archive_name}...")
    try:
        import hashlib
        md5 = hashlib.md5()
        with open(archive_path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                md5.update(chunk)
        md5_hex = md5.hexdigest()
        _log(f"Nexus: MD5 = {md5_hex}, searching...")

        results = api.get_file_by_md5(game_domain, md5_hex)
        if results:
            # Results is a list of dicts, each with 'mod' and 'file_details'
            hit = results[0]
            mod_data = hit.get("mod", {})
            file_data = hit.get("file_details", {})

            cat_name = mod_data.get("category_name", "") or mod_data.get("category", "") or ""
            if not isinstance(cat_name, str):
                cat_name = ""
            try:
                cat_id = int(mod_data.get("category_id", 0) or 0)
            except (TypeError, ValueError):
                cat_id = 0
            if not cat_name and cat_id:
                try:
                    for c in api.get_game_categories(game_domain):
                        if c.category_id == cat_id:
                            cat_name = c.name or ""
                            break
                except Exception:
                    pass
            meta = NexusModMeta(
                game_domain=game_domain,
                mod_id=mod_data.get("mod_id", 0),
                file_id=file_data.get("file_id", 0),
                version=file_data.get("version", "") or file_data.get("mod_version", ""),
                author=mod_data.get("author", ""),
                uploaded_by=mod_data.get("uploaded_by", "") or "",
                nexus_name=mod_data.get("name", ""),
                nexus_file_name=file_data.get("name", "") or file_data.get("file_name", ""),
                installation_file=archive_name,
                installed=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
                description=mod_data.get("summary", ""),
                category_id=cat_id,
                category_name=cat_name,
                file_category=file_data.get("category_name", ""),
                nexus_url=f"https://www.nexusmods.com/{game_domain}/mods/{mod_data.get('mod_id', 0)}",
            )
            _log(f"Nexus: MD5 match - '{meta.nexus_name}' "
                 f"(mod {meta.mod_id}, file {meta.file_id}).")
            return meta
        else:
            _log("Nexus: No MD5 match found - this file may not be from Nexus.")
    except Exception as exc:
        _log(f"Nexus: MD5 lookup failed - {exc}")

    return None


def resolve_nexus_meta_for_archive_domains(
    archive_path: Path,
    game_domains: Iterable[str],
    api: Optional[object] = None,
    log_fn: Optional[callable] = None,
) -> Optional[NexusModMeta]:
    """Identify an archive across a game's primary and additional domains.

    A result that identifies the exact Nexus file wins over a filename-only
    mod-page match. If no domain can identify the file, the primary domain's
    partial result preserves the legacy fallback behaviour.
    """
    domains: list[str] = []
    seen: set[str] = set()
    for raw in game_domains:
        domain = normalise_game_domain(raw or "")
        if domain and domain not in seen:
            seen.add(domain)
            domains.append(domain)
    if not domains:
        return None

    partial: Optional[NexusModMeta] = None
    for domain in domains:
        meta = resolve_nexus_meta_for_archive(
            archive_path, domain, api=api, log_fn=log_fn)
        if meta is None:
            continue
        if meta.file_id > 0:
            return meta
        if partial is None:
            partial = meta
    return partial
