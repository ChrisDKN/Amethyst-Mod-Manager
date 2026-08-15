"""profile_export.py - neutral (GUI-free) helpers for the "Export profile" feature.

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
        "game_domain":   str,   # per-mod Nexus domain from meta.ini
        "ver_label":     str,   # "fileid - version" or "-"
        "ver_options":   list,  # [{"label", "name", "size_bytes"}]
        "optional":      bool,  # user-set; defaults from meta.ini collectionOptional
        "phase":         int,   # install phase; defaults from meta.ini collectionPhase
        "has_fomod":     bool,  # has a FOMOD or BAIN sidecar
        "has_bain":      bool,
        "fomod_export":  bool,  # include installer choices in the export
        "versions_fetched": bool,
        "size_bytes":    int,   # original archive size from meta.ini (0 if unknown)
        "root_folder":   bool,  # deploys to game root (meta.ini rootFolder)
        "enabled":       bool,  # modlist enabled state of the source entry
        "source":        str,   # "nexus" | "thunderstore" | "direct" | "browse"
                                # | "manual" | "bundle" | "ignore";
                                # see apply_source_defaults
        "direct_url":    str,
        "save_edits":    bool,  # ship local file edits as binary patches
                                # (build_patch_jobs); not set by load_rows

        # Thunderstore pin, from the mod's [thunderstore] meta.ini section. Set
        # only for mods installed from Thunderstore; a package is identified by
        # (namespace, name, version) - there is no file-id equivalent.
        "ts_namespace":  str,
        "ts_name":       str,   # PACKAGE name, not the staging folder name
        "ts_version":    str,
        "ts_full_name":  str,   # "{namespace}-{name}-{version}"
        "ts_community":  str,
        "ts_size_bytes": int,
        "ts_ver_options":    list,  # [{"label", "name", "size_bytes"}]
        "ts_versions_fetched": bool,
    }
"""

from __future__ import annotations

import base64
import json
import re
import zlib
import zipfile
import shutil
from pathlib import Path

from Nexus.nexus_meta import normalise_game_domain
from Utils.config_paths import get_fomod_selections_path, get_bain_selections_path


# ---------------------------------------------------------------------------
# Row building (port of workshop_dialog._load_mods)
# ---------------------------------------------------------------------------

# Version labels read "<file id> - <version>". Both an em dash and a plain
# hyphen have been written at different times (and are sitting in users' saved
# workshop settings), so PARSING must accept either while new labels use one.
VER_LABEL_SEP = " - "
_VER_LABEL_RE = re.compile(r"^\s*(\d+)\s+[-—]\s+(.*)$")


def split_ver_label(label: str) -> "tuple[int, str]":
    """``"12345 - 1.2"`` → ``(12345, "1.2")``; ``(0, "")`` when it isn't one."""
    m = _VER_LABEL_RE.match(label or "")
    if not m:
        return 0, ""
    try:
        return int(m.group(1)), m.group(2).strip()
    except ValueError:
        return 0, ""


def load_rows(entries, game) -> list[dict]:
    """Build the per-mod export rows from a list of modlist ``ModEntry`` objects.

    *entries* - non-separator modlist entries, LOWEST priority first (callers
                pass ``reversed(read_modlist(...))``, since modlist.txt stores
                index 0 = highest priority). The resulting row order is the
                manifest's mods-array order, where the last entry wins
                conflicts - matching what our installer's topo sort and Vortex
                both expect. *game* - the configured Game object; used for the
                staging path and active profile dir.
    """
    from Nexus.nexus_meta import read_meta

    staging_root = game.get_effective_mod_staging_path() if game else None
    profile_dir = getattr(game, "_active_profile_dir", None) if game else None

    from Thunderstore.thunderstore_meta import read_meta as ts_read_meta

    rows: list[dict] = []
    for entry in entries:
        name = getattr(entry, "name", None) or str(entry)
        mod_id = file_id = 0
        version = ""
        category_id = 0
        category_name = ""
        game_domain = ""
        size_bytes = 0
        root_folder = False
        col_optional = False
        col_phase = 0
        ts_ns = ts_pkg = ts_version = ts_full = ts_community = ""
        ts_size = 0
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
                    game_domain = normalise_game_domain(meta.game_domain or "")
                    size_bytes = meta.file_size or 0
                    root_folder = bool(meta.root_folder)
                    col_optional = bool(meta.collection_optional)
                    col_phase = meta.collection_phase or 0
                except Exception:
                    pass
                # The same meta.ini can carry BOTH sections (a mod mirrored on
                # Nexus and Thunderstore), so this is a second read, not an else.
                try:
                    tsm = ts_read_meta(meta_path)
                    if tsm.package_id:
                        ts_ns = tsm.namespace
                        ts_pkg = tsm.name
                        ts_version = tsm.version or ""
                        ts_full = tsm.full_name or ""
                        ts_community = tsm.community or ""
                        ts_size = tsm.file_size or 0
                except Exception:
                    pass

        if file_id and version:
            ver_label = f"{file_id} - {version}"
        elif file_id:
            ver_label = str(file_id)
        else:
            ver_label = "-"

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
            "game_domain":      game_domain,
            "ver_label":        ver_label,
            "ver_options":      [{"label": ver_label, "name": "", "size_bytes": 0}],
            "optional":         col_optional,
            "phase":            col_phase,
            "has_fomod":        has_installer,
            "has_bain":         has_bain,
            "fomod_export":     has_installer,
            "versions_fetched": False,
            "size_bytes":       size_bytes,
            "root_folder":      root_folder,
            "enabled":          bool(getattr(entry, "enabled", True)),
            # A Thunderstore pin IS the download source, so detect it here
            # rather than in apply_source_defaults: that helper is shared with
            # the Nexus-collection publisher, which must never offer this
            # source (see CreateCollectionView._seed_rows).
            "source":           "thunderstore" if (ts_ns and ts_pkg) else "nexus",
            "direct_url":       "",
            "source_instructions": "",
            "ts_namespace":     ts_ns,
            "ts_name":          ts_pkg,
            "ts_version":       ts_version,
            "ts_full_name":     ts_full,
            "ts_community":     ts_community,
            "ts_size_bytes":    ts_size,
            "ts_ver_options":   [],
            "ts_versions_fetched": False,
        })

    return rows


# ---------------------------------------------------------------------------
# Save / load export settings (port of _write_settings / _read_settings)
# ---------------------------------------------------------------------------

def _row_file_id(row: dict) -> int:
    """The effective file id for a row: the explicit ``file_id``, else parsed from
    the ``ver_label`` ("fileid - version")."""
    fid = row.get("file_id") or 0
    if not fid:
        lbl = row.get("ver_label", "")
        fid = split_ver_label(lbl)[0]
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
                "ver_label":  r.get("ver_label", "-"),
                "game_domain": r.get("game_domain", ""),
                "phase":      int(r.get("phase") or 0),
                "update_policy": r.get("update_policy", "exact"),
                "instructions": r.get("source_instructions", ""),
                "save_edits": bool(r.get("save_edits", False)),
                # Thunderstore pin: the version may be a user override from the
                # version picker, so it has to survive a save/load round trip.
                "ts_version":  r.get("ts_version", ""),
                "ts_full_name": r.get("ts_full_name", ""),
                "ts_size_bytes": int(r.get("ts_size_bytes") or 0),
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
        # Keep the row's current source when the saved entry has none: a file
        # written before sources existed must not reset a deduced one. For the
        # same reason a saved "nexus" never overrides a detected Thunderstore
        # pin - every settings file written before Thunderstore support existed
        # recorded "nexus" for these mods, and honouring it would silently undo
        # the detection on every load.
        saved_source = m.get("source") or ""
        if saved_source == "nexus" and row.get("source") == "thunderstore":
            saved_source = ""
        row["source"] = saved_source or row.get("source", "nexus")
        row["direct_url"] = m.get("direct_url", "")
        row["source_instructions"] = m.get("instructions", "")
        # Installed meta.ini is authoritative. A saved domain only repairs a
        # row whose current metadata does not identify its Nexus game.
        if not row.get("game_domain") and m.get("game_domain"):
            row["game_domain"] = normalise_game_domain(
                str(m["game_domain"]))
        try:
            row["phase"] = int(m.get("phase") or 0)
        except (TypeError, ValueError):
            row["phase"] = 0
        if m.get("update_policy") in ("exact", "prefer", "latest"):
            row["update_policy"] = m["update_policy"]
        row["save_edits"] = bool(m.get("save_edits", False))
        # A saved Thunderstore version is a deliberate user pin from the version
        # picker (unlike file_id, where the installed file wins), so it takes
        # precedence over the installed meta.ini - but only on a row that really
        # is a Thunderstore mod, so a stale entry can't invent a pin.
        if row.get("ts_namespace") and row.get("ts_name") and m.get("ts_version"):
            row["ts_version"] = str(m["ts_version"])
            row["ts_full_name"] = (
                m.get("ts_full_name")
                or f"{row['ts_namespace']}-{row['ts_name']}-{row['ts_version']}")
            if m.get("ts_size_bytes"):
                try:
                    row["ts_size_bytes"] = int(m["ts_size_bytes"])
                except (TypeError, ValueError):
                    pass
        # Only apply file_id / ver_label from the JSON when the mod has no file_id
        # already set from meta.ini - the installed file takes precedence.
        if not row.get("file_id"):
            if m.get("file_id"):
                row["file_id"] = m["file_id"]
            if m.get("ver_label"):
                row["ver_label"] = m["ver_label"]
                # Back-fill file_id from ver_label if still missing.
                if not row.get("file_id"):
                    parsed = split_ver_label(row["ver_label"])[0]
                    if parsed:
                        row["file_id"] = parsed


# ---------------------------------------------------------------------------
# Binary patches - local file edits shipped as diffs over the pristine download
# ---------------------------------------------------------------------------

def build_patch_jobs(rows, manifest: dict, game, *, scratch_out=None
                     ) -> "tuple[list, list]":
    """Diff each ``save_edits`` row's staged files against its cached archive.

    Attaches the resulting ``{rel_path: source_crc32}`` map to the matching
    manifest mod entry (``patches`` - the same shape Nexus collections use, so
    the collection-install pipeline can apply them) and returns
    ``(jobs, warnings)``: *jobs* are ``(src_path, arcname)`` pairs for
    :func:`write_amethyst`'s ``patch_jobs``, arcnames under ``patches/``.

    Bundle rows are skipped - their files ship verbatim, edits included.
    *scratch_out*, when given, receives the temp dir holding the ``.diff``
    files; the caller must delete it after packing (the files have to outlive
    this call but must not outlive the export).
    """
    import tempfile
    from Utils.collection_export import (
        _cached_archive, _read_row_meta, _safe_archive_component,
        _scan_mod_patches)
    from Utils.config_paths import get_download_cache_dir

    jobs: list = []
    warnings: list = []
    wanted = [r for r in rows
              if r.get("save_edits")
              and r.get("source", "nexus") not in ("bundle", "ignore")]
    if not wanted:
        return jobs, warnings

    staging_root = game.get_effective_mod_staging_path() if game else None
    game_name = getattr(game, "name", "") or ""
    entries = {m.get("name"): m for m in manifest.get("mods") or []}

    patch_root = None
    for row in wanted:
        name = row["name"]
        mod_entry = entries.get(name)
        if mod_entry is None:
            continue
        mod_dir = Path(staging_root) / name if staging_root else None
        if not (mod_dir and mod_dir.is_dir()):
            continue
        meta = _read_row_meta(staging_root, name)
        archive = _cached_archive(meta, game_name)
        if archive is None:
            warnings.append(
                f"'{name}': file edits skipped - the original archive is "
                "not in the download cache.")
            continue
        if patch_root is None:
            patch_root = Path(tempfile.mkdtemp(
                prefix="amethyst_profexport_",
                dir=str(get_download_cache_dir())))
            if scratch_out is not None:
                scratch_out.append(patch_root)
        # The folder is named after the mod, which comes from Nexus metadata -
        # strip anything that could climb out of patches/ on extraction.
        patch_dir_name = _safe_archive_component(name)
        mod_patch_dir = patch_root / patch_dir_name
        found = _scan_mod_patches(mod_dir, archive, mod_patch_dir,
                                  name, warnings)
        if found:
            mod_entry["patches"] = found
            for diff in sorted(mod_patch_dir.rglob("*.diff")):
                rel = diff.relative_to(mod_patch_dir).as_posix()
                jobs.append((diff, f"patches/{patch_dir_name}/{rel}"))
    return jobs, warnings


# ---------------------------------------------------------------------------
# Manifest build + zip (port of _write_manifest)
# ---------------------------------------------------------------------------

def apply_source_defaults(rows, *, ignore_disabled: bool = False) -> int:
    """Replace the un-chosen ``"nexus"`` default with a source that can work.

    Only rows still sitting on ``"nexus"`` are touched, so an explicit choice -
    a manifest seed, saved settings, or a source the user picked by hand -
    always wins. Applied AFTER those, so a stale autosave that recorded
    ``"nexus"`` for a row that can never export as Nexus gets corrected too.

    * No modId AND no fileId → ``"bundle"``. There is no mod page to point at,
      so Nexus is not a possible answer: the row would either block the upload
      (``nexus_missing_file_ids``) or export as ``modId 0`` with its files left
      behind. Bundling ships the files instead, and is exactly the case
      ``_bundle_blocked_reason`` permits. Vortex deduces the same way
      (transformCollection.ts ``deduceSource``: no nexus repo → ``bundle``).
      A row with a modId but no fileId is deliberately left alone - it IS on
      Nexus, and the version picker is how the user fills the fileId in.
    * ``ignore_disabled`` → disabled rows become ``"ignore"``. Only for Nexus
      collections, which have no disabled state and drop those rows anyway; the
      ``.amethyst`` profile export carries ``enabled: false`` through to the
      importer, so defaulting there would LOSE mods it can represent.

    Returns how many rows changed.
    """
    changed = 0
    for row in rows:
        if row.get("source", "nexus") != "nexus":
            continue
        if ignore_disabled and row.get("enabled") is False:
            row["source"] = "ignore"
        elif not row.get("mod_id") and not row.get("file_id"):
            row["source"] = "bundle"
        else:
            continue
        changed += 1
    return changed


def nexus_missing_file_ids(rows) -> list[str]:
    """Names of Nexus-source mods that lack a File ID (can't be exported until set)."""
    return [
        row["name"] for row in rows
        if row.get("source", "nexus") == "nexus" and not row.get("file_id")
    ]


def redistributable_bundle_names(rows) -> list[str]:
    """Names of bundled mods that are also downloadable from Nexus.

    Bundling packs the mod's files into the ``.amethyst`` itself, so for a mod
    with a Nexus page the export stops being a list of what to download and
    becomes a copy of someone else's work. That is fine for a personal backup -
    the export has no upload path - but not for one handed to other people, so
    the Qt view confirms before writing. Publishing is blocked outright; see
    CreateCollectionView._bundle_blocked_reason and collection_export's
    bundle -> nexus downgrade.
    """
    return [
        row["name"] for row in rows
        if row.get("source") == "bundle" and row.get("mod_id")
        and row.get("file_id")
    ]


def _thunderstore_source(row: dict) -> dict:
    """The manifest ``source`` object for a Thunderstore row.

    Namespace, name and version are all carried explicitly rather than left to
    be re-derived from ``fullName``: namespaces and package names contain
    hyphens, so only ``parse_full_name``'s right-split is safe, and a manifest
    that lost ``fullName`` must still install. ``fileSize`` is the only
    integrity check Thunderstore offers (no hashes exist) and becomes the
    importer's ``expected_size``.

    Falls back to ``{"bundle": True}`` when the pin is incomplete - shipping the
    files beats writing an entry no importer can resolve.
    """
    ns = (row.get("ts_namespace") or "").strip()
    pkg = (row.get("ts_name") or "").strip()
    version = (row.get("ts_version") or "").strip()
    if not (ns and pkg and version):
        return {"bundle": True}
    source: dict = {
        "type":      "thunderstore",
        "namespace": ns,
        "name":      pkg,
        "version":   version,
        "fullName":  (row.get("ts_full_name") or "").strip()
                     or f"{ns}-{pkg}-{version}",
        "logicalFilename": row["name"],
    }
    if row.get("ts_community"):
        source["community"] = row["ts_community"]
    if row.get("ts_size_bytes"):
        source["fileSize"] = int(row["ts_size_bytes"])
    return source


def is_thunderstore_source(src: dict) -> bool:
    """Whether a manifest ``source`` object points at a Thunderstore package."""
    return ((src or {}).get("type") or "").strip().lower() == "thunderstore"


def manifest_needs_nexus(manifest: dict) -> bool:
    """Whether importing *manifest* requires a logged-in Nexus account.

    Thunderstore downloads are public and bundled/off-site mods need no API at
    all, so a profile made entirely of those imports with no login. That is not
    an optimisation: the Thunderstore-only games (Risk of Rain 2, Inscryption)
    have an empty ``nexus_game_domain`` by design, so requiring Nexus would
    block the import outright.
    """
    for mod in manifest.get("mods") or []:
        src = mod.get("source") or {}
        stype = (src.get("type") or "").strip().lower()
        if src.get("bundle") is True or stype in (
                "bundle", "thunderstore", "browse", "direct", "manual"):
            continue
        return True
    return False


def thunderstore_entries(manifest: dict) -> list[dict]:
    """The manifest's Thunderstore mods, in mods-array order.

    Each entry carries everything the importer needs so it never has to re-walk
    the manifest: ``array_index`` (the position in ``manifest["mods"]``, which
    fixes the mod's priority relative to the Nexus mods), the package pin, and
    the per-mod flags the install pass has to reapply.

    Entries whose pin is incomplete are dropped - they cannot be downloaded, and
    the exporter writes them as bundles instead.
    """
    out: list[dict] = []
    for idx, mod in enumerate(manifest.get("mods") or []):
        src = mod.get("source") or {}
        if not is_thunderstore_source(src):
            continue
        ns = (src.get("namespace") or "").strip()
        pkg = (src.get("name") or "").strip()
        version = (src.get("version") or "").strip()
        if not (ns and pkg and version):
            continue
        try:
            file_size = int(src.get("fileSize") or 0)
        except (TypeError, ValueError):
            file_size = 0
        out.append({
            "array_index": idx,
            "name":        mod.get("name") or "",
            "namespace":   ns,
            "package":     pkg,
            "version":     version,
            "full_name":   (src.get("fullName") or "").strip()
                           or f"{ns}-{pkg}-{version}",
            "community":   (src.get("community") or "").strip(),
            "file_size":   file_size,
            "logical":     (src.get("logicalFilename") or "").strip(),
            "optional":    bool(mod.get("optional", False)),
            "enabled":     mod.get("enabled", True) is not False,
            "root_folder": ((mod.get("details") or {}).get("type") or "")
                           .strip() == "dinput",
        })
    return out


def build_manifest(rows, game_domain: str, app_version: str, *,
                   game_name=None, profile_dir=None) -> dict:
    """Build the ``manifest.json`` dict from the export *rows*. Mods with
    ``source == "ignore"`` are dropped. FOMOD/BAIN installer choices are embedded
    when ``fomod_export`` is set and a sidecar exists (profile-local preferred)."""
    mods: list[dict] = []
    for row in rows:
        if row.get("source") == "ignore":
            continue
        # Parse fileid from ver_label ("fileid - version") when present.
        ver_label = row["ver_label"]
        file_id = row["file_id"]
        parsed_fid = split_ver_label(ver_label)[0]
        if parsed_fid:
            file_id = parsed_fid

        row_source = row.get("source", "nexus")
        if row_source == "direct":
            source: dict = {
                "type": "direct",
                "url":  row.get("direct_url", ""),
            }
        elif row_source in ("browse", "manual"):
            source = {"type": row_source}
            if row.get("direct_url"):
                source["url"] = row["direct_url"]
            if row.get("source_instructions"):
                source["instructions"] = row["source_instructions"]
        elif row_source == "thunderstore":
            source = _thunderstore_source(row)
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
        mod_domain = (normalise_game_domain(row.get("game_domain") or "")
                      or normalise_game_domain(game_domain))
        if mod_domain:
            mod_entry["domainName"] = mod_domain
        # Carry a disabled modlist state (enabled entries stay implicit). The
        # importer stages the mod normally, then marks it disabled.
        if row.get("enabled") is False:
            mod_entry["enabled"] = False
        # Root-deploy mods use the Nexus-collections "dinput" install type -
        # the importer already maps details.type == "dinput" to meta rootFolder.
        if row.get("root_folder"):
            mod_entry["details"] = {"type": "dinput"}

        # Include version and category from meta.ini if available.
        row_version = row.get("version") or ""
        if not row_version:
            row_version = split_ver_label(ver_label)[1]
        if not row_version and row_source == "thunderstore":
            # A Thunderstore-only mod has no [General] version - the pin is the
            # version, and it may be a user override from the version picker.
            row_version = row.get("ts_version") or ""
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
            # FOMOD - pick the right sidecar + type.
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


def write_amethyst(out_path, manifest: dict, *, staging_root=None,
                   overwrite_root=None, profile_dir=None,
                   bundle_names=None, patch_jobs=None,
                   progress_cb=None) -> Path:
    """Write the ``.amethyst`` zip: ``manifest.json`` + bundled ``mods/`` +
    ``overwrite/`` + ``profile/`` state files. Returns the final path (suffix
    forced to .amethyst when not already .zip/.amethyst).

    *patch_jobs* - ``(src_path, arcname)`` pairs from :func:`build_patch_jobs`
    (binary-patch ``.diff`` files under ``patches/``).

    *progress_cb* (optional) is called as ``progress_cb(done_bytes, total_bytes,
    arcname)`` - once with ``done_bytes=0`` before writing starts (total known),
    then after each member is written. Members are collected up-front so the
    byte total is exact; the callback runs on the caller's thread."""
    out_path = Path(out_path)
    if out_path.suffix.lower() not in (".zip", ".amethyst"):
        out_path = out_path.with_suffix(".amethyst")

    bundle_names = list(bundle_names or [])
    staging_root = Path(staging_root) if staging_root else None
    overwrite_root = Path(overwrite_root) if overwrite_root else None

    # Collect (src_path, arcname, data) members first: src_path=None means an
    # in-memory writestr member (pre-encoded bytes in *data*).
    jobs: list[tuple] = [
        (None, "manifest.json", json.dumps(manifest, indent=2).encode("utf-8")),
    ]

    for src, arcname in (patch_jobs or []):
        jobs.append((Path(src), str(arcname), None))

    if staging_root:
        for name in bundle_names:
            mod_dir = staging_root / name
            if not mod_dir.is_dir():
                continue
            for fp in mod_dir.rglob("*"):
                if fp.is_file():
                    arcname = Path("mods") / name / fp.relative_to(mod_dir)
                    jobs.append((fp, arcname.as_posix(), None))

    if overwrite_root and overwrite_root.is_dir():
        for fp in overwrite_root.rglob("*"):
            if fp.is_file():
                arcname = Path("overwrite") / fp.relative_to(overwrite_root)
                jobs.append((fp, arcname.as_posix(), None))

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
                jobs.append((None, (Path("profile") / fname).as_posix(),
                             json.dumps(ps, indent=2).encode("utf-8")))
            else:
                jobs.append((fp, (Path("profile") / fname).as_posix(), None))
        # Legacy: root-level *.ini files
        for fp in pdir.glob("*.ini"):
            if fp.is_file():
                jobs.append((fp, (Path("profile") / fp.name).as_posix(), None))
        # Bundle whole profile subfolders
        for sub in ("ini files", "Saves", "installed_collections"):
            sub_dir = pdir / sub
            if not sub_dir.is_dir():
                continue
            for fp in sub_dir.rglob("*"):
                if fp.is_file():
                    arcname = Path("profile") / sub / fp.relative_to(sub_dir)
                    jobs.append((fp, arcname.as_posix(), None))

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
                         log_fn=None) -> list[str]:
    """Extract a locally-exported ``.amethyst`` bundle into a freshly-installed
    profile - faithful to the Tk import (CollectionsDialog bundle-zip extraction):

      * ``mods/<name>/…``      → ``<mods_dir>/<name>/…`` **verbatim** (folder names
        preserved exactly, including spaces, keeping the archive's own meta.ini) so
        each bundled mod matches its ``modlist.txt`` entry.
      * ``overwrite/…``        → ``<overwrite_dir>/…``
      * ``profile/…``          → ``<profile_dir>/…`` (modlist.txt, plugins.txt,
        loadorder.txt, profile_state.json, userlist.yaml, *.ini, ini files/, Saves/).

    Nexus-source mods are installed by the collection pipeline; this covers the
    bundled assets + profile state files that live *inside* the local zip.

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

    staged: list[str] = []
    with _zip.ZipFile(src_path, "r") as zf:
        names = zf.namelist()

        # (1) Bundled mods + overwrite - extract verbatim (no rename, keep meta.ini).
        for n in names:
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
            with zf.open(n) as srcf, open(dest, "wb") as dstf:
                shutil.copyfileobj(srcf, dstf)
        if staged:
            log(f"Import: extracted {len(staged)} bundled mod(s): "
                f"{', '.join(staged)}")

        # (2) profile/ state files → copy over the generated ones.
        wrote_profile = False
        for n in names:
            if n.endswith("/"):
                continue
            parts = n.split("/")
            if len(parts) < 2 or parts[0] != "profile":
                continue
            dest = profile_dir / Path(*parts[1:])
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(n) as srcf, open(dest, "wb") as dstf:
                shutil.copyfileobj(srcf, dstf)
            wrote_profile = True
        if wrote_profile:
            log(f"Import: restored profile state files into {profile_dir}")
            # Snapshot the pristine authored order files NOW - the reconcile
            # below drops modlist rows for off-site mods that aren't installed
            # yet, and Reset Load Order needs the full original to put them
            # back in place once the user installs them.
            try:
                from Utils.collection_export import write_amethyst_stash
                if write_amethyst_stash(profile_dir, log_fn=log):
                    log("Import: saved Amethyst/ order snapshot "
                        "(Reset Load Order restores from it).")
            except Exception as exc:
                log(f"Import: order snapshot failed: {exc}")

    # (3) Reconcile the modlist against what's actually on disk: the bundled
    # modlist.txt lists mods that were NOT exported (disabled leftovers from the
    # source profile) - drop those phantom entries now so they don't linger until
    # a manual Refresh. Mirrors the Refresh path's folder-sync.
    try:
        from Utils.modlist import sync_modlist_with_mods_folder
        sync_modlist_with_mods_folder(profile_dir / "modlist.txt", mods_dir)
        log("Import: reconciled modlist.txt against staged mods.")
    except Exception as exc:
        log(f"Import: modlist reconcile failed: {exc}")

    return staged


# ---------------------------------------------------------------------------
# Share code - a compressed, copy-pasteable text form of the manifest
# ---------------------------------------------------------------------------
#
# The "Export code" feature turns the same Amethyst manifest into a short text
# string the user can paste into a chat / forum to share a modlist. It carries
# only what a recipient can re-download - Nexus mods with BOTH a modId and a
# fileId, and Thunderstore mods with a complete (namespace, name, version) pin -
# plus embedded FOMOD/BAIN installer choices. No mod files are bundled (that's
# what the .amethyst zip is for), so a code stays small.
#
# Load order is carried by the ORDER of the manifest's ``mods`` array (top of
# modlist first): the collection-install pipeline that consumes an imported
# manifest topo-sorts ``mods`` when no explicit ``loadOrder`` block is present,
# so mods-array order == modlist priority. For FBLO games (BG3) we additionally
# emit a ``loadOrder`` block so the exact order survives.

CODE_PREFIX = "AMMCODE1:"   # version tag; bump the digit on a format change.


def build_code_manifest(entries, game, app_version: str, *,
                        profile_name=None) -> dict:
    """Build a share-code manifest from *entries* - modlist entries (separators
    included) in ``read_modlist`` order (index 0 = HIGHEST priority = top of
    modlist). Includes mods the recipient can re-download: Nexus mods with a
    modId + fileId, and Thunderstore mods with a complete package pin. Embeds
    FOMOD/BAIN choices, per-mod enabled state and root-deploy flags.

    The collection-install pipeline that consumes an imported manifest treats the
    ``mods`` array as LOW-priority first (``mods[-1]`` becomes the top of the
    modlist - see ``collection_reset._topo_sort_collection``). *entries* is highest-
    priority first, so we reverse when writing the array to keep the imported load
    order identical to the source. No separate ``loadOrder`` block is emitted (that
    would switch the importer onto its FBLO code path); the mods-array order alone
    carries the load order, matching the ``.amethyst`` export.

    Beyond the mods array, the manifest carries:

    * ``modlistSeparators`` - the source modlist's separators, TOP-first, each
      with its member mod names (the exported mods between it and the next
      separator, modlist order) plus optional ``color``/``locked``. The importer
      re-inserts each one above its first surviving member.
    * ``info.exported`` / ``info.gameName`` / ``info.totalSize`` - display
      metadata for the import preview (ISO timestamp, game display name, sum of
      the known archive sizes)."""
    mod_entries = [e for e in entries if not getattr(e, "is_separator", False)]
    rows = load_rows(mod_entries, game)
    # Keep only mods the recipient can actually download: a Nexus mod needs a
    # modId + fileId (from meta or label), a Thunderstore mod needs its full
    # (namespace, name, version) pin. A code ships no files, so anything else
    # would import as a name with nothing behind it.
    keep = [r for r in rows
            if (r.get("mod_id") and _row_file_id(r))
            or (r.get("source") == "thunderstore" and r.get("ts_namespace")
                and r.get("ts_name") and r.get("ts_version"))]
    game_domain = (normalise_game_domain(
        getattr(game, "nexus_game_domain", "") or "") if game else "")
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
    exported member are dropped - the importer would have nothing to anchor them
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
            # Separator state is keyed by DISPLAY name (no _separator suffix)
            # in profile_state - the full name is only a fallback.
            disp = getattr(entry, "display_name", name)
            color = colors.get(disp) or colors.get(name)
            if color:
                current["color"] = color
            if locks.get(disp) or locks.get(name):
                current["locked"] = True
            blocks.append(current)
        elif current is not None and name in kept_names:
            current["mods"].append(name)
    return [b for b in blocks if b["mods"]]


def encode_manifest(manifest: dict) -> str:
    """Serialise a manifest into a compact, copy-pasteable share code:
    JSON → zlib(level 9) → urlsafe base64, with a version prefix."""
    raw = json.dumps(manifest, separators=(",", ":")).encode("utf-8")
    packed = zlib.compress(raw, 9)
    b64 = base64.urlsafe_b64encode(packed).decode("ascii")
    return CODE_PREFIX + b64


def decode_manifest(code: str) -> dict:
    """Reverse :func:`encode_manifest`. Accepts a code with or without the
    ``AMMCODE1:`` prefix and tolerates surrounding whitespace / line breaks.
    Raises ``ValueError`` on a malformed code."""
    if not code:
        raise ValueError("Empty code.")
    text = "".join(code.split())   # strip all whitespace / newlines
    if text.startswith(CODE_PREFIX):
        text = text[len(CODE_PREFIX):]
    # A pasted paste-service link is not a code - tell the user that rather than
    # letting base64 fail with something meaningless. The caller (the import
    # overlay) resolves links via fetch_code_url before ever getting here.
    if _PASTE_URL_RE.match(text):
        raise ValueError("That looks like a link, not a code - it could not be "
                         "downloaded.")
    try:
        packed = base64.urlsafe_b64decode(text.encode("ascii"))
        raw = zlib.decompress(packed)
        manifest = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise ValueError(f"Not a valid Amethyst code: {exc}") from exc
    if not isinstance(manifest, dict) or not manifest.get("mods"):
        raise ValueError("Code does not contain a valid manifest.")
    return manifest


# ---------------------------------------------------------------------------
# Share code links - uploading a code to a paste service
# ---------------------------------------------------------------------------
#
# A code for a large modlist runs to tens of kilobytes, which is awkward to send
# over chat. Uploading it to a paste host turns it into a short URL the recipient
# pastes into the same Import code box.
#
# dpaste.com is the host: no API key (nothing to ship in the binary or leak), an
# anonymous POST API, and a caller-chosen expiry. This is the ONLY outbound use
# of it - the upload is always user-initiated from the export overlay, never
# automatic, because it publishes profile and mod names to a third-party server
# where anyone with the link can read them.
#
# NOTE the host: dpaste.ORG's documented /api/ endpoint answers 405 to a POST
# (its docs are stale). dpaste.COM's v2 API is the one that works - verified
# posting and re-fetching a full-size code byte-for-byte. Re-check with a live
# POST before switching hosts or endpoints.

PASTE_HOST = "dpaste.com"
_PASTE_API = f"https://{PASTE_HOST}/api/v2/"

#: Expiry choices offered in the UI, as (api value in days, English label).
#: dpaste caps anonymous pastes at 365 days - there is no "never", so the
#: longest option is a year. 30 days is the default: most codes are a one-off
#: share with a friend.
PASTE_EXPIRY_CHOICES = (
    ("7",   "1 week"),
    ("30",  "30 days"),
    ("365", "1 year"),
)
DEFAULT_PASTE_EXPIRY = "30"

#: Matches a URL we are willing to fetch a code from: any https host, since a
#: user may well re-host a code themselves. The fetch is hard-limited by size
#: and the body still has to decode as a real code, so a wrong link fails
#: harmlessly rather than doing anything with whatever came back.
_PASTE_URL_RE = re.compile(r"^https?://", re.IGNORECASE)

#: Ceiling on a fetched body. A code is base64 - even a huge modlist is well
#: under this, so anything larger is not a code and is not worth reading.
_MAX_FETCH_BYTES = 8 * 1024 * 1024


def is_code_url(text: str) -> bool:
    """True when *text* looks like a link to a hosted code rather than a code."""
    return bool(_PASTE_URL_RE.match((text or "").strip()))


def upload_code(code: str, *, expires: str = DEFAULT_PASTE_EXPIRY,
                timeout: float = 20.0) -> str:
    """Upload a share *code* to dpaste.org and return the resulting URL.

    *expires* is one of :data:`PASTE_EXPIRY_CHOICES` (an API value). Raises
    ``RuntimeError`` with a user-facing message if the upload fails - callers
    fall back to handing the user the raw code, which always works.
    """
    import urllib.error
    import urllib.parse
    import urllib.request

    if not code:
        raise RuntimeError("Nothing to upload.")

    # syntax=text stops dpaste trying to syntax-highlight a base64 blob. The v2
    # API replies with the bare URL as the body, so there is nothing to parse.
    body = urllib.parse.urlencode({
        "content": code,
        "syntax": "text",
        "expiry_days": expires or DEFAULT_PASTE_EXPIRY,
    }).encode("utf-8")
    req = urllib.request.Request(
        _PASTE_API, data=body, method="POST",
        headers={"User-Agent": "Amethyst-Mod-Manager",
                 "Content-Type": "application/x-www-form-urlencoded"})
    try:
        from Utils.gh_cache import _get_ssl_context
        ctx = _get_ssl_context()
    except Exception:
        ctx = None
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            url = resp.read(2048).decode("utf-8", "replace").strip().strip('"')
    except urllib.error.HTTPError as exc:
        # 429 is a real possibility on a free host if the user clicks a few
        # times - say so plainly instead of showing a bare status code.
        if exc.code == 429:
            raise RuntimeError(
                f"{PASTE_HOST} is rate-limiting - wait a minute and try again."
            ) from exc
        raise RuntimeError(f"{PASTE_HOST} refused the upload (HTTP {exc.code}).") from exc
    except Exception as exc:
        raise RuntimeError(f"Could not reach {PASTE_HOST}: {exc}") from exc
    if not url.lower().startswith("http"):
        raise RuntimeError(f"{PASTE_HOST} returned an unexpected response.")
    return url


def fetch_code_url(url: str, *, timeout: float = 20.0) -> str:
    """Download a share code from *url* and return its text.

    Handles a dpaste.com page link by rewriting it to the ``.txt`` raw form; any
    other host is fetched as given. The body is size-capped and must still
    decode as a real code downstream, so a wrong link fails harmlessly.
    """
    import urllib.error
    import urllib.request

    text = (url or "").strip()
    if not text:
        raise RuntimeError("No link to open.")
    # dpaste serves the paste as an HTML page at the bare URL; appending .txt
    # gives the plain text back exactly as posted.
    low = text.lower()
    if PASTE_HOST in low and not low.endswith(".txt"):
        text = text.rstrip("/") + ".txt"

    req = urllib.request.Request(
        text, headers={"User-Agent": "Amethyst-Mod-Manager"})
    try:
        from Utils.gh_cache import _get_ssl_context
        ctx = _get_ssl_context()
    except Exception:
        ctx = None
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            raw = resp.read(_MAX_FETCH_BYTES + 1)
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise RuntimeError(
                "The paste host is rate-limiting - wait a minute and try again."
            ) from exc
        if exc.code in (404, 410):
            raise RuntimeError("That link no longer exists - it may have expired.") from exc
        raise RuntimeError(f"Link returned HTTP {exc.code}.") from exc
    except Exception as exc:
        raise RuntimeError(f"Could not open the link: {exc}") from exc
    if len(raw) > _MAX_FETCH_BYTES:
        raise RuntimeError("That link is too large to be a share code.")
    return raw.decode("utf-8", "replace").strip()
