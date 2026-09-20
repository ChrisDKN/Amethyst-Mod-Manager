from __future__ import annotations

from contextlib import ExitStack, contextmanager
import hashlib
import base64
import json
from pathlib import Path
import tempfile

from Utils.atomic_write import write_atomic
from Utils.collections.reset import resolve_collection_mod_order, _apply_collection_groups
from Utils.mods.modlist import read_modlist, write_modlist, modlist_lock
from Utils.plugins import (
    PluginEntry, read_plugins, read_loadorder, write_plugins, write_loadorder,
    primary_plugin_order, enforce_primary_plugin_order, invalidate_plugins_cache,
)
from Utils.profiles import groups


def reorder_slots(entries, desired, key):
    rank = {name: index for index, name in enumerate(dict.fromkeys(desired))}
    slots = [i for i, entry in enumerate(entries) if key(entry) in rank]
    ordered = sorted((entries[i] for i in slots), key=lambda entry: rank[key(entry)])
    result = list(entries)
    for i, entry in zip(slots, ordered):
        result[i] = entry
    return result


def _restore_order_files(original):
    for path, data in original.items():
        if data is None:
            path.unlink(missing_ok=True)
        else:
            write_atomic(path, data)
        invalidate_plugins_cache(path)


def _read_recovery(game, group_dir, journal):
    from Utils.collections.grouping import profile_path
    if not journal.is_file():
        return {}
    saved = json.loads(journal.read_text(encoding='utf-8'))
    member = profile_path(game, saved['member'])
    original = {}
    for item in saved['files']:
        if item['profile'] not in (member.name, group_dir.name) or item['file'] not in (
                'modlist.txt', 'plugins.txt', 'loadorder.txt'):
            raise ValueError('Invalid collection reset recovery data.')
        path = profile_path(game, item['profile']) / item['file']
        original[path] = base64.b64decode(item['data'], validate=True) if item['data'] is not None else None
    return original


def _plugin_entries(game, profile_dir):
    star = getattr(game, 'plugins_use_star_prefix', True)
    entries = read_plugins(profile_dir / 'plugins.txt', star_prefix=star)
    lookup = {e.name.lower(): e for e in entries}
    order = read_loadorder(profile_dir / 'loadorder.txt')
    names = list(dict.fromkeys([n.lower() for n in order] + list(lookup)))
    display = {n.lower(): n for n in order}
    return [lookup[n] if n in lookup else PluginEntry(display[n], False) for n in names]


def _source_paths(snapshot, profile_dir, staging=None):
    from Utils.profiles.state import profile_uses_specific_mods
    staging = staging or profile_dir / 'mods'
    root_dir = (profile_dir if profile_uses_specific_mods(profile_dir) or groups.is_group(profile_dir)
                else profile_dir.parent.parent)
    result = {}
    for name, winner in snapshot.plugin_winners().items():
        if winner.mod_key == '[overwrite]':
            root = staging.parent / 'overwrite'
        elif winner.mod_key == '[root_folder]':
            root = root_dir / 'Root_Folder'
        else:
            root = staging / winner.mod_name
        result[name.lower()] = root / bytes(winner.source_rel).decode('utf-8', 'surrogateescape')
    return result


@contextmanager
def _preview(game, profile_dir, entries):
    from Utils.filegraph.service import FileGraphService, CancellationToken
    library = FileGraphService.open_library(game, profile_dir)
    library.ensure_ready(profile_dir)
    session = library.open_profile(profile_dir)
    original = session._catalog_backed_intent({'kind': 'collection_reset'}, CancellationToken())
    proposed = dict(original)
    by_name = {mod['name']: mod for mod in original['mods']}
    proposed['mods'] = [by_name[e.name] for e in entries if not e.is_separator and e.name in by_name]
    proposed['intent_hash'] = hashlib.blake2b(repr(proposed['mods']).encode(), digest_size=24).digest()
    try:
        session.reconcile(intent=original)
        before = session.snapshot()
        session.reconcile(intent=proposed)
        after = session.snapshot()
        yield before, after
    finally:
        session.reconcile(intent=original)


def _plugin_order(game, profile_dir, manifest, amethyst_state, entries, snapshot, folders, log,
                  *, exclusive=False, before_snapshot=None):
    from Utils.collections.install import _entries_from_amethyst_plugins, _loot_available
    from Utils.mods.copy import resolve_target_staging
    staging = resolve_target_staging(game, profile_dir)
    selected = (set() if exclusive else
                {str(p.get('name', '')).lower() for p in manifest.get('plugins', [])})
    owners = ({name.lower(): str(getattr(winner, 'mod_name', '') or '').casefold()
               for name, winner in before_snapshot.plugin_winners().items()}
              if exclusive else {})
    for folder in folders:
        selected.update(name.lower() for name in snapshot.mod_plugins(folder)
                        if not exclusive or owners.get(name.lower()) == folder.casefold())
    primary = {name.lower() for name in primary_plugin_order(game)}
    selected.difference_update(primary)
    present = {entry.name.lower(): entry for entry in entries}
    authored = [present[p['name'].lower()] for p in manifest.get('plugins', [])
                if p.get('name', '').lower() in present]
    known = {entry.name.lower() for entry in authored}
    authored.extend(entry for entry in entries if entry.name.lower() not in known)
    if amethyst_state:
        resolved = _entries_from_amethyst_plugins(
            amethyst_state, authored, {}, getattr(game, 'plugins_use_star_prefix', True), log)
        if resolved:
            return [e.name.lower() for e in resolved if e.name.lower() in selected]
    if getattr(game, 'loot_sort_enabled', False) and _loot_available() and authored:
        from LOOT.loot_sorter import sort_plugins
        from LOOT.game_view import ProfileSources
        from Utils.profiles.state import profile_uses_specific_mods
        root_dir = profile_dir if profile_uses_specific_mods(profile_dir) else Path(game.get_profile_root())
        sources = ProfileSources(
            snapshot, Path(game.get_game_path()), Path(game.get_vanilla_plugins_path()),
            Path(game.get_mod_data_path()), staging, staging.parent / 'overwrite',
            root_dir / 'Root_Folder')
        with tempfile.TemporaryDirectory(prefix='amethyst-collection-order-') as temp:
            temp_dir = Path(temp)
            rules = profile_dir / 'userlist.yaml'
            if rules.is_file():
                (temp_dir / 'userlist.yaml').write_bytes(rules.read_bytes())
            errors = []
            _apply_collection_groups(temp_dir, manifest, log, error_sink=errors)
            if errors:
                raise ValueError("; ".join(errors))
            result = sort_plugins(
                plugin_names=[e.name for e in authored], enabled_set={e.name for e in authored if e.enabled},
                game_name=game.name, game_path=game.get_game_path(), staging_root=staging,
                log_fn=log, game_type_attr=getattr(game, 'loot_game_type', ''),
                game_id=getattr(game, 'game_id', ''), masterlist_url=getattr(game, 'loot_masterlist_url', ''),
                masterlist_repo=getattr(game, 'loot_masterlist_repo', ''),
                game_data_dir=game.get_vanilla_plugins_path(), userlist_path=temp_dir / 'userlist.yaml',
                plugin_winner_paths=_source_paths(snapshot, profile_dir, staging), profile_sources=sources)
        return [name.lower() for name in result.sorted_names if name.lower() in selected]
    return [entry.name.lower() for entry in authored if entry.name.lower() in selected]


def _validate_plugins(game, before, after, before_paths, after_paths):
    from Utils.plugins.parser import check_late_masters, check_missing_masters
    _fixed, changed = enforce_primary_plugin_order(game, after)
    if changed:
        raise ValueError("The collection order conflicts with the game's required plugin order.")
    primary = primary_plugin_order(game)
    def enabled(entries):
        return list(dict.fromkeys(primary + [e.name for e in entries if e.enabled]))
    for check in (check_late_masters, check_missing_masters):
        old = check(enabled(before), before_paths)
        new = check(enabled(after), after_paths)
        prior = {(name.lower(), master.lower()) for name, masters in old.items() for master in masters}
        added = [(name, master) for name, masters in new.items() for master in masters
                 if (name.lower(), master.lower()) not in prior]
        if added:
            name, master = added[0]
            raise ValueError(f'Cannot reset this collection without changing unrelated plugins: {name} requires {master}.')


def reset_group_collection_load_order(game, group_dir: Path, member_name: str,
                                      manifest: dict, *, amethyst_state=None, log_fn=None,
                                      collection_record=None) -> dict:
    return _reset_collection_order(game, member_name, manifest, group_dir=group_dir,
                                   amethyst_state=amethyst_state, log_fn=log_fn,
                                   collection_record=collection_record)


def reset_profile_collection_load_order(game, profile_dir: Path, manifest: dict, *,
                                        amethyst_state=None, log_fn=None,
                                        collection_record=None) -> dict:
    return _reset_collection_order(game, profile_dir.name, manifest,
                                   amethyst_state=amethyst_state, log_fn=log_fn,
                                   collection_record=collection_record)


def _reset_collection_order(game, member_name, manifest, *, group_dir=None,
                             amethyst_state=None, log_fn=None, collection_record=None):
    from Utils.deployment.locking import game_mutation_lock
    from Utils.collections.grouping import profile_path
    log = log_fn or (lambda _message: None)
    member_dir = profile_path(game, member_name)
    from Utils.mods.copy import resolve_target_staging
    staging = resolve_target_staging(game, member_dir)
    paths = (member_dir, group_dir) if group_dir is not None else (member_dir,)
    with game_mutation_lock(game), ExitStack() as locks:
        if not member_dir.is_dir():
            raise ValueError('The collection profile is no longer available.')
        if group_dir is not None:
            locks.enter_context(groups.group_build_lock(group_dir))
            if not groups.is_group(group_dir) or member_name not in groups.get_members(group_dir):
                raise ValueError('The collection is no longer a member of this group.')
        if any(groups.profile_is_locked(path) for path in paths):
            raise ValueError('Unlock the profile before resetting its collection order.')
        affected = {member_name, *groups.member_of_groups(game, member_name)}
        if group_dir is not None:
            affected.add(group_dir.name)
        if game.get_deploy_active() and game.get_last_deployed_profile() in affected:
            raise ValueError('Restore the deployed profile before resetting its collection order.')
        journal_dir = group_dir if group_dir is not None else member_dir
        journal = journal_dir / '.collection-order-recovery.json'
        recovery = _read_recovery(game, journal_dir, journal)
        for path in sorted({*paths, *(path.parent for path in recovery)}):
            locks.enter_context(modlist_lock(path / 'modlist.txt'))
        if recovery:
            _restore_order_files(recovery)
            journal.unlink()
        desired = resolve_collection_mod_order(
            member_dir, manifest, amethyst_state, staging_path=staging,
            collection_record=collection_record)
        if collection_record is not None and not desired:
            raise ValueError('No mods are exclusively owned by this collection; the load order was kept.')
        member_mods = read_modlist(member_dir / 'modlist.txt')
        member_final = reorder_slots(member_mods, desired, lambda e: None if e.is_separator else e.name)
        modlists = {member_dir: member_final}
        if group_dir is not None:
            group_mods = read_modlist(group_dir / 'modlist.txt')
            identities = groups._read_identity_map(group_dir)
            desired_keys = []
            group_identities = set(identities.values())
            for name in desired:
                key, _version = groups._mod_identity_and_version(staging, name)
                duplicate = f'{key}#{name}'
                desired_keys.append(duplicate if duplicate in group_identities else key)
            def group_key(entry):
                if entry.is_separator or not (group_dir / 'mods' / entry.name).is_symlink():
                    return None
                return identities.get(entry.name, f'name:{entry.name}')
            group_final = reorder_slots(group_mods, desired_keys, group_key)
            modlists[group_dir] = group_final
        old_plugins = {path: _plugin_entries(game, path) for path in paths}
        new_plugins = dict(old_plugins)
        if getattr(game, 'plugin_extensions', ()):
            with _preview(game, member_dir, member_final) as (before, after):
                plugin_order = _plugin_order(game, member_dir, manifest, amethyst_state,
                                             old_plugins[member_dir], after, desired, log,
                                             exclusive=collection_record is not None,
                                             before_snapshot=before)
                new_plugins[member_dir] = reorder_slots(old_plugins[member_dir], plugin_order, lambda e: e.name.lower())
                _validate_plugins(game, old_plugins[member_dir], new_plugins[member_dir],
                                  _source_paths(before, member_dir, staging), _source_paths(after, member_dir, staging))
            if group_dir is not None:
                with _preview(game, group_dir, group_final) as (before, after):
                    new_plugins[group_dir] = reorder_slots(old_plugins[group_dir], plugin_order, lambda e: e.name.lower())
                    _validate_plugins(game, old_plugins[group_dir], new_plugins[group_dir],
                                      _source_paths(before, group_dir), _source_paths(after, group_dir))
        proposed = {}
        with tempfile.TemporaryDirectory(prefix='amethyst-group-reset-') as temp:
            for i, (path, mods) in enumerate(modlists.items()):
                output = Path(temp) / str(i)
                output.mkdir()
                write_modlist(output / 'modlist.txt', mods)
                write_plugins(output / 'plugins.txt', new_plugins[path],
                              star_prefix=getattr(game, 'plugins_use_star_prefix', True))
                write_loadorder(output / 'loadorder.txt', new_plugins[path])
                for file in output.iterdir():
                    proposed[path / file.name] = file.read_bytes()
            original = {path: path.read_bytes() if path.exists() else None for path in proposed}
            saved = {'member': member_name, 'files': [
                {'profile': path.parent.name, 'file': path.name,
                 'data': base64.b64encode(data).decode('ascii') if data is not None else None}
                for path, data in original.items()]}
            from Utils.profiles.backup import create_load_order_backup
            for path in paths:
                create_load_order_backup(path, log_fn=log)
            write_atomic(journal, json.dumps(saved).encode('utf-8'))
            try:
                for path, data in proposed.items():
                    write_atomic(path, data)
            except Exception:
                _restore_order_files(original)
                journal.unlink()
                raise
            else:
                journal.unlink()
            finally:
                for path in paths:
                    invalidate_plugins_cache(path / 'plugins.txt')
                    invalidate_plugins_cache(path / 'loadorder.txt')
        from Utils.filegraph.service import FileGraphService
        for path in paths:
            try:
                library = FileGraphService.open_library(game, path, log_fn=log)
                library.ensure_ready(path)
                library.open_profile(path).reconcile(operation_hint={'kind': 'collection_reset'})
            except Exception as exc:
                log(f'Collection reset: refresh needed for {path.name}: {exc}')
        return {'ordered': len(desired), 'group': group_dir.name if group_dir is not None else None, 'member': member_name}
