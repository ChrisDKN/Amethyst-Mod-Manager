"""Cross-profile mod links, discovered from the filesystem."""

from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ModLink:
    source: Path | None = None
    source_profile: str = ""
    targets: tuple[str, ...] = ()
    available: bool = True
    path: Path | None = None


def _resolved(path: Path) -> Path:
    try:
        return path.resolve()
    except (OSError, RuntimeError):
        return Path(os.path.abspath(path))


def _profiles(game):
    from Utils.mods.copy import resolve_target_staging
    from Utils.profiles.groups import is_group
    root = Path(game.get_profile_root()) / "profiles"
    if root.is_dir():
        for profile in sorted(root.iterdir()):
            if profile.is_dir() and not is_group(profile):
                yield profile, Path(resolve_target_staging(game, profile))


def _entries(staging):
    try:
        return list(staging.iterdir())
    except OSError:
        return []


def _links(staging):
    try:
        with os.scandir(staging) as entries:
            return [Path(entry.path) for entry in entries if entry.is_symlink()]
    except OSError:
        return []


def linked_source(staging: Path, profile: Path, name: str) -> Path | None:
    from Utils.profiles.groups import is_group, owner_of
    folder = Path(staging) / name
    if not folder.is_symlink():
        return None
    if is_group(profile):
        owner = owner_of(profile, name)
        if owner:
            folder = profile.parent / owner[0] / "mods" / owner[1]
    return _resolved(folder) if folder.is_symlink() else None


def source_label(profile: Path, source: Path) -> str:
    try:
        parts = source.relative_to(_resolved(profile.parent)).parts
        if len(parts) == 3 and parts[1] == "mods":
            return parts[0]
    except ValueError:
        pass
    from Utils.profiles.groups import is_group
    owners = [other.name for other in _entries(profile.parent)
              if other.is_dir() and not is_group(other)
              and _resolved(other / "mods") == source.parent]
    return ", ".join(sorted(owners)) if owners else ""


def edit_message(staging: Path, profile: Path, name: str) -> str:
    source = linked_source(staging, profile, name)
    if source is None:
        return ""
    label = source_label(profile, source)
    origin = f"profile '{label}'" if label else "shared mod storage"
    return (f"'{name}' is sourced from {origin}. "
            f"Switch to the source profile to change its files.\n\n{source}")


def require_local_mod(staging: Path, profile: Path, name: str) -> None:
    message = edit_message(staging, profile, name)
    if message:
        raise ValueError(message)


def require_local_destination(game, mod_dir: Path, *, profile_dir=None) -> None:
    profile = (profile_dir or getattr(game, "_active_profile_dir", None)
               or Path(game.get_profile_root()) / "profiles" / "default")
    require_local_mod(Path(mod_dir).parent, Path(profile), Path(mod_dir).name)


def path_edit_message(staging: Path, profile: Path, path: Path) -> str:
    path = Path(path)
    for folder in _links(staging):
        source = linked_source(staging, profile, folder.name)
        if source is not None and (path.is_relative_to(folder)
                                   or _resolved(path).is_relative_to(source)):
            return edit_message(staging, profile, folder.name)
    return ""


def relationships(game, profile: Path, staging: Path, names) -> dict[str, ModLink]:
    incoming: dict[Path, set[str]] = {}
    for other, root in _profiles(game):
        for entry in _links(root):
            incoming.setdefault(_resolved(entry), set()).add(other.name)
    result = {}
    for name in names:
        source = linked_source(staging, profile, name)
        if source is not None:
            result[name] = ModLink(source, source_label(profile, source),
                                   available=source.is_dir())
        else:
            targets = tuple(sorted(incoming.get(_resolved(staging / name), ())))
            if targets:
                result[name] = ModLink(targets=targets, path=_resolved(staging / name))
    return result


def eligible_targets(game, source_profile: Path, source_staging: Path) -> set[str]:
    from Utils.profiles.state import profile_uses_specific_mods
    profiles = list(_profiles(game))
    roots = [_resolved(root) for _, root in profiles]
    source_root = _resolved(source_staging)
    shared_root = _resolved(Path(game.get_mod_staging_path()))
    return {profile.name for profile, root in profiles
            if profile != source_profile and profile_uses_specific_mods(profile)
            and _resolved(root) not in (source_root, shared_root)
            and roots.count(_resolved(root)) == 1}


def dependents(game, paths, *, whole_profile=False) -> dict[Path, set[str]]:
    originals = {_resolved(Path(path)) for path in paths
                 if whole_profile or not Path(path).is_symlink()}
    result = {}
    if not originals:
        return result
    for profile, staging in _profiles(game):
        for entry in _links(staging):
            target = _resolved(entry)
            if target in originals or (whole_profile and any(
                    target.is_relative_to(root) for root in originals)):
                if not any(entry.is_relative_to(root) for root in originals):
                    result.setdefault(profile, set()).add(entry.name)
    return result


def dependency_warning(game, paths, *, whole_profile=False, replacing=False,
                       removing=False) -> str:
    affected = dependents(game, paths, whole_profile=whole_profile)
    if not affected:
        return ""
    labels = ", ".join(p.name for p in sorted(affected))
    if removing:
        return (f"\n\nThis also removes the linked mod entries from profiles: {labels}. "
                "Continue?")
    if replacing:
        return f"\n\nReplacing this mod also changes the files used by linked profiles: {labels}. Continue?"
    return (f"\n\nLinked profiles: {labels}. Moving or renaming "
            "this source will leave their mod links unavailable. Continue?")


def operation_paths(profile, staging, names, *, removing=False):
    from Utils.profiles.groups import is_group, owner_of, removal_targets
    grouped = is_group(profile)
    if grouped and removing:
        return list(dict.fromkeys(path for targets in removal_targets(profile, names).values()
                                  for path in targets))
    paths = []
    for name in names:
        owner = owner_of(profile, name) if grouped else None
        paths.append(profile.parent / owner[0] / "mods" / owner[1]
                     if owner else staging / name)
    return paths


def invalidate_dependents(game, paths, *, whole_profile=False, exclude_root=None):
    from Utils.filegraph.service import FileGraphService
    for profile in dependents(game, paths, whole_profile=whole_profile):
        root = FileGraphService.library_root(game, profile)
        if exclude_root is not None and _resolved(root) == _resolved(exclude_root):
            continue
        library = FileGraphService.open_library(game, profile)
        library._native.set_ready(False)
        library._variant_keys_cache = None
        for session in library._profile_sessions():
            session._invalidate_resolution_cache()


def invalidate_linked_catalogs(library):
    from Utils.filegraph.service import FileGraphService
    from Utils.profiles.groups import is_group
    roots = {root for profile, root in _profiles(library.game)
             if _resolved(FileGraphService.library_root(library.game, profile))
             == _resolved(library.root)}
    if is_group(library.root):
        roots.update(_resolved(entry) for entry in _links(library.root / "mods"))
    invalidate_dependents(library.game, roots, whole_profile=True,
                          exclude_root=library.root)


def remove_link_state(profile, name):
    from Utils.profiles.state import _lock_for, read_profile_state, write_profile_state
    with _lock_for(profile):
        state = read_profile_state(profile)
        changed = False
        if "plugin_metadata" in state:
            from amethyst_plugins.metadata import remap
            metadata = remap(state["plugin_metadata"], drops=[name])
            if metadata != state["plugin_metadata"]:
                state["plugin_metadata"] = metadata
                changed = True
        for key in ("mod_strip_prefixes", "disabled_plugins", "excluded_mod_files",
                    "root_mod_files", "mod_notes", "ignored_mod_updates"):
            values = state.get(key)
            if isinstance(values, dict) and name in values:
                del values[name]
                changed = True
        if changed:
            write_profile_state(profile, state)
    for sub, suffix in (("fomod", ".json"), ("fomod", ".xml"), ("bain", ".json")):
        (profile / sub / (name + suffix)).unlink(missing_ok=True)


@contextmanager
def removing_sources(game, paths, *, enabled=True, whole_profile=False, log_fn=None):
    from Utils.mods.copy import resolve_target_staging
    from Utils.mods.remove import _mod_plugin_names
    log = log_fn or (lambda _message: None)
    pending = []
    group_cleanup = None
    paths = list(paths)
    if enabled and game is not None:
        for profile, names in dependents(game, paths, whole_profile=whole_profile).items():
            staging = Path(resolve_target_staging(game, profile))
            for name in sorted(names):
                link = staging / name
                source = _resolved(link)
                pending.append((profile, link, source, _mod_plugin_names(game, [link])))
        if game.get_deploy_active():
            from Utils.filegraph.service import FileGraphService
            from Utils.mods.remove import undeploy_catalog_mods
            from Utils.profiles.groups import is_group
            deployed = game.get_last_deployed_profile()
            for profile, link, _source, _plugins in pending:
                if profile.name == deployed:
                    library = FileGraphService.open_library(game, profile, log_fn=log)
                    undeploy_catalog_mods(game, library.open_profile(profile),
                                          link.parent, [link.name], log_fn=log)
            group = Path(game.get_profile_root()) / "profiles" / (deployed or "")
            if deployed and is_group(group):
                originals = {_resolved(Path(path)) for path in paths
                             if whole_profile or not Path(path).is_symlink()}
                affected = {}
                for link in _links(group / "mods"):
                    source = _resolved(link)
                    if source in originals or (whole_profile and any(
                            source.is_relative_to(root) for root in originals)):
                        affected[link.name] = (source, _mod_plugin_names(game, [link]))
                if affected:
                    library = FileGraphService.open_library(game, group, log_fn=log)
                    session = library.open_profile(group)
                    undeploy_catalog_mods(game, session, group / "mods",
                                          list(affected), log_fn=log)
                    group_cleanup = (group, library, session, affected)
    try:
        yield
    finally:
        for profile, link, source, plugins in pending:
            if source.exists() or not link.is_symlink() or _resolved(link) != source:
                continue
            try:
                _remove_missing_source_link(game, profile, link, plugins, log)
            except Exception as exc:
                log(f"Could not clean up linked mod '{profile.name}/{link.name}': {exc}")
        if group_cleanup is not None:
            group, library, session, affected = group_cleanup
            removed = [name for name, (source, _plugins) in affected.items()
                       if not source.exists()]
            if removed:
                try:
                    from Utils.mods.remove import _remove_plugins_for_mods
                    plugins = set().union(*(affected[name][1] for name in removed))
                    _remove_plugins_for_mods(game, group, group / "mods", removed, log,
                                            preserve_remaining=True, missing_plugins=plugins)
                    session.forget_deployed_mods(removed)
                    for name in removed:
                        library.remove_mod(name)
                except Exception as exc:
                    log(f"Could not clean up deployed group '{group.name}': {exc}")


def _remove_missing_source_link(game, profile, link, plugins, log):
    from Utils.mods.modlist import modlist_lock, read_modlist, write_modlist
    from Utils.mods.groups import reconcile_profile_groups
    from Utils.mods.remove import _remove_plugins_for_mods
    from Utils.filegraph.service import FileGraphService
    name = link.name
    _remove_plugins_for_mods(game, profile, link.parent, [name], log,
                            preserve_remaining=True, missing_plugins=plugins)
    link.unlink()
    remove_link_state(profile, name)
    modlist = profile / "modlist.txt"
    with modlist_lock(modlist):
        entries = [entry for entry in read_modlist(modlist)
                   if entry.is_separator or entry.name != name]
        write_modlist(modlist, entries)
        reconcile_profile_groups(profile, entries)
    library = FileGraphService.open_library(game, profile, log_fn=log)
    library.open_profile(profile).forget_deployed_mods([name])
    library.remove_mod(name)
    log(f"Removed linked mod '{profile.name}/{name}' after its source was deleted.")
