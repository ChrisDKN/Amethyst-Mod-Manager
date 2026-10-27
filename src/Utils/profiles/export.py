"""profiles/export.py - neutral (GUI-free) helpers for the "Export profile" feature.

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
        "optional":      bool,  # defaults from disabled state or collectionOptional
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
import os
import re
import tempfile
import zlib
import zipfile
import shutil
from pathlib import Path

from Nexus.nexus_meta import normalise_game_domain
from Utils.config_paths import get_fomod_selections_path, get_bain_selections_path


class ExportCancelled(Exception):
    pass


def _check_cancel(cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise ExportCancelled()


def _export_check(checkpoint, cancel_event):
    def check():
        _check_cancel(cancel_event)
        if checkpoint:
            checkpoint()
    return check


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
        if getattr(entry, "is_group_header", False):
            continue
        name = getattr(entry, "name", None) or str(entry)
        enabled = bool(getattr(entry, "enabled", True))
        mod_id = file_id = 0
        version = ""
        installation_file = ""
        category_id = 0
        category_name = ""
        game_domain = ""
        size_bytes = 0
        root_folder = False
        col_optional = False
        col_phase = 0
        col_install_type = ""
        ts_ns = ts_pkg = ts_version = ts_full = ts_community = ""
        ts_size = 0
        plugin_source = None
        plugin_source_uri = ""
        if staging_root:
            meta_path = Path(staging_root) / name / "meta.ini"
            try:
                from amethyst_plugins.repositories import installed as read_provider_source, portable_source
                plugin_source = read_provider_source(meta_path)
                plugin_source_uri = portable_source(plugin_source)
            except (OSError, ValueError):
                pass
            if meta_path.is_file():
                try:
                    meta = read_meta(meta_path)
                    mod_id = meta.mod_id or 0
                    file_id = meta.file_id or 0
                    version = meta.version or ""
                    installation_file = meta.installation_file or ""
                    category_id = meta.category_id or 0
                    category_name = meta.category_name or ""
                    game_domain = normalise_game_domain(meta.game_domain or "")
                    size_bytes = meta.file_size or 0
                    root_folder = bool(meta.root_folder)
                    col_optional = bool(meta.collection_optional)
                    col_phase = meta.collection_phase or 0
                    col_install_type = meta.collection_install_type or ""
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
            "installation_file": installation_file,
            "category_id":      category_id,
            "category_name":    category_name,
            "game_domain":      game_domain,
            "ver_label":        ver_label,
            "ver_options":      [{"label": ver_label, "name": "", "size_bytes": 0}],
            "optional":         col_optional or not enabled,
            "phase":            col_phase,
            "collection_install_type": col_install_type,
            "has_fomod":        has_installer,
            "has_bain":         has_bain,
            "fomod_export":     has_installer,
            "versions_fetched": False,
            "size_bytes":       size_bytes,
            "root_folder":      root_folder,
            "enabled":          enabled,
            # A Thunderstore pin IS the download source, so detect it here
            # rather than in apply_source_defaults: that helper is shared with
            # the Nexus-collection publisher, which must never offer this
            # source (see CreateCollectionView._seed_rows).
            "source":           "plugin" if plugin_source_uri else "thunderstore" if (ts_ns and ts_pkg) else "nexus",
            "plugin_source":    plugin_source,
            "plugin_source_uri": plugin_source_uri,
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
                "mod_id":     r.get("mod_id", 0),
                "fomod_export": bool(r.get("fomod_export", True)),
                "archive_id": r.get("archive_id", ""),
                "source":     r.get("source", "nexus"),
                "direct_url": r.get("direct_url", ""),
                "file_id":    _row_file_id(r),
                "mod_id":     r.get("mod_id", 0),
                "version":    r.get("version", ""),
                "size_bytes": r.get("size_bytes", 0),
                "fomod_export": bool(r.get("fomod_export", True)),
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


def read_settings(in_path, rows, *, preserve_pins=False) -> None:
    """Apply settings; collection authoring can explicitly restore saved pins."""
    with open(in_path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    by_name = {m["name"]: m for m in data.get("mods", [])}
    for row in rows:
        m = by_name.get(row["name"])
        if not m:
            continue
        row["optional"] = bool(m.get("optional", False))
        if "fomod_export" in m:
            row["fomod_export"] = bool(m["fomod_export"])
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
        restore_pin = (preserve_pins
                       and (not m.get("mod_id") or m["mod_id"] == row.get("mod_id"))
                       and (not m.get("game_domain") or not row.get("game_domain")
                            or m["game_domain"].lower() == row["game_domain"].lower()))
        if not row.get("file_id") or restore_pin:
            if m.get("file_id"):
                row["file_id"] = m["file_id"]
            if m.get("ver_label"):
                row["ver_label"] = m["ver_label"]
                # Back-fill file_id from ver_label if still missing.
                if not row.get("file_id"):
                    parsed = split_ver_label(row["ver_label"])[0]
                    if parsed:
                        row["file_id"] = parsed
            if restore_pin:
                row["version"] = split_ver_label(row.get("ver_label", ""))[1] or m.get("version", "")
                row["size_bytes"] = int(m.get("size_bytes") or 0)


# ---------------------------------------------------------------------------
# Binary patches - local file edits shipped as diffs over the pristine download
# ---------------------------------------------------------------------------

def build_patch_jobs(rows, manifest: dict, game, *, scratch_out=None,
                     mod_roots=None, archive_paths=None, checkpoint=None, cancel_event=None
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
    from Utils.collections.export import (
        _cached_archive, _read_row_meta, _safe_archive_component,
        _scan_mod_patches)
    from Utils.config_paths import get_download_cache_dir

    check = _export_check(checkpoint, cancel_event)
    check()
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
        check()
        name = row["name"]
        mod_entry = entries.get(name)
        if mod_entry is None:
            continue
        mod_dir = Path(staging_root) / name if staging_root else None
        if mod_roots is not None and name in mod_roots:
            mod_dir = Path(mod_roots[name])
        if not (mod_dir and mod_dir.is_dir()):
            continue
        meta = _read_row_meta(staging_root, name)
        archive = (archive_paths.get(name) if archive_paths is not None
                   else _cached_archive(meta, game_name))
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
                                  name, warnings, checkpoint=check)
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
    CreateCollectionView._bundle_blocked_reason and Utils.collections.export's
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
                "bundle", "thunderstore", "plugin", "browse", "direct", "manual"):
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
        try:
            phase = int(mod.get("phase") or 0)
        except (TypeError, ValueError):
            phase = 0
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
            "version_info": src.get("versionInfo"),
            "logical":     (src.get("logicalFilename") or "").strip(),
            "optional":    bool(mod.get("optional", False)),
            "enabled":     mod.get("enabled", True) is not False,
            "phase":       phase,
            "root_folder": ((mod.get("details") or {}).get("type") or "")
                           .strip() == "dinput",
        })
    return out


def manifest_install_mods(manifest: dict):
    from Nexus.nexus_api import NexusCollectionMod

    mods = []
    total_size = 0
    for index, mod in enumerate(manifest.get("mods") or []):
        if not isinstance(mod, dict):
            raise ValueError(f"Manifest mod {index + 1} is not an object.")
        src = mod.get("source") or {}
        if not isinstance(src, dict):
            raise ValueError(f"Manifest mod {index + 1} has an invalid source.")

        def number(key, value):
            try:
                parsed = int(value or 0)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"Manifest mod {index + 1} has an invalid {key}.") from exc
            if parsed < 0:
                raise ValueError(
                    f"Manifest mod {index + 1} has an invalid {key}.")
            return parsed

        source_type = str(src.get("type") or "nexus").strip().lower()
        if source_type == "plugin":
            from Utils.collections.providers import entries as provider_entries
            provider_entries({"mods": [mod]})
            total_size += src["fileSize"]
            continue
        name = str(mod.get("name") or "")
        file_size = number("fileSize", src.get("fileSize"))
        total_size += file_size
        if src.get("bundle") is True or source_type == "bundle":
            mods.append(NexusCollectionMod(
                mod_name=name, file_name=name, source_type="bundle"))
            continue
        if source_type == "thunderstore":
            mods.append(NexusCollectionMod(
                mod_name=name,
                file_name=str(src.get("fullName") or name),
                size_bytes=file_size, source_type="thunderstore",
                version=str(src.get("version") or ""),
                optional=bool(mod.get("optional", False))))
            continue
        if source_type in ("browse", "direct", "manual"):
            continue
        category = mod.get("category") or {}
        if not isinstance(category, dict):
            category = {}
        mods.append(NexusCollectionMod(
            mod_id=number("modId", src.get("modId")),
            file_id=number("fileId", src.get("fileId")),
            mod_name=name,
            file_name=str(src.get("logicalFilename") or name),
            size_bytes=file_size,
            optional=bool(mod.get("optional", False)),
            source_type="nexus", version=str(mod.get("version") or ""),
            update_policy=str(src.get("updatePolicy") or "exact").lower(),
            category_id=number("category id", category.get("id")),
            category_name=str(category.get("name") or "").strip(),
            domain_name=str(mod.get("domainName") or "").strip()))
    return mods, total_size


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
        elif row_source == "plugin":
            record = row["plugin_source"]
            source = {"type": "plugin", "plugin_id": record["plugin_id"], "provider_id": record["provider_id"],
                      "source": row["plugin_source_uri"], "identity": record["identity"],
                      "sha256": record["sha256"], "fileSize": record["size_bytes"]}
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
        if row.get("phase"):
            mod_entry["phase"] = int(row["phase"])
        if row_source == "nexus" and row.get("update_policy") in ("exact", "prefer", "latest"):
            source["updatePolicy"] = row["update_policy"]
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
        install_type = ("dinput" if row.get("root_folder")
                        else row.get("collection_install_type") or "")
        if install_type:
            mod_entry["details"] = {"type": install_type}

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
                   bundle_roots=None, profile_roots=None, root_folder_root=None,
                   progress_cb=None, checkpoint=None, precommit=None,
                   overwrite=True, cancel_event=None, saves_archive=None,
                   include_profile_saves=True) -> Path:
    """Write a complete archive to a temporary sibling, then publish it."""
    import os
    from Utils.atomic_write import _tmp_for

    out_path = Path(out_path)
    if out_path.suffix.lower() not in (".zip", ".amethyst"):
        out_path = out_path.with_suffix(".amethyst")
    check = _export_check(checkpoint, cancel_event)
    check()
    jobs = amethyst_members(
        manifest, staging_root=staging_root, overwrite_root=overwrite_root,
        profile_dir=profile_dir, bundle_names=bundle_names, patch_jobs=patch_jobs,
        bundle_roots=bundle_roots, profile_roots=profile_roots,
        root_folder_root=root_folder_root, checkpoint=check,
        saves_archive=saves_archive, include_profile_saves=include_profile_saves)
    identities = {}
    for src, _arc, _data in jobs:
        if src is not None:
            info = src.stat()
            identities[src] = (info.st_dev, info.st_ino, info.st_size,
                               info.st_mtime_ns, info.st_ctime_ns)
    total = sum(len(data) if data is not None else identities[src][2]
                for src, _arc, data in jobs)
    done = 0
    if progress_cb:
        progress_cb(0, total, "")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = _tmp_for(out_path, suffix=".part")
    try:
        with temporary.open("xb") as stream:
            with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                for src, arcname, data in jobs:
                    check()
                    if data is not None:
                        zf.writestr(arcname, data)
                        done += len(data)
                        if progress_cb:
                            progress_cb(done, total, arcname)
                    else:
                        info = zipfile.ZipInfo.from_file(src, arcname, strict_timestamps=False)
                        info.compress_type = zipfile.ZIP_DEFLATED
                        with src.open("rb") as original, zf.open(info, "w", force_zip64=True) as target:
                            while True:
                                check()
                                chunk = original.read(1024 * 1024)
                                if not chunk:
                                    break
                                target.write(chunk)
                                done += len(chunk)
                                if progress_cb:
                                    progress_cb(done, total, arcname)
            stream.flush()
            os.fsync(stream.fileno())
        for src, expected in identities.items():
            check()
            info = src.stat()
            actual = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
            if actual != expected:
                raise ValueError(f"Export input changed while packing: {src}")
        if precommit:
            precommit()
        check()
        if overwrite:
            os.replace(temporary, out_path)
        else:
            _publish_new_archive(temporary, out_path)
    finally:
        temporary.unlink(missing_ok=True)
    return out_path


def _publish_new_archive(source, destination):
    import ctypes
    import errno
    import os

    try:
        rename = ctypes.CDLL(None, use_errno=True).renameat2
    except AttributeError:
        rename = None
    if rename is not None:
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                           ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        if rename(-100, os.fsencode(source), -100, os.fsencode(destination), 1) == 0:
            return
        error = ctypes.get_errno()
        if error not in (errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP):
            raise OSError(error, os.strerror(error), str(destination))
    os.link(source, destination, follow_symlinks=False)


def amethyst_members(manifest, *, staging_root=None, overwrite_root=None,
                     profile_dir=None, bundle_names=None, patch_jobs=None,
                     bundle_roots=None, profile_roots=None, root_folder_root=None,
                     checkpoint=None, saves_archive=None, include_profile_saves=True):
    check = checkpoint or (lambda: None)
    jobs = [(None, "manifest.json", json.dumps(manifest, indent=2).encode("utf-8"))]
    if saves_archive is not None:
        jobs.append((Path(saves_archive), "saves/saves.zip", None))
    for src, arcname in patch_jobs or []:
        jobs.append((Path(src), str(arcname), None))
    for name in bundle_names or []:
        check()
        root = (bundle_roots or {}).get(name)
        if root is None and staging_root:
            root = Path(staging_root) / name
        if root is not None:
            root = Path(root)
            if not root.is_dir():
                raise ValueError(f"Bundled mod is unavailable: {name}")
            jobs.append((None, f"mods/{name}/", b""))
            for fp in sorted(root.rglob("*")):
                check()
                if fp.is_file():
                    jobs.append((fp, (Path("mods") / name / fp.relative_to(root)).as_posix(), None))
    if overwrite_root:
        root = Path(overwrite_root)
        if root.is_dir():
            for fp in sorted(root.rglob("*")):
                check()
                if fp.is_file():
                    jobs.append((fp, (Path("overwrite") / fp.relative_to(root)).as_posix(), None))
    if profile_dir:
        from Nexus.nexus_meta import read_meta
        from Utils.profiles.state import (
            merge_ignored_mod_updates, read_ignored_mod_updates, read_profile_state,
        )

        pdir = Path(profile_dir)
        state_path = pdir / "profile_state.json"
        state = (json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file()
                 else read_profile_state(pdir, read_only=True))
        if not isinstance(state, dict):
            raise ValueError("Export profile state must be an object.")
        state = portable_profile_state(state)
        metas = []
        for mod in manifest.get("mods") or []:
            check()
            name = mod.get("name") if isinstance(mod, dict) else None
            if not isinstance(name, str) or Path(name).name != name:
                continue
            root = (bundle_roots or {}).get(name)
            if root is None and staging_root:
                root = Path(staging_root) / name
            if root is not None and (Path(root) / "meta.ini").is_file():
                meta = read_meta(Path(root) / "meta.ini")
                meta.mod_name = name
                metas.append(meta)
        ignored = merge_ignored_mod_updates(read_ignored_mod_updates(pdir, state, read_only=True), metas)
        if ignored:
            state["ignored_mod_updates"] = ignored
        else:
            state.pop("ignored_mod_updates", None)
        jobs.append((None, "profile/profile_state.json", json.dumps(state, indent=2).encode("utf-8")))
        for name in ("modlist.txt", "plugins.txt", "loadorder.txt", "userlist.yaml"):
            check()
            fp = pdir / name
            if not fp.is_file():
                continue
            jobs.append((fp, f"profile/{name}", None))
        for fp in sorted(pdir.glob("*.ini")):
            check()
            if fp.is_file():
                jobs.append((fp, f"profile/{fp.name}", None))
        roots = dict(profile_roots or {})
        if root_folder_root is not None:
            roots["Root_Folder"] = Path(root_folder_root)
        for sub in ("ini files", "Saves", "installed_collections", "fomod", "bain", "Root_Folder"):
            if sub == "Saves" and not include_profile_saves:
                continue
            root = Path(roots.get(sub, pdir / sub))
            if not root.is_dir():
                continue
            for fp in sorted(root.rglob("*")):
                check()
                if fp.is_file():
                    jobs.append((fp, (Path("profile") / sub / fp.relative_to(root)).as_posix(), None))
    seen, files, directories = set(), set(), set()
    for _src, name, _data in jobs:
        directory = name.endswith("/") and _src is None and _data == b""
        parts = name.rstrip("/").split("/") if directory else name.split("/")
        if (name.startswith("/") or "\\" in name or any(part in ("", ".", "..") for part in parts)
                or any(ord(char) < 32 for char in name)):
            raise ValueError(f"Unsafe export member name: {name!r}")
        key = "/".join(parts).casefold()
        parents = {"/".join(parts[:index]).casefold() for index in range(1, len(parts))}
        if key in seen or (not directory and key in directories) or parents.intersection(files):
            raise ValueError(f"Conflicting export member name: {name!r}")
        seen.add(key)
        directories.update(parents)
        (directories if directory else files).add(key)
    return jobs


def portable_profile_state(state):
    import copy

    state = copy.deepcopy(state)
    if "plugin_metadata" in state:
        from amethyst_plugins.metadata import portable
        metadata = portable(state["plugin_metadata"])
        if metadata:
            state["plugin_metadata"] = metadata
        else:
            state.pop("plugin_metadata", None)
    groups = state.get("mod_groups")
    if isinstance(groups, dict):
        state["mod_groups"] = {leader: data for leader, data in groups.items()
                               if not isinstance(data, dict) or "title" not in data}
    settings = state.get("profile_settings")
    if not isinstance(settings, dict):
        settings = {}
        state["profile_settings"] = settings
    settings["profile_specific_mods"] = True
    if settings.get("is_group"):
        settings.pop("is_group", None)
        for key in list(settings):
            if key.startswith("group_") or key == "pending_collection_group":
                settings.pop(key)
        for key in list(state):
            if key.startswith("group_"):
                state.pop(key)
    return state

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
                         log_fn=None, error_sink=None, on_staged=None) -> list[str]:
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
    profile_dir = Path(profile_dir).absolute()
    mods_dir = Path(mods_dir).absolute()
    overwrite_dir = (Path(overwrite_dir).absolute() if overwrite_dir else
                     (mods_dir.parent / "overwrite"))
    if not _zip.is_zipfile(src_path):
        return []

    mods_dir.mkdir(parents=True, exist_ok=True)
    overwrite_dir.mkdir(parents=True, exist_ok=True)

    staged: list[str] = []
    with _zip.ZipFile(src_path, "r") as zf:
        plans = []
        destinations, files, directories = set(), set(), set()
        roots = {
            "mods": mods_dir,
            "overwrite": overwrite_dir,
            "profile": profile_dir,
        }
        for info in zf.infolist():
            name = info.filename
            original = getattr(info, "orig_filename", name)
            if "\0" in original or "\\" in name or len(name) > 4096:
                raise ValueError(f"Unsafe bundle member path: {name!r}")
            parts = name.rstrip("/").split("/") if info.is_dir() else name.split("/")
            if not parts or parts[0] not in roots:
                continue
            if len(parts) == 1 and info.is_dir():
                continue
            if (len(parts) < 2 or any(
                    not part or part in (".", "..") or len(part) > 255
                    for part in parts[1:])):
                raise ValueError(f"Unsafe bundle member path: {name!r}")
            if parts[0] == "mods" and len(parts) < 3 and not info.is_dir():
                raise ValueError(f"Invalid bundled mod member: {name!r}")
            mode = (int(info.external_attr) >> 16) & 0o170000
            if mode == 0o120000:
                raise ValueError(f"Bundle symbolic links are not supported: {name!r}")
            root = roots[parts[0]]
            dest = root.joinpath(*parts[1:])
            cursor = dest
            while cursor != root:
                if cursor.is_symlink():
                    raise ValueError(
                        f"Bundle destination crosses a symbolic link: {name!r}")
                cursor = cursor.parent
            try:
                dest.resolve(strict=False).relative_to(root.resolve())
            except ValueError as exc:
                raise ValueError(f"Unsafe bundle member path: {name!r}") from exc
            key = tuple(part.casefold() for part in dest.parts)
            parents = {key[:index] for index in range(1, len(key))}
            if (key in destinations or (not info.is_dir() and key in directories)
                    or parents.intersection(files)):
                raise ValueError(f"Conflicting bundle member path: {name!r}")
            destinations.add(key)
            directories.update(parents)
            (directories if info.is_dir() else files).add(key)
            plans.append((info, dest, parts[0], parts[1]))

        # (1) Bundled mods + overwrite - extract verbatim (no rename, keep meta.ini).
        try:
            for info, dest, root_name, mod_name in plans:
                if root_name not in ("mods", "overwrite"):
                    continue
                if root_name == "mods" and mod_name not in staged:
                    staged.append(mod_name)
                if info.is_dir():
                    dest.mkdir(parents=True, exist_ok=True)
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as srcf, open(dest, "wb") as dstf:
                    shutil.copyfileobj(srcf, dstf)
        finally:
            if on_staged is not None:
                for name in staged:
                    on_staged(name)
        if staged:
            log(f"Import: extracted {len(staged)} bundled mod(s): "
                f"{', '.join(staged)}")

        # (2) profile/ state files → copy over the generated ones.
        wrote_profile = False
        for info, dest, root_name, _mod_name in plans:
            if root_name != "profile":
                continue
            if info.is_dir():
                dest.mkdir(parents=True, exist_ok=True)
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as srcf, open(dest, "wb") as dstf:
                shutil.copyfileobj(srcf, dstf)
            wrote_profile = True
        if wrote_profile:
            log(f"Import: restored profile state files into {profile_dir}")
            from Utils.profiles.state import restore_ignored_mod_updates
            restore_ignored_mod_updates(profile_dir, mods_dir)
            # Snapshot the pristine authored order files NOW - the reconcile
            # below drops modlist rows for off-site mods that aren't installed
            # yet, and Reset Load Order needs the full original to put them
            # back in place once the user installs them.
            try:
                from Utils.collections.export import write_amethyst_stash
                if write_amethyst_stash(profile_dir, log_fn=log):
                    log("Import: saved Amethyst/ order snapshot "
                        "(Reset Load Order restores from it).")
            except Exception as exc:
                if error_sink is not None:
                    error_sink.append(f"Order snapshot: {exc}")
                log(f"Import: order snapshot failed: {exc}")

    # (3) Reconcile the modlist against what's actually on disk: the bundled
    # modlist.txt lists mods that were NOT exported (disabled leftovers from the
    # source profile) - drop those phantom entries now so they don't linger until
    # a manual Refresh. Mirrors the Refresh path's folder-sync.
    try:
        from Utils.mods.modlist import sync_modlist_with_mods_folder
        sync_modlist_with_mods_folder(profile_dir / "modlist.txt", mods_dir)
        log("Import: reconciled modlist.txt against staged mods.")
    except Exception as exc:
        if error_sink is not None:
            error_sink.append(f"Modlist reconciliation: {exc}")
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
MAX_CODE_LENGTH = 8 * 1024 * 1024
MAX_CODE_MANIFEST_BYTES = 16 * 1024 * 1024


def build_code_manifest(entries, game, app_version: str, *,
                        profile_name=None) -> dict:
    """Build a share-code manifest from *entries* - modlist entries (separators
    included) in ``read_modlist`` order (index 0 = HIGHEST priority = top of
    modlist). Includes mods the recipient can re-download: Nexus mods with a
    modId + fileId, and Thunderstore mods with a complete package pin. Embeds
    FOMOD/BAIN choices, per-mod enabled state and root-deploy flags.

    The collection-install pipeline that consumes an imported manifest treats the
    ``mods`` array as LOW-priority first (``mods[-1]`` becomes the top of the
    modlist - see ``Utils.collections.reset._topo_sort_collection``). *entries* is highest-
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
    keep = []
    for row in rows:
        if row.get("source") == "plugin":
            keep.append(row)
            continue
        if row.get("source") == "thunderstore":
            if row.get("ts_namespace") and row.get("ts_name") and row.get("ts_version"):
                keep.append(row)
                continue
            row = {**row, "source": "nexus"}
        if row.get("mod_id") and _row_file_id(row):
            keep.append(row)
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
            from Utils.profiles.state import read_separator_colors, read_separator_locks
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
    if not isinstance(code, str) or not code:
        raise ValueError("Empty code.")
    if len(code) > MAX_CODE_LENGTH:
        raise ValueError("The share code exceeds 8 MiB.")
    text = "".join(code.split())   # strip all whitespace / newlines
    if text.startswith(CODE_PREFIX):
        text = text[len(CODE_PREFIX):]
    # A pasted paste-service link is not a code - tell the user that rather than
    # letting base64 fail with something meaningless. The caller (the import
    # overlay) resolves links via fetch_code_url before ever getting here.
    from Utils.sharing.paste import is_url
    if is_url(text):
        raise ValueError("That looks like a link, not a code - it could not be "
                         "downloaded.")

    def reject_constant(value):
        raise ValueError(f"Invalid JSON number: {value}")

    try:
        packed = base64.b64decode(text.encode("ascii"), altchars=b"-_", validate=True)
        decoder = zlib.decompressobj()
        raw = decoder.decompress(packed, MAX_CODE_MANIFEST_BYTES + 1)
        if len(raw) > MAX_CODE_MANIFEST_BYTES or decoder.unconsumed_tail:
            raise ValueError("The share manifest exceeds 16 MiB.")
        if not decoder.eof or decoder.unused_data:
            raise ValueError("The share code is truncated or has trailing data.")
        manifest = json.loads(raw.decode("utf-8"), parse_constant=reject_constant)
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
# The transport lives in Utils/paste_upload.py (the log panel uploads through it
# too). These wrappers keep the code-flavoured names and defaults.

from Utils.sharing.paste import (            # noqa: E402  (kept beside its users)
    PASTE_HOST, RETENTION_NOTE, is_url as is_code_url,
)


def upload_code(code: str, *, timeout: float = 30.0) -> str:
    """Upload a share *code* and return its URL. Raises ``RuntimeError`` with a
    user-facing message on failure - callers fall back to the raw code."""
    from Utils.sharing.paste import upload_text
    return upload_text(code, timeout=timeout)


def fetch_code_url(url: str, *, timeout: float = 20.0) -> str:
    """Download a share code from *url*. Raises ``RuntimeError`` on failure."""
    from Utils.sharing.paste import fetch_text
    return fetch_text(url, timeout=timeout)
