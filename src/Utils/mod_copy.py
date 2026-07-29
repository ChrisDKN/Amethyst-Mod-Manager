"""Copy / move a mod's staging folder to another profile — tkinter-free.

Ported from the Tk ``modlist_panel._copy_mod_to_profile`` / ``_copy_mods_to_profile``
file-work so the Qt (or any) GUI can reuse it. Collision decisions (replace / rename
/ skip) are the CALLER's: pass ``dest_name`` to rename, or pre-delete the existing
folder to replace.
"""

from __future__ import annotations

import shutil
from pathlib import Path


def resolve_target_staging(game, target_profile_dir: Path) -> Path:
    """The staging folder a mod should be copied INTO for *target_profile_dir*:
    the profile's own ``mods/`` when it uses profile-specific mods, else the
    game's shared staging folder."""
    from Utils.profile_state import profile_uses_specific_mods
    if profile_uses_specific_mods(target_profile_dir):
        return target_profile_dir / "mods"
    return game.get_mod_staging_path()


def mod_exists_in_profile(target_staging: Path, name: str) -> bool:
    """True if a mod folder named *name* already exists in *target_staging*."""
    return (target_staging / name).is_dir()


def copy_fomod_choice(src_profile_dir: Path, dst_profile_dir: Path,
                      mod_name: str, dest_name: "str | None" = None) -> None:
    """Copy a mod's saved installer-choice JSON (FOMOD or BAIN) between profiles,
    if present. *dest_name* renames the choice file to match a renamed folder.
    Port of Tk ``_copy_fomod_choice``."""
    out = dest_name or mod_name
    for sub in ("fomod", "bain"):
        src = src_profile_dir / sub / f"{mod_name}.json"
        if not src.is_file():
            continue
        dst_dir = dst_profile_dir / sub
        dst = dst_dir / f"{out}.json"
        # An in-place conversion (same profile becoming/leaving profile-specific)
        # calls this with src_profile_dir == dst_profile_dir and no rename — the
        # sidecar is already exactly where it needs to be, and shutil.copy2 would
        # raise SameFileError trying to copy a file onto itself.
        if src.resolve() == dst.resolve():
            continue
        dst_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src), str(dst))


def copy_mod_to_profile(src_staging: Path, src_profile_dir: Path,
                        target_staging: Path, target_profile_dir: Path,
                        mod_name: str, enabled: bool = True, *,
                        dest_name: "str | None" = None,
                        game=None, register: bool = True) -> "str | None":
    """Copy the ``<src_staging>/<mod_name>`` folder into *target_staging* (as
    *dest_name* if given), copy its FOMOD/BAIN choice, and (when *register*)
    prepend it to the target profile's ``modlist.txt``. Returns the staged dest
    name, or None on failure.

    The caller must resolve collisions first: pass ``dest_name`` to install under
    a new name, or delete the existing folder to replace it. If the dest folder
    already exists (and wasn't handled), the copy fails and returns None.

    *game* (optional but recommended) lets us add the copied mod to the target
    staging's ``modindex.bin`` incrementally — without it, the target profile's
    cached index is stale and the mod is invisible to the filemap rebuild
    (no deploy / no plugins / no conflicts) until a full Refresh.

    *register* controls the per-mod modlist prepend. When copying SEVERAL mods,
    pass ``register=False`` and call :func:`register_mods_in_modlist` once with
    the full ordered block afterwards — prepending each mod individually reverses
    the group's relative order (the source-topmost mod is prepended first and
    ends up lowest), matching the Tk single-block prepend."""
    src_folder = Path(src_staging) / mod_name
    if not src_folder.is_dir():
        return None
    out = dest_name or mod_name
    dest_folder = Path(target_staging) / out
    if dest_folder.exists():
        return None
    try:
        dest_folder.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(str(src_folder), str(dest_folder))
    except Exception:
        return None
    copy_fomod_choice(src_profile_dir, target_profile_dir, mod_name,
                      dest_name=out)
    if game is not None:
        _update_target_index(game, target_staging, target_profile_dir,
                             out, dest_folder)
    if register:
        _register_in_modlist(target_profile_dir / "modlist.txt", out, enabled)
    return out


def _update_target_index(game, target_staging: Path, target_profile_dir: Path,
                         mod_name: str, dest_root: Path) -> None:
    """Incrementally add the just-copied mod to the target staging's
    ``modindex.bin`` (+ ``bsa_index.bin``).

    Without this the target profile's cached index is stale after a copy/move
    and the mod is invisible to the filemap rebuild — no deploy, no plugins,
    no conflicts — until a full Refresh rescans the index from disk.

    The index/BSA-index live next to the *target* staging (= where
    ``build_filemap`` reads them for that profile), which may not be the active
    profile. We reuse :func:`filemap.rescan_mods_in_index` so the single-mod
    entry is written with EXACTLY the same strip-prefix / extension rules a full
    Refresh would apply (raw index keys must match, or a later reorder-only
    rebuild reads mismatched paths).

    Best-effort: any failure just leaves the index stale and is silently
    swallowed (the next full Refresh rescans it)."""
    try:
        from Utils.filemap import rescan_mods_in_index
        from Utils.deploy import load_per_mod_strip_prefixes
        index_dir = Path(target_staging).parent
        # A root-flagged mod must NOT have strip_prefixes applied. The flag
        # lives in the mod's own meta.ini (copied verbatim into dest_root); read
        # it directly — the target modlist isn't updated until after this call.
        root_mods = None
        try:
            from Nexus.nexus_meta import read_meta
            if read_meta(Path(dest_root) / "meta.ini").root_folder:
                root_mods = {mod_name}
        except Exception:
            root_mods = None
        rescan_mods_in_index(
            index_dir / "modindex.bin", Path(target_staging), [mod_name],
            strip_prefixes=set(getattr(game, "mod_folder_strip_prefixes", None) or ()) or None,
            per_mod_strip_prefixes=load_per_mod_strip_prefixes(target_profile_dir),
            allowed_extensions=set(getattr(game, "mod_install_extensions", None) or ()) or None,
            normalize_folder_case=getattr(game, "normalize_folder_case", True),
            root_folder_mods=root_mods,
        )
        archive_exts = frozenset(
            getattr(game, "archive_extensions", frozenset()) or frozenset())
        if archive_exts:
            from Utils.bsa_filemap import update_bsa_index
            update_bsa_index(index_dir / "bsa_index.bin",
                             mod_name, dest_root, archive_exts)
    except Exception:
        # Best-effort — the next full index rescan (Refresh) will pick it up.
        pass


def _register_in_modlist(target_modlist: Path, name: str, enabled: bool) -> None:
    """Prepend *name* to the target modlist (dedup by name). No-op if already
    present."""
    register_mods_in_modlist(target_modlist, [(name, enabled)])


def register_mods_in_modlist(target_modlist: Path,
                             mods: "list[tuple[str, bool]]") -> None:
    """Prepend *mods* — ordered highest-priority-first — to the target modlist as
    a single block (dedup by name), preserving their relative order. Mirrors the
    Tk single-block prepend; do NOT call this per-mod in a loop, which reverses
    the group."""
    from Utils.modlist import read_modlist, write_modlist, ModEntry, modlist_lock
    with modlist_lock(target_modlist):
        try:
            entries = read_modlist(target_modlist) if target_modlist.exists() else []
        except Exception:
            entries = []
        existing_names = {e.name for e in entries}
        new_entries = [
            ModEntry(name=name, enabled=enabled, locked=False)
            for name, enabled in mods
            if name not in existing_names
        ]
        if new_entries:
            write_modlist(target_modlist, new_entries + entries)
