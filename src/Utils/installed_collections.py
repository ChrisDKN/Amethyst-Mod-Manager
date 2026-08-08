"""Toolkit-neutral record keeping for collections APPENDED to a profile.

When a collection is appended (installed into an existing profile rather than
a fresh one) we save its manifest + card display info to
``<profile>/installed_collections/<slug>.json`` so the Collections browser can
list "Collections appended to this profile" and offer a clean Remove that
deletes exactly the mods that collection installed.

Ownership matching mirrors collection_diff: a mod belongs to the collection if
its meta.ini ``fromCollection`` equals the slug, or (legacy/un-tagged) its
``fileid`` appears in the saved manifest. Mods stamped by ANOTHER collection or
installed manually are never touched.

Pure stdlib + Utils.* - no GUI toolkit imports.
"""

from __future__ import annotations

import configparser
import json
from datetime import datetime, timezone
from pathlib import Path

from Utils.modlist import read_modlist, write_modlist

INSTALLED_COLLECTIONS_DIR = "installed_collections"


def _safe_filename(slug: str) -> str:
    return "".join(c if (c.isalnum() or c in "-_.") else "_" for c in slug) or "collection"


def record_appended_collection(profile_dir: Path, *, slug: str,
                               revision: "int | None", card: dict,
                               manifest: dict, log_fn=None) -> "Path | None":
    """Write (or overwrite - latest append wins) the record for *slug*."""
    log = log_fn or (lambda _m: None)
    try:
        folder = Path(profile_dir) / INSTALLED_COLLECTIONS_DIR
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{_safe_filename(slug)}.json"
        record = {
            "version": 1,
            "slug": slug,
            "revision": revision,
            "saved": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "card": dict(card or {}),
            "manifest": manifest if isinstance(manifest, dict) else {},
        }
        path.write_text(json.dumps(record, indent=2), encoding="utf-8")
        log(f"Appended collection recorded: {path}")
        return path
    except Exception as exc:
        log(f"could not record appended collection '{slug}': {exc}")
        return None


def list_appended_collections(profile_dir: "Path | None", log_fn=None) -> list[dict]:
    """Return the appended-collection records for *profile_dir*, sorted by
    display name. Corrupt/unreadable files are skipped. Each record gets a
    ``path`` key (Path to its json file) injected."""
    log = log_fn or (lambda _m: None)
    out: list[dict] = []
    if not profile_dir:
        return out
    folder = Path(profile_dir) / INSTALLED_COLLECTIONS_DIR
    if not folder.is_dir():
        return out
    for f in sorted(folder.glob("*.json")):
        try:
            record = json.loads(f.read_text(encoding="utf-8"))
            if not isinstance(record, dict) or not record.get("slug"):
                raise ValueError("not a collection record")
        except Exception as exc:
            log(f"skipping corrupt appended-collection record {f.name}: {exc}")
            continue
        record["path"] = f
        out.append(record)
    out.sort(key=lambda r: str((r.get("card") or {}).get("name")
                               or r.get("slug") or "").lower())
    return out


def _manifest_file_ids(manifest: dict) -> set[int]:
    """Set of fileIds from a collection.json manifest (collection_diff shape)."""
    fids: set[int] = set()
    mods = manifest.get("mods") if isinstance(manifest, dict) else None
    if not isinstance(mods, list):
        return fids
    for entry in mods:
        src = entry.get("source") if isinstance(entry, dict) else None
        if not isinstance(src, dict):
            continue
        try:
            fid = int(src.get("fileId"))
        except (TypeError, ValueError):
            continue
        if fid > 0:
            fids.add(fid)
    return fids


def resolve_owned_mod_names(game, profile_dir: Path, record: dict) -> list[str]:
    """Mod folder names in the profile's modlist owned by *record*'s collection.

    Owned = meta.ini ``fromCollection`` == slug, OR (no fromCollection tag AND
    fileid present in the record's saved manifest). Same safety rule as
    collection_diff: mods from other collections / manual installs are skipped.
    """
    slug = str(record.get("slug") or "")
    if game is None or not slug:
        return []
    try:
        staging = game.get_effective_mod_staging_path()
    except Exception:
        return []
    manifest_fids = _manifest_file_ids(record.get("manifest") or {})
    owned: list[str] = []
    for entry in read_modlist(Path(profile_dir) / "modlist.txt"):
        if entry.is_separator:
            continue
        meta_ini = staging / entry.name / "meta.ini"
        origin = ""
        file_id = 0
        if meta_ini.is_file():
            cp = configparser.ConfigParser()
            try:
                cp.read(str(meta_ini), encoding="utf-8")
                if cp.has_section("General"):
                    origin = cp.get("General", "fromCollection", fallback="").strip()
                    try:
                        file_id = int(cp.get("General", "fileid", fallback="0") or "0")
                    except ValueError:
                        pass
            except Exception:
                pass
        if origin == slug or (not origin and file_id > 0 and file_id in manifest_fids):
            owned.append(entry.name)
    return owned


def remove_appended_collection(game, profile_dir: Path, record: dict,
                               mod_names: list[str], log_fn=None) -> None:
    """Remove *mod_names* (from resolve_owned_mod_names) and the record file.

    Full mod removal (undeploy + plugins + staging + indexes) via
    mod_remove.remove_mods, then strips the names from modlist.txt
    (separators and other mods keep their positions).
    """
    log = log_fn or (lambda _m: None)
    if mod_names:
        from Utils.mod_remove import remove_mods
        remove_mods(game, Path(profile_dir), list(mod_names), log_fn=log)
        from Utils.modlist import modlist_lock
        modlist_path = Path(profile_dir) / "modlist.txt"
        removed_lower = {n.lower() for n in mod_names}
        with modlist_lock(modlist_path):
            entries = read_modlist(modlist_path)
            kept = [e for e in entries
                    if e.is_separator or e.name.lower() not in removed_lower]
            if len(kept) < len(entries):
                write_modlist(modlist_path, kept)
    path = record.get("path")
    try:
        if path:
            Path(path).unlink(missing_ok=True)
            log(f"Appended collection record removed: {path}")
    except Exception as exc:
        log(f"could not delete appended-collection record: {exc}")
