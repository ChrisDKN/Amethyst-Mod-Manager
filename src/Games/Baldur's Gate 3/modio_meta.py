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

Results are written into the same ``meta.ini`` Nexus uses, under modio-
prefixed keys, via :func:`write_modio_meta` (non-destructive).

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
from Utils.pak_reader import extract_meta_lsx

_SECTION = "General"

# meta.ini keys we manage.  Prefixed "modio" so they never collide with the
# Nexus keys in the same [General] section.
_KEY_MOD_ID = "modioModId"
_KEY_FILE_ID = "modioFileId"
_KEY_VERSION = "modioVersion"
_KEY_LATEST_FILE_ID = "modioLatestFileId"
_KEY_LATEST_VERSION = "modioLatestVersion"
_KEY_INSTALLED = "modioInstalled"
_KEY_NAME = "modioName"
_KEY_PROFILE_URL = "modioProfileUrl"


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
    installed: str = ""


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


def read_publish_handle_from_staging(staging_dir: Path) -> tuple[int, str, Optional[Path]]:
    """Find the mod's .pak in *staging_dir* and read its PublishHandle.

    Returns (mod_id, name, pak_path).  Picks the .pak whose ModuleInfo has a
    non-zero PublishHandle; if none do, returns the first pak's values so the
    caller can still log something.
    """
    paks = sorted(staging_dir.rglob("*.pak"))
    first: tuple[int, str, Optional[Path]] = (0, "", None)
    for pak in paks:
        try:
            xml = extract_meta_lsx(pak)
        except Exception as e:
            app_log(f"mod.io: could not read meta.lsx from {pak.name}: {e}")
            continue
        handle, name = parse_publish_handle(xml or "")
        if first[2] is None:
            first = (handle, name, pak)
        if handle > 0:
            return handle, name, pak
    return first


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
    archive_path: Path,
    staging_dir: Path,
    api_key: str,
    log_fn: Optional[Callable[[str], None]] = None,
) -> Optional[ModioMeta]:
    """Identify a BG3 mod on mod.io and return its :class:`ModioMeta`.

    *archive_path* is the original downloaded ``.zip`` (used for md5 match);
    *staging_dir* is the extracted mod folder (used to read PublishHandle
    from the inner ``.pak``).  Returns None if the mod can't be identified
    (not a mod.io pak, no API key, or no match).
    """
    _log = log_fn or (lambda m: None)

    if not api_key:
        _log("mod.io: no API key configured - skipping.")
        return None

    mod_id, pak_name, pak_path = read_publish_handle_from_staging(staging_dir)
    if mod_id <= 0:
        _log("mod.io: no PublishHandle in pak - not a mod.io mod.")
        return None
    _log(f"mod.io: PublishHandle {mod_id} ('{pak_name}') - querying files...")

    modio_api = _load_sibling("modio_api")
    try:
        api = modio_api.ModioAPI(api_key)
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
    if archive_path.is_file():
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
        meta.version = matched.version
        up = " (update available)" if matched.file_id != latest.file_id else ""
        _log(f"mod.io: matched file {matched.file_id} v{matched.version}{up}.")
    else:
        # Can't pin the installed version; record the mod + latest anyway.
        # file_id stays 0 → the update checker reports this mod as "unknown".
        _log(f"mod.io: mod {mod_id} tracked but installed file not identified "
             f"(latest is {latest.file_id} v{latest.version}).")

    return meta


# ---------------------------------------------------------------------------
# meta.ini I/O
# ---------------------------------------------------------------------------

def write_modio_meta(meta_ini_path: Path, meta: ModioMeta) -> None:
    """Write mod.io keys into meta.ini, preserving all existing content."""
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
    if meta.name:
        cp.set(_SECTION, _KEY_NAME, meta.name)
    if meta.profile_url:
        cp.set(_SECTION, _KEY_PROFILE_URL, meta.profile_url)
    if meta.installed:
        cp.set(_SECTION, _KEY_INSTALLED, meta.installed)

    meta_ini_path.parent.mkdir(parents=True, exist_ok=True)
    with open(meta_ini_path, "w", encoding="utf-8") as f:
        cp.write(f)
    app_log(f"mod.io: wrote meta.ini for mod {meta.mod_id} (file {meta.file_id})")


def read_modio_meta(meta_ini_path: Path) -> ModioMeta:
    """Read mod.io keys from meta.ini (missing keys default)."""
    cp = configparser.ConfigParser(interpolation=None)
    cp.read(str(meta_ini_path), encoding="utf-8")
    meta = ModioMeta()
    if not cp.has_section(_SECTION):
        return meta

    def _int(key: str) -> int:
        try:
            return int(cp.get(_SECTION, key, fallback="0") or "0")
        except ValueError:
            return 0

    meta.mod_id = _int(_KEY_MOD_ID)
    meta.file_id = _int(_KEY_FILE_ID)
    meta.version = cp.get(_SECTION, _KEY_VERSION, fallback="")
    meta.latest_file_id = _int(_KEY_LATEST_FILE_ID)
    meta.latest_version = cp.get(_SECTION, _KEY_LATEST_VERSION, fallback="")
    meta.name = cp.get(_SECTION, _KEY_NAME, fallback="")
    meta.profile_url = cp.get(_SECTION, _KEY_PROFILE_URL, fallback="")
    meta.installed = cp.get(_SECTION, _KEY_INSTALLED, fallback="")
    return meta
