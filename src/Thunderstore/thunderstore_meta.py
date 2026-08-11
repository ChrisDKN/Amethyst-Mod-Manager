"""
thunderstore_meta.py
Read and write Thunderstore metadata in a mod's ``meta.ini`` [thunderstore] section.

Thunderstore metadata lives in its OWN section so it never collides with the
MO2-style ``[General]`` section that :mod:`Nexus.nexus_meta` owns::

    [General]
    version=5.4.2305
    installationFile=Subnautica_Modding-BepInExPack-5.4.2305.zip

    [thunderstore]
    community = subnautica
    namespace = Subnautica_Modding
    name = BepInExPack
    version = 5.4.2305
    fullname = Subnautica_Modding-BepInExPack-5.4.2305
    downloadurl = https://thunderstore.io/package/download/.../5.4.2305/
    filesize = 669238
    dependencies = Subnautica_Modding-QModManager-4.4.3
    installed = 2026-08-11T12:00:00

Keys are declared camelCase in ``_KEY_MAP`` but configparser lower-cases them
on write (and matches case-insensitively on read), exactly as the existing
Nexus ``[General]`` section already does on disk - so the two sections look
consistent in a real meta.ini.

Both modules use configparser and preserve unknown sections on write, so a mod
can legitimately carry both (e.g. the same mod mirrored on Nexus). Section
ordering depends on which module wrote last; neither disturbs the other's keys.

Thunderstore has no per-file model - one package version is exactly one zip -
so there is no fileid analogue. ``fullName`` (``{ns}-{name}-{version}``) is the
canonical identifier, and ``fileSize`` is the only integrity check the API
offers: PackageVersion carries no checksum field at all.
"""

from __future__ import annotations

import configparser
import copy
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from Utils.app_log import app_log

SECTION = "thunderstore"


@dataclass
class ThunderstoreModMeta:
    """Thunderstore metadata for a single installed mod."""

    mod_name: str = ""            # folder name in staging
    community: str = ""           # community slug, e.g. "subnautica"
    namespace: str = ""           # team/owner, e.g. "Subnautica_Modding"
    name: str = ""                # package name, e.g. "BepInExPack"
    version: str = ""             # version number, e.g. "5.4.2305"
    full_name: str = ""           # "{namespace}-{name}-{version}"
    download_url: str = ""        # thunderstore.io download endpoint
    website_url: str = ""         # upstream project URL, if any
    description: str = ""         # short package description
    icon_url: str = ""            # package icon
    file_size: int = 0            # zip size in bytes (only integrity check available)
    dependencies: str = ""        # comma-separated pinned "{ns}-{name}-{ver}" triples
    installed: str = ""           # ISO-8601 timestamp
    is_deprecated: bool = False   # package flagged deprecated upstream
    latest_version: str = ""      # newest version seen by the update checker
    has_update: bool = False      # latest_version differs from version
    ignore_update: bool = False   # user muted updates for this mod
    ignored_version: str = ""     # version the user chose to skip

    @property
    def package_id(self) -> str:
        """Version-independent identity, ``{namespace}-{name}``."""
        if self.namespace and self.name:
            return f"{self.namespace}-{self.name}"
        return ""

    def dependency_list(self) -> list[str]:
        """Pinned dependency triples as a list."""
        return [d.strip() for d in self.dependencies.split(",") if d.strip()]


# Mapping: meta.ini key → ThunderstoreModMeta attribute
_KEY_MAP: dict[str, str] = {
    "community":      "community",
    "namespace":      "namespace",
    "name":           "name",
    "version":        "version",
    "fullName":       "full_name",
    "downloadUrl":    "download_url",
    "websiteUrl":     "website_url",
    "description":    "description",
    "iconUrl":        "icon_url",
    "fileSize":       "file_size",
    "dependencies":   "dependencies",
    "installed":      "installed",
    "isDeprecated":   "is_deprecated",
    "latestVersion":  "latest_version",
    "hasUpdate":      "has_update",
    "ignoreUpdate":   "ignore_update",
    "ignoredVersion": "ignored_version",
}

_INT_FIELDS = {"file_size"}
_BOOL_FIELDS = {"is_deprecated", "has_update", "ignore_update"}

# Values stamped once at install time. A caller that builds a fresh
# ThunderstoreModMeta to update one field (an update check, an ignore toggle)
# must not blank these - mirrors the same guard in nexus_meta.write_meta.
_NEVER_BLANK = {"file_size", "dependencies", "download_url", "installed",
                "description", "website_url", "icon_url", "community"}

# Parsed meta cache keyed by path, validated by mtime (see nexus_meta).
_META_CACHE: dict[str, tuple[int, ThunderstoreModMeta]] = {}
_META_CACHE_MAX = 8192


def parse_full_name(full_name: str) -> tuple[str, str, str]:
    """Split a ``{namespace}-{name}-{version}`` triple.

    Package and namespace names may themselves contain hyphens and
    underscores (``Subnautica_Modding-BepInExPack_Subnautica-5.4.1901``), so
    this splits from the RIGHT twice - never ``split("-")``.

    Returns ``(namespace, name, version)``; empty strings when unparseable.
    """
    raw = (full_name or "").strip()
    if not raw:
        return "", "", ""
    parts = raw.rsplit("-", 2)
    if len(parts) != 3:
        return "", "", ""
    return parts[0], parts[1], parts[2]


def read_meta(meta_ini_path: Path) -> ThunderstoreModMeta:
    """Parse the [thunderstore] section of a ``meta.ini``.

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

    # allow_no_value/strict=False tolerate MO2's meta.ini quirks
    cp = configparser.ConfigParser(allow_no_value=True, strict=False)
    try:
        cp.read(path_str, encoding="utf-8")
    except configparser.Error as e:
        app_log(f"thunderstore read_meta: tolerating malformed {path_str}: {e}")

    meta = ThunderstoreModMeta()
    meta.mod_name = meta_ini_path.parent.name

    if cp.has_section(SECTION):
        for ini_key, attr in _KEY_MAP.items():
            raw = cp.get(SECTION, ini_key, fallback=None)
            if raw is None:
                continue
            if attr in _INT_FIELDS:
                try:
                    setattr(meta, attr, int(raw))
                except ValueError:
                    pass
            elif attr in _BOOL_FIELDS:
                setattr(meta, attr, raw.strip().lower() in ("true", "1", "yes"))
            else:
                setattr(meta, attr, raw)

    # Backfill the identity triple either way round so callers can rely on
    # both being present regardless of which the writer stamped.
    if not meta.full_name and meta.namespace and meta.name and meta.version:
        meta.full_name = f"{meta.namespace}-{meta.name}-{meta.version}"
    elif meta.full_name and not (meta.namespace and meta.name):
        ns, name, ver = parse_full_name(meta.full_name)
        meta.namespace = meta.namespace or ns
        meta.name = meta.name or name
        meta.version = meta.version or ver

    if mtime_ns is not None:
        if len(_META_CACHE) >= _META_CACHE_MAX:
            for k in list(_META_CACHE.keys())[: _META_CACHE_MAX // 4]:
                _META_CACHE.pop(k, None)
        _META_CACHE[path_str] = (mtime_ns, copy.copy(meta))
    return meta


def write_meta(meta_ini_path: Path, meta: ThunderstoreModMeta) -> None:
    """Write Thunderstore metadata into a ``meta.ini`` [thunderstore] section.

    Existing content - including the Nexus-owned [General] section - is read
    back and preserved; only [thunderstore] keys are touched.
    """
    cp = configparser.ConfigParser()
    if meta_ini_path.is_file():
        try:
            cp.read(str(meta_ini_path), encoding="utf-8")
        except configparser.Error as e:
            app_log(f"thunderstore write_meta: tolerating malformed "
                    f"{meta_ini_path}: {e}")

    if not cp.has_section(SECTION):
        cp.add_section(SECTION)

    if not meta.full_name and meta.namespace and meta.name and meta.version:
        meta.full_name = f"{meta.namespace}-{meta.name}-{meta.version}"

    for ini_key, attr in _KEY_MAP.items():
        value = getattr(meta, attr, None)
        if value is None:
            continue
        if attr in _BOOL_FIELDS:
            cp.set(SECTION, ini_key, "true" if value else "false")
            continue
        if attr in _NEVER_BLANK and not value:
            continue
        # '%' is configparser's interpolation sigil - escape it so package
        # descriptions containing e.g. "+50% speed" round-trip intact.
        cp.set(SECTION, ini_key, str(value).replace("%", "%%"))

    meta_ini_path.parent.mkdir(parents=True, exist_ok=True)
    with open(meta_ini_path, "w", encoding="utf-8") as f:
        cp.write(f)

    app_log(f"Wrote thunderstore meta.ini: {meta_ini_path}")


def is_thunderstore_mod(meta_ini_path: Path) -> bool:
    """Whether a ``meta.ini`` carries usable Thunderstore metadata."""
    return bool(read_meta(meta_ini_path).package_id)


def build_meta_from_link(link, version_info: dict | None = None
                         ) -> ThunderstoreModMeta:
    """Build metadata from a parsed :class:`Ror2mmLink` plus optional API data.

    *version_info* is a cyberstorm version-detail dict; every field it can
    supply is optional so an install can still be stamped from the link alone
    when the API is unreachable.
    """
    info = version_info or {}
    meta = ThunderstoreModMeta(
        community=getattr(link, "community", "") or "",
        namespace=link.namespace,
        name=link.name,
        version=link.version,
        full_name=info.get("full_version_name")
        or f"{link.namespace}-{link.name}-{link.version}",
        download_url=info.get("download_url") or link.download_url,
        website_url=info.get("website_url") or "",
        description=info.get("description") or "",
        icon_url=info.get("icon_url") or "",
        installed=datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
    )
    try:
        meta.file_size = int(info.get("size") or 0)
    except (TypeError, ValueError):
        meta.file_size = 0

    deps = info.get("dependencies")
    if isinstance(deps, (list, tuple)):
        meta.dependencies = ", ".join(str(d) for d in deps if d)
    elif isinstance(deps, str):
        meta.dependencies = deps

    return meta
