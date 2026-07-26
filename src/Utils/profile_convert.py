"""profile_convert.py — convert a profile between "standard" (shared mod
pool) and "profile-specific" (private ``mods/`` folder), either in place or
by copying its mods into a brand-new profile.

Pure orchestration over the existing single-mod-copy primitives in
``Utils.mod_copy`` (``resolve_target_staging``, ``copy_mod_to_profile``,
``register_mods_in_modlist``) — this module adds no new file-copy mechanics
of its own, it just automates the manual "create a profile, then multi-select
mods and Copy/Move them over" workflow those primitives already support one
mod at a time.

Two entry points cover every case, because ``resolve_target_staging``/
``copy_mod_to_profile`` are already direction-agnostic (they resolve
shared-pool vs. profile-specific purely from the *destination* profile's own
flag):

* :func:`copy_mods_to_new_profile` — creates a new profile (either kind) and
  copies mods into it; the source profile is never modified.
* :func:`convert_in_place` — flips an EXISTING profile's own kind, reading
  the direction from its current ``profile_specific_mods`` flag.

Neither function ever deletes a mod from the shared pool that another
standard profile might still reference — copying into a private
profile-specific folder always leaves the shared-pool original alone.
"""

from __future__ import annotations

import filecmp
import shutil
from pathlib import Path

from Utils.mod_copy import (
    resolve_target_staging, copy_mod_to_profile, register_mods_in_modlist,
)
from Utils.modlist import read_modlist, write_modlist, ModEntry
from Utils.profile_state import profile_uses_specific_mods, merge_profile_settings


def _filter_source_entries(profile_dir: Path, mod_names) -> "list[ModEntry]":
    """Non-separator entries from *profile_dir*'s own modlist, restricted to
    *mod_names*, in their existing relative order (index 0 = highest
    priority) with each entry's own enabled state intact."""
    wanted = set(mod_names)
    entries = read_modlist(profile_dir / "modlist.txt")
    return [e for e in entries if not e.is_separator and e.name in wanted]


def copy_mods_to_new_profile(game, source_profile_dir, mod_names, new_profile_name: str,
                             *, new_profile_specific: bool, log_fn=None) -> Path:
    """Create *new_profile_name* (standard or profile-specific, per
    *new_profile_specific*) and copy *mod_names* into it from
    *source_profile_dir* — a physical copy; *source_profile_dir* is never
    modified. Powers both the modlist menu's "New profile…" destination and
    Profile Settings' "copy to a new profile" conversion mode."""
    log = log_fn or (lambda _m: None)
    from Utils.game_helpers import _create_profile
    source_profile_dir = Path(source_profile_dir)
    source_staging = resolve_target_staging(game, source_profile_dir)

    new_profile_dir = _create_profile(
        game.name, new_profile_name, profile_specific_mods=new_profile_specific)
    dest_staging = resolve_target_staging(game, new_profile_dir)
    # Two standard profiles of the same game share the identical physical
    # staging path — nothing to copy, the mod's files are already there.
    same_pool = Path(source_staging).resolve() == Path(dest_staging).resolve()

    ordered = _filter_source_entries(source_profile_dir, mod_names)
    pairs: list[tuple[str, bool]] = []
    for entry in ordered:
        if same_pool:
            pairs.append((entry.name, entry.enabled))
            continue
        out = copy_mod_to_profile(
            source_staging, source_profile_dir, dest_staging, new_profile_dir,
            entry.name, entry.enabled, game=game, register=False)
        if out is None:
            log(f"[convert] skipped '{entry.name}' (missing, or a mod with "
                "that name already exists at the destination).")
            continue
        pairs.append((out, entry.enabled))

    if pairs:
        register_mods_in_modlist(new_profile_dir / "modlist.txt", pairs)
    log(f"[convert] '{new_profile_name}' created with {len(pairs)} mod(s) "
        f"from '{source_profile_dir.name}'.")
    return new_profile_dir


def convert_in_place(game, profile_dir, mod_names, *, log_fn=None) -> None:
    """Flip *profile_dir* between standard and profile-specific in place,
    keeping exactly *mod_names* as this profile's own modlist afterward (its
    other mods simply stop being referenced by this profile — they are never
    deleted from the shared pool). Direction is read from the profile's
    current ``profile_specific_mods`` flag, not chosen by the caller.

    Returns True if the conversion was applied, False if it was aborted (a
    genuine shared-pool name collision on the specific→standard direction —
    see :func:`_convert_specific_to_standard_in_place`). A False return is
    not an error: nothing on the profile was changed."""
    log = log_fn or (lambda _m: None)
    profile_dir = Path(profile_dir)
    if profile_uses_specific_mods(profile_dir):
        return _convert_specific_to_standard_in_place(game, profile_dir, mod_names, log)
    _convert_standard_to_specific_in_place(game, profile_dir, mod_names, log)
    return True


def _convert_standard_to_specific_in_place(game, profile_dir, mod_names, log) -> None:
    # Resolved BEFORE the flag flips below, while the profile is still
    # standard, so this correctly means "the shared pool".
    shared_staging = resolve_target_staging(game, profile_dir)
    ordered = _filter_source_entries(profile_dir, mod_names)

    (profile_dir / "mods").mkdir(exist_ok=True)
    (profile_dir / "overwrite").mkdir(exist_ok=True)
    (profile_dir / "Root_Folder").mkdir(exist_ok=True)
    own_staging = profile_dir / "mods"

    pairs: list[tuple[str, bool]] = []
    for entry in ordered:
        out = copy_mod_to_profile(
            shared_staging, profile_dir, own_staging, profile_dir,
            entry.name, entry.enabled, game=game, register=False)
        if out is None:
            log(f"[convert] skipped '{entry.name}' (missing from the shared pool).")
            continue
        pairs.append((out, entry.enabled))

    merge_profile_settings(profile_dir, {"profile_specific_mods": True})
    write_modlist(profile_dir / "modlist.txt",
                 [ModEntry(name=n, enabled=e, locked=False) for n, e in pairs])
    log(f"[convert] '{profile_dir.name}' is now profile-specific with "
        f"{len(pairs)} mod(s).")


def _dirs_identical(a: Path, b: Path) -> bool:
    """True if *a* and *b* contain exactly the same relative files with
    byte-identical contents — used to tell a harmless "this exact mod is
    already in the shared pool" (e.g. converting back after an earlier
    standard→specific conversion never deleted the shared-pool original)
    from a genuine name collision with different content."""
    a_files = sorted(p.relative_to(a).as_posix() for p in a.rglob("*") if p.is_file())
    b_files = sorted(p.relative_to(b).as_posix() for p in b.rglob("*") if p.is_file())
    if a_files != b_files:
        return False
    return all(filecmp.cmp(a / rel, b / rel, shallow=False) for rel in a_files)


def _convert_specific_to_standard_in_place(game, profile_dir, mod_names, log) -> bool:
    """Copy every wanted mod out of the profile's own ``mods/`` into the
    shared pool, then remove the (now redundant) private folders and flip
    the flag. A mod whose shared-pool name already exists is fine as long as
    it's byte-identical (e.g. round-tripping a profile that was converted
    to specific and back with nothing else changed) — it's simply adopted.
    A genuine conflict (same name, different content) aborts the WHOLE
    conversion before anything private is deleted or the flag is flipped,
    so nothing on this profile is ever lost. Returns True if applied, False
    if aborted."""
    own_staging = profile_dir / "mods"
    shared_staging = game.get_mod_staging_path()
    ordered = _filter_source_entries(profile_dir, mod_names)

    pairs: list[tuple[str, bool]] = []
    conflicts: list[str] = []
    for entry in ordered:
        dest_folder = shared_staging / entry.name
        if dest_folder.is_dir():
            if not _dirs_identical(own_staging / entry.name, dest_folder):
                conflicts.append(entry.name)
                continue
            pairs.append((entry.name, entry.enabled))
            continue
        out = copy_mod_to_profile(
            own_staging, profile_dir, shared_staging, profile_dir,
            entry.name, entry.enabled, game=game, register=False)
        if out is None:
            conflicts.append(entry.name)
            continue
        pairs.append((out, entry.enabled))

    if conflicts:
        shown = ", ".join(conflicts[:5]) + ("…" if len(conflicts) > 5 else "")
        log(f"[convert] aborted: {len(conflicts)} mod(s) conflict with a "
            f"DIFFERENT mod of the same name already in the shared pool "
            f"({shown}) — rename or remove the conflicting shared-pool "
            "mod(s) first, then try again. Nothing on this profile was changed.")
        return False

    for sub in ("mods", "overwrite", "Root_Folder"):
        shutil.rmtree(profile_dir / sub, ignore_errors=True)
    merge_profile_settings(profile_dir, {"profile_specific_mods": False})
    write_modlist(profile_dir / "modlist.txt",
                 [ModEntry(name=n, enabled=e, locked=False) for n, e in pairs])
    log(f"[convert] '{profile_dir.name}' is now a standard profile with "
        f"{len(pairs)} mod(s).")
    return True
