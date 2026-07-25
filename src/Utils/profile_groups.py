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

Hard guarantee: every physical mod appears at most once in a group's merged
modlist — never duplicated — so a shared requirement mod that several member
profiles happen to have installed (e.g. Content Patcher under Stardew
Valley/SMAPI) is staged/deployed exactly once no matter how many members
reference it.

Per-mod enabled state is the OR across all members: a mod is enabled in the
merged group if it is enabled in ANY member profile that lists it. This is
deliberate — the point of a group is to combine each member's *distinct*
active choices (e.g. one profile's QoL picks + another's decor picks) without
one profile's stance silently overriding another's for a mod they disagree
on. Member priority order still decides each mod's position (and therefore
ordinary file-conflict resolution between two different mods touching the
same file) and which occurrence's "locked" flag survives, but never whether
it is deployed at all.
"""

from __future__ import annotations

from pathlib import Path

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
    (duplicate name, missing member, nested group, profile-specific member)."""


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


def _validate_member(profiles_dir: Path, member_name: str) -> None:
    member_dir = profiles_dir / member_name
    if not member_dir.is_dir():
        raise GroupValidationError(f"Profile '{member_name}' does not exist.")
    if is_group(member_dir):
        raise GroupValidationError(
            f"'{member_name}' is itself a Profile Group — groups cannot be nested."
        )
    if profile_uses_specific_mods(member_dir):
        raise GroupValidationError(
            f"'{member_name}' uses profile-specific mods (its own private mod "
            "folder) and can't be combined into a Profile Group, which requires "
            "all members to share the game's common mod pool."
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
        _validate_member(profiles_dir, m)

    group_dir.mkdir(parents=True, exist_ok=True)
    (group_dir / "modlist.txt").touch()
    (group_dir / "plugins.txt").touch()
    (group_dir / "loadorder.txt").touch()
    write_profile_settings(group_dir, {"is_group": True, "group_members": list(members)})
    return group_dir


def add_member(game, profile_dir: Path, member_name: str) -> None:
    profiles_dir = _profiles_root(game)
    _validate_member(profiles_dir, member_name)
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

def _merge_modlist(profiles_dir: Path, members: list[str], log_fn) -> list[ModEntry]:
    """Combine members' modlists into one flat list (no per-member separator
    headers — those were more confusing than useful once "Refresh Modlist"
    causes near-identical mod sets across members): each mod name appears at
    most once (never duplicated → never double-deployed), enabled if enabled
    in ANY member that lists it (see module docstring). A mod's position
    comes from whichever member's priority slot it's placed in below —
    preferring the highest-priority member that actually enables it, falling
    back to first mention at all if no member enables it — so a mod only
    one profile turns on doesn't get silently pinned to a higher-priority
    profile's spot just because that profile also happens to list it."""
    member_entries: list[tuple[str, list[ModEntry]]] = []
    for member_name in members:
        member_dir = profiles_dir / member_name
        if not member_dir.is_dir():
            log_fn(f"Profile Group: member '{member_name}' no longer exists — skipping.")
            continue
        entries = read_modlist(member_dir / "modlist.txt")
        if entries:
            member_entries.append((member_name, entries))

    # Pass 1: OR the enabled state per mod name; keep the locked flag from,
    # and attribute display "ownership" to, whichever member enables it
    # first (highest priority). Mods disabled everywhere fall back to
    # whichever member mentions them first at all.
    enabled_union: dict[str, bool] = {}
    locked_for_enabled: dict[str, bool] = {}
    home_member_enabled: dict[str, str] = {}
    home_member_any: dict[str, str] = {}
    for member_name, entries in member_entries:
        for e in entries:
            if e.is_separator:
                continue
            home_member_any.setdefault(e.name, member_name)
            if e.enabled:
                enabled_union[e.name] = True
                locked_for_enabled.setdefault(e.name, e.locked)
                home_member_enabled.setdefault(e.name, member_name)
            else:
                enabled_union.setdefault(e.name, False)
    home_member = {**home_member_any, **home_member_enabled}

    # Pass 2: walk members in priority order, flat (no separator headers),
    # placing each mod in the slot of its computed "home" member (deferring
    # it past members that merely mention it without being the one that
    # turns it on). Real user-authored separators from a member's own
    # modlist still pass through, deduped by name like any other entry.
    merged: list[ModEntry] = []
    seen_names: set[str] = set()
    for member_name, entries in member_entries:
        for e in entries:
            if e.name in seen_names:
                continue
            if e.is_separator:
                seen_names.add(e.name)
                merged.append(e)
                continue
            if home_member.get(e.name) != member_name:
                continue
            seen_names.add(e.name)
            final_enabled = enabled_union.get(e.name, e.enabled)
            final_locked = locked_for_enabled.get(e.name, False) if final_enabled else False
            merged.append(ModEntry(name=e.name, enabled=final_enabled,
                                   locked=final_locked, is_separator=False))
    return merged


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


def _sort_with_loot(game, profile_dir: Path, profiles_dir: Path, members: list[str],
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
            staging_root=game.get_effective_mod_staging_path(),
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

    Safe to call repeatedly (before every deploy and on every profile switch).
    """
    _log = log_fn or (lambda _msg: None)
    if not is_group(profile_dir):
        return
    profiles_dir = profile_dir.parent
    members = get_members(profile_dir)

    write_modlist(profile_dir / "modlist.txt", _merge_modlist(profiles_dir, members, _log))

    if getattr(game, "plugin_extensions", None):
        star_prefix = bool(getattr(game, "plugins_use_star_prefix", True))
        merged_order, merged_enabled = _merge_plugins(profiles_dir, members, star_prefix)
        merged_order = _sort_with_loot(game, profile_dir, profiles_dir, members,
                                        merged_order, merged_enabled, _log)
        entries = [PluginEntry(name=n, enabled=merged_enabled.get(n, False)) for n in merged_order]
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
