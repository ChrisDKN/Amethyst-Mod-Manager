"""GUI-neutral orchestration for BSA and BA2 packing and unpacking.

The pure-Python archive backend already lives in :mod:`Utils.bsa.writer`,
:mod:`Utils.ba2.writer`, :mod:`Utils.bsa.extract`, :mod:`Utils.ba2.extract`.
This module hosts the *decision* logic that sits above them - which archive
kind a game packs, how the archive is named, whether a stub plugin is needed,
which files to keep loose, the split-textures multi-pass, the delete-loose
cleanup, and the unpack grouping.

It is a straight port of the Tk ``gui/plugin_panel.py`` pack/unpack methods and
``gui/bsa_unpack_overlay.py``'s grouping, so the Tk and Qt front-ends stay in
lockstep. Nothing here imports tkinter or PySide6 - both GUIs drive it the same
way (resolve a plan, show a menu, run the worker with progress/cancel callbacks).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import filecmp
import os
import shutil
import tempfile
from pathlib import Path
from typing import Callable

from Utils.archives.paths import extraction_paths
from Utils.archives.rules import is_packable
from Utils.atomic_write import atomic_writer

from Utils.ba2.writer import (
    Ba2WriteError,
    ba2_version_for_game,
    write_ba2,
    write_ba2_textures,
)
from Utils.bsa.writer import (
    BsaWriteError,
    bsa_version_for_game,
    is_our_stub_plugin,
    write_bsa,
    write_stub_plugin,
)

ProgressCb = Callable[[int, int, str], None]
CancelCb = Callable[[], bool]

# Pseudo-mods that never pack.
_OVERWRITE_NAME = "Overwrite"
_ROOT_FOLDER_NAME = "Root_Folder"
_DEFAULT_PLUGIN_EXTS = (".esp", ".esm", ".esl")
# Archive suffixes a same-stem archive can carry while still auto-loading with
# its plugin. ``Foo - Main.ba2`` and ``Foo - Textures.ba2`` both belong to Foo.
_ARCHIVE_SIDECAR_SUFFIXES = (" - Main", " - Textures")


# ---------------------------------------------------------------------------
# Game / plugin helpers
# ---------------------------------------------------------------------------
def game_id_of(game) -> str:
    """Best-effort stable id for a game object."""
    return getattr(game, "game_id", None) or type(game).__name__


def archive_kind_for_game(game) -> str | None:
    """Return ``"bsa"``, ``"ba2"`` or ``None`` for the given game.

    ``"bsa"`` for games that pack into BSA v103/v104/v105, ``"ba2"`` for the
    FO4-family BA2, ``None`` for games we can't pack for (Starfield, FO76,
    Morrowind, non-Bethesda). Port of Tk ``_archive_kind_for_current_game``.
    """
    if game is None:
        return None
    game_id = game_id_of(game)
    archive_exts = getattr(game, "archive_extensions", None) or frozenset()
    if bsa_version_for_game(game_id) is not None and ".bsa" in archive_exts:
        return "bsa"
    if ba2_version_for_game(game_id) is not None and ".ba2" in archive_exts:
        return "ba2"
    return None


def is_packable_mod(mod_name: str | None) -> bool:
    """True when *mod_name* is a normal mod (not a pseudo-mod)."""
    return bool(mod_name) and mod_name not in (_OVERWRITE_NAME, _ROOT_FOLDER_NAME)


def find_pack_trigger_plugin(
    mod_dir: Path, mod_name: str, plugin_exts=None,
) -> Path | None:
    """Real plugin in ``mod_dir`` (root only) the archive should be named after.

    An archive only auto-loads when a same-stem plugin sits in the load order;
    naming the archive after an existing plugin avoids a redundant stub. Stubs
    we generated are ignored. Prefer a plugin whose stem matches the mod folder,
    else the first sorted by name (deterministic). Port of Tk
    ``_find_pack_trigger_plugin``.
    """
    exts = {e.lower() for e in (plugin_exts or _DEFAULT_PLUGIN_EXTS)}
    plugins: list[Path] = []
    try:
        for p in mod_dir.iterdir():
            if not p.is_file() or p.suffix.lower() not in exts:
                continue
            if is_our_stub_plugin(p):
                continue
            plugins.append(p)
    except OSError:
        return None
    if not plugins:
        return None
    mod_lower = mod_name.lower()
    plugins.sort(key=lambda p: p.name.lower())
    for p in plugins:
        if p.stem.lower() == mod_lower:
            return p
    return plugins[0]


def is_profile_deployed(game, profile_dir: Path | None) -> bool:
    """True iff *profile_dir* is the profile currently deployed to game_root.

    Pack/Unpack mutate mod-folder contents; doing that while the profile has
    files hard-linked into game_root staleness the deploy log/snapshot and the
    next restore misroutes tracked files into overwrite/. Callers gate on this
    and ask the user to Restore first. Port of Tk ``_is_current_profile_deployed``.
    """
    if game is None or not getattr(game, "is_configured", lambda: False)():
        return False
    if profile_dir is None:
        return False
    try:
        if not game.get_deploy_active():
            return False
        return game.get_last_deployed_profile() == profile_dir.name
    except Exception:
        return False


def mod_has_archive(mod_dir: Path, kind: str) -> bool:
    """True if *mod_dir* contains at least one archive of ``kind`` (bsa/ba2)."""
    suffix = "." + kind
    try:
        return any(
            p.is_file() and p.suffix.lower() == suffix
            for p in mod_dir.iterdir()
        )
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Pack plan + skip-winners
# ---------------------------------------------------------------------------
@dataclass
class PackPlan:
    """Everything resolved before showing the pack options menu."""
    kind: str                              # "bsa" | "ba2"
    game_id: str
    bsa_version: int | None                # BSA format version (None for BA2)
    mod_dir: Path
    mod_name: str
    archive_stem: str
    archive_path: Path
    # For BA2 this is always set; for BSA it's the potential split-textures
    # sibling, only used when the user ticks "Separate textures archive".
    archive_textures_path: Path | None
    stub_plugin_path: Path | None
    existing_any: bool


def plan_pack(
    game, mod_dir: Path, mod_name: str, kind: str, plugin_exts=None,
) -> PackPlan:
    """Resolve archive naming + stub decision for a pack. GUI-free.

    Mirrors the setup block of Tk ``_on_pack_bsa_click`` (before the dialog).
    """
    game_id = game_id_of(game)
    bsa_version = bsa_version_for_game(game_id) if kind == "bsa" else None

    existing_plugin = find_pack_trigger_plugin(mod_dir, mod_name, plugin_exts)
    archive_stem = existing_plugin.stem if existing_plugin is not None else mod_name

    if kind == "ba2":
        archive_path = mod_dir / f"{archive_stem} - Main.ba2"
        archive_textures_path: Path | None = mod_dir / f"{archive_stem} - Textures.ba2"
    else:
        archive_path = mod_dir / f"{archive_stem}.bsa"
        archive_textures_path = mod_dir / f"{archive_stem} - Textures.bsa"

    # An archive only auto-loads if a same-named plugin exists. If the mod ships
    # no real plugin (existing_plugin is None - also covers "only a prior stub"),
    # (re)stamp a minimal stub named after the mod folder.
    stub_plugin_path = None if existing_plugin is not None else mod_dir / f"{mod_name}.esp"

    # "existing" only counts the archives we might actually write. For BSA the
    # textures sibling is only written on the split opt-in, but flagging it as
    # existing here is harmless (worst case an extra overwrite warning).
    existing_any = archive_path.exists() or (
        archive_textures_path is not None and archive_textures_path.exists()
    )

    return PackPlan(
        kind=kind,
        game_id=game_id,
        bsa_version=bsa_version,
        mod_dir=mod_dir,
        mod_name=mod_name,
        archive_stem=archive_stem,
        archive_path=archive_path,
        archive_textures_path=archive_textures_path,
        stub_plugin_path=stub_plugin_path,
        existing_any=existing_any,
    )


def compute_skip_winners(
    snapshot, mod_name: str,
) -> set[str]:
    """Raw mod-folder keys this mod wins a real loose/archive conflict on.

    Packing these would make the file lose to a later loose provider, so the
    caller adds them to the exclusion set. Archive writers walk the raw mod
    folder, which can differ from the deploy path after routing or stripping.
    """
    if snapshot is None:
        raise ValueError("Conflict data is not ready")
    return {
        record.source_rel.decode("utf-8", "surrogateescape").replace("\\", "/").lower()
        for record in snapshot.mod_files(mod_name)
        if (record.namespace in ("normal", "root") and record.winning
            and record.conflict_status > 0 and record.source_rel)
    }


# ---------------------------------------------------------------------------
# Pack worker
# ---------------------------------------------------------------------------
@dataclass
class PackResult:
    main_count: int = 0
    main_size: int = 0
    tex_count: int = 0
    tex_size: int = 0
    packed_keys: list[str] = field(default_factory=list)


class PackCancelled(Exception):
    """Raised by :func:`run_pack` when the cancel callback returns True."""


def _write_pack(
    plan: PackPlan,
    *,
    excluded_keys: frozenset[str] = frozenset(),
    split_textures: bool = False,
    compress: bool = True,
    progress: ProgressCb | None = None,
    cancel: CancelCb | None = None,
) -> PackResult:
    """Write the archive(s) for *plan* and, if needed, the stub plugin.

    Blocking; meant to run on a worker thread. Raises :class:`PackCancelled` on
    cancel, or :class:`BsaWriteError` / :class:`Ba2WriteError` on real failure.
    Port of the ``_worker`` body in Tk ``_on_pack_bsa_click``.
    """
    res = PackResult()
    kind = plan.kind
    mod_dir = plan.mod_dir
    game_id = plan.game_id

    try:
        if kind == "ba2":
            # GNRL pass - everything except .dds. "no packable" is non-fatal
            # (a textures-only mod still gets a DX10 archive below).
            try:
                res.main_count, res.main_size, packed_main = write_ba2(
                    plan.archive_path, mod_dir,
                    game_id=game_id, compress=compress,
                    excluded_keys=excluded_keys, exclude_textures=True,
                    progress=progress, cancel=cancel,
                )
                res.packed_keys.extend(packed_main)
            except Ba2WriteError as exc:
                if "no packable" not in str(exc).lower():
                    raise
            # DX10 pass - only .dds. "no packable"/"no dx10" non-fatal.
            try:
                res.tex_count, res.tex_size, packed_tex = write_ba2_textures(
                    plan.archive_textures_path, mod_dir,
                    game_id=game_id, compress=compress,
                    excluded_keys=excluded_keys,
                    progress=progress, cancel=cancel,
                )
                res.packed_keys.extend(packed_tex)
            except Ba2WriteError as exc:
                msg = str(exc).lower()
                if "no packable" not in msg and "no dx10" not in msg:
                    raise
            if res.main_count == 0 and res.tex_count == 0:
                raise Ba2WriteError("no packable files found")
        else:
            if split_textures and plan.archive_textures_path is not None:
                # Base archive excludes textures; sibling is textures only.
                # Either pass may be empty without aborting the other.
                try:
                    res.main_count, res.main_size, packed_main = write_bsa(
                        plan.archive_path, mod_dir,
                        version=plan.bsa_version, game_id=game_id,
                        compress=compress, excluded_keys=excluded_keys,
                        texture_mode="exclude", progress=progress, cancel=cancel,
                    )
                    res.packed_keys.extend(packed_main)
                except BsaWriteError as exc:
                    if "no packable" not in str(exc).lower():
                        raise
                try:
                    res.tex_count, res.tex_size, packed_tex = write_bsa(
                        plan.archive_textures_path, mod_dir,
                        version=plan.bsa_version, game_id=game_id,
                        compress=compress, excluded_keys=excluded_keys,
                        texture_mode="only", progress=progress, cancel=cancel,
                    )
                    res.packed_keys.extend(packed_tex)
                except BsaWriteError as exc:
                    if "no packable" not in str(exc).lower():
                        raise
                if res.main_count == 0 and res.tex_count == 0:
                    raise BsaWriteError("no packable files found")
            else:
                res.main_count, res.main_size, packed_main = write_bsa(
                    plan.archive_path, mod_dir,
                    version=plan.bsa_version, game_id=game_id,
                    compress=compress, excluded_keys=excluded_keys,
                    progress=progress, cancel=cancel,
                )
                res.packed_keys.extend(packed_main)

        if plan.stub_plugin_path is not None:
            write_stub_plugin(plan.stub_plugin_path, game_id=game_id)
    except (BsaWriteError, Ba2WriteError) as exc:
        if str(exc) == "cancelled":
            raise PackCancelled() from exc
        raise
    return res


def _stage_archives(archives, destination, *, progress=None, cancel=None):
    from Utils.ba2.extract import extract_ba2
    from Utils.bsa.extract import extract_bsa

    members = set()
    destination.mkdir(parents=True, exist_ok=True)
    for archive in archives:
        if cancel is not None and cancel():
            raise InterruptedError("cancelled")
        with tempfile.TemporaryDirectory(prefix=".archive-", dir=destination.parent) as tmp:
            folder = Path(tmp)
            extract = extract_ba2 if archive.suffix.lower() == ".ba2" else extract_bsa
            _, names = extract(archive, folder, progress=progress, cancel=cancel)
            targets = extraction_paths(destination, names)
            for name, target in zip(names, targets):
                source = folder / name
                if target.exists():
                    if not target.is_file() or not filecmp.cmp(source, target, shallow=False):
                        raise ValueError(f"Archives contain conflicting versions of {name}; unpack them separately")
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    source.replace(target)
                members.add(name)
    return members


def _commit_pack(outputs):
    backup_dir = Path(tempfile.mkdtemp(prefix=".amethyst-pack-backup-", dir=outputs[0][1].parent))
    backups, installed = [], []
    try:
        for source, target in outputs:
            if target.exists():
                backup = backup_dir / target.name
                target.replace(backup)
                backups.append((backup, target))
            if source is not None:
                source.replace(target)
                installed.append(target)
    except OSError:
        try:
            for target in reversed(installed):
                target.unlink()
            for backup, target in reversed(backups):
                backup.replace(target)
        except OSError as rollback_error:
            raise OSError(f"Archive rollback failed; original backups remain in {backup_dir}") from rollback_error
        shutil.rmtree(backup_dir, ignore_errors=True)
        raise
    shutil.rmtree(backup_dir, ignore_errors=True)


def run_pack(
    plan: PackPlan,
    *,
    excluded_keys: frozenset[str] = frozenset(),
    split_textures: bool = False,
    compress: bool = True,
    progress: ProgressCb | None = None,
    cancel: CancelCb | None = None,
) -> PackResult:
    """Merge existing archives with enabled loose files before replacing them."""
    from Utils.ba2.extract import Ba2ExtractError, index_ba2
    from Utils.bsa.extract import BsaExtractError, index_bsa

    error = Ba2WriteError if plan.kind == "ba2" else BsaWriteError
    try:
        with tempfile.TemporaryDirectory(prefix=".amethyst-pack-", dir=plan.mod_dir.parent) as tmp:
            workspace = Path(tmp)
            source_dir = workspace / "source"
            archive_paths = [p for p in (plan.archive_path, plan.archive_textures_path) if p is not None]
            archived = _stage_archives([p for p in archive_paths if p.exists()], source_dir,
                                       progress=progress, cancel=cancel)
            loose = set()
            for directory, dirs, files in os.walk(plan.mod_dir):
                dirs[:] = [d for d in dirs if not d.startswith(".")]
                for filename in files:
                    if cancel is not None and cancel():
                        raise PackCancelled()
                    source = Path(directory) / filename
                    name = source.relative_to(plan.mod_dir).as_posix().lower()
                    if name in excluded_keys or not is_packable(name, plan.game_id):
                        continue
                    if name in loose:
                        raise error(f"Duplicate loose file path: {name}")
                    loose.add(name)
                    target = source_dir / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, target)
            output_dir = workspace / "output"
            output_dir.mkdir()
            staged = replace(plan, mod_dir=source_dir,
                             archive_path=output_dir / plan.archive_path.name,
                             archive_textures_path=(output_dir / plan.archive_textures_path.name
                                                    if plan.archive_textures_path else None),
                             stub_plugin_path=(output_dir / plan.stub_plugin_path.name
                                               if plan.stub_plugin_path else None))
            result = _write_pack(staged, split_textures=split_textures, compress=compress,
                                 progress=progress, cancel=cancel)
            output_members = set()
            for output in (staged.archive_path, staged.archive_textures_path):
                if output is not None and output.exists():
                    entries = index_ba2(output) if plan.kind == "ba2" else index_bsa(output)[1]
                    output_members.update(entries)
            if output_members != set(result.packed_keys):
                raise error("Packed filenames differ from their source paths; originals were kept")
            extraction_paths(workspace / "check-paths", list(output_members))
            missing = archived - output_members
            if missing:
                raise error(f"Cannot preserve {len(missing)} archived file(s), including {min(missing)}; originals were kept")
            if cancel is not None and cancel():
                raise PackCancelled()
            outputs = [(output_dir / p.name if (output_dir / p.name).exists() else None, p)
                       for p in archive_paths]
            if plan.stub_plugin_path is not None:
                outputs.append((staged.stub_plugin_path, plan.stub_plugin_path))
            _commit_pack(outputs)
            result.packed_keys = [name for name in result.packed_keys if name in loose]
            return result
    except (BsaExtractError, Ba2ExtractError, InterruptedError) as exc:
        if str(exc) == "cancelled":
            raise PackCancelled() from exc
        raise error(str(exc)) from exc
    except (OSError, ValueError) as exc:
        raise error(str(exc)) from exc


# ---------------------------------------------------------------------------
# Delete-loose cleanup
# ---------------------------------------------------------------------------
def delete_loose_files(mod_dir: Path, packed_rel_keys: list[str]) -> int:
    """Delete every file in *packed_rel_keys* from *mod_dir*, cleaning empty dirs.

    rel_keys are lowercase forward-slash, but on case-sensitive filesystems the
    on-disk path can have any casing, so each rel_key is resolved segment-by-
    segment against the real listing (parent dirs matched too). Returns the
    number of files actually deleted. Port of Tk ``_delete_loose_files``.
    """
    if not packed_rel_keys:
        return 0

    listing_cache: dict[Path, dict[str, str]] = {}

    def _list_lower(d: Path) -> dict[str, str]:
        cached = listing_cache.get(d)
        if cached is not None:
            return cached
        mapping: dict[str, str] = {}
        try:
            for entry in d.iterdir():
                mapping[entry.name.lower()] = entry.name
        except OSError:
            pass
        listing_cache[d] = mapping
        return mapping

    def _resolve_ci(rel: str) -> Path | None:
        cur = mod_dir
        segments = rel.split("/")
        for i, seg in enumerate(segments):
            names = _list_lower(cur)
            actual = names.get(seg.lower())
            if actual is None:
                return None
            cur = cur / actual
            if i < len(segments) - 1 and not cur.is_dir():
                return None
        return cur

    deleted = 0
    empty_candidate_dirs: set[Path] = set()
    for rel in packed_rel_keys:
        target = mod_dir / rel
        if not target.is_file():
            resolved = _resolve_ci(rel)
            if resolved is None or not resolved.is_file():
                continue
            target = resolved
        try:
            target.unlink()
            deleted += 1
            empty_candidate_dirs.add(target.parent)
            listing_cache.pop(target.parent, None)
        except OSError:
            pass
    # Remove any folders we may have just emptied, walking up to the mod root
    # (but never deleting the mod root itself).
    for d in sorted(empty_candidate_dirs, key=lambda p: -len(p.parts)):
        cur = d
        while cur != mod_dir and cur.is_dir():
            try:
                next(cur.iterdir())
                break  # not empty
            except StopIteration:
                try:
                    cur.rmdir()
                except OSError:
                    break
                cur = cur.parent
            except OSError:
                break
    return deleted


# ---------------------------------------------------------------------------
# Unpack grouping
# ---------------------------------------------------------------------------
@dataclass
class UnpackGroup:
    label: str                 # plugin filename or "(no matching plugin)"
    is_orphan: bool
    archives: list[Path]
    total_bytes: int
    total_files: int           # -1 if any archive failed to parse


def archive_plugin_stem(archive_name: str) -> str:
    """Map an archive filename to its companion plugin's stem (lower).

    ``Foo - Main.ba2`` / ``Foo - Textures.ba2`` / ``Foo.bsa`` → ``foo``.
    """
    stem = Path(archive_name).stem
    for suffix in _ARCHIVE_SIDECAR_SUFFIXES:
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    return stem.lower()


def collect_unpack_groups(mod_dir: Path, plugin_exts=None) -> list[UnpackGroup]:
    """Group archives in *mod_dir* by companion plugin stem, with sizes/counts.

    One group per plugin that has matching archives, plus a trailing "(no
    matching plugin)" bucket for orphan archives. Port of
    ``BsaUnpackOverlay._collect_groups`` / ``_build_group``.
    """
    from Utils.bsa.reader import read_bsa_file_list

    exts = {e.lower() for e in (plugin_exts or _DEFAULT_PLUGIN_EXTS)}
    archives_by_stem: dict[str, list[Path]] = {}
    plugin_by_stem: dict[str, Path] = {}
    try:
        entries = list(mod_dir.iterdir())
    except OSError:
        return []

    for p in entries:
        if not p.is_file():
            continue
        ext = p.suffix.lower()
        if ext in (".bsa", ".ba2"):
            stem = archive_plugin_stem(p.name)
            archives_by_stem.setdefault(stem, []).append(p)
        elif ext in exts:
            key = p.stem.lower()
            if key not in plugin_by_stem or p.name < plugin_by_stem[key].name:
                plugin_by_stem[key] = p

    def _build(label: str, archives: list[Path], *, is_orphan: bool) -> UnpackGroup:
        total_bytes = 0
        total_files = 0
        any_unreadable = False
        for p in archives:
            try:
                total_bytes += p.stat().st_size
            except OSError:
                any_unreadable = True
            try:
                total_files += len(read_bsa_file_list(p))
            except Exception:
                any_unreadable = True
        return UnpackGroup(
            label=label, is_orphan=is_orphan, archives=archives,
            total_bytes=total_bytes,
            total_files=-1 if any_unreadable else total_files,
        )

    groups: list[UnpackGroup] = []
    seen_stems: set[str] = set()
    for key in sorted(plugin_by_stem.keys()):
        plugin = plugin_by_stem[key]
        archives = sorted(archives_by_stem.get(key, []), key=lambda p: p.name.lower())
        seen_stems.add(key)
        if not archives:
            continue
        groups.append(_build(plugin.name, archives, is_orphan=False))

    orphan_archives: list[Path] = []
    for stem, archs in archives_by_stem.items():
        if stem in seen_stems:
            continue
        orphan_archives.extend(archs)
    if orphan_archives:
        orphan_archives.sort(key=lambda p: p.name.lower())
        groups.append(_build("(no matching plugin)", orphan_archives, is_orphan=True))

    return groups


def unpack_kind_label(archives: list[Path]) -> str:
    """Title-friendly kind for a set of archives ("BSA"/"BA2"/"Archive")."""
    suffixes = {p.suffix.lower() for p in archives}
    if suffixes == {".ba2"}:
        return "BA2"
    if suffixes == {".bsa"}:
        return "BSA"
    return "Archive"


# ---------------------------------------------------------------------------
# Unpack worker
# ---------------------------------------------------------------------------
class UnpackCancelled(Exception):
    """Raised by :func:`run_unpack` when the cancel callback returns True."""


def run_unpack(
    archive_paths: list[Path],
    mod_dir: Path,
    *,
    progress: ProgressCb | None = None,
    cancel: CancelCb | None = None,
) -> tuple[int, list[str]]:
    """Extract every archive in *archive_paths* into *mod_dir*.

    Returns ``(new_file_count, available_rel_keys)`` including preserved loose files.
    Blocking; run on a worker
    thread. Raises :class:`UnpackCancelled` on cancel, or the underlying
    extract errors otherwise. Port of the ``_worker`` body in Tk ``_do_unpack_bsa``.
    """
    from Utils.ba2.extract import Ba2ExtractError
    from Utils.bsa.extract import BsaExtractError

    created = []
    try:
        with tempfile.TemporaryDirectory(prefix=".amethyst-unpack-", dir=mod_dir.parent) as tmp:
            source = Path(tmp) / "source"
            names = sorted(_stage_archives(archive_paths, source, progress=progress, cancel=cancel))
            targets = extraction_paths(mod_dir, names)
            archives = {path.resolve() for path in archive_paths}
            for target in targets:
                if target in archives or (target.exists() and not target.is_file()):
                    raise ValueError(f"Cannot extract over {target}")
            for name, target in zip(names, targets):
                if cancel is not None and cancel():
                    raise UnpackCancelled()
                if target.exists():
                    continue
                with (source / name).open("rb") as inp, atomic_writer(target, "wb", encoding=None) as out:
                    shutil.copyfileobj(inp, out)
                created.append(target)
            return len(created), names
    except (BsaExtractError, Ba2ExtractError, InterruptedError, OSError, ValueError, UnpackCancelled) as exc:
        for target in reversed(created):
            target.unlink(missing_ok=True)
        if isinstance(exc, (InterruptedError, UnpackCancelled)) or str(exc) == "cancelled":
            raise UnpackCancelled() from exc
        raise BsaExtractError(str(exc)) from exc


def stub_for_unpack(mod_dir: Path, archive_stem: str) -> tuple[Path, bool]:
    """Return ``(stub_path, is_ours)`` for the ``<archive_stem>.esp`` in *mod_dir*.

    ``is_ours`` is True only when the file exists and looks like a stub we
    generated (safe to delete after unpacking). Used by callers to remove the
    generated stub and word the result message. Port of the stub-handling block
    in Tk ``_do_unpack_bsa``.
    """
    stub = mod_dir / f"{archive_stem}.esp"
    is_ours = stub.is_file() and is_our_stub_plugin(stub)
    return stub, is_ours


def auto_disable_packed_files(
    profile_dir: Path | None, mod_name: str, packed_rel_keys: list[str],
) -> int:
    """Add *packed_rel_keys* to this mod's excluded_mod_files (union, not replace).

    Hides every loose file that was just packed from deploy without deleting it.
    No-op for pseudo-mods or when *profile_dir* is None. Returns the new total
    excluded count. Port of Tk ``_auto_disable_packed_files``.
    """
    if not packed_rel_keys or profile_dir is None or not is_packable_mod(mod_name):
        return 0
    from Utils.profiles.state import read_excluded_mod_files, write_excluded_mod_files

    all_excluded = read_excluded_mod_files(profile_dir, None)
    merged = set(all_excluded.get(mod_name, ())) | set(packed_rel_keys)
    all_excluded[mod_name] = sorted(merged)
    write_excluded_mod_files(profile_dir, all_excluded)
    return len(merged)


def clear_excluded_for_unpack(
    profile_dir: Path | None, mod_name: str, unpacked_rel_keys: list[str],
) -> int:
    """Remove *unpacked_rel_keys* from this mod's excluded_mod_files.

    Brings back files that a previous Pack auto-disabled so they show as enabled
    in the Mod Files tab. No-op for pseudo-mods or when *profile_dir* is None.
    Returns the count re-enabled. Port of Tk ``_clear_excluded_for_unpack``.
    """
    if not unpacked_rel_keys or profile_dir is None or not is_packable_mod(mod_name):
        return 0
    from Utils.profiles.state import read_excluded_mod_files, write_excluded_mod_files

    all_excluded = read_excluded_mod_files(profile_dir, None)
    current = set(all_excluded.get(mod_name, ()))
    if not current:
        return 0
    new_set = current - set(unpacked_rel_keys)
    if new_set == current:
        return 0
    if new_set:
        all_excluded[mod_name] = sorted(new_set)
    else:
        all_excluded.pop(mod_name, None)
    write_excluded_mod_files(profile_dir, all_excluded)
    return len(current) - len(new_set)


def read_excluded_for_mod(profile_dir: Path | None, mod_name: str) -> set[str]:
    """Currently-excluded rel_keys for *mod_name* (empty if none / no profile)."""
    if profile_dir is None:
        return set()
    from Utils.profiles.state import read_excluded_mod_files

    return set(read_excluded_mod_files(profile_dir, None).get(mod_name, ()))


def shared_archive_stem(archive_paths: list[Path]) -> str:
    """Plugin stem shared by a group of archives (strip the sidecar suffixes)."""
    stem = archive_paths[0].stem
    for suffix in _ARCHIVE_SIDECAR_SUFFIXES:
        if stem.endswith(suffix):
            return stem[: -len(suffix)]
    return stem
