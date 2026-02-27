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

- **NexusModMeta** — data class holding per-mod Nexus info
- **read_meta** / **write_meta** — I/O helpers (non-destructive: preserves
  existing ``meta.ini`` content when writing)
- **scan_installed_mods** — walk a staging root and collect all mods that
  have Nexus metadata (``modid`` > 0)
"""

from __future__ import annotations

import configparser
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from Utils.app_log import app_log


@dataclass
class NexusModMeta:
    """Nexus metadata for a single installed mod."""

    mod_name: str = ""                 # folder name in staging
    game_domain: str = ""              # e.g. "skyrimspecialedition"
    mod_id: int = 0                    # Nexus mod ID
    file_id: int = 0                   # Nexus file ID
    version: str = ""                  # mod version string
    author: str = ""                   # mod author
    nexus_name: str = ""               # mod name on Nexus (may differ from folder)
    installation_file: str = ""        # original archive filename
    installed: str = ""                # ISO-8601 timestamp
    nexus_url: str = ""                # full Nexus mod page URL
    description: str = ""              # short summary
    category_id: int = 0               # Nexus category
    file_category: str = ""            # MAIN / UPDATE / OPTIONAL / etc
    endorsed: bool = False             # whether the user has endorsed this mod
    latest_file_id: int = 0            # newest known file id (for update checking)
    latest_version: str = ""           # newest known version (for update checking)
    has_update: bool = False           # set by the update checker
    missing_requirements: str = ""     # semicolon-separated "modId:name" pairs

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
    "nexusName":         "nexus_name",
    "installationFile":  "installation_file",
    "installed":         "installed",
    "nexusUrl":          "nexus_url",
    "description":       "description",
    "categoryId":        "category_id",
    "fileCategory":      "file_category",
    "endorsed":          "endorsed",
    "latestFileId":      "latest_file_id",
    "latestVersion":     "latest_version",
    "hasUpdate":         "has_update",
    "missingRequirements": "missing_requirements",
}

# Attributes that are ints
_INT_FIELDS = {"mod_id", "file_id", "category_id", "latest_file_id"}

# Attributes that are bools
_BOOL_FIELDS = {"endorsed", "has_update"}


def read_meta(meta_ini_path: Path) -> NexusModMeta:
    """
    Parse a ``meta.ini`` file and return a :class:`NexusModMeta`.

    Missing keys are silently defaulted.
    """
    cp = configparser.ConfigParser()
    cp.read(str(meta_ini_path), encoding="utf-8")

    meta = NexusModMeta()
    meta.mod_name = meta_ini_path.parent.name

    if not cp.has_section(_SECTION):
        return meta

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

    return meta


def write_meta(meta_ini_path: Path, meta: NexusModMeta) -> None:
    """
    Write (or update) Nexus metadata into a ``meta.ini`` file.

    Existing content is preserved — only the keys we manage are touched.
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
            cp.set(_SECTION, ini_key, "true" if value else "false")
        else:
            cp.set(_SECTION, ini_key, str(value))

    meta_ini_path.parent.mkdir(parents=True, exist_ok=True)
    with open(meta_ini_path, "w", encoding="utf-8") as f:
        cp.write(f)

    app_log(f"Wrote meta.ini: {meta_ini_path}")


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
    with open(meta_ini_path, "w", encoding="utf-8") as f:
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
) -> NexusModMeta:
    """
    Create a :class:`NexusModMeta` from download result + optional API data.

    ``mod_info`` and ``file_info`` are NexusModInfo/NexusModFile if the
    caller chose to fetch them; they enrich the metadata but aren't required.
    """
    meta = NexusModMeta(
        game_domain=game_domain,
        mod_id=mod_id,
        file_id=file_id,
        installation_file=archive_name,
        installed=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
    )

    if mod_info is not None:
        meta.nexus_name = getattr(mod_info, "name", "")
        meta.version = getattr(mod_info, "version", "")
        meta.author = getattr(mod_info, "author", "")
        meta.description = getattr(mod_info, "summary", "")
        meta.category_id = getattr(mod_info, "category_id", 0)
        meta.nexus_url = (
            f"https://www.nexusmods.com/{game_domain}/mods/{mod_id}"
        )

    if file_info is not None:
        meta.version = getattr(file_info, "version", "") or meta.version
        meta.file_category = getattr(file_info, "category_name", "")

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

      1. **Filename parsing** — extract mod_id from the Nexus-style filename
         suffix, then query the API for full details.
      2. **MD5 hash lookup** — hash the archive and query
         ``GET /games/{domain}/mods/md5_search/{hash}``.

    Returns a :class:`NexusModMeta` if identified, or ``None``.
    Both strategies require a valid ``api`` (NexusAPI instance).
    """
    _log = log_fn or (lambda m: None)

    if api is None:
        return None

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
        try:
            mod_info = api.get_mod(game_domain, fn_info.mod_id)
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
                        break
                # If no filename match, check if the version matches
                if meta.file_id == 0 and fn_info.version:
                    for f in files_resp.files:
                        if f.version == fn_info.version or f.mod_version == fn_info.version:
                            meta.file_id = f.file_id
                            meta.file_category = f.category_name
                            break
            except Exception:
                pass

            _log(f"Nexus: Identified as '{mod_info.name}' "
                 f"(mod {meta.mod_id}, file {meta.file_id}).")
            return meta
        except Exception as exc:
            _log(f"Nexus: Filename had mod ID {fn_info.mod_id} but API lookup "
                 f"failed ({exc}). Trying MD5...")

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

            meta = NexusModMeta(
                game_domain=game_domain,
                mod_id=mod_data.get("mod_id", 0),
                file_id=file_data.get("file_id", 0),
                version=file_data.get("version", "") or file_data.get("mod_version", ""),
                author=mod_data.get("author", ""),
                nexus_name=mod_data.get("name", ""),
                installation_file=archive_name,
                installed=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
                description=mod_data.get("summary", ""),
                category_id=mod_data.get("category_id", 0),
                file_category=file_data.get("category_name", ""),
                nexus_url=f"https://www.nexusmods.com/{game_domain}/mods/{mod_data.get('mod_id', 0)}",
            )
            _log(f"Nexus: MD5 match — '{meta.nexus_name}' "
                 f"(mod {meta.mod_id}, file {meta.file_id}).")
            return meta
        else:
            _log("Nexus: No MD5 match found — this file may not be from Nexus.")
    except Exception as exc:
        _log(f"Nexus: MD5 lookup failed — {exc}")

    return None
