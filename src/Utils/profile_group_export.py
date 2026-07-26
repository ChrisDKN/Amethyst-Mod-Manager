"""
profile_group_export.py
Export/import a whole Profile Group — the group's own definition (name +
ordered member list) plus every member profile — as either a ``.amethyst``
zip container or a compact copy-pasteable code.

This module is purely an orchestration layer: it reuses the existing
single-profile export/import primitives (``Utils.profile_export``,
``Utils.collection_install``, ``Utils.game_helpers``, ``Nexus.nexus_api``)
member-by-member. See those modules' own docstrings for what each mechanism
actually does.

Container format (file path): a zip with a top-level ``group.json``
(``{"AmethystGroupManifest": true, "name": ..., "members": [...]}``) plus
each member's normal single-profile export tree
(``manifest.json``/``mods/``/``overwrite/``/``profile/``) namespaced under
``"<member_name>/"``. Detected by content (presence of ``group.json``), not a
new file extension — a Profile Group export is still a plain ``.amethyst``
zip, importable through the same file picker.

Container format (code path): ``Utils.profile_export.encode_group_manifest``'s
``{"AmethystGroupManifest": true, "info": {...}, "members": [{"name",
"manifest"}, ...]}``, using ``GROUP_CODE_PREFIX`` so it can be told apart from
a single-profile code before decoding.

Import ("as Group + separate profiles") runs entirely headless: each
member's manifest already carries every FOMOD/BAIN choice baked in from
export, so ``Utils.collection_install.run_collection_install`` can be called
synchronously in a loop with no interactive review screen per member — it is
toolkit-neutral (no Qt import) and its callbacks/control dataclasses have
safe no-op defaults.
"""

from __future__ import annotations

import json
import zipfile as _zip
from pathlib import Path

from Utils import profile_export
from Utils.profile_groups import get_members

GROUP_JSON_NAME = "group.json"


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def _member_export_manifest(game, member_dir: Path, app_version: str):
    """Build (manifest, bundle_names) for one member profile, applying the
    automatic default source rule: "nexus" when the mod has a valid mod ID +
    file ID (small, re-fetched at import — matches today's single-profile
    default), else "bundle" (embed its actual files, so nothing without a
    Nexus ID is silently dropped). Switches *game*'s active profile dir to
    *member_dir* first — load_rows()/build_manifest() resolve staging/meta
    off the currently active profile."""
    from Utils.modlist import read_modlist
    game.set_active_profile_dir(member_dir)
    game.load_paths()
    modlist_path = member_dir / "modlist.txt"
    entries = ([e for e in reversed(read_modlist(modlist_path)) if not e.is_separator]
              if modlist_path.is_file() else [])
    rows = profile_export.load_rows(entries, game)
    for row in rows:
        row["source"] = ("nexus" if (row.get("mod_id") and profile_export._row_file_id(row))
                         else "bundle")
    game_domain = getattr(game, "nexus_game_domain", "") or ""
    manifest = profile_export.build_manifest(
        rows, game_domain, app_version, game_name=getattr(game, "name", None),
        profile_dir=member_dir)
    bundle_names = [r["name"] for r in rows if r.get("source") == "bundle"]
    return manifest, bundle_names


def write_group_amethyst(game, group_profile_dir: Path, out_path, app_version: str,
                         *, progress_cb=None) -> Path:
    """Export a whole Profile Group (its definition + every member profile)
    into one ``.amethyst`` zip. See module docstring for the container shape."""
    out_path = Path(out_path)
    if out_path.suffix.lower() not in (".zip", ".amethyst"):
        out_path = out_path.with_suffix(".amethyst")

    group_name = group_profile_dir.name
    members = get_members(group_profile_dir)
    profiles_dir = group_profile_dir.parent

    jobs: list[tuple] = [
        (None, GROUP_JSON_NAME, json.dumps(
            {"AmethystGroupManifest": True, "name": group_name, "members": list(members)},
            indent=2).encode("utf-8")),
    ]

    prev_active = getattr(game, "_active_profile_dir", None)
    try:
        for member_name in members:
            member_dir = profiles_dir / member_name
            if not member_dir.is_dir():
                continue
            manifest, bundle_names = _member_export_manifest(game, member_dir, app_version)
            jobs.extend(profile_export._collect_amethyst_jobs(
                manifest, staging_root=member_dir / "mods",
                overwrite_root=member_dir / "overwrite", profile_dir=member_dir,
                bundle_names=bundle_names, arc_prefix=member_name))
    finally:
        game.set_active_profile_dir(prev_active)
        game.load_paths()

    profile_export._write_zip_jobs(out_path, jobs, progress_cb)
    return out_path


def build_group_code_manifest(game, group_profile_dir: Path, app_version: str) -> dict:
    """Build a Profile Group share-code envelope: the group's name/member
    order + each member's own ``build_code_manifest()`` output (Nexus-
    resolvable mods only — the same limitation a single-profile code already
    has, just per member)."""
    from Utils.modlist import read_modlist
    group_name = group_profile_dir.name
    members = get_members(group_profile_dir)
    profiles_dir = group_profile_dir.parent
    member_manifests = []

    prev_active = getattr(game, "_active_profile_dir", None)
    try:
        for member_name in members:
            member_dir = profiles_dir / member_name
            if not member_dir.is_dir():
                continue
            game.set_active_profile_dir(member_dir)
            game.load_paths()
            modlist_path = member_dir / "modlist.txt"
            entries = read_modlist(modlist_path) if modlist_path.is_file() else []
            manifest = profile_export.build_code_manifest(
                entries, game, app_version, profile_name=member_name)
            member_manifests.append({"name": member_name, "manifest": manifest})
    finally:
        game.set_active_profile_dir(prev_active)
        game.load_paths()

    return {
        "AmethystGroupManifest": True,
        "info": {"name": group_name},
        "members": member_manifests,
    }


# ---------------------------------------------------------------------------
# Import — detection helpers
# ---------------------------------------------------------------------------

def read_group_manifest(src_path) -> "dict | None":
    """If *src_path* is a Profile Group export (a zip containing a top-level
    ``group.json``), return ``{"name": ..., "members": [...]}``; else
    ``None``. Cheap content-based detection — call before falling back to
    the ordinary single-profile ``Utils.profile_export.read_manifest()``."""
    src_path = Path(src_path)
    if not _zip.is_zipfile(src_path):
        return None
    with _zip.ZipFile(src_path, "r") as zf:
        if GROUP_JSON_NAME not in zf.namelist():
            return None
        with zf.open(GROUP_JSON_NAME) as fh:
            try:
                data = json.loads(fh.read().decode("utf-8"))
            except Exception:
                return None
    if not isinstance(data, dict) or not data.get("members"):
        return None
    return data


def _read_member_manifest_from_zip(src_path: Path, member_name: str) -> "dict | None":
    arcname = f"{member_name}/manifest.json"
    with _zip.ZipFile(src_path, "r") as zf:
        if arcname not in zf.namelist():
            return None
        with zf.open(arcname) as fh:
            try:
                return json.loads(fh.read().decode("utf-8"))
            except Exception:
                return None


def _manifest_has_nexus_mods(manifest: dict) -> bool:
    """True if any mod in *manifest* needs a live Nexus download (has a real
    mod ID + file ID and isn't bundled/off-site)."""
    for m in (manifest or {}).get("mods", []):
        src = m.get("source") or {}
        src_type = (src.get("type") or "nexus").lower()
        if src.get("bundle") is True or src_type in ("bundle", "browse", "direct"):
            continue
        if src.get("modId") and src.get("fileId"):
            return True
    return False


def group_bundle_needs_nexus_login(src_path) -> bool:
    """True if any member inside a Profile Group ``.amethyst`` container has
    a Nexus-sourced mod — pre-scan so the import flow only requires a login
    when actually needed (a fully-bundled group imports fully offline)."""
    group_manifest = read_group_manifest(src_path)
    if group_manifest is None:
        return False
    src_path = Path(src_path)
    for member_name in group_manifest.get("members") or []:
        manifest = _read_member_manifest_from_zip(src_path, member_name)
        if manifest and _manifest_has_nexus_mods(manifest):
            return True
    return False


def group_code_needs_nexus_login(group_manifest: dict) -> bool:
    """Same check as :func:`group_bundle_needs_nexus_login`, for an already-
    decoded group code envelope."""
    for entry in group_manifest.get("members") or []:
        if _manifest_has_nexus_mods(entry.get("manifest") or {}):
            return True
    return False


# ---------------------------------------------------------------------------
# Import — "as Group + separate profiles" (headless per-member loop)
# ---------------------------------------------------------------------------

def _import_member(game, api, downloader, member_name: str, member_manifest: dict,
                   *, src_zip=None, log_fn=None) -> "tuple[str, Path]":
    """Import one member's manifest into a fresh profile-specific profile.
    Returns (actual_local_name, profile_dir). *src_zip*, if given, is the
    outer group zip to extract this member's bundled mod files from
    (via ``install_local_bundle``'s ``member_prefix``)."""
    from Utils.game_helpers import _create_profile, dedupe_profile_name
    from Nexus.nexus_api import manifest_to_collection_mods
    from Utils.collection_install import (
        run_collection_install, CollectionInstallCallbacks, CollectionInstallControl)
    log = log_fn or (lambda _m: None)

    local_name = dedupe_profile_name(game.name, member_name)
    profile_dir = Path(_create_profile(game.name, local_name, profile_specific_mods=True))

    mods, offsite, _total = manifest_to_collection_mods(member_manifest)
    if offsite:
        log(f"Profile Group import: '{local_name}' has {len(offsite)} off-site "
            "mod(s) with no automatic download — install these manually.")

    run_collection_install(
        game=game, api=api, downloader=downloader, mods=mods,
        download_link_path="", profile_dir=profile_dir, old_profile_dir=None,
        collection_slug=f"group_import_{local_name}",
        collection_schema_cache=member_manifest,
        callbacks=CollectionInstallCallbacks(on_log=log),
        control=CollectionInstallControl())

    if src_zip is not None:
        profile_export.install_local_bundle(
            src_zip, profile_dir, profile_dir / "mods", profile_dir / "overwrite",
            log_fn=log, member_prefix=member_name)

    return local_name, profile_dir


def import_group_bundle(game, api, downloader, src_path, group_manifest: dict,
                        *, log_fn=None, progress_fn=None) -> str:
    """Import a Profile Group ``.amethyst`` container: reconstruct every
    member as its own profile-specific profile, then create the group tying
    them together. *group_manifest* is the dict already returned by
    :func:`read_group_manifest` (the caller decides whether to route here or
    to the "combined" path). Returns the created group's name."""
    from Utils.profile_groups import create_group
    from Utils.game_helpers import dedupe_profile_name
    log = log_fn or (lambda _m: None)
    src_path = Path(src_path)
    member_names = list(group_manifest.get("members") or [])
    total = len(member_names)
    local_members: list[str] = []

    for i, member_name in enumerate(member_names):
        if progress_fn:
            progress_fn(i, total, member_name)
        manifest = _read_member_manifest_from_zip(src_path, member_name)
        if manifest is None:
            log(f"Profile Group import: '{member_name}' manifest missing from "
                "the archive — skipping.")
            continue
        local_name, _profile_dir = _import_member(
            game, api, downloader, member_name, manifest, src_zip=src_path, log_fn=log)
        local_members.append(local_name)
    if progress_fn:
        progress_fn(total, total, "")

    if not local_members:
        raise ValueError("No members could be imported from this Profile Group.")
    group_name = dedupe_profile_name(
        game.name, group_manifest.get("name") or src_path.stem)
    create_group(game, group_name, local_members)
    return group_name


def import_group_code(game, api, downloader, group_manifest: dict, *, log_fn=None,
                      progress_fn=None) -> str:
    """Import a Profile Group share code (already decoded via
    ``Utils.profile_export.decode_group_manifest``). Code manifests are never
    bundled, so there's no per-member bundle-extraction step. Returns the
    created group's name."""
    from Utils.profile_groups import create_group
    from Utils.game_helpers import dedupe_profile_name
    log = log_fn or (lambda _m: None)
    member_entries = list(group_manifest.get("members") or [])
    total = len(member_entries)
    local_members: list[str] = []

    for i, entry in enumerate(member_entries):
        member_name = entry.get("name") or f"Member{i + 1}"
        manifest = entry.get("manifest") or {}
        if progress_fn:
            progress_fn(i, total, member_name)
        local_name, _profile_dir = _import_member(
            game, api, downloader, member_name, manifest, src_zip=None, log_fn=log)
        local_members.append(local_name)
    if progress_fn:
        progress_fn(total, total, "")

    if not local_members:
        raise ValueError("No members could be imported from this Profile Group code.")
    info = group_manifest.get("info") or {}
    group_name = dedupe_profile_name(game.name, info.get("name") or "ImportedGroup")
    create_group(game, group_name, local_members)
    return group_name


# ---------------------------------------------------------------------------
# Import — "as single combined profile"
# ---------------------------------------------------------------------------

def _mod_key(mod: dict) -> str:
    """Identity key for dedup: Nexus-sourced mods key on modId ALONE (not
    modId+fileId) so two members' copies of "the same" mod collapse even
    under a different local name AND a different downloaded file/version —
    e.g. one collection's "Ridgeside Village" and another's "Ridgeside
    Village 2.5.17" are the same Nexus mod ID with a different fileId, and
    must merge into one entry rather than both installing side by side.
    Whichever enabled copy has the strictly newer version wins (see
    merge_group_manifests), falling back to member priority order on a tie
    or an unreadable version. Everything else keys on name. Mirrors
    ``Utils.profile_groups._mod_identity_and_version``, the equivalent key
    for the live (non-export) group merge."""
    src = mod.get("source") or {}
    if src.get("modId"):
        return f"nexus:{src['modId']}"
    return f"name:{mod.get('name', '')}"


def merge_group_manifests(
        member_manifests: "list[tuple[str, dict]]") -> "tuple[dict, dict[str, str]]":
    """Merge several members' manifests (*member_manifests* in priority
    order, index 0 highest, matching ``group_members``) into ONE combined
    manifest — the same dedup + OR-enabled semantics as
    ``Utils.profile_groups._merge_modlist``, applied to manifest mod-entry
    dicts instead of ``ModEntry``. Whose fields the merged entry keeps is
    decided the same way that function decides whose files win: among
    members that enable it, whichever has the strictly newer ``version``
    string wins even from a lower-priority member; a tie, or either version
    unparseable/missing, falls back to member priority order.

    A manifest's ``mods`` array is stored low-priority-first (``mods[-1]`` =
    top of modlist, matching ``build_code_manifest``'s convention), so each
    member's array is reversed to high-to-low before block-concatenating in
    priority order, then reversed back at the end.

    Returns ``(combined, home_member)`` — *combined* is one ordinary
    single-profile manifest (no group-specific keys), safe to feed straight
    into the existing single-profile import path. *home_member* maps each
    surviving mod's ``name`` to the member profile its winning entry came
    from — needed by :func:`build_combined_import_bundle` to know which
    member's subtree a bundle-sourced mod's actual files should be pulled
    from (a code-only merge has no bundled mods, so this map is unused
    there)."""
    from Nexus.nexus_update_checker import _parse_version

    per_member: list[tuple[str, list[dict]]] = [
        (name, list(reversed(manifest.get("mods") or [])))  # now high-to-low
        for name, manifest in member_manifests
    ]

    # Pass 1: OR the enabled state per identity key. Among members that
    # enable it, the "champion" (whose fields the merged entry keeps) starts
    # as the first (highest-priority) enabler and is overtaken only by a
    # strictly newer version from a lower-priority member.
    enabled_union: dict[str, bool] = {}
    champions: dict[str, dict] = {}
    for member_name, mods in per_member:
        for mod in mods:
            k = _mod_key(mod)
            is_enabled = bool(mod.get("enabled", True))
            if not is_enabled:
                enabled_union.setdefault(k, False)
                continue
            enabled_union[k] = True
            parsed = _parse_version(mod.get("version") or "")
            champ = champions.get(k)
            if champ is None or (parsed is not None and champ["version"] is not None
                                 and parsed > champ["version"]):
                champions[k] = {"mod": mod, "member": member_name, "version": parsed}
    home_mod = {k: v["mod"] for k, v in champions.items()}
    home_member_by_key = {k: v["member"] for k, v in champions.items()}

    # Pass 2: first-occurrence position (high-to-low), substituting the OR'd
    # enabled state. *member_name* here mirrors home_mod's own fallback
    # (dict.get(k, mod) falls back to the current iteration's mod when the
    # mod was never enabled anywhere), so home_member stays consistent with
    # which member's dict actually supplied final_mod's fields.
    merged_hi_to_lo: list[dict] = []
    seen: set[str] = set()
    home_member: dict[str, str] = {}
    for member_name, mods in per_member:
        for mod in mods:
            k = _mod_key(mod)
            if k in seen:
                continue
            seen.add(k)
            final_mod = dict(home_mod.get(k, mod))
            if enabled_union.get(k, bool(mod.get("enabled", True))):
                final_mod.pop("enabled", None)   # enabled entries stay implicit
            else:
                final_mod["enabled"] = False
            merged_hi_to_lo.append(final_mod)
            home_member[final_mod["name"]] = home_member_by_key.get(k, member_name)

    merged_mods_low_to_high = list(reversed(merged_hi_to_lo))

    # Separators: flatten (drop per-member grouping, same simplification
    # Utils.profile_groups._merge_modlist already applies), dedup by name.
    seps: list[dict] = []
    seen_sep_names: set[str] = set()
    for _name, manifest in member_manifests:
        for sep in manifest.get("modlistSeparators") or []:
            sep_name = sep.get("name")
            if sep_name in seen_sep_names:
                continue
            seen_sep_names.add(sep_name)
            seps.append(sep)

    first_manifest = member_manifests[0][1] if member_manifests else {}
    combined = {
        "AmethystManifest": True,
        "info": dict(first_manifest.get("info") or {}),
        "mods": merged_mods_low_to_high,
    }
    if seps:
        combined["modlistSeparators"] = seps
    return combined, home_member


def build_combined_import_bundle(src_zip, merged_manifest: dict,
                                 home_member: "dict[str, str]",
                                 member_order: "list[str]", out_path) -> Path:
    """For "Import as single combined profile" (file path): flatten the
    surviving bundle-sourced mods' actual files (each pulled from its owning
    member's namespaced subtree, per *home_member*) plus a merged
    ``overwrite/`` tree and one member's ``profile/`` state files into ONE
    ordinary single-profile-shaped zip — so the result can be handed to the
    existing, unmodified ``install_local_bundle()`` (no ``member_prefix``
    needed) via the normal single-profile bundle-zip import path.

    *member_order* — priority order (index 0 highest), used to pick which
    member's ``overwrite/`` files win on a name collision (higher priority
    wins) and whose ``profile/`` state files to carry over (the highest-
    priority member's)."""
    src_zip = Path(src_zip)
    out_path = Path(out_path)
    bundle_names = {
        m["name"] for m in merged_manifest.get("mods", [])
        if (m.get("source") or {}).get("bundle") is True
    }
    with _zip.ZipFile(src_zip, "r") as zin:
        names = zin.namelist()
        with _zip.ZipFile(out_path, "w", compression=_zip.ZIP_DEFLATED) as zout:
            written: set[str] = set()
            for mod_name in bundle_names:
                member = home_member.get(mod_name)
                if not member:
                    continue
                prefix = f"{member}/mods/{mod_name}/"
                for n in names:
                    if n.startswith(prefix) and not n.endswith("/"):
                        rel = n[len(member) + 1:]
                        zout.writestr(rel, zin.read(n))
                        written.add(rel)
            # overwrite/: highest-priority member's files win a collision —
            # walk priority order and skip a relative path already written.
            for member in member_order:
                prefix = f"{member}/overwrite/"
                for n in names:
                    if n.startswith(prefix) and not n.endswith("/"):
                        rel = n[len(member) + 1:]
                        if rel in written:
                            continue
                        zout.writestr(rel, zin.read(n))
                        written.add(rel)
            # profile/ state files: highest-priority member only.
            if member_order:
                top_member = member_order[0]
                prefix = f"{top_member}/profile/"
                for n in names:
                    if n.startswith(prefix) and not n.endswith("/"):
                        rel = n[len(top_member) + 1:]
                        zout.writestr(rel, zin.read(n))
    return out_path
