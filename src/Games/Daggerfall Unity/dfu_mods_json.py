"""
dfu_mods_json.py
Load-order sync for Daggerfall Unity's Mods.json.

DFU discovers .dfmod files by scanning StreamingAssets/Mods recursively and
assigns each one a LoadPriority from the scan order - which is filesystem
order, not anything the user chose.  It then overlays whatever is stored in
<PersistentDataPath>/Mods/GameData/Mods.json, reading back only Title,
Enabled and LoadPriority per entry and matching on Title.  Higher LoadPriority
wins an asset conflict, so Amethyst's top-of-list mod must get the *highest*
priority.

Matching is by ModTitle, which lives inside the .dfmod asset bundle.  It is
read from the sibling <name>.dfmod.json manifest when the author shipped one
(the Mod Builder emits it alongside the bundle); failing that a best-effort
byte scan of the bundle is attempted.  Entries we cannot title are still
written but DFU will simply not match them, so a miss costs ordering, never
correctness.

Set AMM_DFU_MODS_JSON=0 to skip this step entirely and manage the load order
from DFU's own Mod Loader UI.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

# Written next to Mods.json before the first sync so restore() can put the
# user's own file back.  A zero-byte backup means "there was no file here".
BACKUP_SUFFIX = ".amm-backup"

# Cap the best-effort bundle scan - DREAM and friends ship multi-hundred-MB
# .dfmod files and the manifest, when it is findable at all, sits near the top.
_SCAN_LIMIT = 32 * 1024 * 1024
_SCAN_CHUNK = 1 * 1024 * 1024

_TITLE_RE = re.compile(rb'"ModTitle"\s*:\s*"((?:[^"\\]|\\.)*)"')


def _noop(_msg: str) -> None:
    pass


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def persistent_data_dir(game_path: Path) -> Path:
    """Return DFU's PersistentDataPath for this install."""
    # DaggerfallUnityApplication: a Portable.txt next to the player redirects
    # the whole persistent path into the install folder.
    if (game_path / "Portable.txt").is_file():
        return game_path / "PortableAppdata"
    cfg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(cfg) / "unity3d" / "Daggerfall Workshop" / "Daggerfall Unity"


def mods_json_path(game_path: Path) -> Path:
    """Return the Mods.json that carries the load order."""
    return persistent_data_dir(game_path) / "Mods" / "GameData" / "Mods.json"


# ---------------------------------------------------------------------------
# Titles
# ---------------------------------------------------------------------------

def _title_from_manifest(dfmod: Path) -> str | None:
    manifest = dfmod.with_name(dfmod.name + ".json")   # foo.dfmod -> foo.dfmod.json
    if not manifest.is_file():
        return None
    try:
        data = json.loads(manifest.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None
    title = data.get("ModTitle") if isinstance(data, dict) else None
    return title.strip() if isinstance(title, str) and title.strip() else None


def _title_from_bundle(dfmod: Path) -> str | None:
    """Scan the bundle head for an embedded manifest; None when compressed."""
    try:
        read = 0
        tail = b""
        with dfmod.open("rb") as fh:
            while read < _SCAN_LIMIT:
                chunk = fh.read(_SCAN_CHUNK)
                if not chunk:
                    break
                read += len(chunk)
                m = _TITLE_RE.search(tail + chunk)
                if m:
                    title = m.group(1).decode("utf-8", "replace")
                    title = title.encode().decode("unicode_escape", "replace")
                    return title.strip() or None
                # Overlap so a match straddling the chunk boundary is not lost.
                tail = chunk[-512:]
    except OSError:
        return None
    return None


def read_mod_title(dfmod: Path) -> str | None:
    """Return the mod's ModTitle, or None when it cannot be determined."""
    return _title_from_manifest(dfmod) or _title_from_bundle(dfmod)


# ---------------------------------------------------------------------------
# Sync / restore
# ---------------------------------------------------------------------------

def _load_existing(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return []
    return [e for e in data if isinstance(e, dict)] if isinstance(data, list) else []


def _ensure_backup(path: Path, log_fn) -> None:
    backup = path.with_name(path.name + BACKUP_SUFFIX)
    if backup.exists():
        return
    backup.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        shutil.copy2(path, backup)
        log_fn(f"  Backed up the existing Mods.json → {backup.name}.")
    else:
        backup.write_bytes(b"")


def sync_mods_json(game_path: Path, ordered: list[Path], log_fn=None) -> int:
    """Write Amethyst's load order into Mods.json; returns entries written.

    *ordered* is the deployed .dfmod paths in Amethyst priority order -
    index 0 is the top of the mod list and must end up with the highest
    DFU LoadPriority.
    """
    _log = log_fn or _noop
    if os.environ.get("AMM_DFU_MODS_JSON") == "0":
        _log("  Mods.json sync disabled (AMM_DFU_MODS_JSON=0) - "
             "set the load order in DFU's Mod Loader.")
        return 0

    path = mods_json_path(game_path)
    _ensure_backup(path, _log)

    titles: list[tuple[str, str]] = []          # (FileName, Title)
    untitled: list[str] = []
    for dfmod in ordered:
        stem = dfmod.name[: -len(".dfmod")]
        title = read_mod_title(dfmod)
        if title is None:
            untitled.append(stem)
            title = stem
        titles.append((stem, title))

    managed = {t for _, t in titles}
    preserved = [e for e in _load_existing(path)
                 if isinstance(e.get("Title"), str) and e["Title"] not in managed]
    # Sit the managed block above anything hand-added so a manually dropped
    # mod can never outrank a mod the user ordered in Amethyst.
    base = max((e.get("LoadPriority", 0) for e in preserved
                if isinstance(e.get("LoadPriority"), int)), default=-1) + 1

    total = len(titles)
    entries = list(preserved)
    for i, (stem, title) in enumerate(titles):
        entries.append({
            "FileName": stem,
            "Title": title,
            "Enabled": True,
            "LoadPriority": base + (total - 1 - i),
        })

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, indent=4), encoding="utf-8")

    _log(f"  Wrote {total} mod(s) to {path}.")
    if preserved:
        _log(f"  Left {len(preserved)} entry(s) not managed by Amethyst untouched.")
    if untitled:
        _log("  No ModTitle found for: " + ", ".join(untitled) +
             " - these ship no .dfmod.json manifest, so DFU will keep its own "
             "load position for them. Order them in DFU's Mod Loader.")
    return total


def restore_mods_json(game_path: Path, log_fn=None) -> bool:
    """Put the pre-deploy Mods.json back; returns True if anything changed."""
    _log = log_fn or _noop
    path = mods_json_path(game_path)
    backup = path.with_name(path.name + BACKUP_SUFFIX)
    if not backup.exists():
        return False
    try:
        if backup.stat().st_size == 0:
            # There was no Mods.json before we deployed.
            path.unlink(missing_ok=True)
            _log("  Removed the Mods.json written at deploy time.")
        else:
            shutil.copy2(backup, path)
            _log("  Restored the pre-deploy Mods.json.")
        backup.unlink(missing_ok=True)
        return True
    except OSError as exc:
        _log(f"  Could not restore Mods.json: {exc}")
        return False
