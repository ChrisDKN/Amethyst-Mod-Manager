"""profile_export.py — neutral (GUI-free) helpers for the "Export profile" feature.

Packages the current profile into a shareable, zipped ``.amethyst`` manifest:
``manifest.json`` + bundled ``mods/`` + ``overwrite/`` + ``profile/`` state files.
This is the Amethyst/Nexus-Collections manifest format, so an exported profile can
be re-imported through the collection-install pipeline.

All logic here is a straight port of the pure parts of the Tk ``workshop_dialog``
(``_load_mods`` / ``_write_settings`` / ``_read_settings`` / ``_write_manifest``),
with every tkinter/CTk dependency removed so the Qt view (and CLI / tests) can call
it directly. No PySide6 or tkinter imports.

A *row* is a plain dict describing one mod's export configuration::

    {
        "name":          str,   # mod folder name
        "mod_id":        int,   # from meta.ini
        "file_id":       int,   # from meta.ini (may be overridden by ver_label)
        "version":       str,   # from meta.ini
        "category_id":   int,
        "category_name": str,
        "ver_label":     str,   # "fileid — version" or "—"
        "ver_options":   list,  # [{"label", "name", "size_bytes"}]
        "optional":      bool,  # user-set
        "has_fomod":     bool,  # has a FOMOD or BAIN sidecar
        "has_bain":      bool,
        "fomod_export":  bool,  # include installer choices in the export
        "versions_fetched": bool,
        "size_bytes":    int,   # original archive size from meta.ini (0 if unknown)
        "root_folder":   bool,  # deploys to game root (meta.ini rootFolder)
        "enabled":       bool,  # modlist enabled state of the source entry
        "source":        str,   # "nexus" | "direct" | "bundle" | "ignore"
        "direct_url":    str,
    }
"""

from __future__ import annotations

import base64
import json
import zlib
import zipfile
import shutil
from pathlib import Path

from Utils.config_paths import get_fomod_selections_path, get_bain_selections_path


# ---------------------------------------------------------------------------
# Row building (port of workshop_dialog._load_mods)
# ---------------------------------------------------------------------------

def load_rows(entries, game) -> list[dict]:
    """Build the per-mod export rows from a list of modlist ``ModEntry`` objects.

    *entries* — enabled, non-separator modlist entries (high-priority first, like
                the Tk workshop). *game* — the configured Game object; used for the
                staging path and active profile dir.
    """
    from Nexus.nexus_meta import read_meta

    staging_root = game.get_effective_mod_staging_path() if game else None
    profile_dir = getattr(game, "_active_profile_dir", None) if game else None

    rows: list[dict] = []
    for entry in entries:
        name = getattr(entry, "name", None) or str(entry)
        mod_id = file_id = 0
        version = ""
        category_id = 0
        category_name = ""
        size_bytes = 0
        root_folder = False
        if staging_root:
            meta_path = Path(staging_root) / name / "meta.ini"
            if meta_path.is_file():
                try:
                    meta = read_meta(meta_path)
                    mod_id = meta.mod_id or 0
                    file_id = meta.file_id or 0
                    version = meta.version or ""
                    category_id = meta.category_id or 0
                    category_name = meta.category_name or ""
                    size_bytes = meta.file_size or 0
                    root_folder = bool(meta.root_folder)
                except Exception:
                    pass

        if file_id and version:
            ver_label = f"{file_id} — {version}"
        elif file_id:
            ver_label = str(file_id)
        else:
            ver_label = "—"

        has_fomod = bool(
            profile_dir
            and (Path(profile_dir) / "fomod" / f"{name}.json").is_file()
        )
        has_bain = bool(
            profile_dir
            and (Path(profile_dir) / "bain" / f"{name}.json").is_file()
        )
        # A mod is only ever FOMOD or BAIN; the single "Fomod" column toggles
        # export of whichever installer choices the mod has.
        has_installer = has_fomod or has_bain

        rows.append({
            "name":             name,
            "mod_id":           mod_id,
            "file_id":          file_id,
            "version":          version,
            "category_id":      category_id,
            "category_name":    category_name,
            "ver_label":        ver_label,
            "ver_options":      [{"label": ver_label, "name": "", "size_bytes": 0}],
            "optional":         False,
            "has_fomod":        has_installer,
            "has_bain":         has_bain,
            "fomod_export":     has_installer,
            "versions_fetched": False,
            "size_bytes":       size_bytes,
            "root_folder":      root_folder,
            "enabled":          bool(getattr(entry, "enabled", True)),
            "source":           "nexus",
            "direct_url":       "",
        })

    return rows


# ---------------------------------------------------------------------------
# Save / load export settings (port of _write_settings / _read_settings)
# ---------------------------------------------------------------------------

def _row_file_id(row: dict) -> int:
    """The effective file id for a row: the explicit ``file_id``, else parsed from
    the ``ver_label`` ("fileid — version")."""
    fid = row.get("file_id") or 0
    if not fid:
        lbl = row.get("ver_label", "")
        if lbl and " — " in lbl:
            try:
                fid = int(lbl.split(" — ")[0])
            except ValueError:
                fid = 0
    return fid


def write_settings(out_path, rows) -> Path:
    """Persist the per-mod export flags (optional/source/version) to a JSON file.
    Returns the written path (suffix forced to .json)."""
    out_path = Path(out_path)
    if out_path.suffix.lower() != ".json":
        out_path = out_path.with_suffix(".json")
    data = {
        "version": 1,
        "mods": [
            {
                "name":       r["name"],
                "optional":   r["optional"],
                "source":     r.get("source", "nexus"),
                "direct_url": r.get("direct_url", ""),
                "file_id":    _row_file_id(r),
                "ver_label":  r.get("ver_label", "—"),
            }
            for r in rows
        ],
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    return out_path


def read_settings(in_path, rows) -> None:
    """Apply a saved settings JSON back onto *rows* in place. The installed file's
    ``file_id`` (from meta.ini) always takes precedence over the saved one."""
    with open(in_path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    by_name = {m["name"]: m for m in data.get("mods", [])}
    for row in rows:
        m = by_name.get(row["name"])
        if not m:
            continue
        row["optional"] = bool(m.get("optional", False))
        row["source"] = m.get("source", "nexus")
        row["direct_url"] = m.get("direct_url", "")
        # Only apply file_id / ver_label from the JSON when the mod has no file_id
        # already set from meta.ini — the installed file takes precedence.
        if not row.get("file_id"):
            if m.get("file_id"):
                row["file_id"] = m["file_id"]
            if m.get("ver_label"):
                row["ver_label"] = m["ver_label"]
                # Back-fill file_id from ver_label if still missing.
                if not row.get("file_id") and " — " in row["ver_label"]:
                    try:
                        row["file_id"] = int(row["ver_label"].split(" — ")[0])
                    except ValueError:
                        pass


# ---------------------------------------------------------------------------
# Manifest build + zip (port of _write_manifest)
# ---------------------------------------------------------------------------

def nexus_missing_file_ids(rows) -> list[str]:
    """Names of Nexus-source mods that lack a File ID (can't be exported until set)."""
    return [
        row["name"] for row in rows
        if row.get("source", "nexus") == "nexus" and not row.get("file_id")
    ]


def build_manifest(rows, game_domain: str, app_version: str, *,
                   game_name=None, profile_dir=None) -> dict:
    """Build the ``manifest.json`` dict from the export *rows*. Mods with
    ``source == "ignore"`` are dropped. FOMOD/BAIN installer choices are embedded
    when ``fomod_export`` is set and a sidecar exists (profile-local preferred)."""
    mods: list[dict] = []
    for row in rows:
        if row.get("source") == "ignore":
            continue
        # Parse fileid from ver_label ("fileid — version") when present.
        ver_label = row["ver_label"]
        file_id = row["file_id"]
        if ver_label and " — " in ver_label:
            try:
                file_id = int(ver_label.split(" — ")[0])
            except ValueError:
                pass

        row_source = row.get("source", "nexus")
        if row_source == "direct":
            source: dict = {
                "type": "direct",
                "url":  row.get("direct_url", ""),
            }
        elif row_source == "bundle":
            source = {"bundle": True}
        else:
            source = {
                "modId":  row["mod_id"],
                "fileId": file_id,
                "logicalFilename": row["name"],
            }
            if row.get("size_bytes"):
                source["fileSize"] = row["size_bytes"]

        mod_entry: dict = {
            "name":     row["name"],
            "source":   source,
            "optional": row["optional"],
        }
        # Carry a disabled modlist state (enabled entries stay implicit). The
        # importer stages the mod normally, then marks it disabled.
        if row.get("enabled") is False:
            mod_entry["enabled"] = False
        # Root-deploy mods use the Nexus-collections "dinput" install type —
        # the importer already maps details.type == "dinput" to meta rootFolder.
        if row.get("root_folder"):
            mod_entry["details"] = {"type": "dinput"}

        # Include version and category from meta.ini if available.
        row_version = row.get("version") or ""
        if not row_version and ver_label and " — " in ver_label:
            row_version = ver_label.split(" — ", 1)[1]
        if row_version:
            mod_entry["version"] = row_version
        cat_id = row.get("category_id") or 0
        cat_name = row.get("category_name") or ""
        if cat_id or cat_name:
            mod_entry["category"] = {}
            if cat_id:
                mod_entry["category"]["id"] = cat_id
            if cat_name:
                mod_entry["category"]["name"] = cat_name

        if row["has_fomod"] and row.get("fomod_export", True) and game_name:
            # Prefer the profile-local copy so exports stay profile-specific even
            # if the global installer settings differ. A mod is either BAIN or
            # FOMOD — pick the right sidecar + type.
            if row.get("has_bain"):
                sub_dir, choices_type, path_fn = (
                    "bain", "bain_selections", get_bain_selections_path)
            else:
                sub_dir, choices_type, path_fn = (
                    "fomod", "fomod_selections", get_fomod_selections_path)
            choices_path = None
            if profile_dir:
                candidate = Path(profile_dir) / sub_dir / f"{row['name']}.json"
                if candidate.is_file():
                    choices_path = candidate
            if choices_path is None:
                choices_path = path_fn(game_name, row["name"])
            if choices_path.is_file():
                try:
                    with choices_path.open("r", encoding="utf-8") as fh:
                        choices_data = json.load(fh)
                    mod_entry["choices"] = {
                        "type":       choices_type,
                        "selections": choices_data,
                    }
                except Exception:
                    pass

        mods.append(mod_entry)

    return {
        "AmethystManifest": True,
        "info": {
            "domainName": game_domain,
            "appVersion": app_version,
        },
        "mods": mods,
    }


def _collect_amethyst_jobs(manifest: dict, *, staging_root=None, overwrite_root=None,
                           profile_dir=None, bundle_names=None,
                           arc_prefix: str = "") -> list[tuple]:
    """Collect (src_path, arcname, data) job tuples for one profile's export
    (src_path=None means an in-memory writestr member, pre-encoded in *data*).

    *arc_prefix*, when given, namespaces every arcname under it (e.g.
    ``"<member_name>/"``) — used by the Profile Group exporter to combine
    several members' jobs into one shared zip without them colliding."""
    bundle_names = list(bundle_names or [])
    staging_root = Path(staging_root) if staging_root else None
    overwrite_root = Path(overwrite_root) if overwrite_root else None

    def _arc(rel) -> str:
        return (Path(arc_prefix) / rel).as_posix() if arc_prefix else Path(rel).as_posix()

    jobs: list[tuple] = [
        (None, _arc("manifest.json"), json.dumps(manifest, indent=2).encode("utf-8")),
    ]

    if staging_root:
        for name in bundle_names:
            mod_dir = staging_root / name
            if not mod_dir.is_dir():
                continue
            for fp in mod_dir.rglob("*"):
                if fp.is_file():
                    jobs.append((fp, _arc(Path("mods") / name / fp.relative_to(mod_dir)), None))

    if overwrite_root and overwrite_root.is_dir():
        for fp in overwrite_root.rglob("*"):
            if fp.is_file():
                jobs.append((fp, _arc(Path("overwrite") / fp.relative_to(overwrite_root)), None))

    # Bundle profile state files: fixed names + any *.ini files.
    if profile_dir:
        pdir = Path(profile_dir)
        fixed = [
            "modlist.txt",
            "plugins.txt",
            "loadorder.txt",
            "profile_state.json",
            "userlist.yaml",
        ]
        for fname in fixed:
            fp = pdir / fname
            if not fp.is_file():
                continue
            if fname == "profile_state.json":
                # Inject profile_specific_mods=true if missing.
                try:
                    ps = json.loads(fp.read_text(encoding="utf-8"))
                except Exception:
                    ps = {}
                if not isinstance(ps, dict):
                    ps = {}
                settings = ps.get("profile_settings")
                if not isinstance(settings, dict):
                    settings = {}
                    ps["profile_settings"] = settings
                if not settings.get("profile_specific_mods"):
                    settings["profile_specific_mods"] = True
                jobs.append((None, _arc(Path("profile") / fname),
                             json.dumps(ps, indent=2).encode("utf-8")))
            else:
                jobs.append((fp, _arc(Path("profile") / fname), None))
        # Legacy: root-level *.ini files
        for fp in pdir.glob("*.ini"):
            if fp.is_file():
                jobs.append((fp, _arc(Path("profile") / fp.name), None))
        # Bundle whole profile subfolders
        for sub in ("ini files", "Saves", "installed_collections"):
            sub_dir = pdir / sub
            if not sub_dir.is_dir():
                continue
            for fp in sub_dir.rglob("*"):
                if fp.is_file():
                    jobs.append((fp, _arc(Path("profile") / sub / fp.relative_to(sub_dir)), None))

    return jobs


def _write_zip_jobs(out_path: Path, jobs: list[tuple], progress_cb=None) -> None:
    """Write a flat (src_path_or_None, arcname, data_or_None) job list to one
    zip. *progress_cb* (optional) is called as
    ``progress_cb(done_bytes, total_bytes, arcname)`` — once with
    ``done_bytes=0`` before writing starts (total known), then after each
    member is written."""
    sizes = []
    for src, _arc, data in jobs:
        if data is not None:
            sizes.append(len(data))
        else:
            try:
                sizes.append(src.stat().st_size)
            except OSError:
                sizes.append(0)
    total = sum(sizes)
    done = 0
    if progress_cb:
        progress_cb(0, total, "")

    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for (src, arcname, data), size in zip(jobs, sizes):
            if data is not None:
                zf.writestr(arcname, data)
            else:
                zf.write(src, arcname)
            done += size
            if progress_cb:
                progress_cb(done, total, arcname)


def write_amethyst(out_path, manifest: dict, *, staging_root=None,
                   overwrite_root=None, profile_dir=None,
                   bundle_names=None, progress_cb=None) -> Path:
    """Write the ``.amethyst`` zip: ``manifest.json`` + bundled ``mods/`` +
    ``overwrite/`` + ``profile/`` state files. Returns the final path (suffix
    forced to .amethyst when not already .zip/.amethyst).

    *progress_cb* (optional) is called as ``progress_cb(done_bytes, total_bytes,
    arcname)`` — once with ``done_bytes=0`` before writing starts (total known),
    then after each member is written. Members are collected up-front so the
    byte total is exact; the callback runs on the caller's thread."""
    out_path = Path(out_path)
    if out_path.suffix.lower() not in (".zip", ".amethyst"):
        out_path = out_path.with_suffix(".amethyst")

    jobs = _collect_amethyst_jobs(
        manifest, staging_root=staging_root, overwrite_root=overwrite_root,
        profile_dir=profile_dir, bundle_names=bundle_names)
    _write_zip_jobs(out_path, jobs, progress_cb)
    return out_path


# ---------------------------------------------------------------------------
# Import side
# ---------------------------------------------------------------------------

def read_manifest(src_path) -> dict:
    """Parse the manifest from a ``.amethyst``/``.zip`` archive (extracts the
    inner ``manifest.json``) or a bare ``.json`` file. Returns the parsed dict.
    Raises on read/parse failure."""
    src_path = Path(src_path)
    import json as _json
    import zipfile as _zip
    if _zip.is_zipfile(src_path):
        with _zip.ZipFile(src_path, "r") as zf:
            # Prefer a top-level manifest.json; else the first *manifest.json.
            names = zf.namelist()
            member = None
            for cand in ("manifest.json", "collection.json"):
                if cand in names:
                    member = cand
                    break
            if member is None:
                member = next(
                    (n for n in names if n.rsplit("/", 1)[-1] == "manifest.json"),
                    None)
            if member is None:
                raise ValueError("No manifest.json found in archive.")
            with zf.open(member) as fh:
                return _json.loads(fh.read().decode("utf-8"))
    return _json.loads(src_path.read_text(encoding="utf-8"))


def install_local_bundle(src_path, profile_dir, mods_dir, overwrite_dir=None, *,
                         log_fn=None, member_prefix: str = "") -> list[str]:
    """Extract a locally-exported ``.amethyst`` bundle into a freshly-installed
    profile — faithful to the Tk import (CollectionsDialog bundle-zip extraction):

      * ``mods/<name>/…``      → ``<mods_dir>/<name>/…`` **verbatim** (folder names
        preserved exactly, including spaces, keeping the archive's own meta.ini) so
        each bundled mod matches its ``modlist.txt`` entry.
      * ``overwrite/…``        → ``<overwrite_dir>/…``
      * ``profile/…``          → ``<profile_dir>/…`` (modlist.txt, plugins.txt,
        loadorder.txt, profile_state.json, userlist.yaml, *.ini, ini files/, Saves/).

    Nexus-source mods are installed by the collection pipeline; this covers the
    bundled assets + profile state files that live *inside* the local zip.

    *member_prefix*, when given (e.g. ``"<member_name>"``), restricts extraction
    to entries under ``<member_prefix>/mods/…`` etc. and strips that prefix
    first — used by the Profile Group importer, where several members' files
    share one zip under member-name subfolders.

    Returns the list of extracted bundle folder names.
    """
    import zipfile as _zip
    log = log_fn or (lambda _m: None)
    src_path = Path(src_path)
    profile_dir = Path(profile_dir)
    mods_dir = Path(mods_dir)
    overwrite_dir = Path(overwrite_dir) if overwrite_dir else (mods_dir.parent / "overwrite")
    if not _zip.is_zipfile(src_path):
        return []

    mods_dir.mkdir(parents=True, exist_ok=True)
    overwrite_dir.mkdir(parents=True, exist_ok=True)
    name_prefix = f"{member_prefix}/" if member_prefix else ""

    staged: list[str] = []
    with _zip.ZipFile(src_path, "r") as zf:
        raw_names = zf.namelist()
        # (original_name, name_relative_to_this_member) pairs — zf.open() needs
        # the ORIGINAL (still-prefixed) name; parsing needs the stripped one.
        if name_prefix:
            entries = [(orig, orig[len(name_prefix):]) for orig in raw_names
                      if orig.startswith(name_prefix)]
        else:
            entries = [(orig, orig) for orig in raw_names]

        # (1) Bundled mods + overwrite — extract verbatim (no rename, keep meta.ini).
        for orig, n in entries:
            if n.endswith("/"):
                continue
            parts = n.split("/")
            if len(parts) < 2:
                continue
            if parts[0] == "mods":
                dest = mods_dir / Path(*parts[1:])
                if len(parts) >= 2 and parts[1] not in staged:
                    staged.append(parts[1])
            elif parts[0] == "overwrite":
                dest = overwrite_dir / Path(*parts[1:])
            else:
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(orig) as srcf, open(dest, "wb") as dstf:
                shutil.copyfileobj(srcf, dstf)
        if staged:
            log(f"Import: extracted {len(staged)} bundled mod(s): "
                f"{', '.join(staged)}")

        # (2) profile/ state files → copy over the generated ones.
        wrote_profile = False
        for orig, n in entries:
            if n.endswith("/"):
                continue
            parts = n.split("/")
            if len(parts) < 2 or parts[0] != "profile":
                continue
            dest = profile_dir / Path(*parts[1:])
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(orig) as srcf, open(dest, "wb") as dstf:
                shutil.copyfileobj(srcf, dstf)
            wrote_profile = True
        if wrote_profile:
            log(f"Import: restored profile state files into {profile_dir}")

    # (3) Reconcile the modlist against what's actually on disk: the bundled
    # modlist.txt lists mods that were NOT exported (disabled leftovers from the
    # source profile) — drop those phantom entries now so they don't linger until
    # a manual Refresh. Mirrors the Refresh path's folder-sync.
    try:
        from Utils.modlist import sync_modlist_with_mods_folder
        sync_modlist_with_mods_folder(profile_dir / "modlist.txt", mods_dir)
        log("Import: reconciled modlist.txt against staged mods.")
    except Exception as exc:
        log(f"Import: modlist reconcile failed: {exc}")

    return staged


# ---------------------------------------------------------------------------
# Share code — a compressed, copy-pasteable text form of the manifest
# ---------------------------------------------------------------------------
#
# The "Export code" feature turns the same Amethyst manifest into a short text
# string the user can paste into a chat / forum to share a modlist. It carries
# only what a recipient can rebuild from Nexus — mods with BOTH a modId and a
# fileId — plus embedded FOMOD/BAIN installer choices. No mod files are bundled
# (that's what the .amethyst zip is for), so a code stays small.
#
# Load order is carried by the ORDER of the manifest's ``mods`` array (top of
# modlist first): the collection-install pipeline that consumes an imported
# manifest topo-sorts ``mods`` when no explicit ``loadOrder`` block is present,
# so mods-array order == modlist priority. For FBLO games (BG3) we additionally
# emit a ``loadOrder`` block so the exact order survives.

CODE_PREFIX = "AMMCODE1:"   # version tag; bump the digit on a format change.
GROUP_CODE_PREFIX = "AMMGROUP1:"   # Profile Group code envelope — a sibling
                                   # format, not a bump of CODE_PREFIX, since
                                   # the payload shape is different (a group
                                   # of per-member manifests, not one itself).


def build_code_manifest(entries, game, app_version: str, *,
                        profile_name=None) -> dict:
    """Build a share-code manifest from *entries* — modlist entries (separators
    included) in ``read_modlist`` order (index 0 = HIGHEST priority = top of
    modlist). Includes only mods with both a modId and a fileId; embeds
    FOMOD/BAIN choices, per-mod enabled state and root-deploy flags.

    The collection-install pipeline that consumes an imported manifest treats the
    ``mods`` array as LOW-priority first (``mods[-1]`` becomes the top of the
    modlist — see ``collection_reset._topo_sort_collection``). *entries* is highest-
    priority first, so we reverse when writing the array to keep the imported load
    order identical to the source. No separate ``loadOrder`` block is emitted (that
    would switch the importer onto its FBLO code path); the mods-array order alone
    carries the load order, matching the ``.amethyst`` export.

    Beyond the mods array, the manifest carries:

    * ``modlistSeparators`` — the source modlist's separators, TOP-first, each
      with its member mod names (the exported mods between it and the next
      separator, modlist order) plus optional ``color``/``locked``. The importer
      re-inserts each one above its first surviving member.
    * ``info.exported`` / ``info.gameName`` / ``info.totalSize`` — display
      metadata for the import preview (ISO timestamp, game display name, sum of
      the known archive sizes)."""
    mod_entries = [e for e in entries if not getattr(e, "is_separator", False)]
    rows = load_rows(mod_entries, game)
    # Keep only Nexus-resolvable mods: need modId + a fileId (from meta or label).
    keep = [r for r in rows if r.get("mod_id") and _row_file_id(r)]
    game_domain = (getattr(game, "nexus_game_domain", "") or "") if game else ""
    game_name = game.name if game else None
    profile_dir = getattr(game, "_active_profile_dir", None) if game else None
    # Reverse so the emitted mods array is low-priority first (importer puts
    # mods[-1] at the top of the modlist).
    manifest = build_manifest(
        list(reversed(keep)), game_domain, app_version,
        game_name=game_name, profile_dir=profile_dir)

    kept_names = {r["name"] for r in keep}
    separators = _separator_blocks(entries, kept_names, profile_dir)
    if separators:
        manifest["modlistSeparators"] = separators

    # Display metadata for the import preview. The profile name lets the
    # imported profile be named after the source.
    info = manifest.setdefault("info", {})
    if profile_name:
        info["name"] = profile_name
    if game_name:
        info["gameName"] = game_name
    from datetime import datetime
    info["exported"] = datetime.now().isoformat(timespec="seconds")
    total_size = sum(
        int((m.get("source") or {}).get("fileSize") or 0)
        for m in manifest.get("mods") or [])
    if total_size:
        info["totalSize"] = total_size
    return manifest


def _separator_blocks(entries, kept_names: set, profile_dir) -> list[dict]:
    """Build the ``modlistSeparators`` manifest block: one dict per separator in
    modlist order (top-first) with the names of its exported member mods (the
    kept mods between it and the next separator). Separators whose group has no
    exported member are dropped — the importer would have nothing to anchor them
    to. Colors / locks come from the profile's separator state."""
    colors = {}
    locks = {}
    if profile_dir:
        try:
            from Utils.profile_state import read_separator_colors, read_separator_locks
            colors = read_separator_colors(Path(profile_dir))
            locks = read_separator_locks(Path(profile_dir))
        except Exception:
            pass

    blocks: list[dict] = []
    current: dict | None = None
    for entry in entries:
        name = getattr(entry, "name", None) or str(entry)
        if getattr(entry, "is_separator", False):
            current = {"name": name, "mods": []}
            if colors.get(name):
                current["color"] = colors[name]
            if locks.get(name):
                current["locked"] = True
            blocks.append(current)
        elif current is not None and name in kept_names:
            current["mods"].append(name)
    return [b for b in blocks if b["mods"]]


def _encode_payload(payload: dict, prefix: str) -> str:
    """Shared core for encode_manifest/encode_group_manifest: compact JSON →
    zlib(level 9) → urlsafe base64, with a version-tagged prefix."""
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    packed = zlib.compress(raw, 9)
    b64 = base64.urlsafe_b64encode(packed).decode("ascii")
    return prefix + b64


def _decode_payload(code: str, prefix: str) -> dict:
    """Shared core for decode_manifest/decode_group_manifest. Accepts a code
    with or without *prefix* and tolerates surrounding whitespace/line breaks.
    Raises ``ValueError`` on a malformed code; does not validate the
    resulting payload's shape (callers check for their own required keys)."""
    if not code:
        raise ValueError("Empty code.")
    text = "".join(code.split())   # strip all whitespace / newlines
    if text.startswith(prefix):
        text = text[len(prefix):]
    try:
        packed = base64.urlsafe_b64decode(text.encode("ascii"))
        raw = zlib.decompress(packed)
        return json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise ValueError(f"Not a valid Amethyst code: {exc}") from exc


def encode_manifest(manifest: dict) -> str:
    """Serialise a manifest into a compact, copy-pasteable share code."""
    return _encode_payload(manifest, CODE_PREFIX)


def decode_manifest(code: str) -> dict:
    """Reverse :func:`encode_manifest`. Raises ``ValueError`` on a malformed
    code or one missing the expected single-profile manifest shape."""
    manifest = _decode_payload(code, CODE_PREFIX)
    if not isinstance(manifest, dict) or not manifest.get("mods"):
        raise ValueError("Code does not contain a valid manifest.")
    return manifest


def is_group_code(code: str) -> bool:
    """True if *code* looks like a Profile Group code (by prefix alone) —
    check this before attempting a full :func:`decode_manifest`/
    :func:`decode_group_manifest`, since the two formats use different
    envelopes and this is a cheap way to tell them apart up front."""
    return bool(code) and "".join(code.split()).startswith(GROUP_CODE_PREFIX)


def encode_group_manifest(group_manifest: dict) -> str:
    """Serialise a Profile Group envelope (see
    ``Utils.profile_group_export.build_group_code_manifest``) into a compact,
    copy-pasteable share code."""
    return _encode_payload(group_manifest, GROUP_CODE_PREFIX)


def decode_group_manifest(code: str) -> dict:
    """Reverse :func:`encode_group_manifest`. Raises ``ValueError`` on a
    malformed code or one missing the expected group envelope shape."""
    manifest = _decode_payload(code, GROUP_CODE_PREFIX)
    if not isinstance(manifest, dict) or not manifest.get("members"):
        raise ValueError("Code does not contain a valid Profile Group.")
    return manifest
