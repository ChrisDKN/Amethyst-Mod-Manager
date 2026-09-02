"""
modio_meta.py  (Baldur's Gate 3)

Identify a BG3 mod on mod.io from a freshly-installed archive and stamp the
result into the mod's ``meta.ini`` so the update checker can use it later.

How it works (all confirmed against a real mod.io pak):

  1. The extracted ``.pak`` in the staging folder carries, in its
     ``meta.lsx`` ``ModuleInfo`` node, a ``PublishHandle`` - the mod.io
     **numeric mod id** (0 for vanilla dependency modules / non-mod.io paks).
  2. The original downloaded archive is the ``.zip`` mod.io hashed, so its
     md5 matches the ``filehash.md5`` of exactly one released file.  Matching
     it recovers the **file id** and **version**.
  3. As a fallback when the md5 doesn't match (re-zipped / edited archive),
     ``filesize_uncompressed`` from the files endpoint equals the unpacked
     pak's on-disk size, which uniquely picks the version in practice.
  4. If an older file is no longer returned by mod.io, the pak's packed
     ``Version64`` supplies the installed version for update comparison.

Results are written into a dedicated ``[modio]`` section in the same
``meta.ini`` Nexus uses, via :func:`write_modio_meta` (non-destructive).
Legacy mod.io-prefixed keys in ``[General]`` are read and migrated on write.

This module is loaded by file path (the BG3 folder name has spaces), so it
imports only from packages that are on ``sys.path`` (``Utils.*``) and loads
its siblings with :func:`_load_sibling`.
"""

from __future__ import annotations

import configparser
import hashlib
import importlib.util
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from Utils.app_log import app_log
from Utils.atomic_write import atomic_writer
from Utils.mods.metadata import locked_meta_write
from Utils.bg3.pak import extract_meta_lsx

_SECTION = "modio"
_LEGACY_SECTION = "General"

_KEY_MOD_ID = "modId"
_KEY_FILE_ID = "fileId"
_KEY_VERSION = "version"
_KEY_LATEST_FILE_ID = "latestFileId"
_KEY_LATEST_VERSION = "latestVersion"
_KEY_HAS_UPDATE = "hasUpdate"
_KEY_INSTALLED = "installed"
_KEY_NAME = "name"
_KEY_PROFILE_URL = "profileUrl"

_LEGACY_KEYS = {
    _KEY_MOD_ID: "modioModId",
    _KEY_FILE_ID: "modioFileId",
    _KEY_VERSION: "modioVersion",
    _KEY_LATEST_FILE_ID: "modioLatestFileId",
    _KEY_LATEST_VERSION: "modioLatestVersion",
    _KEY_HAS_UPDATE: "modioHasUpdate",
    _KEY_INSTALLED: "modioInstalled",
    _KEY_NAME: "modioName",
    _KEY_PROFILE_URL: "modioProfileUrl",
}


def _load_sibling(stem: str):
    """Load a sibling BG3 module (modio_api / modio_key) by file path.

    The module is registered in ``sys.modules`` before execution so that
    ``@dataclass`` (which resolves ``cls.__module__``) works.
    """
    import sys
    mod_name = f"{stem}_bg3"
    cached = sys.modules.get(mod_name)
    if cached is not None:
        return cached
    sibling = Path(__file__).resolve().parent / f"{stem}.py"
    spec = importlib.util.spec_from_file_location(mod_name, str(sibling))
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


@dataclass
class ModioMeta:
    """mod.io metadata for a single installed mod."""

    mod_id: int = 0
    file_id: int = 0
    version: str = ""
    name: str = ""
    profile_url: str = ""
    latest_file_id: int = 0
    latest_version: str = ""
    has_update: bool = False
    installed: str = ""
    legacy_storage: bool = False


# ---------------------------------------------------------------------------
# pak meta.lsx parsing
# ---------------------------------------------------------------------------

_ATTR_RE_TMPL = r'<attribute\s+id="{name}"\s+type="[^"]*"\s+value="([^"]*)"\s*/>'


def _attr(block: str, name: str) -> str:
    m = re.search(_ATTR_RE_TMPL.format(name=re.escape(name)), block)
    return m.group(1) if m else ""


def parse_publish_handle(meta_xml: str) -> tuple[int, str]:
    """Return (PublishHandle, Name) from a meta.lsx's ModuleInfo node.

    PublishHandle is the mod.io numeric mod id (0 when not published via
    mod.io). The sibling Dependencies/ModuleShortDesc nodes also carry a
    PublishHandle, so we scope to ModuleInfo's flat attributes (before its
    <children>). Returns (0, "") if ModuleInfo isn't found.
    """
    if not meta_xml:
        return 0, ""
    start = meta_xml.find('<node id="ModuleInfo">')
    if start == -1:
        return 0, ""
    block = meta_xml[start:]
    child_split = block.find("<children>")
    flat = block[:child_split] if child_split != -1 else block

    handle_str = _attr(flat, "PublishHandle")
    try:
        handle = int(handle_str) if handle_str else 0
    except ValueError:
        handle = 0
    return handle, _attr(flat, "Name")


def decode_version64(raw: str) -> str:
    """Decode Larian's packed Version64 into ``major.minor.patch.build``."""
    try:
        value = int(raw or "0")
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    major = value >> 55
    minor = (value >> 47) & 0xFF
    patch = (value >> 31) & 0xFFFF
    build = value & 0x7FFFFFFF
    return f"{major}.{minor}.{patch}.{build}"


def parse_publish_metadata(meta_xml: str) -> tuple[int, str, str]:
    """Return ``(PublishHandle, Name, decoded version)`` from ModuleInfo."""
    if not meta_xml:
        return 0, "", ""
    start = meta_xml.find('<node id="ModuleInfo">')
    if start == -1:
        return 0, "", ""
    block = meta_xml[start:]
    child_split = block.find("<children>")
    flat = block[:child_split] if child_split != -1 else block
    handle, name = parse_publish_handle(meta_xml)
    raw_version = _attr(flat, "Version64") or _attr(flat, "Version")
    return handle, name, decode_version64(raw_version)


def read_publish_metadata_from_staging(
    staging_dir: Path,
) -> tuple[int, str, str, Optional[Path]]:
    """Find the published pak and return its id, name, version and path."""
    paks = sorted(staging_dir.rglob("*.pak"))
    first: tuple[int, str, str, Optional[Path]] = (0, "", "", None)
    for pak in paks:
        try:
            xml = extract_meta_lsx(pak)
        except Exception as e:
            app_log(f"mod.io: could not read meta.lsx from {pak.name}: {e}")
            continue
        handle, name, version = parse_publish_metadata(xml or "")
        if first[3] is None:
            first = (handle, name, version, pak)
        if handle > 0:
            return handle, name, version, pak
    return first


def read_publish_handle_from_staging(staging_dir: Path) -> tuple[int, str, Optional[Path]]:
    """Find the mod's .pak in *staging_dir* and read its PublishHandle.

    Returns (mod_id, name, pak_path).  Picks the .pak whose ModuleInfo has a
    non-zero PublishHandle; if none do, returns the first pak's values so the
    caller can still log something.
    """
    handle, name, _version, pak = read_publish_metadata_from_staging(staging_dir)
    return handle, name, pak


# ---------------------------------------------------------------------------
# Archive identification
# ---------------------------------------------------------------------------

def _md5_file(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest().lower()


def resolve_modio_meta(
    archive_path: Optional[Path],
    staging_dir: Path,
    api_key: str,
    api_path: str,
    log_fn: Optional[Callable[[str], None]] = None,
    publish_metadata: Optional[tuple[int, str, str, Optional[Path]]] = None,
    api=None,
) -> Optional[ModioMeta]:
    """Identify a BG3 mod on mod.io and return its :class:`ModioMeta`.

    *archive_path* is the original downloaded ``.zip`` (used for md5 match),
    or None when resolving an existing staged install. *publish_metadata* can
    supply metadata already read by the caller so the pak is not opened twice.
    Returns None if the mod can't be identified.
    """
    _log = log_fn or (lambda m: None)

    if not api_key:
        _log("mod.io: no API key configured - skipping.")
        return None

    if publish_metadata is None:
        mod_id, pak_name, embedded_version, pak_path = (
            read_publish_metadata_from_staging(staging_dir))
    else:
        mod_id, pak_name, embedded_version, pak_path = publish_metadata
    if mod_id <= 0:
        _log("mod.io: no PublishHandle in pak - not a mod.io mod.")
        return None
    _log(f"mod.io: PublishHandle {mod_id} ('{pak_name}') - querying files...")

    modio_api = _load_sibling("modio_api")
    try:
        api = api or modio_api.ModioAPI(api_key, api_path)
        files = api.get_mod_files(mod_id)
    except Exception as e:
        _log(f"mod.io: file lookup failed - {e}")
        return None

    if not files:
        _log(f"mod.io: mod {mod_id} has no released files.")
        return None

    latest = files[0]
    meta = ModioMeta(
        mod_id=mod_id,
        name=pak_name,
        version=embedded_version,
        latest_file_id=latest.file_id,
        latest_version=latest.version,
        installed=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
    )

    # The mod page is slug-based; the numeric id doesn't resolve in a browser,
    # so capture the real profile_url now (used by the "open page" flag click).
    try:
        meta.profile_url = api.get_mod_profile_url(mod_id)
    except Exception:
        meta.profile_url = ""

    # --- Strategy 1: md5 of the original archive matches a file's filehash ---
    matched = None
    if archive_path is not None and archive_path.is_file():
        try:
            archive_md5 = _md5_file(archive_path)
            for f in files:
                if f.md5 and f.md5 == archive_md5:
                    matched = f
                    break
        except OSError as e:
            _log(f"mod.io: could not hash archive - {e}")

    # --- Strategy 2: unpacked pak size matches filesize_uncompressed ---------
    if matched is None and pak_path is not None:
        try:
            pak_size = pak_path.stat().st_size
            size_hits = [f for f in files if f.filesize_uncompressed == pak_size]
            if len(size_hits) == 1:
                matched = size_hits[0]
                _log("mod.io: identified by uncompressed-size match (md5 miss).")
        except OSError:
            pass

    if matched is not None:
        meta.file_id = matched.file_id
        meta.version = matched.version or embedded_version
        up = " (update available)" if matched.file_id != latest.file_id else ""
        _log(f"mod.io: matched file {matched.file_id} v{matched.version}{up}.")
    else:
        detail = (f"embedded version is {embedded_version}; "
                  if embedded_version else "installed version unknown; ")
        _log(f"mod.io: mod {mod_id} tracked but installed file not identified "
             f"({detail}latest is {latest.file_id} v{latest.version}).")

    return meta


# ---------------------------------------------------------------------------
# meta.ini I/O
# ---------------------------------------------------------------------------

@locked_meta_write
def write_modio_meta(meta_ini_path: Path, meta: ModioMeta) -> None:
    """Write ``[modio]`` metadata and remove legacy ``[General]`` keys."""
    # interpolation=None so a literal '%' in a name/version isn't treated as a
    # ConfigParser interpolation token (would crash on the next read).
    cp = configparser.ConfigParser(interpolation=None)
    if meta_ini_path.is_file():
        cp.read(str(meta_ini_path), encoding="utf-8")
    if not cp.has_section(_SECTION):
        cp.add_section(_SECTION)

    cp.set(_SECTION, _KEY_MOD_ID, str(meta.mod_id))
    cp.set(_SECTION, _KEY_FILE_ID, str(meta.file_id))
    cp.set(_SECTION, _KEY_VERSION, meta.version)
    cp.set(_SECTION, _KEY_LATEST_FILE_ID, str(meta.latest_file_id))
    cp.set(_SECTION, _KEY_LATEST_VERSION, meta.latest_version)
    cp.set(_SECTION, _KEY_HAS_UPDATE, "true" if meta.has_update else "false")
    if meta.name:
        cp.set(_SECTION, _KEY_NAME, meta.name)
    if meta.profile_url:
        cp.set(_SECTION, _KEY_PROFILE_URL, meta.profile_url)
    if meta.installed:
        cp.set(_SECTION, _KEY_INSTALLED, meta.installed)

    # Existing installs used modio-prefixed keys in [General]. Once the new
    # section has been written, remove those copies to complete the migration.
    if cp.has_section(_LEGACY_SECTION):
        for legacy_key in _LEGACY_KEYS.values():
            cp.remove_option(_LEGACY_SECTION, legacy_key)
    meta.legacy_storage = False

    meta_ini_path.parent.mkdir(parents=True, exist_ok=True)
    with atomic_writer(meta_ini_path) as f:
        cp.write(f)
    app_log(f"mod.io: wrote meta.ini for mod {meta.mod_id} (file {meta.file_id})")


def read_modio_meta(meta_ini_path: Path) -> ModioMeta:
    """Read ``[modio]`` metadata, falling back to legacy ``[General]`` keys."""
    cp = configparser.ConfigParser(interpolation=None)
    cp.read(str(meta_ini_path), encoding="utf-8")
    meta = ModioMeta()
    if not cp.has_section(_SECTION) and not cp.has_section(_LEGACY_SECTION):
        return meta
    meta.legacy_storage = any(
        cp.has_option(_LEGACY_SECTION, key)
        for key in _LEGACY_KEYS.values()
    )

    def _raw(key: str, fallback: str = "") -> str:
        if cp.has_option(_SECTION, key):
            return cp.get(_SECTION, key, fallback=fallback)
        legacy_key = _LEGACY_KEYS[key]
        return cp.get(_LEGACY_SECTION, legacy_key, fallback=fallback)

    def _int(key: str) -> int:
        try:
            return int(_raw(key, "0") or "0")
        except ValueError:
            return 0

    meta.mod_id = _int(_KEY_MOD_ID)
    meta.file_id = _int(_KEY_FILE_ID)
    meta.version = _raw(_KEY_VERSION)
    meta.latest_file_id = _int(_KEY_LATEST_FILE_ID)
    meta.latest_version = _raw(_KEY_LATEST_VERSION)
    meta.has_update = _raw(_KEY_HAS_UPDATE).strip().lower() in (
        "true", "1", "yes")
    meta.name = _raw(_KEY_NAME)
    meta.profile_url = _raw(_KEY_PROFILE_URL)
    meta.installed = _raw(_KEY_INSTALLED)
    return meta
