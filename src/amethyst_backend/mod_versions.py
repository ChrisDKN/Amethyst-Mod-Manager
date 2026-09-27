from __future__ import annotations

import copy
from pathlib import Path
from types import SimpleNamespace

from .jobs import JobError, _install_download, _validated_archive_path
from .mod_properties import get_properties
from .nexus import _call, _file, _rate_limits, download_runner


def _context(game, profile, mod, revision=None):
    from Nexus.nexus_meta import normalise_game_domain, read_meta
    from Utils.mods.metadata import meta_file_lock

    properties = get_properties(game, profile, mod)
    if revision is not None and properties["revision"] != revision:
        raise JobError("stale_mod_version", "The installed mod or its ownership changed; read its versions again.",
                       {"revision": properties["revision"]})
    path = Path(properties["path"])
    with meta_file_lock(path):
        meta = read_meta(path)
    if get_properties(game, profile, mod)["revision"] != properties["revision"]:
        raise JobError("stale_mod_version", "The installed mod changed while its identity was read.")
    owner = Path(game.get_profile_root()) / "profiles" / properties["owner_profile"]
    context = {"requested_profile": profile, "requested_mod": mod, "target_profile": owner,
               "target_mod": properties["owner_mod"], "staging": path.parent.parent,
               "destination": path.parent, "nexus_meta": meta}
    domains = getattr(game, "nexus_game_domains", ()) or ()
    if isinstance(domains, str):
        domains = (domains,)
    domains = [normalise_game_domain(value) for value in domains if value]
    fallback = normalise_game_domain(getattr(game, "nexus_game_domain", "") or "")
    if not domains and fallback:
        domains = [fallback]
    domain = normalise_game_domain(meta.game_domain) or (domains[0] if domains else "")
    if meta.mod_id <= 0 or not domain or domain not in domains:
        raise JobError("mod_version_source_unavailable", "This mod has no Nexus identity for the selected game.")
    context["properties"] = properties
    context["domain"] = domain
    return context


def _binding(context):
    meta = context["nexus_meta"]
    return {"source": "nexus", "domain": context["domain"], "mod_id": meta.mod_id,
            "installed_file_id": meta.file_id or None, "installed_version": meta.version or None,
            "requested_profile": context["requested_profile"].name,
            "requested_mod": context["requested_mod"],
            "owner_profile": context["target_profile"].name, "owner_mod": context["target_mod"],
            "affected_profiles": context["properties"]["affected_profiles"]}


def _selected_target(game, context):
    from Utils.mods.copy import resolve_target_staging

    selected = copy.copy(game)
    selected.set_active_profile_dir(context["target_profile"])
    selected.load_paths(read_only=True)
    if (not selected.is_configured()
            or Path(selected.get_profile_root()).resolve() != context["target_profile"].parent.parent.resolve()
            or Path(resolve_target_staging(selected, context["target_profile"])).resolve() != context["staging"].resolve()):
        raise JobError("stale_mod_version", "The target configuration changed; read the versions again.")
    return selected


def versions_runner(session, game, profile, mod):
    def run(_log, progress):
        from Utils.operations import acquire_game_operation
        from Utils.mods.versions import resolve_latest_name_match, sort_key

        def operation():
            with acquire_game_operation(game, "mods.versions.get"):
                progress(0, 1, "Reading installed mod versions")
                context = _context(game, profile, mod)
                _selected_target(game, context)
                api = session.require_api()
                listing = api.get_mod_files(context["domain"], context["nexus_meta"].mod_id)
                installed = context["nexus_meta"].file_id
                match, old = resolve_latest_name_match(listing.files, installed, context["target_mod"])
                files = [{**_file(file), "installed": file.file_id == installed,
                          "recommended": file.file_id == match and match != installed,
                          "older_match": file.file_id in old}
                         for file in sorted(listing.files, key=sort_key)]
                _context(game, profile, mod, context["properties"]["revision"])
                _selected_target(game, context)
                progress(1, 1, "Mod versions ready")
                return {"version_catalog": {**_binding(context), "revision": context["properties"]["revision"],
                                            "recommended_file_id": match if match > 0 and match != installed else None,
                                            "files": files},
                        "rate_limits": _rate_limits(api)}
        return _call(operation)
    return run


def _archive(game, profile, domain, mod_id, file_id, archive_id=None, downloaded=None):
    from Nexus.nexus_meta import normalise_game_domain
    from Utils.downloads.inventory import build_inventory
    from .download_archives import _token

    inventory = build_inventory(game, profile)
    matches = []
    for item in inventory["archives"]:
        identity = item.get("nexus") or {}
        if (normalise_game_domain(identity.get("domain") or "") == domain and identity.get("mod_id") == mod_id
                and identity.get("file_id") == file_id):
            matches.append(item)
    if archive_id is not None:
        selected = next((item for item in inventory["archives"] if item["id"] == archive_id), None)
        if selected is None:
            raise JobError("archive_not_found", "The selected downloaded archive is unavailable.")
        if selected not in matches:
            raise JobError("mod_version_archive_mismatch", "The archive does not identify the selected Nexus file.",
                           {"archive_id": archive_id, "domain": domain, "mod_id": mod_id, "file_id": file_id})
    elif downloaded is not None:
        selected = next((item for item in matches if Path(item["path"]) == downloaded), None)
        if selected is None:
            raise JobError("mod_version_archive_mismatch", "The downloaded archive does not identify the selected Nexus file.")
    elif len(matches) > 1:
        raise JobError("mod_version_archive_ambiguous", "Several downloaded archives identify this version; select one.",
                       {"archives": [{"id": item["id"], "name": item["name"]} for item in matches]})
    else:
        selected = matches[0] if matches else None
    if selected is None:
        return None
    path = _validated_archive_path(inventory, selected)
    paths = [path, path.with_suffix(path.suffix + ".fileid"), path.with_suffix(path.suffix + ".nexus.json")]
    tokens = [_token(value, required=index == 0) for index, value in enumerate(paths)]
    return selected, paths, tokens


def _saved_files(context):
    from Utils.config_paths import get_bain_selections_path, get_fomod_selections_path

    owner, mod = context["target_profile"], context["target_mod"]
    profiles = owner.parent
    paths = []
    for affected in context["properties"]["affected_profiles"]:
        root = profiles / affected["profile"]
        paths.extend(root / name for name in ("modlist.txt", "profile_state.json", "plugins.txt", "loadorder.txt"))
    paths.extend(owner / "fomod" / f"{mod}.{suffix}" for suffix in ("json", "xml"))
    paths.append(owner / "bain" / f"{mod}.json")
    paths.extend((get_fomod_selections_path(context["game"].name, mod),
                  get_bain_selections_path(context["game"].name, mod)))
    saved = {}
    for path in paths:
        path = Path(path)
        if path.is_symlink() or path.parent.is_symlink():
            raise JobError("unsafe_profile_path", "A saved installer or profile state path is a symbolic link.",
                           {"path": str(path)})
        saved[path] = path.read_bytes() if path.exists() else None
    return saved


def _restore_files(game, saved):
    from contextlib import ExitStack
    from Utils.atomic_write import write_atomic
    from Utils.mods.modlist import modlist_lock
    from Utils.plugins import invalidate_plugins_cache
    from Utils.profiles.state import _lock_for

    root = Path(game.get_profile_root()) / "profiles"
    profiles = sorted({root / path.relative_to(root).parts[0] for path in saved if path.is_relative_to(root)})
    with ExitStack() as locks:
        for profile in profiles:
            if profile.is_symlink() or not profile.is_dir():
                raise OSError(f"Profile became unavailable: {profile}")
            locks.enter_context(modlist_lock(profile / "modlist.txt"))
        for profile in profiles:
            locks.enter_context(_lock_for(profile))
        for path, raw in saved.items():
            if path.is_symlink() or path.parent.is_symlink():
                raise OSError(f"State path became a symbolic link: {path}")
            if raw is None:
                path.unlink(missing_ok=True)
                if path.exists():
                    raise OSError(f"Could not remove new state: {path}")
            else:
                write_atomic(path, raw)
                if path.read_bytes() != raw:
                    raise OSError(f"Could not verify restored state: {path}")
            if path.name in {"plugins.txt", "loadorder.txt"}:
                invalidate_plugins_cache(path)


def _capture_plugins(game, context, log, progress):
    if not getattr(game, "plugin_extensions", ()):
        return None
    from Utils.filegraph.service import active_snapshot
    from Utils.mods.state import _reconcile_filegraph
    from Utils.plugins import invalidate_plugins_cache, read_loadorder, read_plugins

    owner = context["target_profile"]
    _reconcile_filegraph(game, owner, kind="full", mod_names=[context["target_mod"]], log=log, progress=progress)
    previous = active_snapshot(game).mod_plugins(context["target_mod"])
    star = getattr(game, "plugins_use_star_prefix", True)
    states = {}
    for affected in context["properties"]["affected_profiles"]:
        profile = owner.parent / affected["profile"]
        invalidate_plugins_cache(profile / "plugins.txt")
        invalidate_plugins_cache(profile / "loadorder.txt")
        states[profile.name] = (read_plugins(profile / "plugins.txt", star_prefix=star),
                                read_loadorder(profile / "loadorder.txt"))
    return previous, states


def _sync_plugins(game, profile, affected, snapshot, captured):
    from Utils.filegraph.service import plugin_source_paths
    from Utils.plugins import (
        PluginEntry, _normalise_ext, insert_by_loadorder, invalidate_plugins_cache,
        read_loadorder, read_plugins, write_loadorder, write_plugins,
    )
    from Utils.plugins.state import _verified_absent_plugins, resolve_plugin_paths_for_game

    previous, states = captured
    entries, order = states[profile.name]
    entries = [PluginEntry(entry.name, entry.enabled) for entry in entries]
    order = list(order)
    star = getattr(game, "plugins_use_star_prefix", True)
    known = {entry.name.lower() for entry in entries}
    if not star:
        entries.extend(PluginEntry(name, False) for name in order if name.lower() not in known)
    candidates = {name.lower(): name for name in previous}
    for mod in affected["mods"]:
        candidates.update((name.lower(), name) for name in snapshot.mod_plugins(mod))
    if not getattr(game, "plugins_include_vanilla", False):
        from Utils.games.registry import _vanilla_plugins_for_game
        vanilla = _vanilla_plugins_for_game(game)
        candidates = {name: spelling for name, spelling in candidates.items() if name not in vanilla}
    data = game.get_vanilla_plugins_path() if hasattr(game, "get_vanilla_plugins_path") else None
    resolved = resolve_plugin_paths_for_game(game, data, snapshot=snapshot)
    winners = plugin_source_paths(snapshot, game)
    for name, path in winners.items():
        if name in candidates and not path.is_file():
            raise OSError(f"The refreshed plugin source is unavailable: {path}")
    resolved.update(winners)
    staged = {name.lower() for name in snapshot.staged_plugins()}
    disabled = {name for name in candidates if name in staged and name not in resolved}
    absent = [name for name in candidates if name not in staged and name not in resolved]
    removed = _verified_absent_plugins(game, data, snapshot, absent, lambda: False)
    if removed is None:
        raise OSError("Could not verify plugins removed by the version change.")
    removed = set(removed)
    entries = [entry for entry in entries if entry.name.lower() not in disabled | removed]
    order = [name for name in order if name.lower() not in removed and (star or name.lower() not in disabled)]
    listed = {entry.name.lower() for entry in entries}
    positions = {name.lower(): index for index, name in enumerate(order)}
    for name, spelling in candidates.items():
        if name not in resolved:
            continue
        if name not in positions:
            positions[name] = len(order)
            order.append(spelling)
        if name not in listed:
            insert_by_loadorder(entries, PluginEntry(spelling, True), positions)
            listed.add(name)
    plugin_path, order_path = profile / "plugins.txt", profile / "loadorder.txt"
    persisted = [PluginEntry(_normalise_ext(entry.name), entry.enabled)
                 for entry in entries if star or entry.enabled]
    if read_plugins(plugin_path, star_prefix=star) != persisted:
        write_plugins(plugin_path, entries, star_prefix=star)
    if read_loadorder(order_path) != order:
        write_loadorder(order_path, [PluginEntry(name, True) for name in order])
    invalidate_plugins_cache(plugin_path)
    invalidate_plugins_cache(order_path)
    if read_plugins(plugin_path, star_prefix=star) != persisted or read_loadorder(order_path) != order:
        raise OSError("Could not verify plugin state after the version change.")


def _refresh(game, context, log, progress, *, materialize, plugins=None):
    from Utils.mods.state import _reconcile_filegraph
    from Utils.profiles.groups import is_group, materialize_group

    synced, failures = [], []
    for affected in context["properties"]["affected_profiles"]:
        target = context["target_profile"].parent / affected["profile"]
        try:
            if target.is_symlink() or not target.is_dir():
                raise OSError("The affected profile is unavailable.")
            selected = copy.copy(game)
            selected.set_active_profile_dir(target)
            selected.load_paths(read_only=True)
            if materialize and is_group(target):
                materialize_group(selected, target, log_fn=log)
            reconciled, generation = _reconcile_filegraph(
                selected, target, kind="full", mod_names=affected["mods"], log=log, progress=progress)
            if plugins is not None:
                from Utils.filegraph.service import active_snapshot
                _sync_plugins(selected, target, affected, active_snapshot(selected), plugins)
            synced.append({"profile": target.name, "reconciled": reconciled, "generation": generation})
        except Exception as exc:
            failures.append({"profile": target.name, "reason": str(exc)})
    return synced, failures


def change_runner(session, game, profile, mod, revision, file_id, options, nxm_link=None):
    def run(log, progress):
        from Nexus.nexus_meta import build_meta_from_download, merge_reinstall_metadata, normalise_game_domain, read_meta
        from Utils.deployment.locking import coordinated_game_mutation
        def operation():
            with coordinated_game_mutation(game, "mods.version.change"):
                context = _context(game, profile, mod, revision)
                context["game"] = game
                selected = _selected_target(game, context)
                properties = context["properties"]
                if properties["routing_scan_errors"]:
                    raise JobError("mod_version_routing_unavailable", "Could not inspect profiles affected by this version change.",
                                   {"profiles": properties["routing_scan_errors"]})
                domain, mod_id = context["domain"], context["nexus_meta"].mod_id
                if nxm_link is not None and (nxm_link.game_domain, nxm_link.mod_id, nxm_link.file_id) != (domain, mod_id, file_id):
                    raise JobError("mod_version_source_mismatch", "The NXM link does not match the selected installed mod and file.")
                api = session.require_api()
                listing = api.get_mod_files(domain, mod_id)
                files = [file for file in listing.files if file.file_id == file_id]
                if len(files) != 1:
                    raise JobError("mod_version_not_found", "The selected file is unavailable or ambiguous in the Nexus listing.",
                                   {"file_id": file_id})
                file = files[0]
                remote = api.get_mod(domain, mod_id)
                refreshed = build_meta_from_download(game_domain=domain, mod_id=mod_id, file_id=file_id,
                                                      archive_name=file.file_name, mod_info=remote, file_info=file)
                metadata = merge_reinstall_metadata(refreshed, context["nexus_meta"])
                metadata.version = refreshed.version
                metadata.ignore_update = False
                metadata.ignored_version = ""
                archive = _archive(game, profile, domain, mod_id, file_id, options.get("archive_id"))
                if archive is None:
                    downloaded = download_runner(session, game.name, domain, mod_id, file_id,
                                                 nxm_link=nxm_link)(log, progress)
                    archive = _archive(game, profile, domain, mod_id, file_id,
                                       downloaded=Path(downloaded["archive"]["path"]))
                item, paths, tokens = archive
                old_meta = (context["destination"] / "meta.ini").read_bytes()
                saved, started, plugins, modlist_before = {}, False, None, None

                def before_install():
                    nonlocal saved, started, plugins, modlist_before
                    _context(game, profile, mod, revision)
                    saved = _saved_files(context)
                    plugins = _capture_plugins(selected, context, log, progress)
                    from Utils.mods.modlist import read_modlist
                    modlist_before = read_modlist(context["target_profile"] / "modlist.txt")
                    _context(game, profile, mod, revision)
                    current = _archive(game, profile, domain, mod_id, file_id, item["id"])
                    if current[1:] != (paths, tokens):
                        raise JobError("mod_version_archive_changed", "The selected archive or its identity changed during preparation.")
                    _selected_target(selected, context)
                    started = True

                def verify_install(name):
                    from Utils.mods.modlist import read_modlist
                    actual = read_meta(context["staging"] / name / "meta.ini")
                    if (name != context["target_mod"] or actual.mod_id != mod_id or actual.file_id != file_id
                            or normalise_game_domain(actual.game_domain) != domain
                            or actual.version != metadata.version or actual.root_folder != metadata.root_folder):
                        raise JobError("mod_version_metadata_failed", "The installed version's metadata could not be verified.")
                    if read_modlist(context["target_profile"] / "modlist.txt") != modlist_before:
                        raise JobError("mod_version_profile_failed", "The version change did not preserve the profile's mod order and state.")

                job = SimpleNamespace(id="mod-version-change", kind="mods.version.change", game=selected,
                                      profile=profile.name, options={**options, "archive_id": item["id"], "if_exists": "replace"})
                try:
                    result = _install_download(job, log, progress, replacement=context, prebuilt_meta=metadata,
                                               before_install=before_install, verify_install=verify_install,
                                               operation_locked=True)
                except Exception as exc:
                    details = getattr(exc, "details", {})
                    if not started or getattr(exc, "name", "") == "install_rollback_failed":
                        if isinstance(exc, JobError):
                            exc.details = {**details, "archive_id": item["id"], "version_change": _binding(context)}
                        raise
                    try:
                        restored = (context["destination"] / "meta.ini").read_bytes() == old_meta
                    except OSError:
                        restored = False
                    if not restored:
                        raise JobError("mod_version_change_failed", "The version change failed after modifying the staged mod.",
                                       {"state_persisted": True, "archive_id": item["id"], "version_change": _binding(context)}) from exc
                    try:
                        _restore_files(game, saved)
                        _, failures = _refresh(game, context, log, progress, materialize=False)
                        if failures:
                            raise OSError(str(failures))
                    except Exception as recovery:
                        raise JobError("mod_version_rollback_failed", "The previous mod files were restored, but profile recovery failed.",
                                       {"files_restored": True, "reason": str(recovery), "archive_id": item["id"]}) from exc
                    raise JobError(getattr(exc, "name", "install_failed"), str(exc),
                                   {**details, "rolled_back": True, "disk_changed": False, "archive_id": item["id"]}) from exc
                synced, failures = _refresh(game, context, log, progress, materialize=True, plugins=plugins)
                result["version_change"] = {**_binding(context), "file_id": file_id, "version": metadata.version,
                                            "synced_profiles": synced}
                if failures:
                    raise JobError("mod_version_sync_failed", "The new version was installed, but some profiles could not refresh.",
                                   {"state_persisted": True, "result": result, "failures": failures})
                progress(1, 1, "Mod version installed")
                return result
        return _call(operation)
    return run
