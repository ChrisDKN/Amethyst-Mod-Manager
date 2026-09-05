"""
profiles/groups.py
Profile Groups: named, ordered combinations of profiles that deploy together
as one merged profile.

A group is an ordinary profile-specific profile dir (profiles/<group>/) with
profile_settings.is_group == True and an ordered group_members list (index 0
= highest priority). Its ``mods/`` is a PER-MOD-DIRECTORY SYMLINK FARM: one
relative link per merged mod pointing at the owning member's real mod folder.
Because every consumer resolves mod files as ``<staging root>/<mod name>``,
dir-level links keep that invariant intact - Filegraph, deploy/undeploy,
plugin sync, conflicts, Mod Files and LOOT treat a group like any profile.
The group owns a real overwrite/, Root_Folder/, and Filegraph catalog.
Members must themselves be profile-specific (a shared-pool member's modlist
carries the whole synced pool - see Utils/profiles/convert.py to convert).

Merge semantics - "adopt once, reconcile thereafter":
  - Identity = Nexus mod ID + version-stripped install name (one Nexus page
    can host several DISTINCT files under one mod ID - see
    _mod_identity_and_version), else folder name; persisted per entry in
    "group_identity_map" so champion folder renames (and identity flips)
    rename the group entry IN PLACE, keeping its position.
  - First adoption: enabled = OR across members; the champion (whose files
    win) is the highest-priority ENABLING member, overtaken only by a
    strictly newer version. Afterwards the group's own order/enabled/locked
    state is authoritative and never re-flipped by member changes.
  - Vanished mods drop (entry, link, catalog, state, plugins); new member mods
    append at the END. A REAL (non-link) folder in the group's mods/ is a
    group-LOCAL mod - wizard output installed while the group was active
    (SMAPI, generated patches) - kept as-is with its catalog entry; group
    removal deletes it like a normal profile's mod. Member separators import once (a name several members
    use is qualified per source profile so each member's section survives,
    with colour/lock/collapse/deploy state carried over); the group owns its
    separators after that.
  - Bethesda profile INIs are COPIED in (once per FILE via
    "group_adopted_inis", so late-gained INIs still adopt while group edits
    and deletions stick); same-named INIs resolve via the creation-time
    "group_ini_source" choice, else member priority.
  - Members' overwrite/ + Root_Folder/ files are COPY-merged the same way
    (once per FILE via "group_adopted_runtime", conflicts by member
    priority); the creation-time "group_overwrite_excluded" list opts
    members out.

materialize_group() is idempotent and O(mod count); it runs only on switch
to the group, Refresh, deploy start, group edits, and install/remove into an
active group - never on ordinary reloads.
"""

from __future__ import annotations

import os
import re
import shutil
import threading
import time
from pathlib import Path

from Utils.app_log import app_log
from Utils.mods.modlist import ModEntry, modlist_lock, read_modlist, write_modlist
from Utils.profiles.state import (
    _read_key,
    _update_key,
    merge_profile_settings,
    profile_uses_specific_mods,
    read_disabled_plugins,
    read_excluded_mod_files,
    read_groundcover_plugins,
    read_mod_notes,
    read_mod_strip_prefixes,
    read_profile_settings,
    read_root_mod_files,
    write_disabled_plugins,
    write_excluded_mod_files,
    write_groundcover_plugins,
    write_mod_notes,
    write_mod_strip_prefixes,
    write_profile_settings,
    write_root_mod_files,
)


_SEPARATOR_SUFFIX = "_separator"


class GroupValidationError(ValueError):
    """Raised when a Profile Group create/edit request violates an invariant
    (duplicate name, missing member, nested group, member not profile-specific)."""


_group_build_locks: dict[str, threading.RLock] = {}
_group_build_locks_guard = threading.Lock()


def group_build_lock(profile_dir: Path) -> threading.RLock:
    """Process-wide lock, one per group profile dir, serializing overlapping
    materialize/remove calls for the same group (e.g. a profile-switch
    materialize racing a deploy's own materialize moments later)."""
    key = str(profile_dir)
    with _group_build_locks_guard:
        lock = _group_build_locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _group_build_locks[key] = lock
        return lock


def staging_walk_roots(staging_root: Path) -> list[Path]:
    """Roots to ``os.walk`` when scanning a WHOLE staging folder: the folder
    itself plus each top-level symlink's resolved target. os.walk never
    descends into symlinked dirs, so walking a group's link farm directly
    finds no files; nested (archive) symlinks stay unfollowed. Identity for
    ordinary staging folders - safe to use unconditionally. Walking a single
    mod dir needs nothing (os.walk follows the top path itself)."""
    roots = [staging_root]
    try:
        with os.scandir(str(staging_root)) as it:
            for entry in it:
                if not entry.is_symlink():
                    continue
                try:
                    if entry.is_dir():
                        roots.append(Path(entry.path).resolve(strict=True))
                except OSError:
                    continue
    except OSError:
        pass
    return roots


def _profiles_root(game) -> Path:
    return game.get_profile_root() / "profiles"


def is_group(profile_dir: "Path | None") -> bool:
    """Return True if *profile_dir* is a Profile Group (merged profile)."""
    if profile_dir is None:
        return False
    return bool(read_profile_settings(profile_dir, None).get("is_group", False))


def list_groups(game) -> list[str]:
    """Return the names of all Profile Groups defined for *game*, sorted."""
    profiles_dir = _profiles_root(game)
    if not profiles_dir.is_dir():
        return []
    return sorted(p.name for p in profiles_dir.iterdir() if p.is_dir() and is_group(p))


def get_members(profile_dir: Path) -> list[str]:
    """Ordered member-profile names for a group (index 0 = highest priority)."""
    settings = read_profile_settings(profile_dir, None)
    members = settings.get("group_members")
    if isinstance(members, list):
        return [m for m in members if isinstance(m, str)]
    return []


def set_members(profile_dir: Path, members: list[str]) -> None:
    merge_profile_settings(profile_dir, {"group_members": list(members)})


def member_of_groups(game, name: str) -> list[str]:
    """Names of every Profile Group for *game* that lists *name* as a member."""
    profiles_dir = _profiles_root(game)
    return [g for g in list_groups(game) if name in get_members(profiles_dir / g)]


def owner_of(group_dir: Path, entry_name: str) -> "tuple[str, str] | None":
    """(member_name, member_folder) that *entry_name*'s symlink points at,
    or None when the entry has no link (or it points outside profiles/)."""
    link = group_dir / "mods" / entry_name
    try:
        if not link.is_symlink():
            return None
        target = Path(os.path.normpath(
            os.path.join(str(link.parent), os.readlink(str(link)))))
        rel = target.relative_to(group_dir.parent)
    except (OSError, ValueError):
        return None
    parts = rel.parts
    if len(parts) == 3 and parts[1] == "mods":
        return parts[0], parts[2]
    return None


def entry_owner_profile(group_dir: Path, entry_name: str
                        ) -> "tuple[Path, str] | None":
    """(profile dir, folder name) holding the real files behind group entry
    *entry_name*: the owning member for a farm link, the group itself for a
    group-LOCAL mod. None when the entry isn't in the group at all.

    Updating an installed mod (Quick Update / Change Version / Reinstall) uses
    this to install straight back into the source profile."""
    if not entry_name:
        return None
    owner = owner_of(group_dir, entry_name)
    if owner is not None:
        member_dir = group_dir.parent / owner[0]
        return (member_dir, owner[1]) if member_dir.is_dir() else None
    local = group_dir / "mods" / entry_name
    try:
        if local.is_dir() and not local.is_symlink():
            return group_dir, entry_name
    except OSError:
        pass
    return None


def profile_is_locked(profile_dir: Path) -> bool:
    """True when a profile is lock-protected (Profile Settings lock, or the
    original default) - same rule the Profile Settings rows use."""
    pset = read_profile_settings(profile_dir, None)
    return bool(pset.get("profile_locked") or pset.get("original_default"))


def _members_providing(group_dir: Path, name: str) -> list[str]:
    """Every member that provides the group entry *name* (its link owner plus
    any member listing the same identity)."""
    profiles_dir = group_dir.parent
    key = _read_identity_map(group_dir).get(name, f"name:{name}")
    out: list[str] = []
    owner = owner_of(group_dir, name)
    if owner is not None:
        out.append(owner[0])
    for member in get_members(group_dir):
        member_dir = profiles_dir / member
        if member in out or not member_dir.is_dir():
            continue
        for e in read_modlist(member_dir / "modlist.txt"):
            if e.is_separator:
                continue
            k, _v = _mod_identity_and_version(member_dir / "mods", e.name)
            if k == key:
                out.append(member)
                break
    return out


def locked_owners(group_dir: Path, mod_names: list[str]) -> dict[str, str]:
    """{mod_name: locked member} for entries whose files live in - or are also
    provided by - a LOCKED member. Removing through the group would delete
    that profile's mod, which is exactly what the lock protects against."""
    profiles_dir = group_dir.parent
    blocked: dict[str, str] = {}
    for name in mod_names:
        for member in _members_providing(group_dir, name):
            if profile_is_locked(profiles_dir / member):
                blocked[name] = member
                break
    return blocked


def _validate_member(game, profiles_dir: Path, member_name: str) -> None:
    if not getattr(game, "profile_groups_supported", True):
        raise GroupValidationError(
            f"Profile Groups aren't supported for {getattr(game, 'name', 'this game')} "
            "- its mod-merging logic doesn't have the per-mod/enabled-state "
            "concept the group merge depends on."
        )
    member_dir = profiles_dir / member_name
    if not member_dir.is_dir():
        raise GroupValidationError(f"Profile '{member_name}' does not exist.")
    if is_group(member_dir):
        raise GroupValidationError(
            f"'{member_name}' is itself a Profile Group - groups cannot be nested."
        )
    if not profile_uses_specific_mods(member_dir):
        raise GroupValidationError(
            f"'{member_name}' shares the game's common mod pool. Profile Group "
            "members must use profile-specific mods, so the group only ever "
            "sees mods deliberately added to that profile. Convert it first "
            "(profile settings → Convert to profile-specific mods)."
        )


def create_group(game, group_name: str, members: list[str], *,
                 ini_source: "str | None" = None,
                 overwrite_excluded: "list[str] | None" = None,
                 log_fn=None) -> Path:
    """Create a Profile Group named *group_name* combining *members* (priority
    order, index 0 = highest) and materialize it. Raises GroupValidationError.

    *ini_source* - when several members provide a same-named profile INI (see
    profile_ini_conflicts), the member whose copy wins; default is the
    highest-priority contributor.
    *overwrite_excluded* - members whose overwrite/Root_Folder files should
    NOT be merged into the group (creation-time choice; default = none, i.e.
    every member's files are adopted)."""
    profiles_dir = _profiles_root(game)
    group_dir = profiles_dir / group_name
    if group_dir.exists():
        raise GroupValidationError(f"A profile or group named '{group_name}' already exists.")
    if not members:
        raise GroupValidationError("A Profile Group needs at least one member profile.")
    if ini_source is not None and ini_source not in members:
        raise GroupValidationError(
            f"INI source '{ini_source}' is not a member of this group.")
    if overwrite_excluded and (bad := [m for m in overwrite_excluded
                                       if m not in members]):
        raise GroupValidationError(
            f"Overwrite exclusion(s) not in the member list: {', '.join(bad)}")
    seen: set[str] = set()
    for m in members:
        if m in seen:
            raise GroupValidationError(f"Duplicate member profile '{m}'.")
        seen.add(m)
        _validate_member(game, profiles_dir, m)

    group_dir.mkdir(parents=True, exist_ok=True)
    (group_dir / "modlist.txt").touch()
    (group_dir / "plugins.txt").touch()
    (group_dir / "loadorder.txt").touch()
    (group_dir / "mods").mkdir(exist_ok=True)
    (group_dir / "overwrite").mkdir(exist_ok=True)
    (group_dir / "Root_Folder").mkdir(exist_ok=True)
    settings = {
        "is_group": True,
        "group_members": list(members),
        "profile_specific_mods": True,
    }
    if ini_source is not None:
        settings["group_ini_source"] = ini_source
    if overwrite_excluded:
        settings["group_overwrite_excluded"] = list(overwrite_excluded)
    write_profile_settings(group_dir, settings)
    try:
        materialize_group(game, group_dir, log_fn=log_fn)
    except Exception:
        # Never leave a half-built ghost group registered in the selector.
        shutil.rmtree(group_dir, ignore_errors=True)
        raise
    return group_dir


def add_member(game, group_dir: Path, member_name: str, *, log_fn=None) -> None:
    profiles_dir = _profiles_root(game)
    _validate_member(game, profiles_dir, member_name)
    # Membership edits hold the group lock across set_members + materialize so
    # an in-flight materialize can't interleave with the change.
    with group_build_lock(group_dir):
        members = get_members(group_dir)
        if member_name in members:
            raise GroupValidationError(f"'{member_name}' is already a member of this group.")
        members.append(member_name)
        set_members(group_dir, members)
        materialize_group(game, group_dir, log_fn=log_fn)


def remove_member(game, group_dir: Path, member_name: str, *, log_fn=None) -> None:
    if not is_group(group_dir):
        return
    with group_build_lock(group_dir):
        set_members(group_dir, [m for m in get_members(group_dir) if m != member_name])
        materialize_group(game, group_dir, log_fn=log_fn)


def move_member(game, group_dir: Path, member_name: str, new_index: int, *,
                log_fn=None) -> None:
    """Reposition *member_name* to *new_index* in the priority order."""
    if not is_group(group_dir):
        return
    with group_build_lock(group_dir):
        members = get_members(group_dir)
        if member_name not in members:
            return
        members.remove(member_name)
        new_index = max(0, min(new_index, len(members)))
        members.insert(new_index, member_name)
        set_members(group_dir, members)
        materialize_group(game, group_dir, log_fn=log_fn)


def rename_profile_everywhere(game, old_name: str, new_name: str, *,
                              log_fn=None) -> list[str]:
    """Rename *old_name* in every group's member list and re-materialize the
    affected groups (links retarget). Returns the updated group names."""
    updated = []
    profiles_dir = _profiles_root(game)
    for group_name in list_groups(game):
        group_dir = profiles_dir / group_name
        with group_build_lock(group_dir):
            members = get_members(group_dir)
            if old_name in members:
                set_members(group_dir,
                            [new_name if m == old_name else m for m in members])
                materialize_group(game, group_dir, log_fn=log_fn)
                updated.append(group_name)
    return updated


def remove_profile_everywhere(game, name: str, *, log_fn=None) -> list[str]:
    """Prune *name* from every group's member list and re-materialize the
    affected groups. Returns the group names that referenced it."""
    affected = []
    profiles_dir = _profiles_root(game)
    for group_name in list_groups(game):
        group_dir = profiles_dir / group_name
        with group_build_lock(group_dir):
            members = get_members(group_dir)
            if name in members:
                set_members(group_dir, [m for m in members if m != name])
                materialize_group(game, group_dir, log_fn=log_fn)
                affected.append(group_name)
    return affected


# ---------------------------------------------------------------------------
# Group-owned state helpers (profile_state.json keys private to this module)
# ---------------------------------------------------------------------------

def _read_identity_map(group_dir: Path) -> dict[str, str]:
    raw = _read_key(group_dir, None, "group_identity_map")
    if isinstance(raw, dict):
        return {k: v for k, v in raw.items()
                if isinstance(k, str) and isinstance(v, str)}
    return {}


def _write_identity_map(group_dir: Path, value: dict[str, str]) -> None:
    _update_key(group_dir, "group_identity_map", dict(value))


def _adopted_separators(group_dir: Path) -> bool:
    return bool(_read_key(group_dir, None, "group_adopted_separators"))


# ---------------------------------------------------------------------------
# Merge (read-only over members)
# ---------------------------------------------------------------------------

# Trailing version-ish tokens ("2.5.17", "v1.15.11", "(1.2)", "1-15-11"),
# stripped repeatedly so "Mod 1.2 (3)" collapses too.
_VERSION_TOKEN_RE = re.compile(
    r"[\s\-_.]*[\(\[]?v?\d+(?:[.\-_]\d+)*[a-z]?[\)\]]?$", re.IGNORECASE)


def _stripped_mod_name(folder: str) -> str:
    """Folder name with trailing version tokens removed, casefolded - the
    stable part of a Nexus-file-derived install name across versions."""
    s = folder.casefold().strip()
    while True:
        t = _VERSION_TOKEN_RE.sub("", s).strip(" -_.")
        if not t or t == s:
            break
        s = t
    return s or folder.casefold()


def _mod_identity_and_version(member_mods_dir: Path, folder: str) -> "tuple[str, str]":
    """Dedup identity + version for one member's mod folder.

    Nexus mods key on ``nexus:<mod_id>:<version-stripped folder name>``:
    the mod ID alone is NOT enough - one Nexus page can host several
    DISTINCT files (Stardew Valley Expanded and Frontier Farm share
    modid 3753), and file_id can't help because different VERSIONS of the
    same file also differ in file_id. The version-stripped install name
    separates the two: same file across versions strips identically
    ("Ridgeside Village 2.5.17" → "ridgeside village", still deduped),
    different files on one page keep different names and stay distinct.
    No meta.ini → the bare folder name, no version."""
    try:
        from Nexus.nexus_meta import read_meta
        meta_path = member_mods_dir / folder / "meta.ini"
        if meta_path.is_file():
            meta = read_meta(meta_path)
            if meta.mod_id:
                return (f"nexus:{meta.mod_id}:{_stripped_mod_name(folder)}",
                        meta.version or "")
    except Exception:
        pass
    return f"name:{folder}", ""


def _merge_members(profiles_dir: Path, members: list[str], log_fn,
                   include_separators: bool):
    """Combine members' modlists into the prescribed merge.

    Returns (records, by_key): records is the ordered list of ("mod", rec) /
    ("sep", srec) tuples (rec = key/folder/member/enabled/locked; srec =
    entry/member/orig, separators only when *include_separators*); by_key
    maps identity → rec. Each identity appears once, at its owner's slot:
    enabled = OR across members; owner = highest-priority ENABLING member,
    overtaken only by a strictly newer version (first lister when nobody
    enables it)."""
    from Nexus.nexus_update_checker import _parse_version

    member_entries: list[tuple[str, list[ModEntry]]] = []
    for member_name in members:
        member_dir = profiles_dir / member_name
        if not member_dir.is_dir():
            log_fn(f"Profile Group: member '{member_name}' no longer exists - skipping.")
            continue
        entries = read_modlist(member_dir / "modlist.txt")
        if entries:
            member_entries.append((member_name, entries))

    identity: dict[tuple[str, str], str] = {}
    base_of: dict[str, str] = {}
    version_of: dict[tuple[str, str], str] = {}
    names_by_key: dict[str, set[str]] = {}
    members_by_key: dict[str, list[str]] = {}
    for member_name, entries in member_entries:
        mods_dir = profiles_dir / member_name / "mods"
        seen_in_member: dict[str, str] = {}
        for e in entries:
            if e.is_separator:
                continue
            key, version = _mod_identity_and_version(mods_dir, e.name)
            base = key
            if key in seen_in_member:
                # ONE profile listing the same mod twice is a deliberate
                # duplicate - Change Version ▸ Keep leaves the old version
                # beside the new one, and that profile shows both rows on its
                # own, so the group must too. Dedup is for the SAME mod
                # installed in DIFFERENT members; collapsing here would make
                # one of the two silently vanish from the group.
                key = f"{key}#{e.name}"
                log_fn(f"Profile Group: '{member_name}' lists '{e.name}' and "
                       f"'{seen_in_member[base]}' as the same mod - keeping "
                       f"both as separate group entries.")
            seen_in_member[base] = e.name
            base_of[key] = base
            identity[(member_name, e.name)] = key
            version_of[(member_name, e.name)] = version
            names_by_key.setdefault(key, set()).add(e.name)
            members_by_key.setdefault(key, []).append(member_name)

    # Pass 1: OR the enabled state per identity; pick each identity's owner.
    enabled_union: dict[str, bool] = {}
    champions: dict[str, dict] = {}
    first_lister: dict[str, tuple[str, str]] = {}
    first_enabler: dict[str, str] = {}
    for member_name, entries in member_entries:
        for e in entries:
            if e.is_separator:
                continue
            key = identity[(member_name, e.name)]
            first_lister.setdefault(key, (member_name, e.name))
            if not e.enabled:
                enabled_union.setdefault(key, False)
                continue
            enabled_union[key] = True
            first_enabler.setdefault(key, member_name)
            parsed = _parse_version(version_of[(member_name, e.name)])
            champ = champions.get(key)
            # A readable version always beats an incumbent whose version can't
            # be read (missing/garbled meta.ini): otherwise an unversioned copy
            # in a higher-priority member pins the group to the OLD mod and the
            # freshly installed one never appears.
            if champ is None or (parsed is not None
                                 and (champ["version"] is None
                                      or parsed > champ["version"])):
                champions[key] = {"member": member_name, "folder": e.name,
                                  "version": parsed,
                                  "version_str": version_of[(member_name, e.name)],
                                  "locked": e.locked}
    owner_by_key: dict[str, tuple[str, str]] = dict(first_lister)
    for key, champ in champions.items():
        owner_by_key[key] = (champ["member"], champ["folder"])

    # Diagnostics: cross-named duplicates, and whether a strictly-newer
    # version (not member priority) decided the winner.
    for key, names in names_by_key.items():
        if not key.startswith("nexus:") or len(names) <= 1:
            continue
        champ = champions.get(key)
        winner_member = owner_by_key[key][0]
        if champ is not None and champ["version_str"] and \
                winner_member != first_enabler.get(key):
            reason = f"newer version {champ['version_str']} wins over member priority"
        else:
            reason = "member priority order"
        log_fn(f"Profile Group: {', '.join(sorted(names))} are the same Nexus "
               f"mod (id {key.split(':', 2)[1]}) under different names across "
               f"members - using '{winner_member}'s copy ({reason}).")

    # Same folder name in several members with no Nexus ID: indistinguishable,
    # collapses to one entry - right for a shared dep installed per profile,
    # wrong for different mods sharing a generic name. Never silent.
    for key, listers in members_by_key.items():
        if key.startswith("nexus:") or len(listers) <= 1:
            continue
        folder = key.split(":", 1)[1]
        winner_member = owner_by_key[key][0]
        log_fn(f"Profile Group: '{folder}' exists in {', '.join(listers)} with "
               f"no Nexus ID to tell the copies apart - merged as ONE mod "
               f"using '{winner_member}'s copy. If these are different mods, "
               f"rename one folder so both can deploy.")

    # Pass 2: walk members in priority order, placing each identity at its
    # owner's exact (member, folder) slot.
    # Separator names shared by several members are qualified per source
    # profile rather than deduped - see _section_name. Pre-count so a name
    # only one member uses stays clean, and seed `taken` with every literal
    # name so a generated one can never shadow a real one.
    sep_counts: dict[str, int] = {}
    for _member, entries in member_entries:
        for e in entries:
            if e.is_separator:
                sep_counts[e.name] = sep_counts.get(e.name, 0) + 1
    sep_taken: set[str] = set(sep_counts)

    records: list[tuple[str, object]] = []
    seen_keys: set[str] = set()
    seen_folders: set[str] = set()
    by_key: dict[str, dict] = {}
    for member_name, entries in member_entries:
        for e in entries:
            if e.is_separator:
                if include_separators:
                    records.append(("sep", {
                        "entry": ModEntry(
                            name=_section_name(e, member_name, sep_counts,
                                               sep_taken),
                            enabled=True, locked=True, is_separator=True),
                        "member": member_name,
                        "orig": e.name,
                    }))
                continue
            key = identity[(member_name, e.name)]
            if key in seen_keys:
                continue
            if owner_by_key[key] != (member_name, e.name):
                continue
            # Two DIFFERENT identities can carry the same folder name across
            # members (generic name, distinct Nexus ids). Only one folder of
            # a given name can exist in the link farm / modlist - first
            # (highest-priority) identity wins, the collision is reported.
            if e.name in seen_folders:
                log_fn(f"Profile Group: '{member_name}/{e.name}' collides "
                       f"with another member's different mod of the same "
                       f"folder name - only the higher-priority one is "
                       f"merged (rename one of the folders to include both).")
                continue
            seen_keys.add(key)
            seen_folders.add(e.name)
            rec = {"key": key, "base": base_of.get(key, key),
                   "folder": e.name, "member": member_name,
                   "enabled": enabled_union.get(key, e.enabled),
                   "locked": champions.get(key, {}).get("locked", False)}
            records.append(("mod", rec))
            by_key[key] = rec
    return records, by_key


def _section_name(entry: ModEntry, member: str, counts: dict[str, int],
                  taken: set[str]) -> str:
    """Imported-separator name: unchanged when unique, qualified with the
    source profile ("User Interface (QoL)") when several members share it -
    merged mods keep member-block order, so a single shared header would
    strand later members' mods under the wrong section. Numeric suffix only
    if the qualified name is somehow taken too."""
    if counts.get(entry.name, 0) <= 1:
        return entry.name
    base = f"{entry.display_name} ({member})"
    cand = f"{base}{_SEPARATOR_SUFFIX}"
    n = 1
    while cand in taken:
        n += 1
        cand = f"{base} ({n}){_SEPARATOR_SUFFIX}"
    taken.add(cand)
    return cand


def _merge_plugins(profiles_dir: Path, members: list[str], star_prefix: bool):
    """(ordered plugin names, {name: enabled}) merged across members for the
    FIRST materialize only: case-insensitive dedup, first-occurrence position,
    enabled if enabled in ANY member (same OR rule as the modlist merge).
    Later materializes reconcile per-mod via sync_plugins_for_mods instead."""
    from Utils.plugins import read_loadorder, read_plugins

    member_data: list[tuple[list[str], dict[str, bool]]] = []
    for member_name in members:
        member_dir = profiles_dir / member_name
        if not member_dir.is_dir():
            continue
        loadorder = read_loadorder(member_dir / "loadorder.txt")
        plugins = read_plugins(member_dir / "plugins.txt", star_prefix=star_prefix)
        # Keyed lowercase - loadorder.txt and plugins.txt can disagree on case.
        enabled_by_name = {p.name.lower(): p.enabled for p in plugins}
        # loadorder.txt is the superset (vanilla + disabled-only entries on
        # legacy engines); walk it first, then plugins.txt-only stragglers.
        names_in_order = list(loadorder)
        seen_local = {n.lower() for n in names_in_order}
        for p in plugins:
            if p.name.lower() not in seen_local:
                names_in_order.append(p.name)
                seen_local.add(p.name.lower())
        member_data.append((names_in_order, enabled_by_name))

    enabled_union: dict[str, bool] = {}
    display_name: dict[str, str] = {}
    for names_in_order, enabled_by_name in member_data:
        for name in names_in_order:
            key = name.lower()
            display_name.setdefault(key, name)
            if enabled_by_name.get(key, False):
                enabled_union[key] = True
            else:
                enabled_union.setdefault(key, False)

    merged_order: list[str] = []
    merged_enabled: dict[str, bool] = {}
    seen_lower: set[str] = set()
    for names_in_order, _enabled in member_data:
        for name in names_in_order:
            key = name.lower()
            if key in seen_lower:
                continue
            seen_lower.add(key)
            shown = display_name[key]
            merged_order.append(shown)
            merged_enabled[shown] = enabled_union.get(key, False)
    return merged_order, merged_enabled


# ---------------------------------------------------------------------------
# Materialization
# ---------------------------------------------------------------------------

def _desired_link_target(profiles_dir: Path, group_mods: Path,
                         member: str, folder: str) -> str:
    """Relative symlink target for a group link → the member's real mod dir."""
    return os.path.relpath(str(profiles_dir / member / "mods" / folder),
                           str(group_mods))


def _sync_link_farm(group_dir: Path, owners: dict[str, tuple[str, str]],
                    log_fn, local_mods: "set[str] | None" = None
                    ) -> "tuple[set[str], set[str]]":
    """Make the farm contain exactly one symlink per *owners* entry, pointing
    at the owning member's mod dir. Idempotent; returns (changed, removed).
    Real directories in *local_mods* are group-LOCAL mods (wizard output) and
    are left alone silently; any OTHER real directory is reported, never
    deleted."""
    profiles_dir = group_dir.parent
    group_mods = group_dir / "mods"
    group_mods.mkdir(exist_ok=True)
    changed: set[str] = set()
    removed: set[str] = set()

    existing: dict[str, os.DirEntry] = {}
    try:
        with os.scandir(str(group_mods)) as it:
            for entry in it:
                existing[entry.name] = entry
    except OSError as exc:
        log_fn(f"Profile Group: could not read {group_mods} ({exc}) - "
               f"links left unchanged.")
        return changed, removed

    local = local_mods or set()
    for name, entry in existing.items():
        if name in owners or name in local:
            continue
        if entry.is_symlink():
            try:
                os.unlink(entry.path)
                removed.add(name)
            except OSError as exc:
                log_fn(f"Profile Group: could not remove stale link '{name}': {exc}")
        else:
            # A real folder with no modlist entry: freshly installed (the
            # modlist sync adopts it right after materialize) or stray.
            log_fn(f"Profile Group: '{name}' is a real folder with no group "
                   f"entry - left for the modlist sync to adopt.")

    for name, (member, folder) in owners.items():
        want = _desired_link_target(profiles_dir, group_mods, member, folder)
        link = group_mods / name
        entry = existing.get(name)
        if entry is not None:
            if entry.is_symlink():
                try:
                    if os.readlink(entry.path) == want:
                        continue
                    os.unlink(entry.path)
                except OSError:
                    pass
            else:
                # Real dir under an entry name - reported above; never replace.
                continue
        try:
            os.symlink(want, str(link))
            changed.add(name)
        except OSError as exc:
            log_fn(f"Profile Group: could not link '{name}' → {want}: {exc}")
    return changed, removed


def _refresh_profile_mods(game, profile_dir: Path, mod_names: list[str],
                          log_fn) -> None:
    """Refresh selected manifests in a profile-specific Filegraph catalog.

    An unready catalog is rebuilt in full so a targeted refresh can never
    accidentally mark a partial first-migration catalog ready.
    """
    if not mod_names:
        return
    try:
        from Utils.filegraph.service import FileGraphService
        library = FileGraphService.open_library(game, profile_dir, log_fn=log_fn)
        if library.status().ready:
            library.refresh(profile_dir, mod_names=mod_names)
        else:
            library.rebuild(profile_dir)
    except Exception as exc:
        log_fn(f"Profile Group: Filegraph update failed ({exc}) - Refresh "
               f"will rebuild the catalog.")


def _catalog_plugins(game, profile_dir: Path, mod_names, log_fn) -> set[str]:
    """Loadable plugin names exposed by selected cataloged mods."""
    if not mod_names:
        return set()
    try:
        from Utils.filegraph.service import FileGraphService
        library = FileGraphService.open_library(game, profile_dir, log_fn=log_fn)
        status = library.ensure_ready(profile_dir)
        profile = library.open_profile(profile_dir)
        snapshot = profile.snapshot()
        if (snapshot.generation == 0
                or snapshot.inventory_generation != status.inventory_generation):
            profile.reconcile()
            snapshot = profile.snapshot()
        result: set[str] = set()
        for name in mod_names:
            for record in snapshot.mod_files(name):
                if record.plugin_key:
                    result.add(record.plugin_key)
        return result
    except Exception as exc:
        log_fn(f"Profile Group: plugin catalog query failed ({exc}).")
        return set()


def _strip_plugins(game, profile_dir: Path, names: set[str], log_fn) -> None:
    """Remove *names* from the profile's plugins.txt + loadorder.txt."""
    if not names:
        return
    try:
        from Utils.plugins import (PluginEntry, read_loadorder, read_plugins,
                                   write_loadorder, write_plugins)
        low = {n.lower() for n in names}
        star = bool(getattr(game, "plugins_use_star_prefix", True))
        ppath = profile_dir / "plugins.txt"
        entries = read_plugins(ppath, star_prefix=star)
        kept = [e for e in entries if e.name.lower() not in low]
        if len(kept) != len(entries):
            write_plugins(ppath, kept, star_prefix=star)
        lopath = profile_dir / "loadorder.txt"
        lo = read_loadorder(lopath)
        kept_lo = [n for n in lo if n.lower() not in low]
        if len(kept_lo) != len(lo):
            write_loadorder(lopath,
                            [PluginEntry(name=n, enabled=True) for n in kept_lo])
    except Exception as exc:
        log_fn(f"Profile Group: plugin strip failed ({exc}).")


def _sync_group_plugins(game, group_dir: Path, changes: list[tuple[str, bool]],
                        log_fn) -> None:
    if not changes or not getattr(game, "plugin_extensions", None):
        return
    try:
        from Utils.plugins.sync import sync_plugins_for_mods
        sync_plugins_for_mods(game, group_dir, group_dir / "mods", changes,
                              log_fn=log_fn)
    except Exception as exc:
        log_fn(f"Profile Group: plugin sync failed ({exc}).")


def materialize_if_group(game, profile_dir: "Path | None", *, log_fn=None,
                         timing=None) -> bool:
    """Materialize *profile_dir* when it is a group; never raises. Returns
    True when a materialize ran. This is the wiring entry point for the
    profile-switch / Refresh / deploy hooks."""
    try:
        if profile_dir is None or not is_group(profile_dir):
            return False
        materialize_group(game, profile_dir, log_fn=log_fn, timing=timing)
        return True
    except Exception as exc:
        (log_fn or app_log)(f"Profile Group: materialize failed for "
                            f"'{getattr(profile_dir, 'name', profile_dir)}': {exc}")
        return True


def _record_materialize(timing, label: str, started: float,
                        category: str = "profile group") -> None:
    if timing is not None:
        timing.record(label, phase_started=started, category=category)


def materialize_group(game, profile_dir: Path, *, log_fn=None,
                      timing=None) -> None:
    """Reconcile a group against its members' current state: adopt new mods,
    drop vanished ones, retarget links, keep every in-group edit. See the
    module docstring for the full contract."""
    _log = log_fn or app_log
    if not is_group(profile_dir):
        return
    lock_started = time.perf_counter()
    with group_build_lock(profile_dir):
        _record_materialize(
            timing, "Wait for active profile-group build lock", lock_started)
        profiles_dir = profile_dir.parent
        members = get_members(profile_dir)
        modlist_path = profile_dir / "modlist.txt"

        lock_started = time.perf_counter()
        with modlist_lock(modlist_path):
            _record_materialize(
                timing, "Wait for profile-group mod-list lock", lock_started)
            phase_started = time.perf_counter()
            current = read_modlist(modlist_path)
            first = not current and not _adopted_separators(profile_dir)
            records, by_key = _merge_members(profiles_dir, members, _log, first)
            identity_map = _read_identity_map(profile_dir)

            final: list[ModEntry] = []
            owners: dict[str, tuple[str, str]] = {}
            renames: list[tuple[str, str]] = []
            drops: list[str] = []
            adds: list[dict] = []
            seen_keys: set[str] = set()

            # A mod's identity can legitimately FLIP between nexus:<id> and
            # name:<folder> (meta.ini gained an ID, or was lost/corrupted).
            # Falling back to a folder-name match keeps the entry in place
            # instead of drop+re-append (which would lose position/state).
            by_folder = {rec["folder"]: rec for rec in by_key.values()}
            group_mods = profile_dir / "mods"
            local_mods: set[str] = set()
            taken: set[str] = set()
            rename_enabled: dict[str, bool] = {}
            # base identity → the entry name already holding it, so a second
            # copy of the same mod (Change Version ▸ Keep) lands NEXT TO its
            # sibling instead of at the bottom of the load order.
            base_anchor: dict[str, str] = {}
            for e in current:
                if e.is_separator:
                    final.append(e)
                    continue
                # A REAL (non-link) folder is a group-LOCAL mod - wizard
                # output installed while the group was active (SMAPI, script
                # extenders, generated patches). It belongs to the group
                # itself: keep it exactly as-is. Checked FIRST - a member
                # record must never claim a local dir via the name fallback.
                p = group_mods / e.name
                if p.is_dir() and not p.is_symlink():
                    final.append(e)
                    local_mods.add(e.name)
                    taken.add(e.name)
                    continue
                key = identity_map.get(e.name, f"name:{e.name}")
                rec = by_key.get(key) or by_folder.get(e.name)
                if rec is None or rec["key"] in seen_keys:
                    drops.append(e.name)
                    continue
                if rec["folder"] != e.name and rec["folder"] in taken:
                    # Champion flipped to a folder name another entry already
                    # claims - unrepresentable in a name-keyed farm; drop.
                    drops.append(e.name)
                    continue
                seen_keys.add(rec["key"])
                if rec["folder"] != e.name:
                    renames.append((e.name, rec["folder"]))
                    rename_enabled[rec["folder"]] = e.enabled
                    e = ModEntry(name=rec["folder"], enabled=e.enabled,
                                 locked=e.locked)
                final.append(e)
                owners[e.name] = (rec["member"], rec["folder"])
                taken.add(e.name)
                base_anchor.setdefault(rec.get("base", rec["key"]), e.name)

            sep_adopt: list[dict] = []
            new_block: list[ModEntry] = []
            for rtype, rec in records:
                if rtype == "sep":
                    if first:
                        final.append(rec["entry"])
                        sep_adopt.append(rec)
                    continue
                if rec["key"] in seen_keys:
                    continue
                if rec["folder"] in taken:
                    _log(f"Profile Group: new arrival '{rec['folder']}' from "
                         f"'{rec['member']}' collides with an existing group "
                         f"entry of the same folder name - skipped (rename "
                         f"one of the folders to include both).")
                    continue
                seen_keys.add(rec["key"])
                entry = ModEntry(name=rec["folder"], enabled=rec["enabled"],
                                 locked=rec["locked"] if rec["enabled"] else False)
                anchor = base_anchor.get(rec.get("base", rec["key"]))
                at = next((i for i, x in enumerate(final)
                           if x.name == anchor), None) if anchor else None
                if at is not None:
                    # A second copy of a mod already in the group: keep the two
                    # together rather than dropping the arrival to the bottom.
                    final.insert(at + 1, entry)
                elif first:
                    final.append(entry)
                else:
                    new_block.append(entry)
                owners[entry.name] = (rec["member"], rec["folder"])
                taken.add(entry.name)
                base_anchor.setdefault(rec.get("base", rec["key"]), entry.name)
                adds.append(rec)
            if new_block:
                final[:0] = new_block
            _record_materialize(
                timing, "Merge profile-group member mod state", phase_started)

            # 1. Plugin removal FIRST - dropped mods' plugins resolve from the
            # group's still-present catalog entries / links. Renames are NOT
            # dropped here: a drop+re-add cycle would reset the group's own
            # plugin enabled state. Their plugins are diffed after the rescan
            # (unchanged names keep their state; only vanished ones strip).
            phase_started = time.perf_counter()
            plugin_exts = tuple(e.lower() for e in
                                (getattr(game, "plugin_extensions", []) or []))
            _sync_group_plugins(game, profile_dir,
                                [(n, False) for n in drops], _log)
            rename_old_plugins: set[str] = set()
            if renames and plugin_exts:
                rename_old_plugins = _catalog_plugins(
                    game, profile_dir, (old for old, _new in renames), _log)
            _record_materialize(
                timing, "Reconcile removed profile-group plugins",
                phase_started, category="plugins")

            # 2. Per-mod group-owned state: migrate renames, drop vanished,
            # adopt new arrivals' state once from the owning member. Imported
            # separators bring their own colour/lock/collapse/deploy state.
            phase_started = time.perf_counter()
            _reconcile_mod_state(profile_dir, profiles_dir, renames, drops,
                                 adds)
            _adopt_separator_state(profile_dir, profiles_dir, sep_adopt)
            # INI + overwrite/Root_Folder adoption is once-per-FILE (not per
            # group), so both run every materialize and pick up files members
            # gained after the group was created.
            _adopt_profile_inis(game, profile_dir, profiles_dir, members, _log)
            _adopt_runtime_dirs(profile_dir, profiles_dir, members, _log)
            _record_materialize(
                timing, "Reconcile profile-group settings and runtime files",
                phase_started)

            # 3. Link farm (group-local real dirs are left alone).
            phase_started = time.perf_counter()
            _sync_link_farm(profile_dir, owners, _log, local_mods)
            _record_materialize(
                timing, "Reconcile profile-group link farm", phase_started)

            # 4. Catalog maintenance: drop removed/renamed-away entries, then
            # refresh only entries whose owning member manifest changed.
            phase_started = time.perf_counter()
            gone = drops + [old for old, _new in renames]
            from Utils.filegraph.service import FileGraphService
            group_library = FileGraphService.open_library(
                game, profile_dir, log_fn=_log)
            for name in gone:
                try:
                    group_library.remove_mod(name)
                except Exception as exc:
                    _log(f"Profile Group: could not remove '{name}' from "
                         f"Filegraph ({exc}).")

            to_refresh = _stale_group_entries(
                game, profile_dir, owners, _log)
            _refresh_profile_mods(game, profile_dir, to_refresh, _log)
            _record_materialize(
                timing, "Check and refresh profile-group catalogs",
                phase_started, category="filegraph")

            # 5. Plugin adoption for new/renamed arrivals (catalog now has them;
            # sync only APPENDS missing plugins, so plugins a renamed mod kept
            # retain the group's enabled state). Then strip plugins the
            # renames no longer provide anywhere.
            phase_started = time.perf_counter()
            plugin_adds = [(r["folder"], True) for r in adds if r["enabled"]]
            plugin_adds += [(new, True) for _old, new in renames
                            if rename_enabled.get(new, True)]
            if first and plugin_exts:
                _adopt_first_plugins(game, profile_dir, profiles_dir, members)
            else:
                _sync_group_plugins(game, profile_dir, plugin_adds, _log)
            if rename_old_plugins:
                still_provided = _catalog_plugins(
                    game, profile_dir, owners, _log)
                stale = rename_old_plugins - still_provided
                if stale:
                    _strip_plugins(game, profile_dir, stale, _log)
            _record_materialize(
                timing, "Adopt profile-group plugin state", phase_started,
                category="plugins")

            # 6. Persist the adopted modlist + identity map + flags.
            # group_members is deliberately NOT written back: it was read at
            # entry, and a membership edit that landed since would be
            # silently reverted (edits hold the group lock, but the deploy
            # worker's materialize does not wait for them to start).
            phase_started = time.perf_counter()
            write_modlist(modlist_path, final)
            _write_identity_map(profile_dir, {
                rec["folder"]: key for key, rec in by_key.items()
                if rec["folder"] in owners
            })
            merge_profile_settings(profile_dir, {
                "is_group": True,
                "profile_specific_mods": True,
            })
            if first and records:
                _update_key(profile_dir, "group_adopted_separators", True)
            _record_materialize(
                timing, "Persist reconciled profile-group state",
                phase_started)

        if adds or drops or renames:
            _log(f"Profile Group '{profile_dir.name}': adopted {len(adds)}, "
                 f"dropped {len(drops)}, renamed {len(renames)} "
                 f"(total {sum(1 for e in final if not e.is_separator)} mods).")


def _stale_group_entries(game, group_dir: Path,
                         owners: dict[str, tuple[str, str]], log_fn):
    """Return entries whose member raw-manifest fingerprint changed.

    Member catalogs are the cheap change detector; a missing first-migration
    catalog is rebuilt once. The group keeps its own candidate variants, so a
    changed member manifest is refreshed through the group's symlink and its
    profile-specific routing rules.
    """
    from Utils.filegraph.service import FileGraphService
    group_library = FileGraphService.open_library(game, group_dir, log_fn=log_fn)
    group_library.ensure_ready(group_dir)
    group_catalog = {
        name.lower(): fingerprint
        for name, fingerprint in group_library.manifest_fingerprints().items()
    }
    member_catalogs: dict[str, dict[str, bytes]] = {}
    stale: list[str] = []
    for name, (member, folder) in owners.items():
        catalog = member_catalogs.get(member)
        if catalog is None:
            member_dir = group_dir.parent / member
            library = FileGraphService.open_library(
                game, member_dir, log_fn=log_fn)
            library.ensure_ready(member_dir)
            catalog = {
                mod_name.lower(): fingerprint
                for mod_name, fingerprint in library.manifest_fingerprints().items()
            }
            member_catalogs[member] = catalog
        raw = catalog.get(folder.lower())
        if raw is None:
            # The member's manifest is absent despite a ready catalog. Scan
            # the group link now, but do not mark it clean so Refresh retries.
            stale.append(name)
            continue
        if group_catalog.get(name.lower()) != raw:
            stale.append(name)
    return stale


def _reconcile_mod_state(group_dir: Path, profiles_dir: Path,
                         renames: list[tuple[str, str]], drops: list[str],
                         adds: list[dict]) -> None:
    """Keep the group's per-mod profile_state submaps in step with the
    reconcile: renamed entries migrate in place, dropped entries are removed,
    new arrivals adopt their owning member's state ONCE (group-owned after)."""
    from Utils.mods.groups import rename_group_mod, detach
    from Utils.profiles.state import read_mod_groups, write_mod_groups
    mod_groups = read_mod_groups(group_dir)
    updated = mod_groups
    for old, new in renames:
        updated = rename_group_mod(updated, old, new)
    updated = detach(updated, drops)
    if updated != mod_groups:
        write_mod_groups(group_dir, updated)
    for reader, writer in (
        (read_disabled_plugins, write_disabled_plugins),
        (read_excluded_mod_files, write_excluded_mod_files),
        (read_root_mod_files, write_root_mod_files),
        (read_mod_notes, write_mod_notes),
        (read_mod_strip_prefixes, write_mod_strip_prefixes),
    ):
        try:
            data = reader(group_dir, None)
            changed = False
            for old, new in renames:
                if old in data:
                    data[new] = data.pop(old)
                    changed = True
            for name in drops:
                if name in data:
                    data.pop(name, None)
                    changed = True
            for rec in adds:
                member_dir = profiles_dir / rec["member"]
                if not member_dir.is_dir():
                    continue
                val = reader(member_dir, None).get(rec["folder"])
                if val and rec["folder"] not in data:
                    data[rec["folder"]] = val
                    changed = True
            if changed:
                writer(group_dir, data)
        except Exception:
            continue


def _adopt_separator_state(group_dir: Path, profiles_dir: Path,
                           seps: list[dict]) -> None:
    """First materialize only: carry each imported separator's colour/lock/
    collapse/deploy-path state from its source member onto its (possibly
    qualified) group name - all four maps key on the separator name."""
    if not seps:
        return
    from Utils.profiles.state import (
        read_collapsed_seps, read_separator_colors, read_separator_deploy_paths,
        read_separator_locks, write_collapsed_seps, write_separator_colors,
        write_separator_deploy_paths, write_separator_locks,
    )
    for reader, writer in (
        (read_separator_colors, write_separator_colors),
        (read_separator_locks, write_separator_locks),
        (read_separator_deploy_paths, write_separator_deploy_paths),
    ):
        try:
            data = reader(group_dir, None)
            changed = False
            for rec in seps:
                member_dir = profiles_dir / rec["member"]
                if not member_dir.is_dir():
                    continue
                name = rec["entry"].name
                if name in data:
                    continue
                val = reader(member_dir, None).get(rec["orig"])
                if val:
                    data[name] = val
                    changed = True
            if changed:
                writer(group_dir, data)
        except Exception:
            continue
    try:
        collapsed = read_collapsed_seps(group_dir, None)
        add = {rec["entry"].name for rec in seps
               if (profiles_dir / rec["member"]).is_dir()
               and rec["orig"] in read_collapsed_seps(
                   profiles_dir / rec["member"], None)}
        if add - collapsed:
            write_collapsed_seps(group_dir, collapsed | add)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Profile-specific INI files (Bethesda family: <profile>/ini files/, deployed
# by symlinking into My Games when the per-profile "profile_ini_files" game
# setting is on)
# ---------------------------------------------------------------------------

_PROFILE_INI_SUBDIR = "ini files"


def _game_supports_profile_inis(game) -> bool:
    # profile_ini_files is a PATHS extra (paths.json, per-profile overridable
    # via profile_overridable_paths_extras + the profile_settings override the
    # Bethesda family applies in _apply_profile_path_overrides) - NOT a
    # game_settings.json key.
    return "profile_ini_files" in tuple(
        getattr(game, "profile_overridable_paths_extras", ()) or ())


def _member_uses_profile_inis(game, member_dir: Path) -> bool:
    """The member's EFFECTIVE profile_ini_files value: its own profile_settings
    override when present, else the game's GLOBAL paths.json value (read raw -
    the game object's live property reflects the ACTIVE profile's overlay,
    which may not be this member)."""
    pset = read_profile_settings(member_dir, None)
    if "profile_ini_files" in pset:
        return bool(pset["profile_ini_files"])
    try:
        import json
        pf = game._paths_file
        if pf.exists():
            data = json.loads(pf.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "profile_ini_files" in data:
                return bool(data["profile_ini_files"])
    except Exception:
        pass
    return bool(getattr(game, "profile_ini_files", False))


def profile_ini_contributors(game, profiles_dir: Path,
                             members: list[str]) -> list[str]:
    """Members (in priority order) whose profile-specific INIs the group
    should inherit: flag effectively on AND a non-empty 'ini files' folder."""
    if not _game_supports_profile_inis(game):
        return []
    out: list[str] = []
    for member in members:
        member_dir = profiles_dir / member
        if not member_dir.is_dir() or not _member_uses_profile_inis(game, member_dir):
            continue
        ini_dir = member_dir / _PROFILE_INI_SUBDIR
        try:
            has_files = ini_dir.is_dir() and any(
                f.is_file() for f in ini_dir.iterdir())
        except OSError:
            has_files = False
        if has_files:
            out.append(member)
    return out


def profile_ini_conflicts(game, profiles_dir: Path,
                          members: list[str]) -> "tuple[list[str], list[str]]":
    """(contributors, conflicting_filenames): INI names (case-insensitive)
    that more than one contributing member provides - the UI prompts which
    profile's copy the group should use before creating it."""
    contributors = profile_ini_contributors(game, profiles_dir, members)
    seen: dict[str, str] = {}
    conflicts: set[str] = set()
    for member in contributors:
        ini_dir = profiles_dir / member / _PROFILE_INI_SUBDIR
        try:
            for f in ini_dir.iterdir():
                if not f.is_file():
                    continue
                key = f.name.lower()
                if key in seen and seen[key] != member:
                    conflicts.add(f.name)
                else:
                    seen.setdefault(key, member)
        except OSError:
            continue
    return contributors, sorted(conflicts)


def _adopt_profile_inis(game, group_dir: Path, profiles_dir: Path,
                        members: list[str], log_fn) -> None:
    """COPY contributing members' profile INIs into the group (never link -
    tools rewrite INIs in place, which would write through into the member)
    and switch the group's profile_ini_files flag on.

    Adopt-once PER FILE ("group_adopted_inis"), evaluated every materialize:
    late-gained member INIs still adopt, while a file the group ever owned -
    including one the user deleted - is never re-copied. Conflicts resolve
    via 'group_ini_source', else member priority. A group whose flag was
    explicitly switched OFF is left alone."""
    contributors = profile_ini_contributors(game, profiles_dir, members)
    if not contributors:
        return
    pset = read_profile_settings(group_dir, None)
    if pset.get("profile_ini_files") is False:
        return
    preferred = pset.get("group_ini_source")
    ordered = list(contributors)
    if preferred in ordered:
        ordered.remove(preferred)
        ordered.insert(0, preferred)

    raw = _read_key(group_dir, None, "group_adopted_inis")
    adopted: set[str] = ({s for s in raw if isinstance(s, str)}
                         if isinstance(raw, list) else set())
    dest = group_dir / _PROFILE_INI_SUBDIR
    dest.mkdir(exist_ok=True)
    # Files already in the group (pre-feature manual copies, etc.) count as
    # adopted - never overwrite them.
    try:
        for f in dest.iterdir():
            if f.is_file():
                adopted.add(f.name.lower())
    except OSError:
        pass

    copied_from: dict[str, str] = {}
    for member in ordered:
        ini_dir = profiles_dir / member / _PROFILE_INI_SUBDIR
        try:
            files = sorted(f for f in ini_dir.iterdir() if f.is_file())
        except OSError:
            continue
        for f in files:
            key = f.name.lower()
            if key in adopted or key in copied_from:
                continue
            try:
                shutil.copy2(str(f), str(dest / f.name))
                copied_from[key] = member
            except OSError as exc:
                log_fn(f"Profile Group: could not copy INI '{f.name}' "
                       f"from '{member}': {exc}")
    if copied_from or (adopted and raw is None):
        _update_key(group_dir, "group_adopted_inis",
                    sorted(adopted | set(copied_from)))
    if not copied_from:
        return
    merge_profile_settings(group_dir, {"profile_ini_files": True})
    by_member: dict[str, int] = {}
    for m in copied_from.values():
        by_member[m] = by_member.get(m, 0) + 1
    log_fn(f"Profile Group: adopted {len(copied_from)} profile INI file(s) "
           f"({', '.join(f'{n} from {m}' for m, n in by_member.items())}) - "
           f"profile-specific INIs enabled for the group.")


# ---------------------------------------------------------------------------
# Member overwrite/ + Root_Folder/ adoption
# ---------------------------------------------------------------------------

_RUNTIME_DIRS = ("overwrite", "Root_Folder")


def runtime_dir_contributors(profiles_dir: Path,
                             members: list[str]) -> list[str]:
    """Members (priority order) with any file in overwrite/ or Root_Folder/."""
    out: list[str] = []
    for member in members:
        member_dir = profiles_dir / member
        if not member_dir.is_dir():
            continue
        for sub in _RUNTIME_DIRS:
            root = member_dir / sub
            if root.is_dir() and any(
                    fns for _dp, _dns, fns in os.walk(root)):
                out.append(member)
                break
    return out


def _adopt_runtime_dirs(group_dir: Path, profiles_dir: Path,
                        members: list[str], log_fn) -> None:
    """COPY members' overwrite/ + Root_Folder/ contents into the group's own
    dirs (never link - overwrite holds runtime-rewritten files, and a link
    would write through into the member).

    Adopt-once PER FILE ("group_adopted_runtime"), evaluated every
    materialize: late-gained files still adopt, group edits and deletions
    stick, and a rel-path several members provide comes from the highest-
    priority one. Members listed in profile_settings
    "group_overwrite_excluded" (creation-time choice) contribute nothing."""
    pset = read_profile_settings(group_dir, None)
    excluded = {m for m in (pset.get("group_overwrite_excluded") or [])
                if isinstance(m, str)}
    raw = _read_key(group_dir, None, "group_adopted_runtime")
    adopted: set[str] = ({s for s in raw if isinstance(s, str)}
                         if isinstance(raw, list) else set())
    seen_before = set(adopted)

    copied = 0
    by_member: dict[str, int] = {}
    for sub in _RUNTIME_DIRS:
        dest_root = group_dir / sub
        # Files already in the group count as owned - never overwrite them.
        if dest_root.is_dir():
            for dp, _dns, fns in os.walk(dest_root):
                base = os.path.relpath(dp, dest_root)
                for fn in fns:
                    rel = fn if base == "." else f"{base}/{fn}"
                    adopted.add(f"{sub}/{rel}".lower())
        for member in members:
            if member in excluded:
                continue
            src_root = profiles_dir / member / sub
            if not src_root.is_dir():
                continue
            for dp, _dns, fns in os.walk(src_root):
                base = os.path.relpath(dp, src_root)
                for fn in fns:
                    rel = fn if base == "." else f"{base}/{fn}"
                    key = f"{sub}/{rel}".lower()
                    if key in adopted:
                        continue
                    src = Path(dp) / fn
                    if src.is_symlink():
                        continue
                    dst = dest_root / rel
                    try:
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(str(src), str(dst))
                        adopted.add(key)
                        copied += 1
                        by_member[member] = by_member.get(member, 0) + 1
                    except OSError as exc:
                        log_fn(f"Profile Group: could not copy "
                               f"{sub}/{rel} from '{member}': {exc}")
    if adopted != seen_before:
        _update_key(group_dir, "group_adopted_runtime", sorted(adopted))
    if copied:
        log_fn(f"Profile Group: adopted {copied} overwrite/Root_Folder "
               f"file(s) ({', '.join(f'{n} from {m}' for m, n in by_member.items())}).")


def _adopt_first_plugins(game, group_dir: Path, profiles_dir: Path,
                         members: list[str]) -> None:
    """First materialize only: seed the group's plugins.txt/loadorder.txt from
    the members' own files (OR-enabled, first-occurrence order) so intra-
    member plugin ordering carries over instead of index-derived order."""
    from Utils.plugins import PluginEntry, write_loadorder, write_plugins
    star = bool(getattr(game, "plugins_use_star_prefix", True))
    order, enabled = _merge_plugins(profiles_dir, members, star)
    if not order:
        return
    entries = [PluginEntry(name=n, enabled=enabled.get(n, False)) for n in order]
    write_loadorder(group_dir / "loadorder.txt", entries)
    write_plugins(group_dir / "plugins.txt", entries, star_prefix=star)

    if not (getattr(game, "groundcover_plugin_extensions", ()) or ()):
        return
    from Utils.profiles.state import groundcover_plugins_configured
    configured = False
    selected: dict[str, str] = {}
    for member in members:
        member_dir = profiles_dir / member
        if not groundcover_plugins_configured(member_dir):
            continue
        configured = True
        for name in read_groundcover_plugins(member_dir):
            selected.setdefault(name.lower(), name)
    if configured:
        available = {name.lower() for name in order}
        write_groundcover_plugins(
            group_dir,
            [name for low, name in selected.items() if low in available],
        )


# ---------------------------------------------------------------------------
# Group-aware full mod removal
# ---------------------------------------------------------------------------

def _member_side_remove(game, profiles_dir: Path, member: str, folder: str,
                        log) -> None:
    """Delete one member's copy of a mod: plugins, folder, modlist row,
    and catalog rows."""
    member_dir = profiles_dir / member
    member_staging = member_dir / "mods"
    try:
        from Utils.mods.remove import _remove_plugins_for_mods
        _remove_plugins_for_mods(game, member_dir, member_staging, [folder], log)
    except Exception as exc:
        log(f"member plugin cleanup failed for '{folder}': {exc}")
    target = member_staging / folder
    try:
        if target.is_symlink():
            target.unlink()
        elif target.is_dir():
            shutil.rmtree(target)
    except OSError as exc:
        log(f"could not delete '{member}/{folder}': {exc}")
    member_modlist = member_dir / "modlist.txt"
    try:
        with modlist_lock(member_modlist):
            entries = read_modlist(member_modlist)
            kept = [e for e in entries if e.name != folder]
            if len(kept) != len(entries):
                write_modlist(member_modlist, kept)
    except Exception as exc:
        log(f"could not update '{member}' modlist: {exc}")
    try:
        from Utils.filegraph.service import FileGraphService
        library = FileGraphService.open_library(game, member_dir, log_fn=log)
        profile = library.open_profile(member_dir)
        profile.forget_deployed_mods([folder])
        library.remove_mod(folder)
    except Exception as exc:
        log(f"member catalog cleanup failed for '{folder}': {exc}")


def remove_member_mod(game, member_dir: Path, folder: str, *,
                      log_fn=None) -> None:
    """Delete ONE member profile's copy of a mod - files, plugins, modlist row,
    catalog rows - without touching the group.

    For when the group has already moved on from that folder: a Change Version
    update whose reconcile renamed the group entry to the newly installed
    version (same identity, newer) leaves only the member's stale old copy to
    clean up. Going through remove_mods there would resolve staging against the
    ACTIVE profile (the group) and delete nothing."""
    log = log_fn or app_log
    if game is None or not folder:
        return
    _member_side_remove(game, member_dir.parent, member_dir.name, folder, log)


def remove_mods_from_group(game, group_dir: Path, mod_names: list[str],
                           log_fn=None, *,
                           delete_member_copies: bool = True) -> list[str]:
    """Fully remove *mod_names* from a group: undeploy via Filegraph state,
    strip plugins group-side, delete EVERY member copy of each identity (a
    second lister would otherwise resurrect it next materialize), then the
    group link/catalog/adopted state. delete_member_copies=False = DETACH
    (move-to-owning-member): members keep their files, so a locked member
    doesn't block it. Does NOT touch the group's modlist.txt - the caller
    removes the rows (remove_mods contract) for the names RETURNED."""
    log = log_fn or app_log
    if game is None or not mod_names:
        return []
    if delete_member_copies:
        blocked = locked_owners(group_dir, mod_names)
        if blocked:
            for _n, _m in blocked.items():
                log(f"Profile Group: '{_n}' belongs to the LOCKED profile "
                    f"'{_m}' - not removed. Switch to that profile to remove "
                    f"it there, or unlock it.")
            mod_names = [n for n in mod_names if n not in blocked]
            if not mod_names:
                return []
    with group_build_lock(group_dir):
        profiles_dir = group_dir.parent
        staging = group_dir / "mods"
        identity_map = _read_identity_map(group_dir)

        from Utils.filegraph.service import FileGraphService
        group_library = FileGraphService.open_library(
            game, group_dir, log_fn=log)
        group_profile = group_library.open_profile(group_dir)

        # 1. Undeploy from the game dir while the group's catalog/links are
        # still intact (identity checks resolve through the links).
        try:
            deploy_active = bool(game.get_deploy_active())
        except Exception:
            deploy_active = True
        if deploy_active:
            try:
                from Utils.mods.remove import undeploy_catalog_mods
                undeploy_catalog_mods(
                    game, group_profile, staging, mod_names, log_fn=log)
            except Exception as exc:
                log(f"undeploy during group remove failed: {exc}")
        else:
            log("no deployment is active - skipping undeploy of removed mod(s).")

        # 2. Group-side plugin cleanup (resolves via group catalog/links).
        try:
            from Utils.mods.remove import _remove_plugins_for_mods
            _remove_plugins_for_mods(game, group_dir, staging, mod_names, log)
        except Exception as exc:
            log(f"group plugin cleanup during remove failed: {exc}")

        # 3. Member-side removal: the owner's copy AND any other member's copy
        # of the same identity (a second lister would otherwise resurrect the
        # mod on the next materialize, after the owner's copy was deleted).
        if delete_member_copies:
            members = get_members(group_dir)
            for name in mod_names:
                owner = owner_of(group_dir, name)
                if owner is None:
                    _p = staging / name
                    if _p.is_dir() and not _p.is_symlink():
                        log(f"Profile Group: '{name}' is a group-local mod - "
                            f"removing it from the group only.")
                    else:
                        log(f"Profile Group: no owning member found for "
                            f"'{name}' - removing the group entry only.")
                    continue
                key = identity_map.get(name, f"name:{name}")
                removed_pairs = {owner}
                _member_side_remove(game, profiles_dir, owner[0], owner[1], log)
                for member in members:
                    member_dir = profiles_dir / member
                    if not member_dir.is_dir():
                        continue
                    for e in read_modlist(member_dir / "modlist.txt"):
                        if e.is_separator:
                            continue
                        k, _v = _mod_identity_and_version(
                            member_dir / "mods", e.name)
                        if k == key and (member, e.name) not in removed_pairs:
                            removed_pairs.add((member, e.name))
                            log(f"Profile Group: '{member}/{e.name}' is the "
                                f"same mod - removed as well.")
                            _member_side_remove(game, profiles_dir, member,
                                                e.name, log)

        # 4. Group-side cleanup: links (or the real folder, for a group-LOCAL
        # mod like wizard-installed SMAPI), catalog rows, adopted per-mod state.
        for name in mod_names:
            link = staging / name
            try:
                if link.is_symlink():
                    link.unlink()
                elif link.is_dir() and delete_member_copies:
                    shutil.rmtree(link)
            except OSError as exc:
                log(f"could not remove group entry '{name}': {exc}")
        try:
            group_profile.forget_deployed_mods(mod_names)
            for name in mod_names:
                group_library.remove_mod(name)
        except Exception as exc:
            log(f"group catalog cleanup during remove failed: {exc}")
        _reconcile_mod_state(group_dir, profiles_dir, [], list(mod_names), [])
        identity_map = _read_identity_map(group_dir)
        for name in mod_names:
            identity_map.pop(name, None)
        _write_identity_map(group_dir, identity_map)
    return list(mod_names)
