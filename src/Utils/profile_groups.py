"""
profile_groups.py
Profile Groups: named, ordered combinations of profiles that deploy together
as one merged virtual profile.

A group is stored as a normal profile directory (profiles/<group_name>/) with
profile_settings.is_group == True and profile_settings.group_members == [...]
(ordered, index 0 = highest priority — same convention as modlist.txt itself).
There is no separate registry file: "list all groups" means "scan profiles/*,
read profile_settings, filter is_group", mirroring every other profile-listing
helper in game_helpers.py.

materialize_group() regenerates modlist.txt / plugins.txt / loadorder.txt / the
relevant profile_state.json sub-maps from the members' *current* on-disk
state. It is safe to call before every deploy and on every profile switch:
restore/undeploy (Utils/deploy_standard.py::restore_data_core) never re-reads
modlist.txt to reconstruct a previous deploy — it replays the filemap/backup
artifacts written at deploy time — so overwriting modlist.txt here never
corrupts an existing deployment.

Every member profile must use profile-specific mods (its own private
``<profile_dir>/mods``, not the shared game-wide pool). This is required, not
just allowed: a profile that shares the common pool inherits whatever that
pool's "default" profile looked like when the game was added (and anything
"Refresh Modlist" has synced in since), so its modlist.txt can carry dozens or
hundreds of mods the user never explicitly chose for that profile — exactly
the noise a purpose-built profile (and therefore a group built from one)
should never see. Requiring profile-specific members guarantees every
profile's modlist.txt reflects only what was deliberately put there.

The group itself is ALSO profile-specific internally (profile_settings.
profile_specific_mods is set alongside is_group at creation) so it gets its
own private ``<group_dir>/mods`` — materialize_group() populates that folder
by linking (hardlink or symlink, matching the game's configured deploy mode)
each winning mod's files in from whichever member actually owns it. This is
necessary, not optional: unlike the old shared-pool design, members now keep
their mod files in genuinely different physical locations, so the group must
assemble its own copy of "the files that should deploy" rather than just
writing a modlist.txt that references an already-shared pool.

Hard guarantee: every physical mod is linked into the group's own mods/
folder at most once — never duplicated — so a shared requirement mod that
several member profiles happen to have installed separately (e.g. Content
Patcher under Stardew Valley/SMAPI, installed once per profile since profile-
specific profiles don't share files) is staged/deployed exactly once no
matter how many members have their own copy of it. This holds even when two
members' copies of "the same" mod ended up under DIFFERENT local folder
names — independently-curated Nexus collections routinely install one mod
under two different names (e.g. "Ridgeside Village" vs "Ridgeside Village
2.5.17", the same mod ID with a version suffix baked into one collection's
own naming) — because the merge keys on each mod's Nexus mod ID (read from
its meta.ini) when one is available, not the bare folder name; see
_mod_identity_and_version(). Two folders that genuinely are different mods
but happen to share a generic name (no Nexus ID on one or both) still fall
back to name-based identity, same as before. When both copies of the same
mod ID are actually enabled, whichever has the strictly newer version wins
regardless of member priority order (see _merge_modlist) — a tie, or an
unreadable version, falls back to priority order.

Per-mod enabled state is the OR across all members: a mod is enabled in the
merged group if it is enabled in ANY member profile that lists it. This is
deliberate — the point of a group is to combine each member's *distinct*
active choices (e.g. one profile's QoL picks + another's decor picks) without
one profile's stance silently overriding another's for a mod they disagree
on. Member priority order still decides each mod's position (and therefore
ordinary file-conflict resolution between two different mods touching the
same file), which occurrence's "locked" flag survives, and — new since
members are physically separate — *whose copy of the file bytes* gets linked
in when two members both have a mod by the same name, but never whether it
is deployed at all.
"""

from __future__ import annotations

import threading
from pathlib import Path

from Utils.filemap import OVERWRITE_NAME
from Utils.modlist import ModEntry, read_modlist, write_modlist
from Utils.plugins import PluginEntry, read_loadorder, read_plugins, write_loadorder, write_plugins
from Utils.profile_state import (
    merge_profile_settings,
    profile_uses_specific_mods,
    read_collapsed_seps,
    read_disabled_plugins,
    read_excluded_mod_files,
    read_ignored_missing_requirements,
    read_mod_notes,
    read_mod_strip_prefixes,
    read_plugin_locks,
    read_profile_settings,
    read_root_folder_state,
    read_separator_colors,
    read_separator_deploy_paths,
    read_separator_locks,
    write_collapsed_seps,
    write_disabled_plugins,
    write_excluded_mod_files,
    write_ignored_missing_requirements,
    write_mod_notes,
    write_mod_strip_prefixes,
    write_plugin_locks,
    write_profile_settings,
    write_root_folder_state,
    write_separator_colors,
    write_separator_deploy_paths,
    write_separator_locks,
)

class GroupValidationError(ValueError):
    """Raised when a Profile Group create/edit request violates an invariant
    (duplicate name, missing member, nested group, member not profile-specific)."""


_group_build_locks: dict[str, threading.RLock] = {}
_group_build_locks_guard = threading.Lock()


def group_build_lock(profile_dir: Path) -> threading.RLock:
    """Process-wide lock, one per group profile dir, serializing concurrent
    materialize_group() calls for the same group against each other.

    materialize_group() writes the group's own modlist.txt/plugins.txt/
    loadorder.txt/profile_state.json (reconciled from its members' current
    state — see _reconcile_group_modlist/_reconcile_group_plugins). Without
    this lock, two overlapping calls (e.g. a profile-switch materialize
    racing a deploy's own materialize moments later) could interleave their
    reads and writes of those same small files and produce an inconsistent
    reconcile. The group owns no physical mods/ folder to race over any
    more — every mod resolves straight to its real owning member via
    build_group_resolver — so this is purely a small-file read/write race
    now, not the large-scale physical-staging race this lock originally
    guarded against."""
    key = str(profile_dir)
    with _group_build_locks_guard:
        lock = _group_build_locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _group_build_locks[key] = lock
        return lock


def _profiles_root(game) -> Path:
    return game.get_profile_root() / "profiles"


def is_group(profile_dir: "Path | None") -> bool:
    """Return True if *profile_dir* is a Profile Group (synthetic merged profile)."""
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


def _validate_member(game, profiles_dir: Path, member_name: str) -> None:
    if not getattr(game, "profile_groups_supported", True):
        raise GroupValidationError(
            f"Profile Groups aren't supported for {getattr(game, 'name', 'this game')} "
            "yet — its mod-merging logic doesn't have a per-mod/enabled-state "
            "concept the group's virtual merge depends on."
        )
    member_dir = profiles_dir / member_name
    if not member_dir.is_dir():
        raise GroupValidationError(f"Profile '{member_name}' does not exist.")
    if is_group(member_dir):
        raise GroupValidationError(
            f"'{member_name}' is itself a Profile Group — groups cannot be nested."
        )
    if not profile_uses_specific_mods(member_dir):
        raise GroupValidationError(
            f"'{member_name}' shares the game's common mod pool. Profile Group "
            "members must use profile-specific mods (its own private mod "
            "folder), so the group only ever sees mods you explicitly added to "
            "that profile — not everything in the shared pool. Create a new "
            "profile with that option checked to use it in a group."
        )


def create_group(game, group_name: str, members: list[str]) -> Path:
    """Create a new Profile Group named *group_name* combining *members* (in
    priority order, index 0 = highest). Raises GroupValidationError if invalid."""
    profiles_dir = _profiles_root(game)
    group_dir = profiles_dir / group_name
    if group_dir.exists():
        raise GroupValidationError(f"A profile or group named '{group_name}' already exists.")
    if not members:
        raise GroupValidationError("A Profile Group needs at least one member profile.")
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
    # The group owns no mods/ folder of its own — every mod resolves straight
    # to whichever member profile actually owns it (see build_group_resolver).
    # It DOES own a small real overwrite/ and Root_Folder/ (runtime files,
    # manual additions, LOOT clean-plugin output — normal-sized, not the
    # "duplicate every member's mod files" problem); profile_specific_mods
    # gives it those via the existing get_effective_overwrite_path()/
    # get_effective_root_folder_path() resolution.
    (group_dir / "overwrite").mkdir(exist_ok=True)
    (group_dir / "Root_Folder").mkdir(exist_ok=True)
    write_profile_settings(group_dir, {
        "is_group": True,
        "group_members": list(members),
        "profile_specific_mods": True,
    })
    return group_dir


def add_member(game, profile_dir: Path, member_name: str) -> None:
    profiles_dir = _profiles_root(game)
    _validate_member(game, profiles_dir, member_name)
    members = get_members(profile_dir)
    if member_name in members:
        raise GroupValidationError(f"'{member_name}' is already a member of this group.")
    members.append(member_name)
    set_members(profile_dir, members)


def remove_member(profile_dir: Path, member_name: str) -> None:
    set_members(profile_dir, [m for m in get_members(profile_dir) if m != member_name])


def move_member(profile_dir: Path, member_name: str, new_index: int) -> None:
    """Reposition *member_name* to *new_index* in the priority order."""
    members = get_members(profile_dir)
    if member_name not in members:
        return
    members.remove(member_name)
    new_index = max(0, min(new_index, len(members)))
    members.insert(new_index, member_name)
    set_members(profile_dir, members)


def rename_profile_everywhere(game, old_name: str, new_name: str) -> list[str]:
    """Rewrite *old_name* to *new_name* in every group's member list for *game*.
    Returns the names of groups that were updated."""
    updated = []
    profiles_dir = _profiles_root(game)
    for group_name in list_groups(game):
        group_dir = profiles_dir / group_name
        members = get_members(group_dir)
        if old_name in members:
            set_members(group_dir, [new_name if m == old_name else m for m in members])
            updated.append(group_name)
    return updated


def member_of_groups(game, name: str) -> list[str]:
    """Return the names of every Profile Group for *game* that currently lists
    *name* as a member — read-only counterpart to
    :func:`remove_profile_everywhere`, used to warn before an action (like
    converting a profile away from profile-specific mods) would silently
    invalidate a group that depends on it."""
    profiles_dir = _profiles_root(game)
    return [g for g in list_groups(game) if name in get_members(profiles_dir / g)]


def remove_profile_everywhere(game, name: str) -> list[str]:
    """Prune *name* out of every group's member list for *game*.
    Returns the names of groups that referenced it."""
    affected = []
    profiles_dir = _profiles_root(game)
    for group_name in list_groups(game):
        group_dir = profiles_dir / group_name
        members = get_members(group_dir)
        if name in members:
            set_members(group_dir, [m for m in members if m != name])
            affected.append(group_name)
    return affected


# ---------------------------------------------------------------------------
# Materialization
# ---------------------------------------------------------------------------

def _mod_identity_and_version(profiles_dir: Path, member_name: str,
                              mod_name: str) -> "tuple[str, str]":
    """Best-effort dedup identity + version for one member's mod folder: its
    Nexus mod ID and version string (both read from meta.ini) when
    resolvable, else the bare folder name as identity with no version.

    The same physical Nexus mod can end up under a different LOCAL folder
    name across independently-installed collections (one curator's
    collection.json names it "Ridgeside Village", another's names the same
    mod ID "Ridgeside Village 2.5.17") — keying the merge on the folder name
    alone would treat those as two unrelated mods and stage/deploy both,
    which is exactly the kind of duplicate/conflicting content a group must
    never produce. fileId is deliberately NOT part of the identity: two
    members installing different downloaded versions of the same mod are
    still the same mod for merge purposes — see _merge_modlist for how the
    version is then used to pick which copy's files actually win."""
    try:
        from Nexus.nexus_meta import read_meta
        meta_path = profiles_dir / member_name / "mods" / mod_name / "meta.ini"
        if meta_path.is_file():
            meta = read_meta(meta_path)
            if meta.mod_id:
                return f"nexus:{meta.mod_id}", (meta.version or "")
    except Exception:
        pass
    return f"name:{mod_name}", ""


def _merge_modlist(profiles_dir: Path, members: list[str],
                   log_fn) -> tuple[list[ModEntry], dict[str, str]]:
    """Combine members' modlists into one flat list (no per-member separator
    headers — those were more confusing than useful once "Refresh Modlist"
    causes near-identical mod sets across members): each mod IDENTITY (see
    _mod_identity_and_version — Nexus mod ID when resolvable, else folder
    name) appears at most once (never duplicated → never double-deployed),
    enabled if enabled in ANY member that lists it (see module docstring).

    Whose copy of the files wins is decided in this order: (1) among members
    that actually ENABLE it, whichever has the STRICTLY NEWER version per its
    meta.ini (e.g. 2.5.17 beats 2.5.16, even from a lower-priority member) —
    this is the one place member priority order is NOT the final word; (2) a
    tie, or either version unparseable/missing, falls back to member priority
    order (highest-priority enabling member wins), the same rule used
    everywhere else in a group; (3) if no member enables it at all, whichever
    member mentions it first at all, so a mod only one profile turns on
    doesn't get silently pinned to a higher-priority profile's spot just
    because that profile also happens to list it.

    Returns (merged_entries, home_member) — home_member maps each OUTPUT
    entry's folder name to the member profile whose copy of it "won" (used
    by the caller to know whose physical files to link into the group's own
    staging)."""
    from Nexus.nexus_update_checker import _parse_version

    member_entries: list[tuple[str, list[ModEntry]]] = []
    for member_name in members:
        member_dir = profiles_dir / member_name
        if not member_dir.is_dir():
            log_fn(f"Profile Group: member '{member_name}' no longer exists — skipping.")
            continue
        entries = read_modlist(member_dir / "modlist.txt")
        if entries:
            member_entries.append((member_name, entries))

    # Resolve each entry's dedup identity + version once up front — reused
    # by both the champion pass and the log-diagnostics below.
    identity: dict[tuple[str, str], str] = {}
    version_of: dict[tuple[str, str], str] = {}
    names_by_key: dict[str, set[str]] = {}
    for member_name, entries in member_entries:
        for e in entries:
            if e.is_separator:
                continue
            key, version = _mod_identity_and_version(profiles_dir, member_name, e.name)
            identity[(member_name, e.name)] = key
            version_of[(member_name, e.name)] = version
            names_by_key.setdefault(key, set()).add(e.name)

    # Pass 1: OR the enabled state per mod identity. Among members that
    # enable it, the "champion" (whose files/locked-flag win) starts as the
    # first (highest-priority) enabler and is overtaken only by a STRICTLY
    # newer version from a lower-priority member — see docstring above.
    enabled_union: dict[str, bool] = {}
    home_member_any: dict[str, str] = {}
    champions: dict[str, dict] = {}
    first_enabler: dict[str, str] = {}
    for member_name, entries in member_entries:
        for e in entries:
            if e.is_separator:
                continue
            key = identity[(member_name, e.name)]
            home_member_any.setdefault(key, member_name)
            if not e.enabled:
                enabled_union.setdefault(key, False)
                continue
            enabled_union[key] = True
            first_enabler.setdefault(key, member_name)
            version_str = version_of[(member_name, e.name)]
            parsed = _parse_version(version_str)
            champ = champions.get(key)
            if champ is None or (parsed is not None and champ["version"] is not None
                                 and parsed > champ["version"]):
                champions[key] = {"member": member_name, "version": parsed,
                                  "version_str": version_str, "locked": e.locked}
    home_member_by_key = {**home_member_any,
                          **{k: v["member"] for k, v in champions.items()}}
    locked_for_enabled = {k: v["locked"] for k, v in champions.items()}

    # Diagnostics: a cross-named duplicate, and whether a strictly-newer
    # version (rather than plain priority order) is what decided the winner.
    for key, names in names_by_key.items():
        if not key.startswith("nexus:") or len(names) <= 1:
            continue
        champ = champions.get(key)
        winner = home_member_by_key.get(key)
        if champ is not None and champ["version_str"] and winner != first_enabler.get(key):
            reason = f"newer version {champ['version_str']} wins over member priority order"
        else:
            reason = "member priority order"
        log_fn(f"Profile Group: {', '.join(sorted(names))} are the same "
               f"Nexus mod (id {key.split(':', 1)[1]}) under different "
               f"names across members — using '{winner}'s copy ({reason}).")

    # Pass 2: walk members in priority order, flat (no separator headers),
    # placing each identity in the slot of its computed "home" member
    # (deferring it past members that merely mention it without being the
    # one that turns it on). Real user-authored separators from a member's
    # own modlist still pass through, deduped by literal name like today —
    # they aren't Nexus mods and have no identity to resolve.
    merged: list[ModEntry] = []
    seen_names: set[str] = set()       # separators
    seen_keys: set[str] = set()        # mods, by identity
    home_member: dict[str, str] = {}   # OUTPUT folder name -> owning member
    for member_name, entries in member_entries:
        for e in entries:
            if e.is_separator:
                if e.name in seen_names:
                    continue
                seen_names.add(e.name)
                merged.append(e)
                continue
            key = identity[(member_name, e.name)]
            if key in seen_keys:
                continue
            if home_member_by_key.get(key) != member_name:
                continue
            seen_keys.add(key)
            final_enabled = enabled_union.get(key, e.enabled)
            final_locked = locked_for_enabled.get(key, False) if final_enabled else False
            merged.append(ModEntry(name=e.name, enabled=final_enabled,
                                   locked=final_locked, is_separator=False))
            home_member[e.name] = member_name
    return merged, home_member


def _merge_plugins(profiles_dir: Path, members: list[str], star_prefix: bool):
    """Return (ordered plugin names, {name: enabled}) merged across members:
    each plugin appears at most once (deduped case-insensitively), enabled if
    enabled in ANY member — same OR rule as _merge_modlist, for the same
    reason (a shared dependency's plugin shouldn't get vetoed by a profile
    that happens to have it turned off)."""
    member_data: list[tuple[list[str], dict[str, bool]]] = []
    for member_name in members:
        member_dir = profiles_dir / member_name
        if not member_dir.is_dir():
            continue
        loadorder = read_loadorder(member_dir / "loadorder.txt")
        plugins = read_plugins(member_dir / "plugins.txt", star_prefix=star_prefix)
        enabled_by_name = {p.name: p.enabled for p in plugins}
        # loadorder.txt is the superset (includes vanilla/disabled-only entries
        # on legacy engines); walk it first, then pick up any plugins.txt-only
        # stragglers so nothing enabled is silently dropped.
        names_in_order = list(loadorder)
        seen_local = {n.lower() for n in names_in_order}
        for p in plugins:
            if p.name.lower() not in seen_local:
                names_in_order.append(p.name)
                seen_local.add(p.name.lower())
        member_data.append((names_in_order, enabled_by_name))

    # Pass 1: OR the enabled state per plugin (keyed case-insensitively), and
    # remember the first-seen spelling to use in the output.
    enabled_union: dict[str, bool] = {}
    display_name: dict[str, str] = {}
    for names_in_order, enabled_by_name in member_data:
        for name in names_in_order:
            key = name.lower()
            display_name.setdefault(key, name)
            if enabled_by_name.get(name, False):
                enabled_union[key] = True
            else:
                enabled_union.setdefault(key, False)

    # Pass 2: first-occurrence position, substituting the OR'd enabled state.
    merged_order: list[str] = []
    merged_enabled: dict[str, bool] = {}
    seen_lower: set[str] = set()
    for names_in_order, _enabled_by_name in member_data:
        for name in names_in_order:
            key = name.lower()
            if key in seen_lower:
                continue
            seen_lower.add(key)
            shown = display_name[key]
            merged_order.append(shown)
            merged_enabled[shown] = enabled_union.get(key, False)
    return merged_order, merged_enabled


def _merge_keyed_dict(profiles_dir: Path, members: list[str], reader) -> dict:
    """First-occurrence(by member priority)-wins keyed merge."""
    merged: dict = {}
    for member_name in members:
        member_dir = profiles_dir / member_name
        if not member_dir.is_dir():
            continue
        for k, v in reader(member_dir).items():
            merged.setdefault(k, v)
    return merged


def _merge_union_set(profiles_dir: Path, members: list[str], reader) -> set:
    merged: set = set()
    for member_name in members:
        member_dir = profiles_dir / member_name
        if member_dir.is_dir():
            merged |= set(reader(member_dir))
    return merged


def _reconcile_group_modlist(current: list[ModEntry],
                             prescribed: list[ModEntry]) -> list[ModEntry]:
    """"Adopt once, reconcile thereafter": merge the group's own previous
    modlist with a freshly recomputed merge from its members.

    - A mod present in both keeps the GROUP's own entry (position/enabled/
      locked) verbatim — once adopted, a user's in-group edit is permanent
      and is never silently re-flipped by a member's later change (this is a
      deliberate departure from continuously re-computing the OR-merge on
      every materialize, which made in-group edits impossible to keep).
    - A mod only in *prescribed* (a member added it, or this is the group's
      very first materialize) is appended at the end — new arrivals never
      jump ahead of mods the user already arranged.
    - A mod only in *current* (no member references it any more) is dropped.
    - Any separator the user added directly in the group (organizing the
      merged list) is preserved verbatim in its existing position;
      _merge_modlist never emits separators of its own, so there's nothing
      to reconcile it against.
    """
    prescribed_by_name = {e.name: e for e in prescribed if not e.is_separator}
    result: list[ModEntry] = []
    seen: set[str] = set()
    for e in current:
        if e.is_separator:
            result.append(e)
            continue
        if e.name in prescribed_by_name:
            result.append(e)
            seen.add(e.name)
        # else: no member references this mod any more — drop it.
    for e in prescribed:
        if e.is_separator or e.name in seen:
            continue
        result.append(e)
        seen.add(e.name)
    return result


def _reconcile_group_plugins(
    current_order: list[str], current_enabled: dict[str, bool],
    prescribed_order: list[str], prescribed_enabled: dict[str, bool],
) -> "tuple[list[str], dict[str, bool]]":
    """Same "adopt once" reconciliation as _reconcile_group_modlist, for the
    plugin load order: kept plugins retain the group's own order/enabled
    state; new plugins (from a member, or LOOT's sort of the very first
    materialize) are appended; plugins no member references any more are
    dropped."""
    prescribed_set = set(prescribed_order)
    order = [n for n in current_order if n in prescribed_set]
    seen = set(order)
    for n in prescribed_order:
        if n not in seen:
            order.append(n)
            seen.add(n)
    enabled = {n: current_enabled.get(n, prescribed_enabled.get(n, False)) for n in order}
    return order, enabled


def _sort_with_loot(game, profile_dir: Path, resolver: dict[str, Path],
                     profiles_dir: Path, members: list[str],
                     merged_order: list[str], merged_enabled: dict[str, bool], log_fn):
    """Best-effort LOOT auto-sort of a freshly merged load order. Never raises —
    naive block-concatenation across profiles can violate plugin master
    dependencies, so this is run automatically for groups, but a failure here
    must not block the deploy; it just leaves the concatenated order in place."""
    try:
        from LOOT.loot_sorter import is_available, sort_plugins
    except Exception:
        return merged_order
    if not getattr(game, "loot_sort_enabled", False) or not merged_order:
        return merged_order
    try:
        if not is_available():
            return merged_order
        plugin_locks = _merge_keyed_dict(profiles_dir, members, read_plugin_locks)
        locked = [n for n in merged_order if plugin_locks.get(n)]
        unlocked = [n for n in merged_order if not plugin_locks.get(n)]
        if not unlocked:
            return merged_order
        enabled_set = {n for n in unlocked if merged_enabled.get(n, False)}
        result = sort_plugins(
            plugin_names=unlocked,
            enabled_set=enabled_set,
            game_name=getattr(game, "name", ""),
            game_path=game.get_game_path(),
            # The group has no mods/ folder of its own — resolver maps each
            # mod straight to whichever member profile actually owns it (see
            # build_group_resolver / resolve_mod_dir in deploy_shared.py).
            staging_root=resolver,
            filemap_path=profile_dir / "filemap.txt",
            game_type_attr=getattr(game, "loot_game_type", ""),
            game_id=game.game_id,
            masterlist_url=getattr(game, "loot_masterlist_url", ""),
            masterlist_repo=getattr(game, "loot_masterlist_repo", ""),
            game_data_dir=(game.get_vanilla_plugins_path()
                           if hasattr(game, "get_vanilla_plugins_path") else None),
            log_fn=lambda m: log_fn(f"[loot] {m}"),
        )
        return locked + list(result.sorted_names)
    except Exception as exc:
        log_fn(f"Profile Group: LOOT auto-sort skipped ({exc}).")
        return merged_order


def materialize_group(game, profile_dir: Path, *, log_fn=None) -> None:
    """Regenerate a Profile Group's modlist.txt / plugins.txt / loadorder.txt /
    profile_state.json sub-maps from its members' current on-disk state.

    "Adopt once, reconcile thereafter": the group owns no mods/ folder of its
    own (see build_group_resolver — every mod resolves straight to whichever
    member profile actually owns it), and its modlist/plugin order is
    authoritative once adopted. A mod/plugin a member still references keeps
    the group's own position/enabled/locked state exactly as the user left
    it; only mods/plugins newly added by a member get appended, and ones no
    member references any more get dropped (see _reconcile_group_modlist /
    _reconcile_group_plugins). This deliberately replaces the previous
    continuous "OR across all members" recompute, which made an in-group
    edit impossible to keep — the trade-off is intentional, not a bug.

    Safe to call repeatedly (before every deploy and on every profile switch).
    """
    _log = log_fn or (lambda _msg: None)
    if not is_group(profile_dir):
        return
    with group_build_lock(profile_dir):
        profiles_dir = profile_dir.parent
        members = get_members(profile_dir)

        prescribed_entries, home_member = _merge_modlist(profiles_dir, members, _log)
        current_entries = read_modlist(profile_dir / "modlist.txt")
        final_entries = _reconcile_group_modlist(current_entries, prescribed_entries)
        write_modlist(profile_dir / "modlist.txt", final_entries)

        resolver = build_group_resolver(profile_dir, final_entries, home_member)

        if getattr(game, "plugin_extensions", None):
            star_prefix = bool(getattr(game, "plugins_use_star_prefix", True))
            prescribed_order, prescribed_enabled = _merge_plugins(profiles_dir, members, star_prefix)
            prescribed_order = _sort_with_loot(game, profile_dir, resolver, profiles_dir, members,
                                                prescribed_order, prescribed_enabled, _log)
            current_plugin_entries = read_plugins(profile_dir / "plugins.txt", star_prefix=star_prefix)
            current_order = [e.name for e in current_plugin_entries]
            current_enabled = {e.name: e.enabled for e in current_plugin_entries}
            final_order, final_enabled = _reconcile_group_plugins(
                current_order, current_enabled, prescribed_order, prescribed_enabled,
            )
            entries = [PluginEntry(name=n, enabled=final_enabled.get(n, False)) for n in final_order]
            write_loadorder(profile_dir / "loadorder.txt", entries)
            write_plugins(profile_dir / "plugins.txt", entries, star_prefix=star_prefix)

        merge_profile_settings(profile_dir, {"is_group": True, "group_members": members})
        write_disabled_plugins(profile_dir, _merge_keyed_dict(profiles_dir, members, read_disabled_plugins))
        write_excluded_mod_files(profile_dir, _merge_keyed_dict(profiles_dir, members, read_excluded_mod_files))
        write_mod_notes(profile_dir, _merge_keyed_dict(profiles_dir, members, read_mod_notes))
        write_plugin_locks(profile_dir, _merge_keyed_dict(profiles_dir, members, read_plugin_locks))
        write_mod_strip_prefixes(profile_dir, _merge_keyed_dict(profiles_dir, members, read_mod_strip_prefixes))
        write_separator_locks(profile_dir, _merge_keyed_dict(profiles_dir, members, read_separator_locks))
        write_separator_colors(profile_dir, _merge_keyed_dict(profiles_dir, members, read_separator_colors))
        write_separator_deploy_paths(profile_dir, _merge_keyed_dict(profiles_dir, members, read_separator_deploy_paths))
        write_collapsed_seps(profile_dir, _merge_union_set(profiles_dir, members, read_collapsed_seps))
        write_ignored_missing_requirements(
            profile_dir, _merge_union_set(profiles_dir, members, read_ignored_missing_requirements)
        )
        write_root_folder_state(
            profile_dir,
            any(read_root_folder_state(profiles_dir / m) for m in members if (profiles_dir / m).is_dir()),
        )


# ---------------------------------------------------------------------------
# Virtual merge resolver — see the "Profile Groups: replace physical
# symlink-staging with a virtual merge" plan. A group owns no mods/ folder
# of its own; every consumer that used to read one (filemap/index building,
# deploy/restore/undeploy, LOOT plugin discovery, flatpak sandbox grants)
# instead gets this {mod_name: real_owning_member_dir} map wherever it used
# to receive a single staging_root Path — see resolve_mod_dir in
# deploy_shared.py.
# ---------------------------------------------------------------------------

def build_group_resolver(profile_dir: Path, merged: list[ModEntry],
                         home_member: dict[str, str]) -> dict[str, Path]:
    """Build a {mod_name: real_mod_dir} resolver for a group from an already-
    computed merge (see _merge_modlist) — every enabled mod maps directly to
    its winning member's own mods/ folder, never to a group-owned copy.

    Pure path arithmetic, no disk I/O. See resolve_mod_dir in
    deploy_shared.py for how downstream code consumes this instead of a
    single shared staging_root Path."""
    profiles_dir = profile_dir.parent
    resolver: dict[str, Path] = {OVERWRITE_NAME: profile_dir / "overwrite"}
    for e in merged:
        if e.is_separator or not e.enabled:
            continue
        member = home_member.get(e.name)
        if member:
            resolver[e.name] = profiles_dir / member / "mods" / e.name
    return resolver


def get_group_resolver(profile_dir: Path, *, log_fn=None) -> "dict[str, Path] | None":
    """Build the resolver for *profile_dir* (a group) from its OWN current,
    already-adopted modlist.txt — not a fresh re-merge — so it always
    reflects the user's in-group edits (order/enabled/locked) exactly as
    materialize_group() last reconciled them, never the raw "OR across
    members" result. A fresh _merge_modlist() pass is still used, but only
    for its home_member map (which member currently owns each mod's real
    files) — that can legitimately change between materializes (a member
    updates a mod, say) even when the group's own adopted mod SET hasn't.
    A mod in the group's modlist with no current home_member (no member
    references it any more, but the group hasn't been re-materialized since)
    is simply omitted from the resolver rather than raising — the same
    "deploys nothing until reconciled" degradation build_group_resolver
    already applies to any unresolvable entry.

    Returns None if profile_dir isn't a group. Safe to call any time — it
    only reads members' modlist.txt/meta.ini (cheap) and the group's own
    modlist.txt, never writes or restages anything.
    """
    if not is_group(profile_dir):
        return None
    _log = log_fn or (lambda _msg: None)
    profiles_dir = profile_dir.parent
    members = get_members(profile_dir)
    _, home_member = _merge_modlist(profiles_dir, members, _log)
    current_entries = read_modlist(profile_dir / "modlist.txt")
    return build_group_resolver(profile_dir, current_entries, home_member)
