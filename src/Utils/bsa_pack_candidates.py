"""Rank mods by how much they would gain from being packed into a BSA / BA2.

Packing trades loose files for archived ones, which cuts the number of files the
engine stats at load. The catch is that an archived file loses to *any* loose
file from *any* mod, so a mod that currently wins a conflict stops winning the
moment it is packed. This module answers "which mods are safe to pack, and which
would gain the most", so the user does not have to audit hundreds of mods by eye.

A mod is disqualified from the safe bucket when it wins a contested file - either
against another mod's loose file (``filemap.txt`` says it wins a key more than
one enabled mod ships) or against a file inside another enabled mod's archive
(the engine mounts archives first, so the loose copy wins outright today).

GUI-free by design (see the sibling ``plugin_audit_core`` / ``skygen_core``):
takes explicit ``progress_fn`` / ``log_fn`` callbacks and imports no toolkit, so
it stays headlessly testable and safe to call from a worker thread.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from Utils.archive_rules import is_packable, texture_extensions_for_game
from Utils.bsa_pack_ops import (
    archive_kind_for_game,
    find_pack_trigger_plugin,
    game_id_of,
    is_packable_mod,
    mod_has_archive,
)

# Bucket names, in display order.
BUCKET_SAFE = "safe"
BUCKET_CARE = "care"
BUCKET_REPACK = "repack"
BUCKET_TOOBIG = "toobig"
BUCKET_SKIP = "skip"

BUCKET_ORDER = (BUCKET_SAFE, BUCKET_CARE, BUCKET_REPACK, BUCKET_TOOBIG,
                BUCKET_SKIP)

# Format ceilings, mirroring what the writers actually enforce.
#
# BSA: v104 packs file data_offset as uint32 and v105 kept that field, so an
# archive past 4 GiB is unreadable either way (bsa_writer raises there). The
# per-file size field is 30 bits, and bsa_writer raises rather than skipping the
# file - so one oversized file blocks the whole pack.
_BSA_ARCHIVE_MAX = 0xFFFFFFFF          # 4 GiB
_BSA_FILE_MAX = 0x3FFFFFFF             # ~1 GiB
# BA2: data_offset is u64, so there is no archive-wide offset ceiling; only the
# u32 packed/unpacked size fields bound a single file.
_BA2_ARCHIVE_MAX = None
_BA2_FILE_MAX = 0xFFFFFFFF


def _limits(kind: str) -> tuple[int | None, int]:
    """(archive ceiling or None, per-file ceiling) for ``"bsa"`` / ``"ba2"``."""
    if kind == "ba2":
        return _BA2_ARCHIVE_MAX, _BA2_FILE_MAX
    return _BSA_ARCHIVE_MAX, _BSA_FILE_MAX


@dataclass
class Candidate:
    """One mod's packing assessment. ``packable_count`` is the ranking score."""
    mod_name: str
    packable_count: int = 0
    packable_bytes: int = 0
    winning_count: int = 0
    has_archive: bool = False
    archived_count: int = 0
    needs_stub: bool = False
    bucket: str = BUCKET_SKIP
    # Splitting textures into a sibling archive is what keeps this mod under the
    # format ceiling - the pack dialog's "Separate textures archive" tick.
    needs_split: bool = False
    # Files whose own size field cannot hold them. The writer raises instead of
    # skipping, so even one of these blocks the whole pack.
    oversize_files: int = 0
    texture_bytes: int = 0


def resolve_paths(game, profile: str) -> tuple[Path, Path, Path]:
    """(staging, profile_dir, index_path) for *profile*, as the app lays them out.

    The mod index lives next to the shared mods folder, not inside the profile -
    see the stray-index sweep in deploy_pipeline.
    """
    try:
        staging = Path(game.get_effective_mod_staging_path())
    except Exception:
        staging = Path(game.get_mod_staging_path())
    profile_dir = game.get_profile_root() / "profiles" / (profile or "default")
    return staging, profile_dir, staging.parent / "modindex.bin"


def _archive_owners(bsa_index) -> dict[str, set[str]]:
    """{archived file path: mods whose archives ship it}, lowercase fwd-slash.

    Archive-internal paths are stored in the same key space as loose rel_keys,
    so the result compares directly against a mod's index keys.
    """
    owners: dict[str, set[str]] = {}
    for mod_name, archives in (bsa_index or {}).items():
        for _name, _mtime, paths in archives:
            for p in paths:
                owners.setdefault(p, set()).add(mod_name)
    return owners


def analyse(
    game,
    staging: Path,
    profile_dir: Path | None,
    index_path: Path | None,
    *,
    progress_fn: Callable[[float], None] | None = None,
    log_fn: Callable[[str], None] | None = None,
) -> list[Candidate]:
    """Assess every enabled mod for packing. Blocking - run on a worker thread.

    Returns candidates sorted by packable file count, descending. An empty list
    means the game has no archive format we can write (Starfield, FO76,
    Morrowind, non-Bethesda) or the profile has no mod index yet.
    """
    def _log(msg: str) -> None:
        if log_fn:
            log_fn(msg)

    kind = archive_kind_for_game(game)
    if kind is None:
        _log("this game has no BSA/BA2 format we can write - nothing to assess")
        return []
    from Utils.modlist import read_modlist
    from Utils.filegraph_service import FileGraphService, source_path

    game_id = game_id_of(game)
    staging = Path(staging)
    if profile_dir is None:
        _log("no active profile - nothing to assess")
        return []
    library = FileGraphService.open_library(game, profile_dir, log_fn=log_fn)
    library.ensure_ready(profile_dir)
    profile = library.open_profile(profile_dir)
    profile.reconcile(operation_hint={"kind": "pack_analysis"})
    snapshot = profile.snapshot()

    # bsa_pack_ops.is_packable_mod screens the bare pseudo-mod names; the index
    # and filemap use the bracketed sentinels, so screen those too.
    from Utils.filegraph_constants import OVERWRITE_NAME, ROOT_FOLDER_NAME
    pseudo = {OVERWRITE_NAME, ROOT_FOLDER_NAME}
    enabled = [
        e.name for e in read_modlist(profile_dir / "modlist.txt")
        if not e.is_separator and e.enabled
        and is_packable_mod(e.name) and e.name not in pseudo
    ] if profile_dir is not None else []
    if not enabled:
        _log("no enabled mods in this profile")
        return []

    archive_max, file_max = _limits(kind)
    # Bare extensions (no dot) so the per-file test is a plain suffix compare.
    tex_exts = {e.lstrip(".")
                for e in texture_extensions_for_game(game_id)}

    out: list[Candidate] = []
    total = len(enabled)
    for i, mod_name in enumerate(enabled):
        if progress_fn:
            progress_fn((i + 1) / total)
        normal = [
            record for record in snapshot.mod_files(mod_name)
            if record.candidate_id and record.provider_kind != "archive_member"
        ]
        mod_dir = staging / mod_name

        own_archives = snapshot.archive_files(mod_name)
        archived_count = len(own_archives)
        # Fall back to a disk check: an archive the TOC parser choked on is
        # absent from the index, and "already ships an archive" must not depend
        # on our ability to read it.
        has_archive = bool(own_archives) or mod_has_archive(mod_dir, kind)

        packable_count = 0
        packable_bytes = 0
        texture_bytes = 0
        oversize_files = 0
        winning_count = 0
        for record in normal:
            rel_key = record.legacy_rel.replace("\\", "/").lower()
            # Root-routed files deploy to the game root, never into a Data
            # archive, so they are outside this question entirely.
            if record.namespace == "root":
                continue
            # A BSA stores everything under a folder; bsa_writer._collect_files
            # silently drops root-level files, so they can never be packed.
            if "/" not in rel_key:
                continue
            if not is_packable(rel_key, game_id):
                continue
            packable_count += 1
            try:
                fsize = source_path(game, mod_name, record.source_rel).stat().st_size
            except OSError:
                fsize = 0
            packable_bytes += fsize
            if fsize > file_max:
                oversize_files += 1
            if rel_key.rsplit(".", 1)[-1] in tex_exts:
                texture_bytes += fsize
            # Any surviving conflict win (loose or archive opponent) becomes
            # load-order-dependent once this provider moves into an archive.
            if record.conflict_status > 0:
                winning_count += 1

        # Does this fit the format? Textures can go to a sibling archive, which
        # is the writer's own suggested remedy for an over-size pack, so a mod
        # over the ceiling still qualifies when both halves fit on their own.
        needs_split = False
        over_limit = oversize_files > 0
        if not over_limit and archive_max is not None:
            if packable_bytes > archive_max:
                main_bytes = packable_bytes - texture_bytes
                if main_bytes <= archive_max and texture_bytes <= archive_max:
                    needs_split = True
                else:
                    over_limit = True

        if packable_count == 0:
            bucket = BUCKET_SKIP
        elif over_limit:
            bucket = BUCKET_TOOBIG
        elif has_archive:
            bucket = BUCKET_REPACK
        elif winning_count:
            bucket = BUCKET_CARE
        else:
            bucket = BUCKET_SAFE

        out.append(Candidate(
            mod_name=mod_name,
            packable_count=packable_count,
            packable_bytes=packable_bytes,
            winning_count=winning_count,
            has_archive=has_archive,
            archived_count=archived_count,
            # No real plugin to name the archive after means the pack writes a
            # stub .esp so the engine auto-loads it.
            needs_stub=(bucket in (BUCKET_SAFE, BUCKET_CARE)
                        and find_pack_trigger_plugin(
                            mod_dir, mod_name,
                            getattr(game, "plugin_extensions", None)) is None),
            bucket=bucket,
            needs_split=needs_split,
            oversize_files=oversize_files,
            texture_bytes=texture_bytes,
        ))

    out.sort(key=lambda c: (-c.packable_count, c.mod_name.lower()))
    _log(f"assessed {len(out)} enabled mod(s) for {kind.upper()} packing")
    return out


def group_by_bucket(candidates: list[Candidate]) -> dict[str, list[Candidate]]:
    """Split an :func:`analyse` result into its buckets, order preserved."""
    out: dict[str, list[Candidate]] = {b: [] for b in BUCKET_ORDER}
    for c in candidates:
        out.setdefault(c.bucket, []).append(c)
    return out
