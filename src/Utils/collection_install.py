"""Toolkit-neutral automatic (premium) Nexus collection install (no Tk / no Qt).

A faithful port of ``gui/collections_dialog.py:_run_install`` - the premium
download→install pipeline - with the Tk widget calls replaced by a callback
interface and the Tk-only ``install_mod_from_archive`` replaced by the neutral
``Utils.mod_install.install_collection_archive``. The heavy backend
(``fomod_installer``/``bain_installer``/``nexus_download``/``collection_reset``/
``nexus_meta``/``loot_sorter``) is reused verbatim.

Load order is driven by ``collection.json`` from the collection archive: the
``mods`` array (via ``_resolve_collection_priorities``) defines install order,
the ``plugins`` array defines plugins.txt order. Both are written after all mods
install.

The caller (Qt) constructs :class:`CollectionInstallCallbacks` (each a single
``Signal.emit`` marshaling to a UI-thread slot) + :class:`CollectionInstallControl`
(cancel/pause/stop Events) and runs :func:`run_collection_install` on a daemon
thread. Interactive FOMOD/BAIN mods with no author selections are DEFERRED to the
end and resolved one-by-one via ``callbacks.resolve_fomod`` / ``resolve_bain``.

v1 wires the NEW-profile path (the primary flow). Append/update reconcile helpers
are ported but not yet wired (see ``overwrite_existing`` / ``update_context``).
"""

from __future__ import annotations

import itertools as _itertools
import json
import queue as _queue
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from Utils.collection_reset import (
    _resolve_collection_priorities, _apply_collection_groups)
from Utils.config_paths import get_download_cache_dir_for_game, list_all_cache_dirs
from Utils.download_locations import (
    is_default_downloads_disabled, load_extra_download_locations)
from Utils.download_scheduler import order_by_size, run_pipelined
from Utils.extract_budget import ExtractionMemoryBudget, get_uncompressed_size
from Utils.mod_install import (
    install_collection_archive, FOMOD_DEFERRED, BAIN_DEFERRED)
from Utils.modlist import read_modlist, write_modlist, ModEntry
from Utils.plugins import write_plugins, write_loadorder, PluginEntry
from Utils.ui_config import (
    load_collection_settings, load_clear_archive_after_install,
    load_keep_fomod_archives)
from Nexus.nexus_download import (
    DownloadResult, _find_cached_archive, _clean_nexus_stem,
    delete_archive_and_sidecar, _get_downloads_dir)
from Nexus.nexus_meta import build_meta_from_download


# ---------------------------------------------------------------------------
# Pure map helpers moved verbatim from gui/collections_dialog.py (imported back
# there to keep ONE implementation). No Tk.
# ---------------------------------------------------------------------------
def _build_collision_suffix_map(
    schema_mods: "list[dict]",
    schema_file_id_to_logical: "dict[int, str]",
    schema_pos_to_name: "dict[int, str]",
    schema_file_id_to_pos: "dict[int, int]",
) -> "dict[int, str]":
    """Return file_id → suffix to append when multiple collection entries from
    different mod pages would otherwise install into the same folder. Returns ""
    for non-colliding entries; the suffix string for colliders."""
    base_to_fids: dict[str, list[int]] = {}
    fid_to_base: dict[int, str] = {}
    fid_to_mod_id: dict[int, int] = {}
    for sm in schema_mods:
        src = sm.get("source") or {}
        fid = src.get("fileId")
        if fid is None:
            continue
        fid = int(fid)
        logical = schema_file_id_to_logical.get(fid, "") or ""
        schema_name = schema_pos_to_name.get(
            schema_file_id_to_pos.get(fid, -1), "") or ""
        base = (logical or schema_name or sm.get("name") or "").strip()
        if not base:
            continue
        fid_to_base[fid] = base
        mid = src.get("modId")
        if mid:
            fid_to_mod_id[fid] = int(mid)
        base_to_fids.setdefault(base.lower(), []).append(fid)

    result: dict[int, str] = {}
    for fid, base in fid_to_base.items():
        siblings = base_to_fids.get(base.lower(), [])
        if len(siblings) <= 1:
            result[fid] = ""
            continue
        sibling_mod_ids = {fid_to_mod_id.get(s) for s in siblings}
        sibling_mod_ids.discard(None)
        if len(sibling_mod_ids) <= 1:
            result[fid] = ""
            continue
        mod_id = fid_to_mod_id.get(fid)
        result[fid] = f" ({mod_id})" if mod_id else ""
    return result


def _fomod_choices_from_collection(choices: dict) -> "dict[str, dict[str, list[str]]]":
    """Convert a collection.json FOMOD ``choices`` block to the saved_selections
    format ``{step_key: {group: [plugins]}}`` that ``resolve_files`` expects.

    Steps are keyed by their NAME when available: the ``options`` array only
    holds the steps the author actually visited, so its position does not match
    the FOMOD's real step index whenever a step was skipped by a visibility
    condition. ``resolve_files`` falls back to a name lookup per step.
    """
    result: dict = {}
    seen_names: set = set()
    for step_idx, step in enumerate(choices.get("options", [])):
        groups: dict = {}
        for group in step.get("groups", []):
            group_name = group.get("name", "")
            plugin_names = [c["name"] for c in group.get("choices", []) if c.get("name")]
            if plugin_names:
                groups[group_name] = plugin_names
        if groups:
            step_name = (step.get("name") or "").strip()
            if step_name and step_name not in seen_names:
                seen_names.add(step_name)
                result[step_name] = groups
            else:
                result[str(step_idx)] = groups
    return result


# ---------------------------------------------------------------------------
# Callback / control interface (the Qt caller wires each to a Signal.emit).
# ---------------------------------------------------------------------------
def _noop(*_a, **_k):
    return None


@dataclass
class CollectionInstallCallbacks:
    on_status: Callable[[str], None] = _noop            # status line text
    on_progress: Callable[["float | None"], None] = _noop  # 0..1 or None=hide
    on_agg_download: Callable[[int, int, float], None] = _noop  # bytes cur,total,MB/s
    on_display_total: Callable[[int], None] = _noop     # true collection size (bytes)
    # RED - active downloads
    on_dl_mod_start: Callable[[int, str, int], None] = _noop   # file_id,name,size
    on_dl_mod_update: Callable[[int, int, int], None] = _noop  # file_id,cur,tot
    on_dl_mod_finish: Callable[[int], None] = _noop            # file_id
    # GREEN - extracting/queued
    on_extract_queue: Callable[[int, str], None] = _noop       # file_id,name
    on_extract_add: Callable[[int, str], None] = _noop
    on_extract_update: Callable[[int, int, int], None] = _noop  # file_id,cur,tot (tot 0 = busy)
    on_extract_remove: Callable[[int], None] = _noop
    on_row_installed: Callable[[int], None] = _noop            # file_id landed
    # manual (non-premium) mode - current-mod card payload dict
    on_manual_mod: Callable[[dict], None] = _noop
    # logging / lifecycle
    on_log: Callable[[str], None] = _noop
    on_done: Callable[[int, int, int, str], None] = _noop      # installed,skipped,total,profile
    on_paused: Callable[[int, str], None] = _noop              # installed,profile
    on_cancelled: Callable[[object], None] = _noop             # profile_dir (Path)
    # interactive resolvers (BLOCK the worker; caller marshals a wizard)
    resolve_fomod: "Callable | None" = None   # (config, base, name, inst, act, loose, saved) -> dict|None
    resolve_bain: "Callable | None" = None     # (subpkgs, root, name) -> {"selected":[...]}|None


@dataclass
class CollectionInstallControl:
    cancel: threading.Event = field(default_factory=threading.Event)
    pause: threading.Event = field(default_factory=threading.Event)
    stop: threading.Event = field(default_factory=threading.Event)  # set by BOTH pause & cancel
    # manual mode - user actions from the overlay: a str path (Select File…)
    # or None (Skip, honored for optional mods only). Mirrors Tk's
    # _manual_file_queue.
    manual_queue: _queue.Queue = field(default_factory=_queue.Queue)


# ---------------------------------------------------------------------------
# Cancel cleanup (neutral body of _do_cancel_cleanup; the Tk topbar tail is the
# caller's job).
# ---------------------------------------------------------------------------
def cleanup_cancelled_install(game, profile_dir: "Path | None", *,
                              delete_profile: bool = True,
                              clear_cache: bool = False, log_fn=_noop) -> None:
    """Restore any deployed files, optionally delete the collection profile
    dir, and optionally clear this game's download cache.

    ``delete_profile`` must be True ONLY when the cancelled install created
    the profile itself (a fresh new-profile install). Continue/append/resume/
    update installs target a pre-existing profile - deleting it would destroy
    the user's mods and settings (GH#278)."""
    import shutil
    if profile_dir is not None and Path(profile_dir).is_dir() and game is not None \
            and getattr(game, "is_configured", lambda: True)():
        try:
            game.set_active_profile_dir(Path(profile_dir))
            game.load_paths()
            if hasattr(game, "restore"):
                game.restore()
        except Exception as exc:
            log_fn(f"Cancel: restore failed: {exc}")
        try:
            from Utils.deploy import restore_root_folder
            root_folder_dir = game.get_effective_root_folder_path()
            game_root = game.get_game_path()
            if root_folder_dir.is_dir() and game_root:
                restore_root_folder(
                    root_folder_dir, game_root,
                    data_deploy_dirs=game.root_restore_protect_dirs()
                    if hasattr(game, "root_restore_protect_dirs") else None,
                )
        except Exception as exc:
            log_fn(f"Cancel: restore_root_folder failed: {exc}")
        try:
            game.set_active_profile_dir(None)
            game.load_paths()
        except Exception:
            pass
    if profile_dir is not None and Path(profile_dir).is_dir():
        if delete_profile:
            try:
                shutil.rmtree(str(profile_dir))
                log_fn(f"Cancel: deleted profile dir {profile_dir}")
            except Exception as exc:
                log_fn(f"Cancel: failed to delete profile dir: {exc}")
        else:
            log_fn(f"Cancel: kept pre-existing profile dir {profile_dir}")
    if clear_cache:
        try:
            game_cache = get_download_cache_dir_for_game(getattr(game, "name", "") or "")
            if game_cache and game_cache.is_dir():
                for item in game_cache.iterdir():
                    try:
                        if item.is_file() or item.is_symlink():
                            item.unlink()
                        elif item.is_dir():
                            shutil.rmtree(str(item), ignore_errors=True)
                    except Exception:
                        pass
                log_fn("Cancel: cleared download cache")
        except Exception as exc:
            log_fn(f"Cancel: failed to clear download cache: {exc}")


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------
def run_collection_install(
        *, game, api, downloader, mods: list, download_link_path: str,
        profile_dir: "Path | None", old_profile_dir: "Path | None",
        collection_slug: str, revision_number: "int | None" = None,
        collection_total_size: int = 0,
        collection_schema_cache: "dict | None" = None,
        overwrite_existing: "bool | None" = None,
        skipped_fids: "set[int] | None" = None,
        skipped_mods: "list | None" = None,
        skip_existing: bool = False,
        with_bundled: bool = True,
        update_context: "dict | None" = None,
        manual_mode: bool = False,
        download_only: bool = False,
        append_card_info: "dict | None" = None,
        local_bundle_zip: str = "",
        callbacks: "CollectionInstallCallbacks | None" = None,
        control: "CollectionInstallControl | None" = None) -> None:
    """Download then install every mod in *mods* in collection-defined order.

    Faithful port of ``CollectionsDialog._run_install`` - see module docstring.
    ``overwrite_existing``: None=new-profile install (the wired v1 path); a bool
    selects the append path (ported but not yet exercised by the Qt caller).
    ``append_card_info``: display fields for the collection (NexusCollection as
    a dict) - on append runs the manifest is recorded to
    ``<profile>/installed_collections/<slug>.json`` (instead of clobbering the
    profile's primary ``collection.json``) so the Collections browser can list
    and cleanly remove appended collections. Written up-front, so a cancelled
    or paused append still leaves the record and Remove cleans up the partial
    install.
    ``update_context``: when set (a collection UPDATE - continue semantics, so
    ``overwrite_existing`` stays None), the Step-3 modlist write uses the
    order-preserving ``_reconcile_update_modlist`` (snapshot + schema-neighbour
    insertion, run BEFORE Step 3b like Tk) instead of the new-profile write,
    and the final reconciliation is skipped.
    ``manual_mode``: non-premium install (port of Tk ``_run_manual_install``):
    the download producer is replaced by a sequential per-mod prompt
    (``callbacks.on_manual_mod``) + download-folder poll; the user downloads
    each archive in a browser (or picks it / skips via ``control.manual_queue``)
    and the same install consumers take it from there.
    ``download_only``: fetch every manifest archive into the download cache and
    install NOTHING - no extraction, staging or profile writes. ``profile_dir``
    is then None and the run ends once the download pipeline drains.
    ``local_bundle_zip``: the ``.amethyst`` file behind a local-manifest import.
    There is no cached collection archive for those, so Step 3b reads the
    manifest's binary patches (``patches/`` members) from this zip instead; the
    bundled ``mods/`` + ``profile/`` members are extracted by the caller after
    the run (``profile_export.install_local_bundle``).
    """
    cb = callbacks or CollectionInstallCallbacks()
    ctl = control or CollectionInstallControl()
    log = cb.on_log
    game_domain = (getattr(game, "nexus_game_domain", None)
                   or getattr(game, "game_id", "") or "")

    _slug = collection_slug or ""
    _set_status = cb.on_status
    _set_progress = cb.on_progress

    # download_only never touches a profile: no active-profile swap (that would
    # repoint the live game object), and the paths stay None so a missed write
    # raises instead of landing in the user's real profile.
    if download_only:
        modlist_path = plugins_path = staging_path = None
    else:
        game.set_active_profile_dir(profile_dir)
        game.load_paths()
        modlist_path = profile_dir / "modlist.txt"
        plugins_path = profile_dir / "plugins.txt"
        staging_path = game.get_effective_mod_staging_path()
    installed = 0
    skipped = 0
    total = len(mods)

    _is_append_run = overwrite_existing is not None
    _append_pre_existing: "set[str]" = set()
    if _is_append_run and modlist_path is not None and modlist_path.is_file():
        try:
            _append_pre_existing = {
                e.name.lower() for e in read_modlist(modlist_path)
                if not e.is_separator
            }
        except Exception:
            _append_pre_existing = set()

    # ------------------------------------------------------------------
    # Step 1: fetch/parse collection.json for authoritative order
    # ------------------------------------------------------------------
    collection_schema: dict = {}
    if collection_schema_cache:
        collection_schema = collection_schema_cache
        log("Collection install: reusing cached collection.json")
    if download_link_path and not collection_schema:
        _set_status("Downloading collection manifest…")
        try:
            # Cache-first (reads/keeps <slug>_rev<rev>.7z in the download cache)
            # - the detail view usually cached it already, so a flaky CDN at
            # install time doesn't cost us the manifest.
            from Utils.collection_manifest import load_collection_manifest
            collection_schema = load_collection_manifest(
                api, getattr(game, "name", "") or "", _slug, revision_number,
                download_link_path, log_fn=log)
            log(f"Collection install: parsed collection.json "
                f"({len(collection_schema.get('mods', []))} mod entries, "
                f"{len(collection_schema.get('plugins', []))} plugins)")
        except Exception as exc:
            log(f"Collection install: could not download collection.json: {exc}")
    if not collection_schema and not _is_append_run and profile_dir is not None:
        # Last resort (continue/update runs): the profile's saved manifest from
        # the original install. Never on append - that file belongs to the
        # profile's primary collection, not the one being appended.
        _saved = profile_dir / "collection.json"
        if _saved.is_file():
            try:
                collection_schema = json.loads(_saved.read_text(encoding="utf-8"))
                log("Collection install: using the profile's saved collection.json")
            except Exception:
                collection_schema = {}
    if not collection_schema:
        log("WARNING: collection manifest unavailable - install order falls "
            "back to GraphQL and FOMOD/BAIN choices canNOT be auto-applied "
            "(installers will prompt at the end)")

    if download_only:
        pass            # no profile to record the manifest into
    elif _is_append_run:
        # Append: record under installed_collections/ - do NOT clobber the
        # profile's primary collection.json (the update path diffs against it).
        from Utils.installed_collections import record_appended_collection
        record_appended_collection(
            profile_dir, slug=_slug, revision=revision_number,
            card=append_card_info or {}, manifest=collection_schema or {},
            log_fn=log)
    elif collection_schema:
        try:
            (profile_dir / "collection.json").write_text(
                json.dumps(collection_schema, indent=2), encoding="utf-8")
            log(f"Collection install: saved manifest to {profile_dir / 'collection.json'}")
        except Exception as exc:
            log(f"Collection install: could not save manifest: {exc}")

    schema_mods: list[dict] = collection_schema.get("mods", [])
    schema_file_id_to_pos: dict[int, int] = _resolve_collection_priorities(collection_schema)
    schema_pos_to_name: dict[int, str] = {}
    schema_file_id_to_logical: dict[int, str] = {}
    schema_file_id_to_mod_id: dict[int, int] = {}
    schema_file_id_to_install_type: dict[int, str] = {}
    schema_file_id_to_category: dict[int, str] = {}
    schema_file_id_to_phase: dict[int, int] = {}
    # source.fileSize / source.md5 / mods[].domainName - the GraphQL mod list
    # omits these for cross-domain entries (e.g. a Skyrim SE mod referenced by
    # an Enderal SE collection), so manual mode matches against the manifest
    # values first (Tk parity: collections_dialog.py schema_file_id_to_size/…).
    schema_file_id_to_size: dict[int, int] = {}
    schema_file_id_to_md5: dict[int, str] = {}
    schema_file_id_to_domain: dict[int, str] = {}
    # mods-array index (collection.json install order). NOT the same as
    # schema_file_id_to_pos, which is the REVERSED priority rank (0 = top of
    # modlist) - manual mode prompts in the order the author listed the mods.
    schema_file_id_to_arrayidx: dict[int, int] = {}
    fomod_by_file_id: dict[int, dict] = {}
    bain_by_file_id: dict[int, dict] = {}
    _raw_logical: dict[int, str] = {}
    _raw_name: dict[int, str] = {}
    for schema_mod in schema_mods:
        src = schema_mod.get("source") or {}
        fid = src.get("fileId")
        if fid is not None:
            fid = int(fid)
            _raw_logical[fid] = src.get("logicalFilename") or ""
            _raw_name[fid] = schema_mod.get("name") or ""
    _logical_counts: dict[str, int] = {}
    for raw in _raw_logical.values():
        if raw:
            _logical_counts[raw] = _logical_counts.get(raw, 0) + 1

    for pos, schema_mod in enumerate(schema_mods):
        src = schema_mod.get("source") or {}
        fid = src.get("fileId")
        if fid is not None:
            fid = int(fid)
            topo_pos = schema_file_id_to_pos.get(fid, pos)
            schema_pos_to_name[topo_pos] = schema_mod.get("name") or ""
            raw_logical = _raw_logical.get(fid, "")
            schema_name = _raw_name.get(fid, "")
            if raw_logical and _logical_counts.get(raw_logical, 0) > 1:
                logical = schema_name or raw_logical
            else:
                logical = raw_logical or schema_name
            schema_file_id_to_logical[fid] = logical
            mid = src.get("modId")
            if mid:
                schema_file_id_to_mod_id[fid] = int(mid)
            _sz = src.get("fileSize")
            if _sz:
                try:
                    schema_file_id_to_size[fid] = int(_sz)
                except (TypeError, ValueError):
                    pass
            _md5_v = (src.get("md5") or "").strip().lower()
            if _md5_v:
                schema_file_id_to_md5[fid] = _md5_v
            _dom = (schema_mod.get("domainName") or "").strip()
            if _dom:
                schema_file_id_to_domain[fid] = _dom
            schema_file_id_to_arrayidx[fid] = pos
            _details = schema_mod.get("details") or {}
            _det_type = (_details.get("type") or "").strip()
            if _det_type:
                schema_file_id_to_install_type[fid] = _det_type
            _det_cat = (_details.get("category") or "").strip()
            if _det_cat:
                schema_file_id_to_category[fid] = _det_cat
            try:
                schema_file_id_to_phase[fid] = int(schema_mod.get("phase") or 0)
            except (TypeError, ValueError):
                schema_file_id_to_phase[fid] = 0
            choices = schema_mod.get("choices") or {}
            _ctype = choices.get("type") or ""
            # Vortex manifests carry no "type" key - a bare {"options": [...]}
            # (see any published collection.json) is the FOMOD replay format.
            if _ctype == "fomod" or (not _ctype and choices.get("options")):
                fomod_by_file_id[fid] = _fomod_choices_from_collection(choices)
            elif _ctype == "fomod_selections":
                fomod_by_file_id[fid] = choices["selections"]
            elif _ctype == "bain_selections":
                bain_by_file_id[fid] = choices["selections"]

    def _sort_key(m):
        return schema_file_id_to_pos.get(m.file_id, len(schema_mods))

    ordered_mods = sorted(mods, key=_sort_key)

    schema_file_id_to_suffix: dict[int, str] = _build_collision_suffix_map(
        schema_mods, schema_file_id_to_logical, schema_pos_to_name,
        schema_file_id_to_pos)

    # ------------------------------------------------------------------
    # Step 2 pre-scan staging for already-installed mods
    # ------------------------------------------------------------------
    already_installed_by_ids: dict[tuple[int, int], str] = {}
    already_installed_by_fid: dict[int, str] = {}
    staging_lower_map: dict[str, str] = {}
    # folder name (lower) -> file_id recorded in its meta.ini (0 if none). Used to
    # guard name-fallback removal of unticked optionals: a folder that carries a
    # DIFFERENT mod's file_id must never be removed just because its name matches
    # the cleaned-up title of a skipped optional (e.g. "X - AE" cleans to "X").
    staging_folder_fid: dict[str, int] = {}

    # download_only leaves these paths None, so the pre-scan and the
    # unticked-optional removal below are both inert.
    _profile_mod_names: set[str] = set()
    if modlist_path is not None and modlist_path.is_file():
        try:
            for entry in read_modlist(modlist_path):
                _profile_mod_names.add(entry.name.lower())
        except Exception:
            pass

    import configparser as _cp
    if staging_path is not None and staging_path.exists():
        for mod_dir in staging_path.iterdir():
            if not mod_dir.is_dir():
                continue
            if mod_dir.name.lower() in _profile_mod_names:
                staging_lower_map[mod_dir.name.lower()] = mod_dir.name
            meta_ini = mod_dir / "meta.ini"
            if not meta_ini.is_file():
                continue
            try:
                _parser = _cp.ConfigParser()
                _parser.read(str(meta_ini), encoding="utf-8")
                fid_str = _parser.get("General", "fileid", fallback="").strip()
                mid_str = _parser.get("General", "modid", fallback="").strip()
                if fid_str and fid_str != "0":
                    if skip_existing and mod_dir.name.lower() not in _profile_mod_names:
                        continue
                    _fid = int(fid_str)
                    _mid = int(mid_str) if mid_str.isdigit() else 0
                    staging_folder_fid[mod_dir.name.lower()] = _fid
                    if _mid > 0:
                        already_installed_by_ids[(_mid, _fid)] = mod_dir.name
                    else:
                        already_installed_by_fid[_fid] = mod_dir.name
            except Exception:
                pass

    def _match_existing(mod) -> str:
        _mid = schema_file_id_to_mod_id.get(mod.file_id, 0) or getattr(mod, "mod_id", 0) or 0
        if _mid > 0 and (_mid, mod.file_id) in already_installed_by_ids:
            return already_installed_by_ids[(_mid, mod.file_id)]
        return already_installed_by_fid.get(mod.file_id, "")

    def _name_candidates(mod) -> "list[str]":
        from Utils.mod_name_utils import _suggest_mod_names
        logical = schema_file_id_to_logical.get(mod.file_id, "") or ""
        schema_name = schema_pos_to_name.get(
            schema_file_id_to_pos.get(mod.file_id, -1), "") or ""
        candidates: list[str] = []
        name_sources = (logical, schema_name) if (logical or schema_name) \
            else (mod.mod_name or "",)
        for raw in name_sources:
            if raw:
                for s in _suggest_mod_names(raw):
                    if s and s not in candidates:
                        candidates.append(s)
        return candidates

    # Remove staging folders for unticked optional mods
    if skipped_fids and skipped_mods and staging_path is not None:
        import shutil as _shutil_skip
        _removed_folders: list[str] = []
        for mod in skipped_mods:
            if not mod.file_id or mod.file_id not in skipped_fids:
                continue
            # Exact (mod_id, file_id) / file_id match is always safe.
            folder_name = _match_existing(mod)
            if not folder_name:
                # Name fallback: only for legacy installs with no id match. A
                # cleaned title ("HSMarkarth - The Warrens - AE" → "HSMarkarth -
                # The Warrens") can collide with a DIFFERENT mod's folder, so
                # never remove a folder that carries another mod's file_id -
                # removing an optional must never take out another mod.
                for candidate in _name_candidates(mod):
                    key = candidate.lower()
                    if key not in staging_lower_map:
                        continue
                    cand_folder = staging_lower_map[key]
                    _cand_fid = staging_folder_fid.get(cand_folder.lower(), 0)
                    if _cand_fid and _cand_fid != mod.file_id:
                        log(f"Collection install: NOT removing '{cand_folder}' as "
                            f"the unticked optional '{mod.mod_name}' "
                            f"(file_id={mod.file_id}) - folder belongs to "
                            f"file_id={_cand_fid}")
                        continue
                    folder_name = cand_folder
                    break
            if folder_name:
                skip_dir = staging_path / folder_name
                if skip_dir.is_dir():
                    log(f"Collection install: removing unticked optional mod "
                        f"'{folder_name}' (file_id={mod.file_id})")
                    try:
                        _shutil_skip.rmtree(skip_dir)
                        _removed_folders.append(folder_name)
                    except Exception as exc:
                        log(f"Collection install: failed to remove '{folder_name}': {exc}")
        if _removed_folders and modlist_path is not None and modlist_path.is_file():
            try:
                _removed_set = set(_removed_folders)
                entries = [e for e in read_modlist(modlist_path)
                           if e.name not in _removed_set]
                write_modlist(modlist_path, entries)
            except Exception:
                pass

    install_order: list[tuple[int, str]] = []
    to_download: list = []

    # Per-mod outcome tracker for the end-of-install verification summary. Maps
    # file_id -> {name, status, detail}. Every mod that should end up staged is
    # recorded so we can loudly report any that silently fell out of the pipeline
    # (the "N mods missing" bug). status ∈ existing/queued/installed/deferred/
    # download_failed/stage_empty/error/no_file_id.
    _mod_outcomes: "dict[int, dict]" = {}

    def _record_outcome(mod, status, detail=""):
        fid = getattr(mod, "file_id", 0) or 0
        _mod_outcomes[fid] = {"name": getattr(mod, "mod_name", "") or "",
                              "mod_id": getattr(mod, "mod_id", 0) or 0,
                              "status": status, "detail": detail}

    # Bundle-source entries ship inside the collection archive and have no Nexus
    # file ID - Step 2c installs + counts them (or, for local .amethyst imports,
    # the post-install bundle extraction does). Counting them "skipped" here
    # reported perfectly-installed bundled mods as skipped in the final summary.
    _schema_bundle_names = {
        (m.get("name") or "").strip().lower() for m in schema_mods
        if ((m.get("source") or {}).get("type") or "").lower() == "bundle"
        or (m.get("source") or {}).get("bundle") is True}

    # Classify: already-installed (skip) vs needs downloading
    for mod in ordered_mods:
        if not mod.file_id:
            if (getattr(mod, "source_type", "") == "bundle"
                    or (mod.mod_name or "").strip().lower() in _schema_bundle_names):
                _record_outcome(mod, "bundled")
                continue
            log(f"Collection install: skipping '{mod.mod_name}' - no file ID")
            _record_outcome(mod, "no_file_id")
            skipped += 1
            continue
        existing_folder: str = _match_existing(mod)
        if not existing_folder:
            for candidate in _name_candidates(mod):
                key = candidate.lower()
                if key in staging_lower_map:
                    existing_folder = staging_lower_map[key]
                    break
        if existing_folder:
            log(f"Collection install: '{mod.mod_name}' already installed as "
                f"'{existing_folder}' - skipping")
            _record_outcome(mod, "existing", existing_folder)
            if not skip_existing:
                install_order.append((_sort_key(mod), existing_folder))
            installed += 1
        else:
            _record_outcome(mod, "queued")
            to_download.append(mod)

    # Mods classified as already-present/skipped BEFORE the pipeline. Status
    # and progress always count the whole collection - "Downloaded X/546" -
    # never just the to-download subset (Tk parity: _pre_done + count / total).
    _pre_done = installed + skipped

    # ------------------------------------------------------------------
    # Step 2 pipeline: download + install concurrently (producer/consumer).
    # ------------------------------------------------------------------
    _col_cfg = load_collection_settings()
    _DL_WORKERS = _col_cfg["max_concurrent"]
    _INSTALL_WORKERS = _col_cfg.get("max_extract_workers", 4)
    # Archive-clear settings, read ONCE - _maybe_delete_archive used to re-parse
    # the settings INI per mod while holding _install_lock, serialising the
    # install consumers on file I/O for nothing (settings don't change mid-run).
    _col_force_clear_cfg = bool(_col_cfg.get("clear_archive_after_install", False))
    _clear_after_install_cfg = load_clear_archive_after_install()
    _keep_fomod_archives_cfg = load_keep_fomod_archives()

    def _scan_dirs(include_all: bool = False) -> "list[Path]":
        """Folders checked for an already-downloaded archive: game cache dirs,
        plus (when the premium check_download_locations setting allows it, or
        always in manual mode) the system Downloads dir + extra locations."""
        dirs: list[Path] = list(list_all_cache_dirs(getattr(game, "name", "") or ""))
        seen: set = {p.resolve() for p in dirs}
        if include_all or _col_cfg.get("check_download_locations", True):
            if not is_default_downloads_disabled():
                _sys_dl = _get_downloads_dir()
                if _sys_dl.resolve() not in seen and _sys_dl.is_dir():
                    dirs.append(_sys_dl)
                    seen.add(_sys_dl.resolve())
            for _xl in load_extra_download_locations():
                _xp = Path(_xl).expanduser().resolve()
                if _xp not in seen and Path(_xl).is_dir():
                    dirs.append(Path(_xl).expanduser())
                    seen.add(_xp)
        return dirs
    # Decouple downloads from installs: size the hand-off queue so all download
    # workers can deposit a finished archive without blocking even when every
    # install worker is busy extracting. Downloaded archives live on disk; queue
    # items are cheap (mod, result) tuples, and _mem_budget still caps concurrent
    # extraction - so a generous queue lets the 8 download slots stay saturated
    # (matching Tk's observed behaviour) instead of stalling in bursts. Was
    # max(_INSTALL_WORKERS + 1, 5), which blocked producers once installs (slow
    # per-archive 7z spawns for many tiny mods) fell behind.
    _PIPELINE_QUEUE_SIZE = max(_DL_WORKERS + _INSTALL_WORKERS + 8, 32)
    _DONE_SENTINEL = None
    import os as _os_col
    _COL_TIMING = bool(_os_col.environ.get("MM_COL_TIMING"))
    # Wall-clock run start. _maybe_delete_archive only ever deletes archives
    # whose mtime is at/after this - i.e. fetched DURING this run. An archive
    # that predates the run was detected on disk (another manager's download
    # folder, an earlier manual download, a previous run's cache) and must
    # survive 'Clear archive after install' - the manager didn't download it.
    import time as _wall_time
    _run_started_ts = _wall_time.time()

    _dl_results: dict[int, tuple] = {}
    _dl_lock = threading.Lock()
    _dl_done = 0
    _dl_total = len(to_download)

    _to_download_fids = {getattr(m, "file_id", None) for m in to_download}
    _total_bytes = sum(getattr(m, "size_bytes", 0) or 0 for m in ordered_mods)
    # The real collection size (installed/uncompressed = totalSize + assetsSizeBytes,
    # from get_collection_detail) is what the detail header shows and what the user
    # expects to see. The download bar tracks compressed archive bytes (_total_bytes),
    # which is much smaller, so surface the true size separately for the label.
    if collection_total_size > 0:
        cb.on_display_total(int(collection_total_size))
    _dl_bytes_done = sum(
        getattr(m, "size_bytes", 0) or 0 for m in ordered_mods
        if getattr(m, "file_id", None) not in _to_download_fids)
    _per_mod_prev: dict[int, int] = {}

    # Aggregate-download speed state (replaces the Tk after()-timer poll).
    import time as _time_mod
    _agg_state = {"prev_bytes": 0, "prev_time": _time_mod.monotonic(), "speed": 0.0,
                  "last_emit": 0.0}
    # Progress-emit throttle: NexusDownloader calls progress_cb per read (~every
    # few KB). Emitting a Signal per chunk (×N concurrent downloads) floods the Qt
    # event loop and the X server's shared-memory backing store → the desktop can
    # freeze (xcb_shm_create_segment failures). Cap emissions to ~10/sec each, as
    # the Tk version did via a 200ms after()-timer poll.
    _EMIT_INTERVAL = 0.1
    _dl_last_emit: dict[int, float] = {}

    def _status_line(downloaded: int, done: int) -> str:
        """Overlay progress line; download_only has no install half."""
        if download_only:
            return f"Downloaded {downloaded}/{total}…"
        return f"Downloaded {downloaded}/{total}, installed {done}/{total}…"

    _col_cancel = ctl.cancel
    _col_pause = ctl.pause
    _col_stop = ctl.stop
    _dl_finished = threading.Event()

    _mem_budget = ExtractionMemoryBudget(max_workers=_INSTALL_WORKERS)
    _archive_use_count: dict[str, int] = {}
    _external_archive_paths: set[str] = set()

    _install_lock = threading.Lock()
    _install_counters = {"installed": 0, "skipped": 0, "done": 0, "downloaded": 0}
    _install_results: dict[int, str] = dict(already_installed_by_fid)
    _install_results.update(
        {fid: folder for (_mid, fid), folder in already_installed_by_ids.items()})
    _fomod_deferred: list = []
    _bain_deferred: list = []

    # Priority hand-off queue: when several downloaded archives are waiting, the
    # install consumers always take the SMALLEST first so one big archive can't
    # back up a pile of quick installs behind it. Items are
    # ``(priority, seq, payload)``; priority = archive size in bytes (smallest
    # first), seq is a monotonic tiebreaker so the payload tuples are never
    # compared. DONE sentinels use +inf priority so they sort AFTER all real
    # work - a consumer never exits while a smaller item is still queued.
    _install_queue: _queue.PriorityQueue = _queue.PriorityQueue(
        maxsize=_PIPELINE_QUEUE_SIZE)
    _iq_seq = _itertools.count()
    _iq_seq_lock = threading.Lock()

    def _iq_next_seq() -> int:
        with _iq_seq_lock:
            return next(_iq_seq)

    def _enqueue_install(mod, result, domain) -> None:
        """Put a downloaded (mod, result, domain) onto the priority install queue,
        keyed on archive size (smallest installs first)."""
        size = 0
        try:
            if result is not None and getattr(result, "file_path", None):
                size = Path(result.file_path).stat().st_size
        except OSError:
            size = 0
        if not size:
            size = getattr(mod, "size_bytes", 0) or 0
        _install_queue.put((size, _iq_next_seq(), (mod, result, domain)))

    def _enqueue_done() -> None:
        """Put a DONE sentinel that sorts after every real item (+inf priority)."""
        _install_queue.put((float("inf"), _iq_next_seq(), _DONE_SENTINEL))

    def _agg_push(force: bool = False):
        now = _time_mod.monotonic()
        # Throttle emissions to ~10/sec (speed is still averaged over 0.5s).
        if not force and now - _agg_state["last_emit"] < _EMIT_INTERVAL:
            return
        _agg_state["last_emit"] = now
        with _dl_lock:
            agg = _dl_bytes_done
            total = _total_bytes
        dt = now - _agg_state["prev_time"]
        if dt >= 0.5:
            _agg_state["speed"] = (agg - _agg_state["prev_bytes"]) / dt
            _agg_state["prev_bytes"] = agg
            _agg_state["prev_time"] = now
        cb.on_agg_download(agg, total, _agg_state["speed"] / (1024 * 1024))

    def _build_prebuilt_meta(mod, effective_domain):
        try:
            _effective_mod_id = schema_file_id_to_mod_id.get(mod.file_id, 0) or mod.mod_id
            pmeta = build_meta_from_download(
                game_domain=effective_domain, mod_id=_effective_mod_id,
                file_id=mod.file_id, archive_name=mod.file_name or "",
                from_collection=_slug)
            pmeta.nexus_name = mod.mod_name or ""
            pmeta.author = mod.mod_author or ""
            pmeta.version = mod.version or ""
            if getattr(mod, "category_id", 0):
                pmeta.category_id = mod.category_id
            if getattr(mod, "category_name", ""):
                pmeta.category_name = mod.category_name
            # Manifest category name (details.category) - the only source, as
            # the GraphQL mod list omits categories. Applied when the mod
            # object itself carries none.
            _schema_cat = schema_file_id_to_category.get(mod.file_id, "")
            if _schema_cat and not pmeta.category_name:
                pmeta.category_name = _schema_cat
            if schema_file_id_to_install_type.get(mod.file_id, "").lower() == "dinput":
                pmeta.root_folder = True
            pmeta.collection_optional = bool(getattr(mod, "optional", False))
            pmeta.collection_phase = schema_file_id_to_phase.get(mod.file_id, 0)
            return pmeta
        except Exception:
            return None

    def _preferred_name(mod):
        logical = schema_file_id_to_logical.get(mod.file_id, "") or ""
        schema_name = schema_pos_to_name.get(
            schema_file_id_to_pos.get(mod.file_id, -1), "") or ""
        pref = logical or schema_name or mod.mod_name or ""
        return pref + schema_file_id_to_suffix.get(mod.file_id, "")

    # ---- link prefetch (stage 1 of the pipeline) ----------------------
    def _cached_archive_for(mod, mod_domain):
        """Return a ready-to-use DownloadResult if this mod's archive is already
        in a scanned download folder, else None. Runs in the link-fetch stage so
        cached mods cost NO get_download_links call and no download slot."""
        _exp_size = (schema_file_id_to_size.get(mod.file_id, 0)
                     or getattr(mod, "size_bytes", 0) or 0)
        for _ext_dir in _scan_dirs():
            _ext_found, _ext_complete = _find_cached_archive(
                _ext_dir, mod.file_name or mod.mod_name or "",
                _exp_size, mod.mod_id, mod.file_id,
                expected_md5=(schema_file_id_to_md5.get(mod.file_id, "")
                              or (getattr(mod, "md5", "") or "").strip().lower()))
            if _ext_found and _ext_complete:
                log(f"Collection install: '{mod.mod_name}' found in {_ext_dir} - "
                    "using local copy, skipping download")
                with _install_lock:
                    _external_archive_paths.add(str(_ext_found))
                return DownloadResult(
                    success=True, file_path=_ext_found, file_name=_ext_found.name,
                    bytes_downloaded=_ext_found.stat().st_size, game_domain=mod_domain,
                    mod_id=mod.mod_id, file_id=mod.file_id)
        return None

    def _fetch_link_one(mod):
        """Stage 1: hand back either a cached-archive DownloadResult (no download
        needed) or the mod's signed CDN links, fetched AHEAD of the download so
        the download worker starts transferring with zero link-fetch latency.
        Returns a ``(kind, payload)`` tuple: ("cached", DownloadResult) or
        ("links", links|None). Never raises - a link-fetch failure yields
        ("links", None) and download_file re-fetches (surfacing the error)."""
        mod_domain = (getattr(mod, "domain_name", "") or "").strip() or game_domain
        if _col_stop.is_set():
            return ("links", None)
        cached = _cached_archive_for(mod, mod_domain)
        if cached is not None:
            return ("cached", cached)
        try:
            links = api.get_download_links(
                game_domain=mod_domain, mod_id=mod.mod_id, file_id=mod.file_id)
        except Exception as exc:
            log(f"Collection install: link prefetch failed for '{mod.mod_name}' "
                f"(mod_id={mod.mod_id}, file_id={mod.file_id}): {exc} - will "
                "retry the fetch inline")
            links = None
        return ("links", links)

    # ---- download producer (stage 2 of the pipeline) ------------------
    def _download_one(mod, prefetched=None):
        nonlocal _dl_done
        mod_domain = (getattr(mod, "domain_name", "") or "").strip() or game_domain
        # Expected archive size for cache validation / partial-download detection.
        # The GraphQL mod list omits size_bytes for cross-domain entries (e.g. a
        # Skyrim SE mod referenced by an imported/Enderal collection), so fall
        # back to the manifest's source.fileSize like the manual path does. Without
        # this, expected_size_bytes=0 disables the 95%-truncation check and a
        # partially-downloaded archive gets extracted (and fails) instead of being
        # redownloaded.
        _exp_size = (schema_file_id_to_size.get(mod.file_id, 0)
                     or getattr(mod, "size_bytes", 0) or 0)
        if _col_stop.is_set():
            with _dl_lock:
                _dl_done += 1
            _enqueue_install(mod, None, mod_domain)
            return

        def _progress_cb(cur, tot, _fid=mod.file_id, _mod=mod):
            nonlocal _dl_bytes_done, _total_bytes
            with _dl_lock:
                prev = _per_mod_prev.get(_fid, 0)
                delta = max(cur - prev, 0)
                _per_mod_prev[_fid] = cur
                _dl_bytes_done += delta
                is_first = prev == 0 and cur > 0
                # A mod's declared size is often unknown (0) or an estimate; the
                # real content-length (`tot`) or bytes seen so far may exceed it.
                # Grow the aggregate denominator so the download bar stays within
                # 0–100% instead of pegging early / overshooting.
                _declared = getattr(_mod, "size_bytes", 0) or 0
                _real = max(tot if tot and tot > 0 else 0, cur)
                if _real > _declared:
                    _total_bytes += _real - _declared
                    _mod.size_bytes = _real
            if is_first:
                cb.on_dl_mod_start(_fid, _mod.mod_name or _mod.file_name or "",
                                   getattr(_mod, "size_bytes", 0) or 0)
            # Throttle per-mod bar updates to ~10/sec per file (always emit the
            # final 100% so the bar never sticks short).
            _now = _time_mod.monotonic()
            _complete = tot > 0 and cur >= tot
            if is_first or _complete or _now - _dl_last_emit.get(_fid, 0.0) >= _EMIT_INTERVAL:
                _dl_last_emit[_fid] = _now
                cb.on_dl_mod_update(_fid, cur, tot)
            _agg_push(force=is_first)

        result = None
        effective_domain = mod_domain

        # Stage 1 (_fetch_link_one) already handled the cached-archive scan and
        # link prefetch. A ("cached", result) payload means the archive is on
        # disk - skip the download entirely; ("links", links) feeds the signed
        # CDN links straight into download_file so it doesn't re-fetch. A bare
        # None (e.g. non-pipelined caller) falls back to download_file's own
        # cache check + link fetch.
        _pref_links = None
        if prefetched is not None:
            _kind, _payload = prefetched
            if _kind == "cached":
                result = _payload
            else:
                _pref_links = _payload

        try:
            if result is None:
                result = downloader.download_file(
                    game_domain=mod_domain, mod_id=mod.mod_id, file_id=mod.file_id,
                    progress_cb=_progress_cb, cancel=_col_stop,
                    known_file_name=mod.file_name or "",
                    expected_size_bytes=_exp_size,
                    prefetched_links=_pref_links,
                    dest_dir=get_download_cache_dir_for_game(getattr(game, "name", "") or ""))
        except Exception as exc:
            import traceback as _tb
            log(f"Collection install: download exception for '{mod.mod_name}' "
                f"(mod_id={mod.mod_id}, file_id={mod.file_id}): {exc}\n{_tb.format_exc()}")

        # From here on the counters and the install-queue handoff MUST fire
        # exactly once per mod no matter what the UI callbacks do - an escaped
        # exception would kill this download worker and wedge the whole
        # pipeline (fetchers block on the bounded ready queue, run_pipelined
        # never returns, Cancel can't unblock it).
        mod_size = getattr(mod, "size_bytes", 0) or 0
        try:
            if mod_size > 0 and _per_mod_prev.get(mod.file_id, 0) == 0:
                _progress_cb(mod_size, mod_size)
        except Exception as exc:
            log(f"Collection install: progress callback failed for "
                f"'{mod.mod_name}': {exc}")

        with _dl_lock:
            _dl_done += 1
            _dl_results[mod.file_id] = (result, effective_domain)
            done = _dl_done
        with _install_lock:
            if result and result.success and result.file_path:
                _akey = str(result.file_path)
                _archive_use_count[_akey] = _archive_use_count.get(_akey, 0) + 1
            _inst_done = _install_counters["done"]
        try:
            _set_status(_status_line(_pre_done + done, _pre_done + _inst_done))
            cb.on_dl_mod_finish(mod.file_id)
            # No extract queue in download_only - nothing is ever extracted.
            if result and result.success and result.file_path and not download_only:
                cb.on_extract_queue(mod.file_id,
                                    mod.mod_name or mod.file_name or "")
        except Exception as exc:
            log(f"Collection install: finish callback failed for "
                f"'{mod.mod_name}': {exc}")
        # The install queue is bounded; if it ever fills (installs falling far
        # behind downloads) this put() blocks the download worker so it can't
        # start the next download. The queue is now sized generously so that
        # shouldn't happen, but MM_COL_TIMING=1 logs any block >0.05s to confirm.
        if _COL_TIMING:
            _t_put = _time_mod.monotonic()
            _enqueue_install(mod, result, effective_domain)
            _blocked = _time_mod.monotonic() - _t_put
            if _blocked > 0.05:
                log(f"[timing] download worker blocked {_blocked:.2f}s on install "
                    f"queue for '{mod.mod_name}' (queue full - install is the "
                    f"bottleneck)")
        else:
            _enqueue_install(mod, result, effective_domain)

    # ---- install consumer --------------------------------------------
    def _install_one(mod, result, effective_domain):
        if _col_stop.is_set():
            with _install_lock:
                _install_counters["skipped"] += 1
                _install_counters["done"] += 1
            cb.on_extract_remove(mod.file_id)
            return
        if result is None or not result.success or not result.file_path:
            if result is None:
                _reason = "no result (exception during download)"
            elif not result.success:
                _reason = (result.error or "unknown error").strip() or "unknown error"
                if not result.file_path:
                    _reason += " (no file_path)"
            else:
                _reason = "success but no file_path"
            log(f"Collection install: download failed for '{mod.mod_name}' "
                f"(mod_id={mod.mod_id}, file_id={mod.file_id}): {_reason}")
            with _install_lock:
                _record_outcome(mod, "download_failed", _reason)
                _install_counters["skipped"] += 1
                _install_counters["done"] += 1
            cb.on_extract_remove(mod.file_id)
            return

        if download_only:
            # The cached archive IS the result. Returning here also skips
            # _maybe_delete_archive, so 'Clear archive after install' can never
            # delete what we just fetched.
            with _install_lock:
                _record_outcome(mod, "downloaded", Path(result.file_path).name)
                _install_counters["downloaded"] += 1
                _install_counters["done"] += 1
                done_so_far = _install_counters["done"]
            cb.on_extract_remove(mod.file_id)
            with _dl_lock:
                dl_done_now = _dl_done
            _set_status(_status_line(_pre_done + dl_done_now, _pre_done + done_so_far))
            _set_progress((_pre_done + done_so_far) / total if total else 1.0)
            return

        archive_path = str(result.file_path)
        auto_fomod = fomod_by_file_id.get(mod.file_id)
        auto_bain = bain_by_file_id.get(mod.file_id)
        _pmeta = _build_prebuilt_meta(mod, effective_domain)
        _preferred = _preferred_name(mod)

        _extract_est = get_uncompressed_size(archive_path)
        _mem_budget.acquire(_extract_est)
        _fomod_flag = {"value": False}

        def _capture_fomod(is_fomod=False):
            _fomod_flag["value"] = is_fomod

        cb.on_extract_add(mod.file_id, _preferred or (mod.mod_name or mod.file_name or ""))
        try:
            folder_name = install_collection_archive(
                archive_path, game, profile_dir, log_fn=log,
                progress_fn=lambda d, t, p=None, _f=mod.file_id:
                    cb.on_extract_update(_f, int(d), int(t)),
                fomod_auto_selections=auto_fomod, bain_auto_selections=auto_bain,
                prebuilt_meta=_pmeta, preferred_name=_preferred,
                skip_index_update=True, overwrite_existing=overwrite_existing,
                defer_interactive_fomod=(auto_fomod is None),
                defer_interactive_bain=(auto_bain is None),
                resolve_fomod=cb.resolve_fomod, resolve_bain=cb.resolve_bain,
                on_installed=_capture_fomod, cancel=_col_stop)
        finally:
            _mem_budget.release(_extract_est)
            cb.on_extract_remove(mod.file_id)
        _installed_was_fomod = _fomod_flag["value"]

        if folder_name == FOMOD_DEFERRED:
            with _install_lock:
                _fomod_deferred.append((mod, result, effective_domain))
                _record_outcome(mod, "deferred", "fomod")
                _install_counters["done"] += 1
            return
        if folder_name == BAIN_DEFERRED:
            with _install_lock:
                _bain_deferred.append((mod, result, effective_domain))
                _record_outcome(mod, "deferred", "bain")
                _install_counters["done"] += 1
            return

        # Paused/cancelled mid-extraction: the temp files are already removed by
        # prepare_archive; KEEP the downloaded archive so resume can reuse it, and
        # skip the "produced NO staged files - dropped" warning (that's for a real
        # structural failure, not a user pause).
        if not folder_name and _col_stop.is_set():
            with _install_lock:
                _record_outcome(mod, "cancelled", "paused mid-extraction")
                _install_counters["skipped"] += 1
                _install_counters["done"] += 1
            return

        with _install_lock:
            if folder_name:
                _install_results[mod.file_id] = folder_name
                _record_outcome(mod, "installed", folder_name)
                _install_counters["installed"] += 1
            else:
                # install_collection_archive returned falsy - the mod extracted
                # but nothing was staged (structure not recognised / all files
                # filtered out). This is the silent-drop path behind "N mods
                # missing"; log it prominently for the end-of-install summary.
                log(f"Collection install: '{mod.mod_name}' produced NO staged "
                    f"files (mod_id={mod.mod_id}, file_id={mod.file_id}, "
                    f"archive={Path(archive_path).name}) - dropped.")
                _record_outcome(mod, "stage_empty",
                                f"archive={Path(archive_path).name}")
                _install_counters["skipped"] += 1
            _install_counters["done"] += 1
            done_so_far = _install_counters["done"]
            _maybe_delete_archive(archive_path, _installed_was_fomod)

        with _dl_lock:
            dl_done_now = _dl_done
        _set_status(_status_line(_pre_done + dl_done_now, _pre_done + done_so_far))
        _set_progress((_pre_done + done_so_far) / total if total else 1.0)
        if mod.file_id and folder_name:
            cb.on_row_installed(mod.file_id)

    def _maybe_delete_archive(archive_path: str, was_fomod: bool) -> None:
        """Decrement archive use-count; delete at zero honoring settings, but
        never an archive that predates the run. Caller must hold _install_lock."""
        if archive_path not in _archive_use_count:
            return
        _archive_use_count[archive_path] -= 1
        _keep_for_fomod = (not _col_force_clear_cfg and was_fomod
                           and _keep_fomod_archives_cfg)
        _should_clear = _col_force_clear_cfg or (
            _clear_after_install_cfg and not _keep_for_fomod)
        if manual_mode:
            # Tk manual parity: always delete after a successful install unless
            # it was a FOMOD and the user keeps FOMOD archives (the user just
            # downloaded it by hand - leaving it behind clutters ~/Downloads).
            _should_clear = not (was_fomod and _keep_fomod_archives_cfg)
        if not (_archive_use_count[archive_path] == 0 and _should_clear
                and archive_path not in _external_archive_paths):
            return
        try:
            _pre_existing = Path(archive_path).stat().st_mtime < _run_started_ts
        except OSError:
            _pre_existing = True  # can't prove we downloaded it - keep it
        if _pre_existing:
            log(f"Collection install: kept pre-existing archive "
                f"'{Path(archive_path).name}' (not downloaded by this run)")
            return
        try:
            delete_archive_and_sidecar(Path(archive_path))
        except Exception as _del_exc:
            log(f"Collection install: could not remove archive '{archive_path}': {_del_exc}")

    def _install_consumer():
        while True:
            _prio, _seq, payload = _install_queue.get()
            if payload is _DONE_SENTINEL:
                _install_queue.task_done()
                break
            mod, result, effective_domain = payload
            try:
                _install_one(mod, result, effective_domain)
            except Exception as exc:
                import traceback as _tbx
                log(f"Collection install: unexpected error installing "
                    f"'{mod.mod_name}' (mod_id={getattr(mod,'mod_id',0)}, "
                    f"file_id={getattr(mod,'file_id',0)}): {exc}\n{_tbx.format_exc()}")
                with _install_lock:
                    _record_outcome(mod, "error", str(exc))
                    _install_counters["skipped"] += 1
                    _install_counters["done"] += 1
            finally:
                _install_queue.task_done()

    def _write_preliminary_plugins_txt(label: str) -> None:
        try:
            import os as _os
            _plugin_exts = (".esm", ".esl", ".esp")
            _pre_plugins: list = []
            _seen_plugins: set = set()
            _pre_staging = game.get_effective_mod_staging_path()
            with _install_lock:
                _pre_results = dict(_install_results)
            for _fid, _fname in _pre_results.items():
                _mod_dir = _pre_staging / _fname
                if not _mod_dir.is_dir():
                    continue
                for _root, _dirs, _files in _os.walk(str(_mod_dir)):
                    for _fn in _files:
                        if _fn.lower().endswith(_plugin_exts):
                            _pname_low = _fn.lower()
                            if _pname_low not in _seen_plugins:
                                _seen_plugins.add(_pname_low)
                                _pre_plugins.append(PluginEntry(name=_fn, enabled=True))
            if _pre_plugins:
                _star_pre = getattr(game, "plugins_use_star_prefix", True)
                write_plugins(profile_dir / "plugins.txt", _pre_plugins, star_prefix=_star_pre)
                write_loadorder(profile_dir / "loadorder.txt", _pre_plugins)
                log(f"Collection install: wrote preliminary plugins.txt "
                    f"({len(_pre_plugins)} plugin(s)) - {label}.")
        except Exception as _pre_exc:
            log(f"Collection install: preliminary plugins.txt skipped - {_pre_exc}")

    # ---- manual (non-premium) producer --------------------------------
    # Port of Tk _run_manual_install's sequential prompt+poll loop; the
    # install side is the shared consumer pipeline above.
    def _manual_domain(mod) -> str:
        return ((getattr(mod, "domain_name", "") or "").strip()
                or schema_file_id_to_domain.get(mod.file_id, "")
                or game_domain)

    def _manual_url(mod) -> str:
        # Prefer collection.json's source.modId + domainName for cross-domain
        # entries so "Open Download Page" lands on the mod's real Nexus page.
        _mid = schema_file_id_to_mod_id.get(mod.file_id, 0) or mod.mod_id
        return (f"https://www.nexusmods.com/{_manual_domain(mod)}/mods/{_mid}"
                f"?tab=files&file_id={mod.file_id}")

    # file_id → (real archive filename, size_bytes) from the Nexus files API.
    # The manifest's file_name/logicalFilename is display-quality only (a
    # share-code export writes the staging FOLDER name there), so it neither
    # shows the user the archive they're about to download nor reliably
    # matches it on disk. Best-effort: ("", 0) when offline/unresolvable.
    _manual_real_file: dict[int, tuple[str, int]] = {}

    def _resolve_manual_file(mod) -> "tuple[str, int]":
        cached = _manual_real_file.get(mod.file_id)
        if cached is not None:
            return cached
        real_name, real_size = "", 0
        _mid = schema_file_id_to_mod_id.get(mod.file_id, 0) or mod.mod_id
        try:
            if api is not None and _mid and mod.file_id:
                files = api.get_mod_files(_manual_domain(mod), _mid)
                for f in files.files:
                    if f.file_id == mod.file_id:
                        fn = (f.file_name or "").strip()
                        if fn and "/" not in fn:
                            real_name = fn
                        real_size = int(f.size_in_bytes
                                        or (f.size_kb or 0) * 1024 or 0)
                        break
        except Exception as exc:
            log(f"Manual install: file lookup failed for mod {_mid} "
                f"file {mod.file_id} - {exc}")
        _manual_real_file[mod.file_id] = (real_name, real_size)
        return real_name, real_size

    def _wait_for_manual_file(mod) -> "Path | None":
        """Poll download folders until the mod's archive appears, or the user
        picks a file / skips / pauses / cancels."""
        scan_dirs = _scan_dirs(include_all=True)
        _eff_mod_id = schema_file_id_to_mod_id.get(mod.file_id, 0) or mod.mod_id
        _real_name, _real_size = _resolve_manual_file(mod)
        _exp_size = (schema_file_id_to_size.get(mod.file_id, 0)
                     or getattr(mod, "size_bytes", 0) or 0
                     or _real_size)
        _exp_md5 = (schema_file_id_to_md5.get(mod.file_id, "")
                    or (getattr(mod, "md5", "") or "").strip().lower())
        # Match on the real upload's display stem when known - the manifest
        # name may be a mod-page or staging-folder label that shares no stem
        # with the archive the browser actually saves.
        _match_name = (_clean_nexus_stem(
                           Path(_real_name).stem,
                           str(_eff_mod_id) if _eff_mod_id else "")
                       if _real_name else (mod.file_name or mod.mod_name or ""))
        while not _col_stop.is_set():
            try:
                item = ctl.manual_queue.get_nowait()
                if item is None:
                    return None  # skip
                p = Path(item)
                if p.is_file():
                    return p
            except _queue.Empty:
                pass
            for folder in scan_dirs:
                if not folder.is_dir():
                    continue
                found, is_complete = _find_cached_archive(
                    folder, _match_name,
                    _exp_size, _eff_mod_id, mod.file_id,
                    expected_md5=_exp_md5)
                if found and is_complete:
                    return found
            _col_stop.wait(2.0)
        return None  # paused / cancelled

    def _manual_produce(mods_seq: list) -> None:
        nonlocal _dl_done
        _current_phase: "int | None" = None
        for i, mod in enumerate(mods_seq):
            mod_domain = _manual_domain(mod)
            if _col_stop.is_set():
                with _dl_lock:
                    _dl_done += 1
                _enqueue_install(mod, None, mod_domain)
                continue

            _this_phase = schema_file_id_to_phase.get(mod.file_id, 0)
            if (_current_phase is not None and _this_phase != _current_phase
                    and not download_only):
                # All earlier-phase installs must land before a later-phase
                # FOMOD reads plugins.txt (Tk _write_phase_plugins_txt parity).
                _install_queue.join()
                _write_preliminary_plugins_txt(
                    f"phase {_current_phase} → {_this_phase}")
            _current_phase = _this_phase

            _real_name, _real_size = _resolve_manual_file(mod)
            cb.on_manual_mod({
                "idx": _pre_done + i + 1,
                "total": total,
                "n_manual": len(mods_seq),
                "installed_base": installed,
                "name": mod.mod_name or f"Mod {mod.mod_id}",
                "size": (schema_file_id_to_size.get(mod.file_id, 0)
                         or getattr(mod, "size_bytes", 0) or 0
                         or _real_size),
                "file_name": _real_name or mod.file_name or "",
                "optional": bool(getattr(mod, "optional", False)),
                "url": _manual_url(mod),
                "upcoming": [(m.mod_name or f"Mod {m.mod_id}", _manual_url(m))
                             for m in mods_seq[i + 1:i + 5]],
            })

            archive = _wait_for_manual_file(mod)
            if _col_stop.is_set():
                with _dl_lock:
                    _dl_done += 1
                _enqueue_install(mod, None, mod_domain)
                continue
            if archive is None:
                log(f"Manual install: skipped '{mod.mod_name}'")
                with _install_lock:
                    _record_outcome(mod, "skipped_manual")
                    _install_counters["skipped"] += 1
                    _install_counters["done"] += 1
                with _dl_lock:
                    _dl_done += 1
                continue

            result = DownloadResult(
                success=True, file_path=archive, file_name=archive.name,
                bytes_downloaded=archive.stat().st_size,
                game_domain=mod_domain, mod_id=mod.mod_id, file_id=mod.file_id)
            with _dl_lock:
                _dl_done += 1
            with _install_lock:
                # Counted but NOT marked external → deleted after install IF
                # it appeared during this run (fresh hand-download; Tk manual
                # parity - declutters ~/Downloads). Archives that predate the
                # run are kept by _maybe_delete_archive's mtime guard.
                _akey = str(archive)
                _archive_use_count[_akey] = _archive_use_count.get(_akey, 0) + 1
            if not download_only:
                cb.on_extract_queue(mod.file_id, mod.mod_name or mod.file_name or "")
            _enqueue_install(mod, result, mod_domain)

    # ---- launch pipeline ---------------------------------------------
    if to_download:
        if manual_mode:
            _set_status(f"Waiting for manual downloads - {_dl_total} mod(s)…")
        else:
            _set_status(f"Downloading {_dl_total} mod(s)…" if download_only
                        else f"Downloading & installing {_dl_total} mod(s)…")
        _set_progress(_pre_done / total if total else 0.0)
        if not manual_mode:
            # Download strictly smallest→largest: all workers pull from the head
            # of the size-sorted list, so quick mods land first and the big
            # archives come last. Deliberate Qt change - Tk honoured the
            # `download_order` setting (default "largest" = largest-first); Qt
            # ignores that legacy key and always goes smallest-first. (Was a
            # double-ended scheduler that dedicated one worker to the
            # largest-remaining mods.)
            _to_download_sorted = order_by_size(to_download)
            if _total_bytes > 0:
                cb.on_agg_download(_dl_bytes_done, _total_bytes, 0.0)

        # Each download fetches its own signed CDN link lazily inside
        # download_file (exactly one get_download_links call per mod actually
        # downloaded - cached mods cost nothing).

        _consumer_threads: list[threading.Thread] = []
        for _ci in range(_INSTALL_WORKERS):
            t = threading.Thread(target=_install_consumer, daemon=True,
                                 name=f"col-install-{_ci}")
            t.start()
            _consumer_threads.append(t)

        if manual_mode:
            # Prompt order: phase first, then the author's mods-array order
            # within a phase - the order a human reads the collection page.
            to_download.sort(
                key=lambda m: (schema_file_id_to_phase.get(m.file_id, 0),
                               schema_file_id_to_arrayidx.get(
                                   m.file_id, len(schema_mods))))
            _manual_produce(to_download)
        else:
            # Two-stage pipeline: a link-fetch pool mints signed CDN links (and
            # does the cached-archive scan) AHEAD of the download workers so a
            # worker finishing a tiny archive finds the next link already waiting
            # and starts transferring with zero link-fetch latency. This keeps
            # all _DL_WORKERS slots continuously saturated instead of stuttering
            # in bursts of _DL_WORKERS between synchronized get_download_links
            # round-trips. Same rate-limit cost (one link fetch per downloaded
            # mod); links are minted only ~1 step ahead so they never go stale.
            #
            # link_workers: for tiny archives the download finishes in ~100ms but
            # a get_download_links round-trip is ~150ms, so a single fetch stream
            # can't keep 8 download slots fed - throughput ends up capped by the
            # fetch rate (2 fetchers ≈ 13 links/sec observed). Match the fetch
            # pool to the download width so link fetches, not downloads, stop
            # being the bottleneck; Nexus premium rate limits (~2.5k/hr) leave
            # ample headroom (a whole collection is ~100 fetches).
            run_pipelined(_to_download_sorted, _fetch_link_one, _download_one,
                          _DL_WORKERS, link_workers=max(4, _DL_WORKERS),
                          stop=_col_stop)

        _dl_finished.set()
        if not manual_mode:
            cb.on_agg_download(_total_bytes, _total_bytes, 0.0)
        for _ in range(_INSTALL_WORKERS):
            _enqueue_done()
        for t in _consumer_threads:
            t.join()

        # download_only extracts nothing, so nothing can be deferred.
        if not download_only:
            _process_deferred(
                _bain_deferred, _fomod_deferred, game, profile_dir, api,
                schema_mods, schema_file_id_to_phase, schema_file_id_to_pos,
                schema_file_id_to_mod_id, schema_file_id_to_install_type,
                schema_file_id_to_category,
                schema_file_id_to_logical, schema_pos_to_name, schema_file_id_to_suffix,
                fomod_by_file_id, bain_by_file_id, _install_results,
                _install_counters, _install_lock, _archive_use_count,
                _external_archive_paths, _col_stop, _slug, overwrite_existing,
                _write_preliminary_plugins_txt, _maybe_delete_archive, cb, log, _set_status)

    if download_only:
        # Everything below (mod index, bundled assets, modlist/plugins/filemap,
        # reconciliation, separators, the staged-files verification) writes to a
        # profile this run never created.
        _downloaded = _install_counters["downloaded"]
        skipped += _install_counters["skipped"]     # failed/stopped downloads
        log(f"Collection download: {_downloaded} archive(s) downloaded, "
            f"{skipped} skipped, {total} total - nothing installed "
            f"(Download only).")
        if _col_cancel.is_set():
            cb.on_cancelled(None)
            return
        # Pause can't be resumed here (a resume point needs a profile), so report
        # it as stopped-early rather than letting it read as a complete run.
        _set_status(f"Stopped - downloaded {_downloaded}/{total}."
                    if _col_pause.is_set()
                    else f"Downloaded {_downloaded}/{total} - nothing installed.")
        cb.on_done(_downloaded, skipped, total, "")
        return

    installed += _install_counters["installed"]
    skipped += _install_counters["skipped"]

    # rebuild mod index once for all newly installed mods.
    # NB: use the *canonical* game attrs (mod_folder_strip_prefixes /
    # mod_install_extensions) + per-mod strip prefixes + root-flag set - the
    # same params deploy_pipeline's rescan/build_filemap uses. Reading the
    # non-existent strip_prefixes / install_extensions attrs would return None
    # → an UNSTRIPPED index (Bethesda appends index as "Data/…"), so appended
    # mods deploy double-nested / with wrong conflicts until a manual Refresh.
    #
    # The index MUST land where build_filemap / the conflict rebuild reads it:
    # next to the EFFECTIVE filemap (get_effective_filemap_path().parent), NOT
    # profile_dir. For a normal (shared-mods) append target those two differ -
    # the shared game root vs <profile_dir> - so writing to profile_dir left the
    # appended mods invisible to the reload (no root flags, no conflicts, no
    # plugins) until Refresh. Only profile-specific-mods profiles (fresh
    # collection installs) coincide, which masked the bug. Mirrors
    # mod_install._update_indexes.
    if _install_counters["installed"] > 0:
        try:
            log("Updating mod index…")
            from Utils.filemap import rebuild_mod_index
            from Utils.deploy import load_per_mod_strip_prefixes
            from Nexus.nexus_meta import collect_root_flagged_mods
            _staging = game.get_effective_mod_staging_path()
            try:
                _index_dir = game.get_effective_filemap_path().parent
            except Exception:
                _index_dir = profile_dir
            try:
                _rf_mods = collect_root_flagged_mods(modlist_path, _staging, log_fn=log)
            except Exception:
                _rf_mods = set()
            rebuild_mod_index(
                _index_dir / "modindex.bin", _staging,
                strip_prefixes=set(getattr(game, "mod_folder_strip_prefixes", None) or ()) or None,
                per_mod_strip_prefixes=load_per_mod_strip_prefixes(profile_dir),
                allowed_extensions=set(getattr(game, "mod_install_extensions", None) or ()) or None,
                root_folder_mods=set(_rf_mods or ()) or None,
                normalize_folder_case=getattr(game, "normalize_folder_case", True))
        except Exception as _idx_exc:
            log(f"Mod index rebuild skipped: {_idx_exc}")

    # build install_order from parallel results
    for mod in to_download:
        sort_key = _sort_key(mod)
        folder = (_install_results.get(mod.file_id)
                  or schema_pos_to_name.get(sort_key) or mod.mod_name)
        if mod.file_id in _install_results:
            install_order.append((sort_key, folder))

    # Step 2c: bundled assets from the collection archive
    _bundled_folders: list[str] = []
    if with_bundled:
        try:
            _n_bundled, _n_bundle_skipped, _b_names = _install_bundled_assets(
                game, api, profile_dir, staging_path, collection_schema,
                schema_mods, download_link_path, revision_number,
                collection_slug, staging_lower_map, install_order, log, _set_status)
            installed += _n_bundled
            skipped += _n_bundle_skipped
            _bundled_folders.extend(_b_names)
        except Exception as exc:
            log(f"Collection install: error processing bundled assets: {exc}")

    # Step 3: write modlist.txt.
    #   * update_context set → order-preserving update reconcile. Runs HERE,
    #     BEFORE Step 3b (Tk parity) - Step 3b prepends bundled folders straight
    #     into modlist.txt, so reconciling after it would wipe any bundled
    #     folder that has no schema entry (not in the snapshot or install_order).
    #   * new-profile path → fresh write.
    #   * append path → append reconcile.
    if update_context is not None and not _col_pause.is_set():
        try:
            install_order.sort(key=lambda x: x[0])
            if modlist_path.is_file():
                _reconcile_update_modlist(modlist_path, install_order,
                                          update_context, log)
        except Exception as exc:
            log(f"Collection update: reconcile modlist failed: {exc}")
    elif overwrite_existing is None and not _col_pause.is_set():
        _write_new_profile_modlist(profile_dir, modlist_path, install_order, log)
    elif _is_append_run and not _col_pause.is_set():
        install_order.sort(key=lambda x: x[0])
        _append_reconcile_modlist(modlist_path, install_order, _append_pre_existing, log)

    # Step 3b: bundled folders + binary patches + INI tweaks (before LOOT).
    _amethyst_state = None
    if with_bundled and not _col_pause.is_set() and overwrite_existing is None:
        try:
            _step3b_bundled, _amethyst_state = _run_step3b(
                game, api, profile_dir, staging_path,
                collection_schema, download_link_path,
                collection_slug, revision_number,
                _install_results, log,
                local_bundle_zip=local_bundle_zip)
            _bundled_folders.extend(_step3b_bundled or [])
        except Exception as exc:
            log(f"Collection install: Step 3b failed: {exc}")
    if _amethyst_state and not _col_pause.is_set():
        try:
            _persist_amethyst_stash(profile_dir, _amethyst_state, log)
        except Exception as exc:
            log(f"Collection install: could not save Amethyst snapshot: {exc}")

    # Bundled folders are copied straight into staging by Steps 2c/3b, which run
    # AFTER the index rebuild above - so they have no modindex.bin entry, and
    # build_filemap deploys NOTHING for a mod it can't find in the index (it
    # warns "has NO index entry"). The mod is staged and in modlist.txt, so it
    # looks installed while contributing no files: bundled DynDOLOD/Pandora
    # output silently loses to the animation mods it is supposed to overwrite,
    # and the game reports missing behaviours. Index them here rather than
    # rebuilding the whole staging tree again - this is the same subset rescan
    # the root-flag toggle uses.
    def _rescan_staged_subset(mod_names, what):
        from Utils.filemap import rescan_mods_in_index
        from Utils.deploy import load_per_mod_strip_prefixes
        from Nexus.nexus_meta import collect_root_flagged_mods
        _staging = game.get_effective_mod_staging_path()
        try:
            _index_dir = game.get_effective_filemap_path().parent
        except Exception:
            _index_dir = profile_dir
        try:
            _rf_mods = collect_root_flagged_mods(modlist_path, _staging,
                                                 log_fn=log)
        except Exception:
            _rf_mods = set()
        _uniq = list(dict.fromkeys(mod_names))
        rescan_mods_in_index(
            _index_dir / "modindex.bin", _staging, _uniq,
            strip_prefixes=set(getattr(game, "mod_folder_strip_prefixes", None) or ()) or None,
            per_mod_strip_prefixes=load_per_mod_strip_prefixes(profile_dir),
            allowed_extensions=set(getattr(game, "mod_install_extensions", None) or ()) or None,
            root_folder_mods=set(_rf_mods or ()) or None,
            normalize_folder_case=getattr(game, "normalize_folder_case", True),
            log_fn=log)
        log(f"Collection install: indexed {len(_uniq)} {what}: "
            f"{', '.join(_uniq[:5])}" + (" …" if len(_uniq) > 5 else ""))

    if _bundled_folders and not _col_pause.is_set():
        try:
            _rescan_staged_subset(_bundled_folders,
                                  "bundled folder(s) so they deploy")
        except Exception as exc:
            log(f"Collection install: could not index bundled folders ({exc}) "
                "- run Refresh if bundled content does not deploy")

    # Step 3c: build filemap.txt BEFORE the LOOT sort in Step 4.
    #   LOOT resolves each plugin to the copy of its *winning* enabled mod via
    #   filemap.txt (LOOT/loot_sorter._read_filemap_winners) so it reads the
    #   correct header (masters/ESL flags) - the same file that would deploy.
    #   Without a fresh filemap it falls back to an arbitrary staging tree walk
    #   and can sort against the wrong copy, producing an order that differs
    #   from a post-deploy manual sort. The profile's active dir is already
    #   pointed at profile_dir (set at Step 0), so this builds for the right
    #   staging/modlist. New-profile/continue/update runs only (LOOT is gated
    #   on overwrite_existing is None in _write_collection_plugins).
    if (not _col_pause.is_set() and overwrite_existing is None
            and getattr(game, "loot_sort_enabled", False) and _loot_available()):
        try:
            from Utils.deploy_pipeline import _build_filemap_for_game
            _build_filemap_for_game(game, profile_dir.name, log_fn=log)
        except Exception as exc:
            log(f"Collection install: filemap rebuild before LOOT failed: {exc}")

    # Step 4: write plugins.txt / loadorder.txt from collection.json (or the
    # archive's exact exported order, which also skips the LOOT sort).
    if not _col_pause.is_set():
        _write_collection_plugins(
            game, profile_dir, plugins_path, collection_schema,
            overwrite_existing, _is_append_run, log, _set_status,
            amethyst_state=_amethyst_state)

    # Final reconciliation - new-profile path only. Update runs were already
    # reconciled at Step 3 (order-preserving), append runs by
    # _append_reconcile_modlist; re-sorting here would shove the user's
    # existing mods around (Tk parity: skipped for update + append).
    if (install_order and modlist_path.is_file() and not _col_pause.is_set()
            and overwrite_existing is None and update_context is None):
        try:
            _folder_to_key: dict[str, int] = {folder: key for key, folder in install_order}
            _existing = read_modlist(modlist_path)
            _known = [e for e in _existing if e.name in _folder_to_key]
            _unknown = [e for e in _existing if e.name not in _folder_to_key]
            for e in _known:
                e.enabled = True
            for e in _unknown:
                if not e.is_separator:
                    e.enabled = True
            _known.sort(key=lambda e: _folder_to_key[e.name])
            _reconciled = _known + _unknown
            write_modlist(modlist_path, _reconciled)
            log(f"Collection install: reconciled modlist.txt "
                f"({len(_known)} ordered, {len(_unknown)} trailing)")
        except Exception as exc:
            log(f"Collection install: reconcile modlist failed: {exc}")

    # Share-code extras - must run AFTER the reconcile passes above (the final
    # reconcile force-enables every entry and would shove freshly-inserted
    # separators, which have no install_order key, to the bottom).
    if modlist_path.is_file() and not _col_pause.is_set():
        try:
            _apply_schema_disabled_mods(
                modlist_path, collection_schema, schema_file_id_to_pos,
                install_order, log)
        except Exception as exc:
            log(f"Collection install: apply disabled states failed: {exc}")
        try:
            _apply_manifest_separators(
                profile_dir, modlist_path, collection_schema, log)
        except Exception as exc:
            log(f"Collection install: apply separators failed: {exc}")

    # Amethyst profile fidelity: an archive exported by our Create Collection
    # carries the source profile's exact modlist + portable state in Amethyst/
    # (read out of the extracted archive in Step 3b). Applied LAST - it is the
    # authored order, so it overrides the reconcile/rule-derived one - and only
    # on fresh installs: update/append runs must keep the user's own
    # arrangement (same philosophy as _reconcile_update_modlist).
    if (_amethyst_state and modlist_path.is_file() and not _col_pause.is_set()
            and overwrite_existing is None and update_context is None
            and not _is_append_run):
        try:
            _am_stats = _apply_amethyst_profile_state(
                profile_dir, modlist_path, _amethyst_state, log)
            _strip_changed = (_am_stats or {}).get("strip_changed") or []
            if _strip_changed:
                # Strip prefixes are baked into modindex.bin at rebuild time -
                # mods whose prefixes just changed need their entries redone or
                # the next deploy uses the un-stripped paths.
                try:
                    _rescan_staged_subset(
                        _strip_changed, "mod(s) with imported strip prefixes")
                except Exception as exc:
                    log(f"Collection install: strip-prefix rescan failed "
                        f"({exc}) - run Refresh before deploying")
        except Exception as exc:
            log(f"Collection install: Amethyst profile state failed: {exc}")

    # Restore the original profile dir
    try:
        game.set_active_profile_dir(old_profile_dir)
        game.load_paths()
    except Exception:
        pass

    # End-of-install verification: every non-optional manifest mod SHOULD have
    # ended up staged. Loudly report any that didn't (the "N mods missing" bug),
    # with the recorded reason per mod, so a failure is visible + diagnosable
    # instead of silently swallowed. Only meaningful on a clean finish (a paused
    # / cancelled run legitimately leaves mods un-installed).
    if not _col_cancel.is_set() and not _col_pause.is_set():
        try:
            _final_staging = game.get_effective_mod_staging_path()
            _missing: list = []
            for mod in ordered_mods:
                fid = getattr(mod, "file_id", 0) or 0
                if not fid:
                    continue
                folder = _install_results.get(fid)
                staged_ok = bool(folder) and (_final_staging is not None
                                              and (Path(_final_staging) / folder).is_dir())
                if not staged_ok:
                    oc = _mod_outcomes.get(fid, {})
                    if oc.get("status") == "skipped_manual":
                        continue  # user chose to skip an optional mod
                    _missing.append((getattr(mod, "mod_name", "") or f"file {fid}",
                                     getattr(mod, "mod_id", 0) or 0, fid,
                                     oc.get("status", "unknown"),
                                     oc.get("detail", "")))
            if _missing:
                log(f"⚠ Collection install: {len(_missing)} mod(s) did NOT install "
                    f"and are missing from the profile:")
                for _nm, _mid, _fid, _st, _dt in _missing:
                    log(f"    • {_nm} (mod_id={_mid}, file_id={_fid}) - "
                        f"{_st}{(': ' + _dt) if _dt else ''}")
                _set_status(f"Done, but {len(_missing)} mod(s) failed to install "
                            "- see log.")
        except Exception as _ver_exc:
            log(f"Collection install: verification summary failed: {_ver_exc}")

    # Terminal handling
    if _col_cancel.is_set():
        cb.on_cancelled(profile_dir)
        return
    if _col_pause.is_set():
        try:
            from Utils.profile_state import write_collection_install_paused
            write_collection_install_paused(profile_dir, True)
        except Exception:
            pass
        cb.on_paused(installed, str(profile_dir.name))
        return

    cb.on_done(installed, skipped, total, str(profile_dir.name))


# ---------------------------------------------------------------------------
# Deferred BAIN/FOMOD (extracted from _run_install 3508-3735 for readability).
# ---------------------------------------------------------------------------
def _process_deferred(
        _bain_deferred, _fomod_deferred, game, profile_dir, api,
        schema_mods, schema_file_id_to_phase, schema_file_id_to_pos,
        schema_file_id_to_mod_id, schema_file_id_to_install_type,
        schema_file_id_to_category,
        schema_file_id_to_logical, schema_pos_to_name, schema_file_id_to_suffix,
        fomod_by_file_id, bain_by_file_id, _install_results,
        _install_counters, _install_lock, _archive_use_count,
        _external_archive_paths, _col_stop, _slug, overwrite_existing,
        _write_preliminary_plugins_txt, _maybe_delete_archive, cb, log, _set_status):
    from Nexus.nexus_meta import build_meta_from_download

    def _mk_meta_and_name(mod, domain):
        try:
            _mid = schema_file_id_to_mod_id.get(mod.file_id, 0) or mod.mod_id
            pmeta = build_meta_from_download(
                game_domain=domain, mod_id=_mid, file_id=mod.file_id,
                archive_name=mod.file_name or "", from_collection=_slug)
            pmeta.nexus_name = mod.mod_name or ""
            pmeta.author = mod.mod_author or ""
            pmeta.version = mod.version or ""
            if getattr(mod, "category_id", 0):
                pmeta.category_id = mod.category_id
            if getattr(mod, "category_name", ""):
                pmeta.category_name = mod.category_name
            _schema_cat = schema_file_id_to_category.get(mod.file_id, "")
            if _schema_cat and not pmeta.category_name:
                pmeta.category_name = _schema_cat
            if schema_file_id_to_install_type.get(mod.file_id, "").lower() == "dinput":
                pmeta.root_folder = True
            pmeta.collection_optional = bool(getattr(mod, "optional", False))
            pmeta.collection_phase = schema_file_id_to_phase.get(mod.file_id, 0)
        except Exception:
            pmeta = None
        logical = schema_file_id_to_logical.get(mod.file_id, "") or ""
        schema_name = schema_pos_to_name.get(
            schema_file_id_to_pos.get(mod.file_id, -1), "") or ""
        pref = (logical or schema_name or mod.mod_name or "") \
            + schema_file_id_to_suffix.get(mod.file_id, "")
        return pmeta, pref

    def _record(mod, folder):
        with _install_lock:
            if folder:
                _install_results[mod.file_id] = folder
                _install_counters["installed"] += 1
            else:
                log(f"Collection install: deferred mod '{mod.mod_name}' produced "
                    f"NO staged files (mod_id={getattr(mod,'mod_id',0)}, "
                    f"file_id={mod.file_id}) - dropped.")
                _install_counters["skipped"] += 1
        if folder and mod.file_id:
            cb.on_row_installed(mod.file_id)

    # Deferred BAIN first (before FOMODs).
    if _bain_deferred and not _col_stop.is_set():
        _bain_deferred.sort(key=lambda t: (
            schema_file_id_to_phase.get(t[0].file_id, 0),
            schema_file_id_to_pos.get(t[0].file_id, len(schema_mods))))
        log(f"Installing {len(_bain_deferred)} deferred BAIN mod(s)…")
        _set_status(f"Installing {len(_bain_deferred)} deferred BAIN mod(s)…")
        for _mod, _result, _domain in _bain_deferred:
            if _col_stop.is_set():
                break
            _archive = str(_result.file_path)
            _pmeta, _pref = _mk_meta_and_name(_mod, _domain)
            cb.on_extract_add(_mod.file_id, _pref or (_mod.mod_name or ""))
            try:
                _folder = install_collection_archive(
                    _archive, game, profile_dir, log_fn=log,
                    progress_fn=lambda d, t, p=None, _f=_mod.file_id:
                        cb.on_extract_update(_f, int(d), int(t)),
                    bain_auto_selections=bain_by_file_id.get(_mod.file_id),
                    prebuilt_meta=_pmeta, preferred_name=_pref,
                    skip_index_update=True, overwrite_existing=overwrite_existing,
                    resolve_bain=cb.resolve_bain, cancel=_col_stop)
            except Exception as _exc:
                log(f"Collection install: failed to install deferred BAIN "
                    f"'{_mod.mod_name}': {_exc}")
                _folder = None
            finally:
                cb.on_extract_remove(_mod.file_id)
            _record(_mod, _folder)
            with _install_lock:
                _maybe_delete_archive(_archive, True)

    # Deferred FOMODs - write prelim plugins.txt first, then per-phase.
    if _fomod_deferred and not _col_stop.is_set():
        _write_preliminary_plugins_txt("pre-FOMOD")
        _fomod_deferred.sort(key=lambda t: (
            schema_file_id_to_phase.get(t[0].file_id, 0),
            schema_file_id_to_pos.get(t[0].file_id, len(schema_mods))))
        _phase_counts: dict[int, int] = {}
        for _t in _fomod_deferred:
            _ph = schema_file_id_to_phase.get(_t[0].file_id, 0)
            _phase_counts[_ph] = _phase_counts.get(_ph, 0) + 1
        _phase_summary = ", ".join(
            f"phase {p}: {_phase_counts[p]}" for p in sorted(_phase_counts))
        log(f"Installing {len(_fomod_deferred)} deferred FOMOD mod(s) ({_phase_summary})…")
        _set_status(f"Installing {len(_fomod_deferred)} deferred FOMOD mod(s)…")
        _current_phase = None
        for _mod, _result, _domain in _fomod_deferred:
            if _col_stop.is_set():
                break
            _this_phase = schema_file_id_to_phase.get(_mod.file_id, 0)
            if _current_phase is not None and _this_phase != _current_phase:
                _write_preliminary_plugins_txt(f"phase {_current_phase} → {_this_phase}")
            _current_phase = _this_phase
            _archive = str(_result.file_path)
            _pmeta, _pref = _mk_meta_and_name(_mod, _domain)
            cb.on_extract_add(_mod.file_id, _pref or (_mod.mod_name or ""))
            try:
                _folder = install_collection_archive(
                    _archive, game, profile_dir, log_fn=log,
                    progress_fn=lambda d, t, p=None, _f=_mod.file_id:
                        cb.on_extract_update(_f, int(d), int(t)),
                    fomod_auto_selections=fomod_by_file_id.get(_mod.file_id),
                    bain_auto_selections=bain_by_file_id.get(_mod.file_id),
                    prebuilt_meta=_pmeta, preferred_name=_pref,
                    skip_index_update=True, overwrite_existing=overwrite_existing,
                    resolve_fomod=cb.resolve_fomod, resolve_bain=cb.resolve_bain,
                    cancel=_col_stop)
            except Exception as _exc:
                log(f"Collection install: failed to install deferred FOMOD "
                    f"'{_mod.mod_name}': {_exc}")
                _folder = None
            finally:
                cb.on_extract_remove(_mod.file_id)
            _record(_mod, _folder)
            with _install_lock:
                _maybe_delete_archive(_archive, True)


# ---------------------------------------------------------------------------
# modlist / plugins writers (new-profile path, extracted for readability).
# ---------------------------------------------------------------------------
def _write_new_profile_modlist(profile_dir, modlist_path, install_order, log):
    install_order.sort(key=lambda x: x[0])
    try:
        _pre_existing = read_modlist(modlist_path) if modlist_path.is_file() else []
    except Exception:
        _pre_existing = []
    _ord_names_lower = {folder.lower() for _, folder in install_order}
    _preserved = [e for e in _pre_existing
                  if not e.is_separator and e.name.lower() not in _ord_names_lower]
    modlist_entries = [ModEntry(name=folder, enabled=True, locked=False)
                       for _, folder in install_order]
    if not modlist_entries:
        return
    try:
        _candidates: dict[str, list] = {}
        _order: list = []
        for me in modlist_entries:
            _order.append(me)
            if "__" in me.name:
                bname = me.name.split("__", 1)[0]
                _candidates.setdefault(bname, []).append(me)
        _bundle_map = {k: v for k, v in _candidates.items() if len(v) >= 2}
        _bundle_members = {id(e) for vs in _bundle_map.values() for e in vs}
        _non_bundle = [e for e in _order if id(e) not in _bundle_members]
        final_entries: list = list(_non_bundle)
        for bname, variants in _bundle_map.items():
            final_entries.append(
                ModEntry(name=f"{bname}_separator", enabled=True, locked=True, is_separator=True))
            for v in variants:
                v.locked = False
                v.enabled = True
                final_entries.append(v)
        user_sep_name = "User_Installed_separator"
        if _preserved:
            final_entries.append(
                ModEntry(name=user_sep_name, enabled=True, locked=True, is_separator=True))
            final_entries.extend(_preserved)
        write_modlist(modlist_path, final_entries)
        if _bundle_map or _preserved:
            from Utils.profile_state import read_separator_locks, write_separator_locks
            _locks = read_separator_locks(profile_dir)
            for bname in _bundle_map:
                _locks[f"{bname}_separator"] = True
            if _preserved:
                _locks[user_sep_name] = True
            write_separator_locks(profile_dir, _locks)
        log(f"Collection install: wrote modlist.txt with {len(final_entries)} entries")
    except Exception as exc:
        log(f"Collection install: failed to write modlist.txt: {exc}")


def _apply_schema_disabled_mods(modlist_path, collection_schema,
                                schema_file_id_to_pos, install_order, log):
    """Mark mods the manifest carries as ``enabled: false`` disabled in
    modlist.txt (share-code exports include the source profile's disabled mods
    so the recipient gets the same modlist, not an everything-on one). The mod
    is resolved to its staged folder via its priority key in ``install_order``
    (covers renamed/suffixed folders), falling back to a name match."""
    schema_mods: list[dict] = collection_schema.get("mods", [])
    key_to_folder: dict[int, str] = {key: folder for key, folder in install_order}
    targets: set[str] = set()
    for m in schema_mods:
        if m.get("enabled") is not False:
            continue
        folder = ""
        fid = (m.get("source") or {}).get("fileId")
        if fid is not None:
            try:
                folder = key_to_folder.get(
                    schema_file_id_to_pos.get(int(fid), -1), "")
            except (TypeError, ValueError):
                folder = ""
        if not folder:
            folder = m.get("name") or ""
        if folder:
            targets.add(folder.lower())
    if not targets:
        return
    entries = read_modlist(modlist_path)
    changed = 0
    for e in entries:
        if (not e.is_separator and not e.locked and e.enabled
                and e.name.lower() in targets):
            e.enabled = False
            changed += 1
    if changed:
        write_modlist(modlist_path, entries)
        log(f"Collection install: disabled {changed} mod(s) "
            f"(manifest enabled=false).")


def _apply_manifest_separators(profile_dir, modlist_path, collection_schema, log):
    """Re-insert the source modlist's separators from the manifest's
    ``modlistSeparators`` block (share-code exports; see
    ``profile_export._separator_blocks``). Each separator lands above its first
    member mod present in modlist.txt; separators whose name already exists
    (append / update re-runs) or whose members all fell out are skipped.
    Colors / locks are written to the profile's separator state for the
    separators actually inserted."""
    seps: list[dict] = collection_schema.get("modlistSeparators") or []
    if not seps:
        return
    entries = read_modlist(modlist_path)
    existing = {e.name.lower() for e in entries}
    colors: dict[str, str] = {}
    locks: dict[str, bool] = {}
    added = 0
    for sep in seps:
        name = (sep.get("name") or "").strip()
        if not name:
            continue
        if not name.endswith("_separator"):
            name += "_separator"
        if name.lower() in existing:
            continue
        members = {str(m).lower() for m in (sep.get("mods") or [])}
        idx = next((i for i, e in enumerate(entries)
                    if not e.is_separator and e.name.lower() in members), None)
        if idx is None:
            continue
        entries.insert(idx, ModEntry(name=name, enabled=True, locked=True,
                                     is_separator=True))
        existing.add(name.lower())
        added += 1
        # State keys use the DISPLAY name - that is what the modlist UI
        # reads (modlist_model keys _sep_locks/_sep_colors by display_name).
        disp = name[:-len("_separator")]
        if sep.get("color"):
            colors[disp] = str(sep["color"])
        if sep.get("locked"):
            locks[disp] = True
    if not added:
        return
    write_modlist(modlist_path, entries)
    try:
        from Utils.profile_state import (
            read_separator_colors, write_separator_colors,
            read_separator_locks, write_separator_locks)
        if colors:
            merged_c = read_separator_colors(profile_dir)
            merged_c.update(colors)
            write_separator_colors(profile_dir, merged_c)
        if locks:
            merged_l = read_separator_locks(profile_dir)
            merged_l.update(locks)
            write_separator_locks(profile_dir, merged_l)
    except Exception as exc:
        log(f"Collection install: separator colors/locks skipped: {exc}")
    log(f"Collection install: inserted {added} separator(s) from manifest.")


def _read_amethyst_export_data(archive_root, log) -> "dict | None":
    """Read the archive's ``Amethyst/`` profile-fidelity folder (written by
    collection_export._amethyst_state_jobs). Returns ``{modlist_text,
    profile_state, bundles, format}`` or None when absent (Vortex-authored
    collections). Files are additive across formats, so a newer format is
    still read - unknown content is simply ignored."""
    adir = archive_root / "Amethyst"
    modlist_file = adir / "modlist.txt"
    if not modlist_file.is_file():
        return None
    data: dict = {
        "modlist_text": modlist_file.read_text(encoding="utf-8",
                                               errors="surrogateescape"),
        "profile_state": {},
        "bundles": {},
        "format": 1,
    }
    try:
        exp = json.loads((adir / "export.json").read_text(encoding="utf-8"))
        data["format"] = int(exp.get("format") or 1)
        bundles = exp.get("bundles")
        if isinstance(bundles, dict):
            data["bundles"] = {str(k): str(v) for k, v in bundles.items()}
    except Exception:
        pass
    ps_file = adir / "profile_state.json"
    if ps_file.is_file():
        try:
            raw = json.loads(ps_file.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data["profile_state"] = raw
        except Exception as exc:
            log(f"Collection install: Amethyst profile_state unreadable: {exc}")
    # Kept as bytes: the game rewrites plugins.txt in cp1252, and read_plugins'
    # encoding fallback needs the raw file, not a lossy decode.
    for key, fname in (("plugins_bytes", "plugins.txt"),
                       ("loadorder_bytes", "loadorder.txt"),
                       ("userlist_bytes", "userlist.yaml")):
        f = adir / fname
        if f.is_file():
            try:
                data[key] = f.read_bytes()
            except OSError as exc:
                log(f"Collection install: Amethyst {fname} unreadable: {exc}")
    log(f"Collection install: archive carries Amethyst profile data "
        f"(format {data['format']})")
    return data


def _apply_amethyst_profile_state(profile_dir, modlist_path, data,
                                  log) -> dict:
    """Apply an Amethyst-authored collection's exact modlist (order, separators,
    enabled state) and portable profile_state on top of the finished install.
    Caller gates to fresh installs (Reset Load Order reuses it on an existing
    profile - same semantics). Entries naming mods that never got installed
    (deselected optionals, disabled-at-export mods) are skipped; installed mods
    the modlist doesn't know are kept below the ordered block. Returns
    ``{strip_changed, ordered, leftovers, skipped}`` - *strip_changed* lists
    installed mods whose strip prefixes changed (the caller must subset-rescan
    them); the counts cover mods only, not separators."""
    from Utils.modlist import modlist_lock, parse_modlist_text

    authored = parse_modlist_text(data.get("modlist_text") or "")
    bundles: dict = data.get("bundles") or {}
    renames: dict[str, str] = {}      # authored name -> installed folder name
    final_entries: list = []
    stats = {"strip_changed": [], "ordered": 0, "leftovers": 0, "skipped": 0}
    if authored:
        with modlist_lock(modlist_path):
            existing = read_modlist(modlist_path)
            by_lower = {e.name.lower(): e for e in existing
                        if not e.is_separator}
            consumed: set[int] = set()
            ordered: list = []
            sep_names: set[str] = set()
            skipped = 0
            for a in authored:
                if a.is_separator:
                    if a.name.lower() in sep_names:
                        continue
                    sep_names.add(a.name.lower())
                    ordered.append(ModEntry(name=a.name, enabled=True,
                                            locked=True, is_separator=True))
                    continue
                target = by_lower.get(a.name.lower())
                if target is None and a.name in bundles:
                    for cand in (bundles.get(a.name), a.name):
                        if not cand:
                            continue
                        clean = (re.sub(r"[^\w\s\-]", "", str(cand)).strip()
                                 .replace(" ", "_") or str(cand))
                        target = by_lower.get(clean.lower())
                        if target is not None:
                            break
                if target is None or id(target) in consumed:
                    skipped += 1
                    continue
                consumed.add(id(target))
                renames[a.name] = target.name
                ordered.append(ModEntry(name=target.name, enabled=a.enabled,
                                        locked=a.locked))
            leftovers = [
                e for e in existing
                if id(e) not in consumed
                and not (e.is_separator and e.name.lower() in sep_names)]
            final_entries = ordered + leftovers
            write_modlist(modlist_path, final_entries)
            stats["ordered"] = sum(1 for e in ordered if not e.is_separator)
            stats["leftovers"] = sum(1 for e in leftovers
                                     if not e.is_separator)
            stats["skipped"] = skipped
            log(f"Collection install: applied exported load order - "
                f"{len(ordered)} entries, {skipped} skipped (not installed), "
                f"{len(leftovers)} unlisted kept below")
    else:
        final_entries = read_modlist(modlist_path)

    # LOOT user rules, verbatim. Step 4's _apply_collection_groups already
    # merged the manifest's pluginRules into userlist.yaml, but those rules
    # were DERIVED from this file at export - the original is the superset
    # (custom groups, rules on plugins outside the collection), so it wins.
    userlist_bytes = data.get("userlist_bytes")
    if userlist_bytes:
        try:
            (Path(profile_dir) / "userlist.yaml").write_bytes(userlist_bytes)
            log("Collection install: wrote exported userlist.yaml (LOOT rules)")
        except OSError as exc:
            log(f"Collection install: userlist.yaml not applied: {exc}")

    state = data.get("profile_state") or {}
    if not isinstance(state, dict) or not state:
        return stats
    installed_lower = {e.name.lower() for e in final_entries
                       if not e.is_separator}
    # Separator state (locks/colors/collapsed/deploy paths) is keyed by DISPLAY
    # name in profile_state ("Movement", not "Movement_separator") - that is
    # what the modlist UI reads and writes. Accept both spellings when
    # filtering so hand-edited or older state still comes through.
    seps_lower = set()
    for e in final_entries:
        if e.is_separator:
            seps_lower.add(e.name.lower())
            seps_lower.add(e.display_name.lower())

    def _mod_dict(raw) -> dict:
        """Filter a mod-keyed dict to installed mods, remapping authored
        (pre-bundle-rename) names to their installed folder names."""
        out: dict = {}
        if not isinstance(raw, dict):
            return out
        for k, v in raw.items():
            name = renames.get(str(k), str(k))
            if name.lower() in installed_lower:
                out[name] = v
        return out

    def _sep_dict(raw) -> dict:
        if not isinstance(raw, dict):
            return {}
        return {str(k): v for k, v in raw.items()
                if str(k).lower() in seps_lower}

    from Utils import profile_state as _ps

    strip_changed: list[str] = []
    try:
        incoming = _mod_dict(state.get("mod_strip_prefixes"))
        if incoming:
            cur = _ps.read_mod_strip_prefixes(profile_dir)
            for name, val in incoming.items():
                new = [str(x) for x in (val or [])]
                if cur.get(name) != new:
                    cur[name] = new
                    strip_changed.append(name)
            if strip_changed:
                _ps.write_mod_strip_prefixes(profile_dir, cur)
    except Exception as exc:
        log(f"Collection install: strip prefixes not applied: {exc}")

    applied: list[str] = ["mod_strip_prefixes"] if strip_changed else []

    def _merge_dict(key, read_fn, write_fn, mapper):
        try:
            incoming = mapper(state.get(key))
            if not incoming:
                return
            cur = read_fn(profile_dir)
            cur.update(incoming)
            write_fn(profile_dir, cur)
            applied.append(key)
        except Exception as exc:
            log(f"Collection install: {key} not applied: {exc}")

    _merge_dict("separator_locks",
                _ps.read_separator_locks, _ps.write_separator_locks, _sep_dict)
    _merge_dict("separator_colors",
                _ps.read_separator_colors, _ps.write_separator_colors, _sep_dict)
    _merge_dict("separator_deploy_paths",
                _ps.read_separator_deploy_paths,
                _ps.write_separator_deploy_paths, _sep_dict)
    _merge_dict("disabled_plugins",
                _ps.read_disabled_plugins, _ps.write_disabled_plugins, _mod_dict)
    _merge_dict("excluded_mod_files",
                _ps.read_excluded_mod_files, _ps.write_excluded_mod_files,
                _mod_dict)
    _merge_dict("root_mod_files",
                _ps.read_root_mod_files, _ps.write_root_mod_files, _mod_dict)
    _merge_dict("mod_notes",
                _ps.read_mod_notes, _ps.write_mod_notes, _mod_dict)
    _merge_dict("plugin_locks",
                _ps.read_plugin_locks, _ps.write_plugin_locks,
                lambda raw: dict(raw) if isinstance(raw, dict) else {})

    try:
        raw = state.get("collapsed_seps")
        if isinstance(raw, list) and raw:
            vals = {str(x) for x in raw if str(x).lower() in seps_lower}
            if vals:
                _ps.write_collapsed_seps(
                    profile_dir, set(_ps.read_collapsed_seps(profile_dir)) | vals)
                applied.append("collapsed_seps")
    except Exception as exc:
        log(f"Collection install: collapsed_seps not applied: {exc}")
    try:
        raw = state.get("ignored_missing_requirements")
        if isinstance(raw, list) and raw:
            _ps.write_ignored_missing_requirements(
                profile_dir,
                set(_ps.read_ignored_missing_requirements(profile_dir))
                | {str(x) for x in raw})
            applied.append("ignored_missing_requirements")
    except Exception as exc:
        log(f"Collection install: ignored requirements not applied: {exc}")

    if applied:
        log(f"Collection install: merged exported profile state "
            f"({', '.join(applied)})")
    stats["strip_changed"] = strip_changed
    return stats


def _persist_amethyst_stash(profile_dir, data, log) -> None:
    """Persist the archive's Amethyst/ data as ``<profile>/Amethyst/`` so Reset
    Load Order restores the exact exported profile offline - no archive
    re-extract (or redownload) needed. Same layout the ``.amethyst`` import
    snapshots via ``collection_export.write_amethyst_stash``."""
    stash = Path(profile_dir) / "Amethyst"
    stash.mkdir(parents=True, exist_ok=True)
    (stash / "modlist.txt").write_text(
        data.get("modlist_text") or "", encoding="utf-8",
        errors="surrogateescape")
    for key, fname in (("plugins_bytes", "plugins.txt"),
                       ("loadorder_bytes", "loadorder.txt"),
                       ("userlist_bytes", "userlist.yaml")):
        b = data.get(key)
        if b:
            (stash / fname).write_bytes(b)
    ps = data.get("profile_state")
    if ps:
        (stash / "profile_state.json").write_text(
            json.dumps(ps, indent=1), encoding="utf-8")
    (stash / "export.json").write_text(json.dumps({
        "format": data.get("format", 1),
        "bundles": data.get("bundles") or {},
    }, indent=1), encoding="utf-8")
    log("Collection install: saved Amethyst/ order snapshot to the profile")


def _extract_amethyst_members(archive_path, cache_dir, log) -> "dict | None":
    """Extract ONLY the Amethyst/ members of a cached collection .7z and read
    them. Returns None when the archive has no Amethyst folder (Vortex-authored
    or pre-feature export). Far cheaper than a full extractall - the archive
    can be multi-GB of bundled tool output."""
    import shutil as _shutil
    import tempfile as _tf
    import py7zr
    tmp = Path(_tf.mkdtemp(prefix="amethyst_reset_", dir=str(cache_dir)))
    try:
        with py7zr.SevenZipFile(str(archive_path), mode="r") as arc:
            targets = [n for n in arc.getnames()
                       if n == "Amethyst" or n.startswith("Amethyst/")]
            if not targets:
                return None
            arc.reset()
            arc.extract(path=str(tmp), targets=targets)
        return _read_amethyst_export_data(tmp, log)
    except Exception as exc:
        log(f"Reset load order: could not read Amethyst data from "
            f"{archive_path.name}: {exc}")
        return None
    finally:
        _shutil.rmtree(tmp, ignore_errors=True)


def load_amethyst_reset_data(game, slug, *, profile_dir=None,
                             revision_hint=None, domain="",
                             api_provider=None, log=None) -> "dict | None":
    """Amethyst profile-fidelity data for Reset Load Order.

    Checks the profile's own ``Amethyst/`` snapshot first (written by every
    collection install and ``.amethyst`` import - this is the only source for
    imported profiles, which have no slug). Then a cached ``<slug>_rev<N>.7z``
    (preferring *revision_hint*, else the newest cached revision), extracting
    only its Amethyst/ folder. On a cache miss it calls *api_provider*
    (lazily, so no auth work happens when a local source hits) and redownloads
    the archive via the collection's download link. Returns None when the
    collection carries no Amethyst data - the caller falls back to the
    manifest-based reset.
    """
    import shutil as _shutil
    log = log or (lambda *_a: None)
    if profile_dir is not None:
        try:
            data = _read_amethyst_export_data(Path(profile_dir), log)
        except Exception:
            data = None
        if data is not None:
            log("Reset load order: using the profile's Amethyst snapshot")
            return data
    slug = (slug or "").strip()
    if not slug:
        return None
    cache_dir = get_download_cache_dir_for_game(getattr(game, "name", "") or "")
    best = None
    best_rev = -1
    try:
        for p in cache_dir.glob(f"{slug}_rev*.7z"):
            m = re.fullmatch(re.escape(slug) + r"_rev(\d+)\.7z", p.name)
            rev = int(m.group(1)) if m else -1
            if revision_hint is not None and rev == int(revision_hint):
                best, best_rev = p, rev
                break
            if rev > best_rev:
                best, best_rev = p, rev
    except OSError:
        pass
    if best is not None:
        data = _extract_amethyst_members(best, cache_dir, log)
        if data is not None:
            log(f"Reset load order: using Amethyst data from cached "
                f"{best.name}")
            return data
        # The cached archive predates the Amethyst folder (or is
        # Vortex-authored) - redownloading the same revision can't help.
        return None
    if api_provider is None:
        return None
    try:
        api = api_provider()
    except Exception as exc:
        log(f"Reset load order: Nexus API unavailable: {exc}")
        return None
    if api is None:
        return None
    try:
        (_name, _sz, _cnt, _mods, dl_path, revs,
         _card) = api.get_collection_detail(slug, domain, revision_hint)
    except Exception as exc:
        log(f"Reset load order: collection lookup failed: {exc}")
        return None
    rev = revision_hint
    if rev is None:
        try:
            pub = [int(r.get("revisionNumber") or 0) for r in (revs or [])
                   if (r.get("revisionStatus") or "").lower() == "published"]
            rev = max(pub) if pub else None
        except Exception:
            rev = None
    root = _ensure_collection_archive_extracted(
        game, api, slug, rev, dl_path or "", log)
    if root is None:
        return None
    try:
        return _read_amethyst_export_data(root, log)
    finally:
        _shutil.rmtree(root, ignore_errors=True)


def _entries_from_amethyst_plugins(amethyst_state, author_entries, vanilla_map,
                                   star_prefix, log) -> "list[PluginEntry]":
    """Order the install's plugin set by the exported profile's exact
    plugins.txt/loadorder.txt (Amethyst/ folder). Returns [] when the archive
    carries no plugin files or they don't parse - the caller falls back to the
    LOOT sort. Only ORDER and enabled state come from the export; the plugin
    SET stays the one this install actually produced (deselected optionals are
    already filtered out of *author_entries*, conditional plugins recovered
    from the filemap ride along at the end in their current order)."""
    import tempfile as _tf
    from Utils.plugins import read_loadorder, read_plugins
    plugins_bytes = (amethyst_state or {}).get("plugins_bytes")
    if not plugins_bytes:
        return []
    lo_names: list[str] = []
    with _tf.TemporaryDirectory() as td:
        pp = Path(td) / "plugins.txt"
        pp.write_bytes(plugins_bytes)
        authored = read_plugins(pp, star_prefix=star_prefix)
        lo_bytes = amethyst_state.get("loadorder_bytes")
        if lo_bytes:
            lp = Path(td) / "loadorder.txt"
            lp.write_bytes(lo_bytes)
            lo_names = read_loadorder(lp)
    if not authored and not lo_names:
        return []
    enabled_map = {e.name.lower(): e.enabled for e in authored}
    if not star_prefix:
        # Legacy format: plugins.txt lists only ENABLED plugins; a plugin in
        # loadorder.txt but absent from plugins.txt is installed-but-disabled.
        for n in lo_names:
            enabled_map.setdefault(n.lower(), False)
    # loadorder.txt is the full order including vanilla masters; plugins.txt
    # order is the fallback when it wasn't exported.
    order_names = lo_names or [e.name for e in authored]
    order_map = {n.lower(): i for i, n in enumerate(order_names)}
    author_lower = {e.name.lower() for e in author_entries}
    _ext_order = {".esm": 0, ".esp": 1, ".esl": 2}
    vanilla_prepend = [
        PluginEntry(name=orig, enabled=True)
        for low, orig in sorted(
            vanilla_map.items(),
            key=lambda kv: (_ext_order.get(Path(kv[0]).suffix, 9), kv[0]))
        if low not in author_lower]
    candidates = vanilla_prepend + author_entries
    unknown_base = len(order_map)
    keyed: list = []
    for i, e in enumerate(candidates):
        low = e.name.lower()
        # Vanilla masters stay force-enabled; everything else takes the
        # exported enabled state when the export knows the plugin.
        if low not in vanilla_map and low in enabled_map:
            e = PluginEntry(name=e.name, enabled=enabled_map[low])
        keyed.append((order_map.get(low, unknown_base + i), i, e))
    keyed.sort(key=lambda t: (t[0], t[1]))
    return [t[2] for t in keyed]


def _write_collection_plugins(game, profile_dir, plugins_path, collection_schema,
                              overwrite_existing, _is_append_run, log, _set_status,
                              amethyst_state=None):
    from Utils.game_helpers import _vanilla_plugins_for_game
    schema_plugins: list[dict] = collection_schema.get("plugins", [])
    if schema_plugins and overwrite_existing is None:
        try:
            author_entries = [
                PluginEntry(name=p.get("name", ""), enabled=p.get("enabled", True))
                for p in schema_plugins if p.get("name", "")]
            author_lower = {e.name.lower() for e in author_entries}
            vanilla_map = _vanilla_plugins_for_game(game)
            plugins_include_vanilla = getattr(game, "plugins_include_vanilla", False)
            # The full vanilla set (base + DLC + .ccc-listed CC content) stays out
            # of plugins.txt: the engine force-loads it before reading the file and
            # strips any such entries on launch. MO2/Vortex/LOOT exclude it too.
            vanilla_lower = set() if plugins_include_vanilla else set(vanilla_map.keys())
            deployed = _filemap_deployed_plugins(game, profile_dir)
            # Drop manifest plugins whose file was never installed. A collection's
            # ``plugins`` array covers ALL its mods including optional ones the
            # user skipped (e.g. GTS's 119 Anniversary-Edition patch mods), and
            # Vortex only lists plugins that exist on disk - writing the array
            # verbatim leaves phantom plugins.txt entries that inflate the
            # regular-slot count and that the panel's prune refuses to bulk-remove
            # (> _PRUNE_MAX). Keep = deployed per the filemap / vanilla+CC /
            # on disk at a staged-mod root, overwrite or Data. Skip the filter
            # when both scans come back empty (no filemap AND wrong/empty staging
            # path) - a miss means nothing then.
            on_disk = _on_disk_plugin_names(game)
            if deployed or on_disk:
                kept: list[PluginEntry] = []
                missing: list[str] = []
                for e in author_entries:
                    low = e.name.lower()
                    if low in deployed or low in vanilla_map or low in on_disk:
                        kept.append(e)
                    else:
                        missing.append(e.name)
                if missing:
                    author_entries = kept
                    author_lower = {e.name.lower() for e in author_entries}
                    log(f"Collection install: skipped {len(missing)} manifest "
                        f"plugin(s) with no installed file (skipped optional "
                        f"mods): {', '.join(missing[:8])}"
                        f"{', …' if len(missing) > 8 else ''}")
            # Recover plugins staged by the collection's mods but absent from the
            # manifest's ``plugins`` array (FOMOD-conditional / unlisted plugins).
            # These are read from the filemap built in Step 3c so the LOOT sort
            # covers the SAME set as a later manual sort - otherwise they're
            # dropped and the manual sort re-inserts them (the "400+ moved" bug).
            for low, orig in deployed.items():
                if low in author_lower or low in vanilla_map:
                    continue
                author_entries.append(PluginEntry(name=orig, enabled=True))
                author_lower.add(low)
            _apply_collection_groups(profile_dir, collection_schema, log)
            final_entries: list[PluginEntry] = []
            star_prefix = getattr(game, "plugins_use_star_prefix", True)
            if amethyst_state:
                # An Amethyst-authored archive carries the exact exported
                # plugin order - apply it and skip the LOOT sort (LOOT would
                # re-sort away the author's manual tweaks).
                try:
                    final_entries = _entries_from_amethyst_plugins(
                        amethyst_state, author_entries, vanilla_map,
                        star_prefix, log)
                    if final_entries:
                        log(f"Collection install: applied exported plugin "
                            f"order ({len(final_entries)} plugin(s)) - "
                            "LOOT sort skipped.")
                except Exception as exc:
                    log(f"Collection install: exported plugin order failed "
                        f"({exc}) - falling back to LOOT.")
                    final_entries = []
            loot_enabled = getattr(game, "loot_sort_enabled", False)
            if not final_entries and loot_enabled and _loot_available():
                try:
                    _set_status("Running LOOT sort to apply collection load order…")
                    from LOOT.loot_sorter import sort_plugins as _loot_sort
                    _ext_order = {".esm": 0, ".esp": 1, ".esl": 2}
                    vanilla_prepend = [
                        PluginEntry(name=orig, enabled=True)
                        for low, orig in sorted(
                            vanilla_map.items(),
                            key=lambda kv: (_ext_order.get(Path(kv[0]).suffix, 9), kv[0]))
                        if low not in author_lower]
                    all_entries = vanilla_prepend + author_entries
                    name_to_enabled = {e.name: e.enabled for e in all_entries}
                    loot_result = _loot_sort(
                        plugin_names=[e.name for e in all_entries],
                        enabled_set={e.name for e in all_entries if e.enabled},
                        game_name=game.name, game_path=game.get_game_path(),
                        staging_root=game.get_effective_mod_staging_path(), log_fn=log,
                        game_type_attr=getattr(game, "loot_game_type", ""),
                        game_id=getattr(game, "game_id", ""),
                        masterlist_url=getattr(game, "loot_masterlist_url", ""),
                        masterlist_repo=getattr(game, "loot_masterlist_repo", ""),
                        game_data_dir=(game.get_vanilla_plugins_path()
                                       if hasattr(game, "get_vanilla_plugins_path") else None),
                        userlist_path=profile_dir / "userlist.yaml")
                    final_entries = [
                        PluginEntry(name=n, enabled=name_to_enabled.get(n, True))
                        for n in loot_result.sorted_names]
                    log(f"Collection install: LOOT sort produced {len(final_entries)} plugin(s).")
                except Exception as loot_exc:
                    log(f"Collection install: LOOT sort failed - {loot_exc}; "
                        "falling back to flat list.")
            if not final_entries:
                _ext_order = {".esm": 0, ".esp": 1, ".esl": 2}
                vanilla_prefix = [
                    PluginEntry(name=orig, enabled=True)
                    for low, orig in sorted(
                        vanilla_map.items(),
                        key=lambda kv: (_ext_order.get(Path(kv[0]).suffix, 9), kv[0]))
                    if low not in author_lower]
                final_entries = vanilla_prefix + author_entries
            write_plugins(plugins_path,
                          [e for e in final_entries if e.name.lower() not in vanilla_lower],
                          star_prefix=star_prefix)
            write_loadorder(plugins_path.parent / "loadorder.txt", final_entries)
            log(f"Collection install: wrote plugins.txt ({len(final_entries)} plugin(s)).")
        except Exception as exc:
            log(f"Collection install: failed to write plugins.txt: {exc}")
    elif schema_plugins and _is_append_run:
        try:
            _apply_collection_groups(profile_dir, collection_schema, log)
        except Exception as exc:
            log(f"Collection append: failed to write userlist.yaml rules: {exc}")


def _loot_available() -> bool:
    try:
        from LOOT.loot_sorter import is_available
        return bool(is_available())
    except Exception:
        return False


def _filemap_deployed_plugins(game, profile_dir) -> "dict[str, str]":
    """Top-level plugin names the freshly-built filemap.txt deploys, keyed
    {lower: original_name}. Port of gui_qt.plugin_state._filemap_deployed_plugins
    (kept here so the neutral install layer doesn't import the Qt module).

    A collection's manifest ``plugins`` array doesn't always list every plugin
    that its mods actually ship (FOMOD-conditional plugins, plugins bundled in a
    mod but omitted from the author's list). Those show up in the panel/manual
    sort via this same filemap recovery, so the install-time LOOT sort must feed
    them in too - otherwise they're dropped from plugins.txt and a later manual
    sort re-inserts them, reporting hundreds of "moved" plugins.
    """
    staging = (game.get_effective_mod_staging_path()
               if hasattr(game, "get_effective_mod_staging_path") else None)
    if staging is None:
        return {}
    fm = staging.parent / "filemap.txt"
    if not fm.is_file():
        return {}
    exts = tuple(e.lower() for e in (getattr(game, "plugin_extensions", []) or [])) \
        or (".esp", ".esm", ".esl")
    found: "dict[str, str]" = {}
    try:
        # surrogateescape: filemap.txt paths derive from on-disk filenames that
        # may contain non-UTF-8 bytes (decoded to surrogate code points); a
        # plain utf-8 read would crash on them.
        for line in fm.read_text(encoding="utf-8",
                                 errors="surrogateescape").splitlines():
            if "\t" not in line:
                continue
            rel_path = line.split("\t", 1)[0].replace("\\", "/")
            if "/" in rel_path:
                continue   # top-level plugins only (matches deploy layout)
            low = rel_path.lower()
            if low.endswith(exts):
                found.setdefault(low, rel_path)
    except OSError:
        pass
    return found


def _on_disk_plugin_names(game) -> "set[str]":
    """Lowercase filenames of plugin files present on disk outside the filemap:
    each staged mod's root, overwrite/ (+ overwrite/Data) and the game Data dir
    (+ its _Core swap-deploy variant). Complements _filemap_deployed_plugins as
    evidence that a manifest-listed plugin was actually installed - the filemap
    is only rebuilt at Step 3c when LOOT is enabled, so it can be missing or
    stale here. Mod roots only (no recursion): a plugin nested deeper that still
    deploys top-level always appears in a fresh filemap, and load_plugins'
    deployed-plugin recovery re-adds any such miss on the next reload."""
    exts = tuple(e.lower() for e in (getattr(game, "plugin_extensions", []) or ())) \
        or (".esp", ".esm", ".esl")
    found: "set[str]" = set()

    def _scan_flat(d: Path) -> None:
        try:
            for entry in d.iterdir():
                if entry.is_file() and entry.name.lower().endswith(exts):
                    found.add(entry.name.lower())
        except OSError:
            pass

    staging = (game.get_effective_mod_staging_path()
               if hasattr(game, "get_effective_mod_staging_path") else None)
    if staging is not None and staging.is_dir():
        try:
            for mod_dir in staging.iterdir():
                if mod_dir.is_dir():
                    _scan_flat(mod_dir)
        except OSError:
            pass
        overwrite_dir = staging.parent / "overwrite"
        _scan_flat(overwrite_dir)
        _scan_flat(overwrite_dir / "Data")
    data_dir = (game.get_vanilla_plugins_path()
                if hasattr(game, "get_vanilla_plugins_path") else None)
    if data_dir is not None:
        _scan_flat(data_dir)
        _scan_flat(data_dir.parent / (data_dir.name + "_Core"))
    return found


# ---------------------------------------------------------------------------
# Bundled assets (Step 2c). Ported from _run_install 3771-3888.
# ---------------------------------------------------------------------------
def _install_bundled_assets(game, api, profile_dir, staging_path, collection_schema,
                            schema_mods, download_link_path, revision_number,
                            collection_slug, staging_lower_map, install_order, log,
                            _set_status) -> "tuple[int, int, list[str]]":
    """Returns ``(installed, skipped, folders)`` - skipped counts bundled assets
    missing from the archive or that failed to copy (Tk counted these in the
    final "(N skipped)" summary). *folders* is every staging folder this touched,
    so the caller can get them into the mod index: they land AFTER the index
    rebuild, and build_filemap deploys nothing for a mod with no index entry."""
    import tempfile as _tf
    import shutil as _shutil
    bundle_schema_mods = [
        m for m in schema_mods
        if (m.get("source") or {}).get("type", "").lower() == "bundle"]
    if not (bundle_schema_mods and download_link_path):
        return 0, 0, []
    installed = 0
    skipped = 0
    touched: list[str] = []
    _scratch_root = get_download_cache_dir_for_game(getattr(game, "name", "") or "")
    bundle_extract_dir = _tf.mkdtemp(prefix="amethyst_bundle_", dir=str(_scratch_root))
    try:
        _slug = (collection_slug or "").strip()
        _rev = int(revision_number) if revision_number is not None else "x"
        _cached_archive = _scratch_root / f"{_slug}_rev{_rev}.7z"
        cj_full: dict = {}
        if _slug and _cached_archive.is_file():
            _set_status(f"Extracting cached collection archive for "
                        f"{len(bundle_schema_mods)} bundled mod(s)…")
            log(f"Collection install: reusing cached archive {_cached_archive}")
            try:
                import py7zr as _py7zr_local
                with _py7zr_local.SevenZipFile(str(_cached_archive), mode="r") as arc:
                    arc.extractall(path=bundle_extract_dir)
                _cj_path = Path(bundle_extract_dir) / "collection.json"
                if _cj_path.is_file():
                    cj_full = json.loads(_cj_path.read_text(encoding="utf-8"))
            except Exception as exc:
                log(f"Collection install: cached archive extract failed ({exc}) - re-downloading")
                cj_full = {}
        if not cj_full:
            _set_status(f"Downloading collection archive for "
                        f"{len(bundle_schema_mods)} bundled mod(s)…")
            cj_full = api.get_collection_archive_full(
                download_link_path, bundle_extract_dir,
                keep_archive_at=str(_cached_archive) if _slug else None)
        if cj_full:
            _bundled_meta_map = _installed_bundled_meta_map(staging_path, _slug)
            for bm in bundle_schema_mods:
                bm_name = bm.get("name") or ""
                src = bm.get("source") or {}
                file_expr = src.get("fileExpression") or bm_name
                bundle_subdir = Path(bundle_extract_dir) / "bundled" / file_expr
                if not bundle_subdir.is_dir():
                    bundle_subdir = Path(bundle_extract_dir) / "bundled" / bm_name
                if not bundle_subdir.is_dir():
                    log(f"Collection install: bundled asset '{bm_name}' not found in archive")
                    skipped += 1
                    continue
                mod_name_clean = re.sub(r"[^\w\s\-]", "", bm_name).strip().replace(" ", "_") or file_expr
                if mod_name_clean.lower() in {k.lower() for k in staging_lower_map}:
                    log(f"Collection install: bundled '{bm_name}' already installed - skipping")
                    existing = staging_lower_map.get(mod_name_clean.lower(), mod_name_clean)
                    install_order.append((-1, existing))
                    touched.append(existing)
                    installed += 1
                    continue
                _meta_hit = (_bundled_meta_map.get(file_expr.lower())
                             or _bundled_meta_map.get(bm_name.lower()))
                if _meta_hit:
                    log(f"Collection install: bundled '{bm_name}' already installed "
                        f"as '{_meta_hit}' - skipping")
                    install_order.append((-1, _meta_hit))
                    touched.append(_meta_hit)
                    installed += 1
                    continue
                _set_status(f"Installing bundled asset: {bm_name}…")
                try:
                    import configparser as _cpi
                    dest = staging_path / mod_name_clean
                    if dest.exists():
                        _shutil.rmtree(dest)
                    _shutil.copytree(str(bundle_subdir), str(dest))
                    cp = _cpi.ConfigParser()
                    general = {
                        "modname": bm_name, "installationfile": file_expr,
                        "fromCollection": _slug, "fromCollectionBundled": "true"}
                    if revision_number is not None:
                        general["fromCollectionRevision"] = str(int(revision_number))
                    if bm.get("optional"):
                        general["collectionOptional"] = "true"
                    try:
                        _bm_phase = int(bm.get("phase") or 0)
                    except (TypeError, ValueError):
                        _bm_phase = 0
                    if _bm_phase:
                        general["collectionPhase"] = str(_bm_phase)
                    cp["General"] = general
                    with open(dest / "meta.ini", "w", encoding="utf-8") as mf:
                        cp.write(mf)
                    install_order.append((-1, mod_name_clean))
                    touched.append(mod_name_clean)
                    installed += 1
                    log(f"Collection install: installed bundled asset "
                        f"'{bm_name}' → '{mod_name_clean}'")
                except Exception as exc:
                    log(f"Collection install: failed to install bundled asset '{bm_name}': {exc}")
                    skipped += 1
    finally:
        try:
            _shutil.rmtree(bundle_extract_dir, ignore_errors=True)
        except Exception:
            pass
    return installed, skipped, touched


def _installed_bundled_meta_map(staging_path: Path, slug: str) -> "dict[str, str]":
    """Map installationfile/modname of installed bundled mods → folder name
    (ported from _installed_bundled_meta_map)."""
    import configparser as _cpi
    meta_map: dict[str, str] = {}
    if not staging_path.is_dir():
        return meta_map
    for mod_dir in staging_path.iterdir():
        meta_path = mod_dir / "meta.ini"
        if not mod_dir.is_dir() or not meta_path.is_file():
            continue
        cp = _cpi.ConfigParser()
        try:
            cp.read(meta_path, encoding="utf-8")
            if not cp.has_section("General"):
                continue
            if not cp["General"].getboolean("fromCollectionBundled", fallback=False):
                continue
            if (cp["General"].get("fromCollection", "") or "").strip() != slug:
                continue
            for key in ("installationfile", "modname"):
                val = (cp["General"].get(key, "") or "").strip()
                if val:
                    meta_map[val.lower()] = mod_dir.name
        except Exception:
            continue
    return meta_map


# ---------------------------------------------------------------------------
# Step 3b: bundled folders + binary patches + INI tweaks from the cached archive.
# Ported from _install_bundled_from_extracted / _apply_collection_binary_patches
# / _apply_collection_ini_tweaks (+ _ensure_collection_archive_extracted).
# ---------------------------------------------------------------------------
def _ensure_collection_archive_extracted(game, api, collection_slug,
                                         revision_number, download_link_path, log):
    """Return a dir with the extracted collection archive (cached .7z preferred),
    or None. Caller rmtree's the returned dir."""
    import shutil as _shutil
    import tempfile as _tf
    slug = (collection_slug or "").strip()
    rev = int(revision_number) if revision_number is not None else "x"
    if not slug:
        return None
    cache_dir = get_download_cache_dir_for_game(getattr(game, "name", "") or "")
    archive_path = cache_dir / f"{slug}_rev{rev}.7z"
    if not archive_path.is_file():
        if not download_link_path:
            log(f"Collection archive: not at {archive_path} and no link - skipping")
            return None
        log(f"Collection archive: not cached, downloading to {archive_path}")
        _fetch_dir = Path(_tf.mkdtemp(prefix="amethyst_bundle_fetch_", dir=str(cache_dir)))
        try:
            cj = api.get_collection_archive_full(
                download_link_path, str(_fetch_dir), keep_archive_at=str(archive_path))
            if not cj or not archive_path.is_file():
                log("Collection archive: fallback download failed")
                return None
        finally:
            _shutil.rmtree(_fetch_dir, ignore_errors=True)
    extract_dir = Path(_tf.mkdtemp(prefix="amethyst_archive_extract_", dir=str(cache_dir)))
    try:
        import py7zr
        with py7zr.SevenZipFile(str(archive_path), mode="r") as arc:
            arc.extractall(path=str(extract_dir))
    except Exception as exc:
        log(f"Collection archive: failed to extract {archive_path}: {exc}")
        _shutil.rmtree(extract_dir, ignore_errors=True)
        return None
    return extract_dir


def _extract_local_bundle_patches(game, bundle_zip, log) -> "Path | None":
    """Extract a local ``.amethyst``'s ``patches/`` members into a temp dir
    usable as a Step-3b archive root, or None when it has none. The zip's
    other members (mods/, overwrite/, profile/) are deliberately left alone -
    ``install_local_bundle`` extracts those after the install run.
    Caller rmtree's the returned dir."""
    import shutil as _shutil
    import tempfile as _tf
    import zipfile as _zip
    bundle_zip = Path(bundle_zip)
    try:
        if not _zip.is_zipfile(bundle_zip):
            return None
        with _zip.ZipFile(bundle_zip, "r") as zf:
            members = [
                n for n in zf.namelist()
                if n.startswith("patches/") and not n.endswith("/")
                # Arcnames are author-controlled: never let one climb out.
                and ".." not in Path(n).parts
            ]
            if not members:
                return None
            cache_dir = get_download_cache_dir_for_game(
                getattr(game, "name", "") or "")
            extract_dir = Path(_tf.mkdtemp(
                prefix="amethyst_import_patches_", dir=str(cache_dir)))
            try:
                for n in members:
                    dest = extract_dir / n
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(n) as srcf, open(dest, "wb") as dstf:
                        _shutil.copyfileobj(srcf, dstf)
            except Exception:
                _shutil.rmtree(extract_dir, ignore_errors=True)
                raise
            log(f"Import: extracted {len(members)} binary patch file(s) "
                "from the bundle")
            return extract_dir
    except Exception as exc:
        log(f"Import: could not read patches from {bundle_zip.name}: {exc}")
        return None


def _install_bundled_from_extracted(archive_root, modlist_path, staging_path,
                                    collection_slug, revision_number, log
                                    ) -> "list[str]":
    """Install the archive's bundled/ folders; returns the staging folders used
    (the caller must get them into the mod index - see _install_bundled_assets)."""
    import re as _re
    import shutil as _shutil
    import configparser as _cpi
    slug = (collection_slug or "").strip()
    rev_str = str(int(revision_number)) if revision_number is not None else ""
    bundled_root = archive_root / "bundled"
    if not bundled_root.is_dir():
        return []
    bundle_folders = [p for p in sorted(bundled_root.iterdir()) if p.is_dir()]
    if not bundle_folders:
        return []
    log(f"Collection bundled-cache: installing {len(bundle_folders)} bundled folder(s)")
    # fileExpression/name → (optional, phase) from the archive's own manifest,
    # so bundled meta.ini gets the same collectionOptional/collectionPhase
    # stamps as Nexus-sourced mods.
    bundle_flags: "dict[str, tuple[bool, int]]" = {}
    try:
        cj = json.loads((archive_root / "collection.json").read_text(encoding="utf-8"))
        for m in cj.get("mods") or []:
            src = m.get("source") or {}
            if (src.get("type") or "").lower() != "bundle":
                continue
            try:
                _ph = int(m.get("phase") or 0)
            except (TypeError, ValueError):
                _ph = 0
            flags = (bool(m.get("optional")), _ph)
            for key in (src.get("fileExpression"), m.get("name")):
                if key:
                    bundle_flags.setdefault(str(key).lower(), flags)
    except Exception:
        pass
    staging_lower_map = ({p.name.lower(): p.name for p in staging_path.iterdir() if p.is_dir()}
                         if staging_path.exists() else {})
    bundled_meta_map = _installed_bundled_meta_map(staging_path, slug)
    new_mod_names: list[str] = []
    for src_folder in bundle_folders:
        raw_name = src_folder.name
        clean = _re.sub(r"[^\w\s\-]", "", raw_name).strip().replace(" ", "_") or raw_name
        if clean.lower() in staging_lower_map:
            new_mod_names.append(staging_lower_map[clean.lower()])
            continue
        if raw_name.lower() in bundled_meta_map:
            new_mod_names.append(bundled_meta_map[raw_name.lower()])
            continue
        dest = staging_path / clean
        if dest.exists():
            _shutil.rmtree(dest, ignore_errors=True)
        _shutil.copytree(str(src_folder), str(dest))
        cp = _cpi.ConfigParser()
        general = {"modname": raw_name, "installationfile": raw_name,
                   "fromCollection": slug, "fromCollectionBundled": "true"}
        if rev_str:
            general["fromCollectionRevision"] = rev_str
        _opt, _ph = bundle_flags.get(raw_name.lower(), (False, 0))
        if _opt:
            general["collectionOptional"] = "true"
        if _ph:
            general["collectionPhase"] = str(_ph)
        cp["General"] = general
        try:
            with open(dest / "meta.ini", "w", encoding="utf-8") as mf:
                cp.write(mf)
        except Exception:
            pass
        new_mod_names.append(clean)
        log(f"Collection bundled-cache: installed '{raw_name}' → '{clean}'")
    if new_mod_names and modlist_path.is_file():
        try:
            existing = read_modlist(modlist_path)
            existing_lower = {e.name.lower() for e in existing}
            prepend = [ModEntry(name=n, enabled=True, locked=False)
                       for n in new_mod_names if n.lower() not in existing_lower]
            if prepend:
                write_modlist(modlist_path, prepend + existing)
                log(f"Collection bundled-cache: prepended {len(prepend)} bundled mod(s)")
        except Exception as exc:
            log(f"Collection bundled-cache: modlist update failed: {exc}")
    return new_mod_names


def _apply_collection_binary_patches(archive_root, collection_schema, staging_path,
                                     install_results, collection_slug,
                                     revision_number, log):
    from Utils.collection_patches import apply_collection_patches
    staging_lower = ({p.name.lower(): p.name for p in staging_path.iterdir() if p.is_dir()}
                     if staging_path.exists() else {})

    def _folder_for(schema_entry):
        src = schema_entry.get("source") or {}
        fid = src.get("fileId")
        if fid is not None:
            folder = install_results.get(int(fid))
            if folder:
                return folder
        schema_name = schema_entry.get("name") or ""
        if schema_name:
            return staging_lower.get(schema_name.lower())
        return None

    slug = (collection_slug or "").strip()
    rev_str = str(int(revision_number)) if revision_number is not None else None
    result = apply_collection_patches(
        archive_root=archive_root, collection_schema=collection_schema,
        staging_path=staging_path, mod_folder_for=_folder_for, log_fn=log,
        collection_slug=slug, collection_revision=rev_str)
    if (result.applied or result.crc_mismatch or result.missing_diff
            or result.missing_target or result.failed):
        log(f"Collection patches: applied={result.applied}, "
            f"crc_mismatch={result.crc_mismatch}, missing_diff={result.missing_diff}, "
            f"missing_target={result.missing_target}, failed={result.failed}")


def _apply_collection_ini_tweaks(archive_root, profile_dir, game, log):
    from Utils.collection_ini_tweaks import GAME_INI_TARGETS, apply_collection_ini_tweaks
    if not (archive_root / "INI Tweaks").is_dir():
        return
    try:
        from Games.Bethesda.bethesda_ini import _read_ini_key, _set_ini_key
    except Exception as exc:
        log(f"Collection INI tweaks: INI helpers unavailable ({exc}) - skipped")
        return
    prefix_ini_dir = None
    get_mygames = getattr(game, "_mygames_path", None)
    if callable(get_mygames):
        try:
            prefix_ini_dir = get_mygames()
        except Exception:
            prefix_ini_dir = None
    game_name = getattr(game, "name", "") or ""
    allowed_targets = GAME_INI_TARGETS.get(game_name)
    ini_target_dir = profile_dir
    profile_name = profile_dir.name
    get_ini_dir = getattr(game, "_profile_ini_dir", None)
    if callable(get_ini_dir):
        try:
            ini_target_dir = get_ini_dir(profile_name)
            ini_target_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            log(f"Collection INI tweaks: could not resolve profile 'ini files' "
                f"folder ({exc}) - using profile root")
            ini_target_dir = profile_dir
    if not getattr(game, "profile_ini_files", False):
        try:
            game.set_profile_ini_files(True)
            log("Collection INI tweaks: enabled profile-specific INI files")
        except Exception as exc:
            log(f"Collection INI tweaks: could not enable profile INI files ({exc})")
    result = apply_collection_ini_tweaks(
        archive_root=archive_root, profile_dir=ini_target_dir,
        prefix_ini_dir=prefix_ini_dir, set_ini_key=_set_ini_key,
        read_ini_key=_read_ini_key, log_fn=log, allowed_targets=allowed_targets)
    if result.files_processed or result.skipped:
        log(f"Collection INI tweaks: files={result.files_processed}, "
            f"added={result.keys_added}, changed={result.keys_changed}, "
            f"unchanged={result.keys_unchanged}, skipped={result.skipped}")


def _run_step3b(game, api, profile_dir, staging_path, collection_schema,
                download_link_path, collection_slug, revision_number,
                install_results, log, *, local_bundle_zip=""
                ) -> "tuple[list[str], dict | None]":
    """Install bundled folders + apply binary patches + INI tweaks from the cached
    collection archive. Runs after modlist is written, before LOOT. Returns
    ``(bundled_folders, amethyst_state)``: the bundled staging folders (the
    caller must add them to the mod index) and the archive's ``Amethyst/``
    profile-fidelity data (None when absent - Vortex-authored collections).

    A local ``.amethyst`` import has no cached collection archive - its binary
    patches come out of the bundle zip itself (*local_bundle_zip*); the zip's
    bundled mods/profile files are handled by the caller afterwards."""
    import shutil as _shutil
    archive_root = _ensure_collection_archive_extracted(
        game, api, collection_slug, revision_number, download_link_path or "", log)
    if archive_root is None and local_bundle_zip:
        archive_root = _extract_local_bundle_patches(
            game, local_bundle_zip, log)
    if archive_root is None:
        return [], None
    modlist_path = profile_dir / "modlist.txt"
    bundled: list[str] = []
    amethyst_state = None
    try:
        try:
            bundled = _install_bundled_from_extracted(
                archive_root, modlist_path, staging_path, collection_slug,
                revision_number, log) or []
        except Exception as exc:
            log(f"Collection install: bundled step failed: {exc}")
        try:
            _apply_collection_binary_patches(
                archive_root, collection_schema, staging_path, install_results,
                collection_slug, revision_number, log)
        except Exception as exc:
            log(f"Collection install: patches step failed: {exc}")
        try:
            _apply_collection_ini_tweaks(archive_root, profile_dir, game, log)
        except Exception as exc:
            log(f"Collection install: INI tweaks step failed: {exc}")
        try:
            amethyst_state = _read_amethyst_export_data(archive_root, log)
        except Exception as exc:
            log(f"Collection install: Amethyst state read failed: {exc}")
    finally:
        _shutil.rmtree(archive_root, ignore_errors=True)
    return bundled, amethyst_state


# ---------------------------------------------------------------------------
# Append reconcile (ported; used only on the append path - not yet wired by Qt).
# ---------------------------------------------------------------------------
def _append_reconcile_modlist(modlist_path, install_order, pre_existing, log):
    """Re-apply the collection's load order but only reposition mods newly
    installed by this run; every pre-existing mod keeps its position + state.
    Ported from CollectionsDialog._append_reconcile_modlist."""
    from Utils.modlist import modlist_lock
    with modlist_lock(modlist_path):
        try:
            existing = read_modlist(modlist_path) if modlist_path.is_file() else []
        except Exception:
            existing = []
        _ord = [(k, f) for k, f in install_order]
        _new_names = {f for _, f in _ord if f.lower() not in pre_existing}
        # Keep pre-existing entries where they are; drop the freshly-installed
        # ones so we can reinsert them in collection order.
        kept = [e for e in existing if e.name not in _new_names]
        new_entries = [ModEntry(name=f, enabled=True, locked=False)
                       for _, f in sorted(_ord, key=lambda x: x[0])
                       if f in _new_names]
        # Insert the new mods at the top (highest priority) preserving kept order.
        write_modlist(modlist_path, new_entries + kept)
    log(f"Collection append: placed {len(new_entries)} new mod(s), "
        f"preserved {len(kept)} existing entrie(s)")


def _reconcile_update_modlist(modlist_path, install_order, update_context, log):
    """Rebuild modlist.txt after a collection UPDATE install.

    Preserves separators and the user's existing load order for mods that are
    still in the new revision. New mods (installed during this run that weren't
    in the pre-update snapshot) are inserted relative to their schema-defined
    neighbours; mods with no schema position go at the top of the list.

    ``install_order`` is the sorted list of ``(schema_pos, folder_name)`` pairs
    the installer produced. ``update_context["snapshot"]`` is the pre-removal
    modlist (order minus the mods removed during the update). Verbatim port of
    ``CollectionsDialog._reconcile_update_modlist``."""
    snapshot: "list[ModEntry]" = list(update_context.get("snapshot") or [])

    # Existing snapshot folder names (non-separator) - the mods staying put.
    snapshot_folder_lower: set[str] = {
        e.name.lower() for e in snapshot if not e.is_separator
    }

    # Partition install_order into "already in snapshot" (no-op, order preserved)
    # vs "new" (need insertion).
    new_folders: "list[tuple[int, str]]" = [
        (pos, folder) for pos, folder in install_order
        if folder.lower() not in snapshot_folder_lower
    ]

    # Split new folders by whether they have a defined schema position.
    unplaced: "list[str]" = []
    placeable: "list[tuple[int, str]]" = []
    for pos, folder in new_folders:
        if pos < 0:
            unplaced.append(folder)
        else:
            placeable.append((pos, folder))
    placeable.sort(key=lambda x: x[0])

    result: list = list(snapshot)  # copy
    sorted_io = sorted(install_order, key=lambda x: x[0])

    def _find_result_index(folder_lower: str) -> int:
        for i, e in enumerate(result):
            if not e.is_separator and e.name.lower() == folder_lower:
                return i
        return -1

    for pos, folder in placeable:
        # Right neighbour: first folder in sorted_io with pos > this pos that is
        # currently present in result (and not this same folder).
        insert_idx = None
        for npos, nfolder in sorted_io:
            if npos <= pos or nfolder == folder:
                continue
            idx = _find_result_index(nfolder.lower())
            if idx >= 0:
                insert_idx = idx
                break
        if insert_idx is None:
            # Left neighbour: last folder with pos < this pos in result.
            left_candidates = [
                (npos, nfolder) for npos, nfolder in sorted_io
                if npos < pos and nfolder != folder
            ]
            for npos, nfolder in sorted(left_candidates, key=lambda x: -x[0]):
                idx = _find_result_index(nfolder.lower())
                if idx >= 0:
                    insert_idx = idx + 1
                    break
        if insert_idx is None:
            insert_idx = 0
        result.insert(insert_idx, ModEntry(name=folder, enabled=True, locked=False))

    # Unplaced (no schema position) go at the very top.
    for folder in reversed(unplaced):
        result.insert(0, ModEntry(name=folder, enabled=True, locked=False))

    # Force-enable every mod entry we're writing - update never leaves a mod
    # disabled. Separators keep their locked/enabled state.
    for e in result:
        if not e.is_separator:
            e.enabled = True

    write_modlist(modlist_path, result)
    log(f"Collection update: reconciled modlist.txt "
        f"({len(snapshot)} preserved, {len(placeable)} inserted, "
        f"{len(unplaced)} unplaced at top)")
