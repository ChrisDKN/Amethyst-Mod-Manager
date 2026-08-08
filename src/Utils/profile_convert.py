"""
profile_convert.py
Convert a shared-pool profile to profile-specific mods (Profile Group
members must be profile-specific - see Utils/profile_groups.py).

Clones every listed mod from the shared pool into the profile's own mods/
(bulk assets hardlinked, in-place-editable files copied - see _COPY_EXTS),
creates overwrite/ and Root_Folder/, flips profile_specific_mods, and
rebuilds the profile's indexes. The shared pool is untouched; a clone
failure aborts and rolls back. No reverse direction.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from Utils.app_log import app_log
from Utils.modlist import read_modlist
from Utils.profile_state import merge_profile_settings, profile_uses_specific_mods


class ConvertError(ValueError):
    """Raised when a profile cannot be converted (already specific, group…)."""


# Files that get REWRITTEN IN PLACE by the app or its tools (meta.ini writes,
# text-editor saves, xEdit's cleaned-plugin rescue). Hardlinking those would
# leak every later edit between the converted profile and the shared pool, so
# they are always real copies; bulk assets (meshes/textures/archives - the
# actual disk weight) stay hardlinked.
_COPY_EXTS = frozenset({
    ".ini", ".json", ".txt", ".xml", ".cfg", ".conf", ".toml", ".yaml",
    ".yml", ".esp", ".esm", ".esl",
})


def _clone_tree(src: Path, dst: Path) -> None:
    """copytree that hardlinks large static assets and real-copies files that
    may later be edited in place (see _COPY_EXTS); cross-FS falls back to
    copies throughout."""

    def _link_or_copy(s: str, d: str) -> None:
        if os.path.splitext(s)[1].lower() in _COPY_EXTS:
            shutil.copy2(s, d)
            return
        try:
            os.link(s, d)
        except OSError:
            shutil.copy2(s, d)

    shutil.copytree(str(src), str(dst), copy_function=_link_or_copy,
                    symlinks=False)


def convert_profile_to_specific(game, profile_dir: Path, *, log_fn=None,
                                progress_fn=None) -> list[str]:
    """Convert *profile_dir* (shared-pool) to profile-specific mods.

    Returns the list of mod folder names cloned. Raises ConvertError when the
    profile is already profile-specific, is a Profile Group, or doesn't exist.
    *progress_fn(done, total)* is optional (clone progress for a UI worker).
    """
    log = log_fn or app_log
    if not profile_dir.is_dir():
        raise ConvertError(f"Profile '{profile_dir.name}' does not exist.")
    if profile_uses_specific_mods(profile_dir):
        raise ConvertError(f"'{profile_dir.name}' already uses profile-specific mods.")
    from Utils.profile_groups import is_group
    if is_group(profile_dir):
        raise ConvertError("A Profile Group cannot be converted.")

    shared_staging = Path(game.get_mod_staging_path())
    dest_staging = profile_dir / "mods"
    dest_staging.mkdir(exist_ok=True)
    (profile_dir / "overwrite").mkdir(exist_ok=True)
    (profile_dir / "Root_Folder").mkdir(exist_ok=True)

    entries = [e for e in read_modlist(profile_dir / "modlist.txt")
               if not e.is_separator]
    cloned: list[str] = []
    missing: list[str] = []
    created_this_run: list[Path] = []
    total = len(entries)
    for i, e in enumerate(entries):
        src = shared_staging / e.name
        dst = dest_staging / e.name
        if dst.exists():
            # A completed clone (retry after interruption) - trustworthy
            # because clones land under a temp name and only a FINISHED tree
            # is renamed into place.
            cloned.append(e.name)
        elif src.is_dir() and not src.is_symlink():
            tmp = dest_staging / f".convert-tmp-{e.name}"
            try:
                if tmp.exists():
                    shutil.rmtree(tmp)
                _clone_tree(src, tmp)
                tmp.rename(dst)
                cloned.append(e.name)
                created_this_run.append(dst)
            except Exception as exc:
                # A failed clone is FATAL: converting with holes would make
                # the profile silently drop those mods forever. Roll back
                # everything this run created and leave the profile shared.
                shutil.rmtree(tmp, ignore_errors=True)
                for d in created_this_run:
                    shutil.rmtree(d, ignore_errors=True)
                raise ConvertError(
                    f"Cloning '{e.name}' failed ({exc}) - conversion aborted, "
                    f"profile left unchanged.") from exc
        else:
            missing.append(e.name)
        if progress_fn is not None:
            try:
                progress_fn(i + 1, total)
            except Exception:
                pass
    if missing:
        log(f"Convert: {len(missing)} modlist entr(y/ies) had no folder in the "
            f"shared pool and were not cloned: "
            f"{', '.join(missing[:8])}{'…' if len(missing) > 8 else ''}")

    # Flip the flag only after every clone landed (helpers that resolve the
    # profile's staging must see the new layout for the index rebuild below).
    merge_profile_settings(profile_dir, {"profile_specific_mods": True})

    try:
        from Nexus.nexus_meta import collect_root_flagged_mods
        from Utils.deploy_shared import load_per_mod_strip_prefixes
        from Utils.filemap import rebuild_mod_index
        rf_mods = collect_root_flagged_mods(profile_dir / "modlist.txt",
                                            dest_staging, log_fn=log)
        rebuild_mod_index(
            profile_dir / "modindex.bin", dest_staging,
            strip_prefixes=set(getattr(game, "mod_folder_strip_prefixes", None) or ()) or None,
            per_mod_strip_prefixes=load_per_mod_strip_prefixes(profile_dir),
            allowed_extensions=set(getattr(game, "mod_install_extensions", None) or ()) or None,
            normalize_folder_case=getattr(game, "normalize_folder_case", True),
            root_folder_mods=set(rf_mods or ()) or None,
            log_fn=log,
        )
    except Exception as exc:
        log(f"Convert: index rebuild failed ({exc}) - run Refresh to rebuild.")
    archive_exts = frozenset(getattr(game, "archive_extensions", frozenset()) or frozenset())
    if archive_exts:
        try:
            from Utils.bsa_filemap import rebuild_bsa_index
            rebuild_bsa_index(profile_dir / "bsa_index.bin", dest_staging,
                              archive_exts, log_fn=log)
        except Exception:
            pass

    log(f"Convert: '{profile_dir.name}' now uses profile-specific mods "
        f"({len(cloned)} mod(s) cloned from the shared pool).")
    return cloned
