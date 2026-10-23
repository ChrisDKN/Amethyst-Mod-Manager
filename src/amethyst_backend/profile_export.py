from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from .jobs import JobCancelled, JobError

SOURCES = ("nexus", "thunderstore", "plugin", "direct", "browse", "manual", "bundle", "ignore")
CHANGE_KEYS = {"mod", "source", "mod_id", "file_id", "version", "size_bytes", "game_domain",
               "optional", "phase", "update_policy", "fomod_export", "save_edits",
               "direct_url", "instructions", "ts_version", "ts_size_bytes", "archive_id"}
OPTION_KEYS = {"settings", "changes", "include_saves", "include_overwrite", "include_root_folder"}


def validate_options(params):
    options = {key: params.get(key, True) for key in (
        "include_saves", "include_overwrite", "include_root_folder")}
    if any(not isinstance(value, bool) for value in options.values()):
        raise ValueError("Export inclusion flags must be boolean.")
    settings = params.get("settings", "latest")
    if (not isinstance(settings, str) or not settings
            or len(settings) > 255 or any(ord(char) < 32 for char in settings)
            or (settings not in ("latest", "defaults")
                and (Path(settings).name != settings or "\\" in settings or not settings.endswith(".json")))):
        raise ValueError("'settings' must be latest, defaults, or a saved JSON filename.")
    changes = params.get("changes", [])
    if not isinstance(changes, list) or len(changes) > 4096:
        raise ValueError("'changes' must be an array of at most 4096 mod settings.")
    if len(json.dumps(changes).encode("utf-8")) > 8 * 1024 * 1024:
        raise ValueError("Export changes exceed 8 MiB.")
    seen = set()
    for change in changes:
        if not isinstance(change, dict) or set(change) - CHANGE_KEYS:
            raise ValueError("An export change contains unsupported settings.")
        name = change.get("mod")
        if not isinstance(name, str) or not name or name.casefold() in seen:
            raise ValueError("Export changes must name unique mods.")
        seen.add(name.casefold())
        for key, value in change.items():
            if key in {"optional", "fomod_export", "save_edits"}:
                if not isinstance(value, bool):
                    raise ValueError(f"'{key}' must be boolean.")
            elif key in {"mod_id", "file_id", "size_bytes", "ts_size_bytes", "phase"}:
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise ValueError(f"'{key}' must be a non-negative integer.")
            elif not isinstance(value, str) or len(value) > 65536:
                raise ValueError(f"'{key}' must be a string of at most 65536 characters.")
        if "source" in change and change["source"] not in SOURCES:
            raise ValueError("The export source is unsupported.")
        if "update_policy" in change and change["update_policy"] not in ("exact", "prefer", "latest"):
            raise ValueError("The update policy must be exact, prefer, or latest.")
        if "archive_id" in change and not change["archive_id"]:
            raise ValueError("'archive_id' cannot be empty.")
    return {**options, "settings": settings, "changes": copy.deepcopy(changes)}


def _metadata(path, captured):
    from .profile_share import _capture

    try:
        return _capture(path, captured)
    except JobError as exc:
        names = {"unsafe_share_input": "unsafe_profile_export",
                 "share_input_unavailable": "profile_export_input_unavailable"}
        raise JobError(names.get(exc.name, exc.name), str(exc).replace("share-code", "profile export"),
                       exc.details) from exc


def _token(path):
    try:
        info = Path(path).lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise JobError("unsafe_profile_export", "Export inputs must be regular files.",
                       {"path": str(path)})
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _tree(root, check):
    root = Path(root)
    try:
        info = root.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(info.st_mode):
        raise JobError("unsafe_profile_export", "An export source folder is unavailable or linked.",
                       {"path": str(root)})
    result = []
    pending = [root]
    while pending:
        check()
        directory = pending.pop()
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise JobError("unsafe_profile_export", "An export source folder became a link.")
        result.append((str(directory.relative_to(root)), "directory", info.st_dev, info.st_ino))
        with os.scandir(directory) as entries:
            for entry in entries:
                check()
                path = Path(entry.path)
                if entry.is_dir(follow_symlinks=False):
                    pending.append(path)
                else:
                    token = _token(path)
                    if token is None:
                        raise JobError("stale_profile_export", "An export input disappeared while scanning.")
                    result.append((str(path.relative_to(root)), "file", *token))
    return sorted(result)


def _settings_files(profile):
    root = profile / "workshop"
    if root.is_symlink():
        raise JobError("unsafe_profile_export", "The export settings folder is a link.")
    files = []
    for path in root.glob("*.json"):
        token = _token(path)
        if token is not None:
            files.append({"name": path.name, "size_bytes": token[2],
                          "modified_ns": token[3], "token": token})
    return sorted(files, key=lambda value: (-value["modified_ns"], value["name"]))


def _apply_changes(rows, changes):
    from Utils.profiles.export import VER_LABEL_SEP

    by_name = {row["name"]: row for row in rows}
    for change in changes:
        name = change["mod"]
        if name not in by_name:
            raise JobError("mod_not_found", "The export change names an unlisted mod.", {"mod": name})
        row = by_name[name]
        if "file_id" in change and change["file_id"] != row["file_id"]:
            row["size_bytes"] = 0
        if "ts_version" in change and change["ts_version"] != row["ts_version"]:
            row["ts_size_bytes"] = 0
        for key, value in change.items():
            if key != "mod":
                row["source_instructions" if key == "instructions" else key] = value
        row["ver_label"] = (f'{row["file_id"]}{VER_LABEL_SEP}{row["version"]}'
                            if row["file_id"] and row["version"] else str(row["file_id"] or "-"))
        if "ts_version" in change:
            row["ts_full_name"] = f'{row["ts_namespace"]}-{row["ts_name"]}-{row["ts_version"]}'
        if change.get("fomod_export") and not row["has_fomod"]:
            raise JobError("profile_export_choices_missing", "The mod has no saved installer choices.",
                           {"mod": name})


@contextmanager
def _snapshot(game, profile, options, stop, log, progress):
    from Utils.config_paths import get_bain_selections_path, get_fomod_selections_path
    from Utils.mods.modlist import write_modlist
    from Utils.operations import acquire_game_operation
    from Utils.profiles import export
    from Utils.profiles.state import _LEGACY
    from .mod_properties import _context, _profiles
    from .profile_share import _name

    def check():
        if stop.is_set():
            raise JobCancelled("Profile export cancelled.")

    selected = copy.copy(game)
    selected.set_active_profile_dir(None)
    if not selected.load_paths(read_only=True):
        raise JobError("game_not_configured", "The game's saved paths are unavailable.")
    with acquire_game_operation(selected, "profiles.export"):
        configuration = [str(selected.get_game_path()), str(selected.get_prefix_path()),
                         str(selected.get_mod_staging_path()), str(selected.get_profile_root())]
        captured, ownership, unavailable, trees = {}, {}, {}, {}
        with tempfile.TemporaryDirectory(prefix="amethyst-profile-export-") as temporary:
            snapshot = Path(temporary)
            staging = snapshot / "mods"
            staging.mkdir()
            with _profiles(selected, profile) as (records, errors):
                record = records[profile.name]
                state = copy.deepcopy(record["state"])
                state["profile_settings"] = copy.deepcopy(record["settings"])
                for key, filename in _LEGACY.items():
                    if key not in state:
                        raw = _metadata(profile / filename, captured)
                        if raw is not None:
                            state[key] = json.loads(raw)
                if "ignored_missing_requirements" not in state:
                    raw = _metadata(profile / "ignored_missing_requirements.txt", captured)
                    if raw is not None:
                        state["ignored_missing_requirements"] = raw.decode("utf-8").splitlines()
                (snapshot / "profile_state.json").write_text(json.dumps(
                    export.portable_profile_state(state)), encoding="utf-8")
                real = [entry for entry in record["entries"]
                        if not entry.is_separator and not entry.is_group_header]
                names = set()
                dependencies = {profile.name: record["inputs"]}
                for index, entry in enumerate(real):
                    check()
                    progress(index, len(real), "Reading export metadata")
                    _name(entry.name)
                    if entry.name.casefold() in names:
                        raise JobError("ambiguous_mod", "The profile contains duplicate mod names.")
                    names.add(entry.name.casefold())
                    try:
                        owner, owner_mod, folder, _aliases, _errors = _context(
                            selected, profile, entry.name, records, errors)
                        dependencies[owner["path"].name] = owner["inputs"]
                        ownership[entry.name] = (owner["path"], owner_mod, folder)
                    except JobError as exc:
                        if exc.name not in ("mod_files_missing", "profile_group_owner_stale"):
                            raise
                        owner, owner_mod, folder = record, entry.name, None
                        ownership[entry.name] = None
                        unavailable[entry.name] = {"name": exc.name, "message": str(exc)}
                    target = staging / entry.name
                    target.mkdir()
                    if folder is not None:
                        raw = _metadata(folder / "meta.ini", captured)
                        if raw is not None:
                            (target / "meta.ini").write_bytes(raw)
                    for sub, saved in (("fomod", get_fomod_selections_path), ("bain", get_bain_selections_path)):
                        candidates = [profile / sub / f"{entry.name}.json",
                                      owner["path"] / sub / f"{owner_mod}.json",
                                      saved(game.name, owner_mod)]
                        for candidate in dict.fromkeys(candidates):
                            raw = _metadata(candidate, captured)
                            if raw is not None:
                                json.loads(raw)
                                destination = snapshot / sub / f"{entry.name}.json"
                                destination.parent.mkdir(exist_ok=True)
                                destination.write_bytes(raw)
                                break
                for name in ("plugins.txt", "loadorder.txt", "userlist.yaml"):
                    raw = _metadata(profile / name, captured)
                    if raw is not None:
                        (snapshot / name).write_bytes(raw)
                ini_names = sorted(path.name for path in profile.glob("*.ini"))
                for name in ini_names:
                    raw = _metadata(profile / name, captured)
                    if raw is not None:
                        (snapshot / name).write_bytes(raw)
                proxy = copy.copy(selected)
                proxy.set_active_profile_dir(snapshot)
                proxy.get_effective_mod_staging_path = lambda: staging
                rows = export.load_rows(list(reversed(real)), proxy)
                settings_files = _settings_files(profile)
                chosen = None
                if options["settings"] == "latest":
                    chosen = settings_files[0]["name"] if settings_files else None
                elif options["settings"] != "defaults":
                    chosen = options["settings"]
                    if chosen not in {value["name"] for value in settings_files}:
                        raise JobError("profile_export_settings_not_found", "The selected export settings are unavailable.")
                if chosen is not None:
                    raw = _metadata(profile / "workshop" / chosen, captured)
                    saved_path = snapshot / "saved.json"
                    saved_path.write_bytes(raw)
                    export.read_settings(saved_path, rows)
                for row in rows:
                    for key in ("mod_id", "file_id", "size_bytes", "phase", "ts_size_bytes"):
                        value = row.get(key, 0)
                        if value is None or value == "":
                            value = 0
                        if (isinstance(value, bool) or not isinstance(value, (int, str))
                                or not str(value).isascii() or not str(value).isdigit()):
                            raise JobError("profile_export_settings_invalid", "A saved numeric export setting is invalid.",
                                           {"mod": row["name"], "setting": key})
                        row[key] = int(value)
                export.apply_source_defaults(rows)
                _apply_changes(rows, options["changes"])
                for row in rows:
                    if row["source"] == "thunderstore" and row["ts_namespace"] and row["ts_name"] and not row["ts_version"]:
                        row["source"] = "bundle"
                included = {row["name"] for row in rows if row["source"] != "ignore"}
                write_modlist(snapshot / "modlist.txt", [entry for entry in record["entries"]
                              if entry.is_separator or (not entry.is_group_header and entry.name in included)])
            roots = {name: value[2] for name, value in ownership.items() if value is not None}
            for row in rows:
                if (row["source"] == "bundle" or row.get("save_edits")) and row["source"] != "ignore" and row["name"] in roots:
                    root = roots[row["name"]]
                    progress(0, 0, f'Scanning export files: {row["name"]}')
                    trees[root] = _tree(root, check)
            profile_roots = {sub: profile / sub for sub in ("ini files", "installed_collections")}
            if options["include_saves"]:
                profile_roots["Saves"] = profile / "Saves"
            else:
                profile_roots["Saves"] = snapshot / "no-saves"
            private = bool(record["settings"].get("profile_specific_mods"))
            base = profile if private else Path(selected.get_profile_root())
            overwrite_root = base / "overwrite" if options["include_overwrite"] else None
            root_folder = base / "Root_Folder" if options["include_root_folder"] else snapshot / "no-root-folder"
            for root in [*profile_roots.values(), root_folder, overwrite_root]:
                if root is not None and not root.is_relative_to(snapshot):
                    trees[root] = _tree(root, check)

            def verify():
                check()
                for path, expected in captured.items():
                    current = {}
                    _metadata(path, current)
                    if current[path] != expected:
                        raise JobError("stale_profile_export", "Export metadata or installer choices changed.",
                                       {"path": str(path)})
                if _settings_files(profile) != settings_files or sorted(path.name for path in profile.glob("*.ini")) != ini_names:
                    raise JobError("stale_profile_export", "Export settings or INI files changed.")
                with _profiles(selected, profile) as (current, current_errors):
                    for name, inputs in dependencies.items():
                        if name not in current or current[name]["inputs"] != inputs:
                            raise JobError("stale_profile_export", "Profile configuration or mod order changed.")
                    for name, expected in ownership.items():
                        try:
                            owner, owner_mod, folder, _aliases, _errors = _context(
                                selected, profile, name, current, current_errors)
                        except JobError as exc:
                            if expected is None and exc.name == unavailable[name]["name"]:
                                continue
                            raise JobError("stale_profile_export", "Profile Group ownership changed.") from exc
                        if expected != (owner["path"], owner_mod, folder):
                            raise JobError("stale_profile_export", "Profile Group ownership changed.")
                for root, expected in trees.items():
                    if _tree(root, check) != expected:
                        raise JobError("stale_profile_export", "Exported files changed.", {"path": str(root)})
                fresh = copy.copy(game)
                fresh.set_active_profile_dir(None)
                if not fresh.load_paths(read_only=True):
                    raise JobError("stale_profile_export", "Game configuration is no longer available.")
                actual = [str(fresh.get_game_path()), str(fresh.get_prefix_path()),
                          str(fresh.get_mod_staging_path()), str(fresh.get_profile_root())]
                if actual != configuration:
                    raise JobError("stale_profile_export", "Game configuration changed.")

            scratch = []
            try:
                yield {"game": proxy, "profile": snapshot, "selected": selected,
                       "rows": rows, "roots": roots, "unavailable": unavailable,
                       "settings": chosen, "settings_files": settings_files,
                       "captured": captured, "trees": trees, "dependencies": dependencies,
                       "ownership": ownership, "configuration": configuration,
                       "profile_roots": profile_roots, "overwrite": overwrite_root,
                       "root_folder": root_folder, "check": check, "verify": verify,
                       "scratch": scratch}
            finally:
                for folder in scratch:
                    shutil.rmtree(folder, ignore_errors=True)


def _plan(context, profile, options, progress):
    from Nexus.nexus_meta import normalise_game_domain
    from Utils.profiles import export
    from version import __version__

    rows = context["rows"]
    blockers = []
    nexus = {}
    for row in rows:
        source, name = row["source"], row["name"]
        if source == "ignore":
            continue
        if name in context["unavailable"]:
            blockers.append({"mod": name, **context["unavailable"][name]})
        if source not in SOURCES:
            blockers.append({"mod": name, "name": "source_unsupported"})
        if source == "nexus":
            if not row["mod_id"] or not row["file_id"]:
                blockers.append({"mod": name, "name": "nexus_pin_missing"})
            elif row["file_id"] in nexus:
                blockers.append({"mod": name, "name": "duplicate_nexus_file_id", "other_mod": nexus[row["file_id"]]})
            nexus[row["file_id"]] = name
        if source == "plugin" and not row.get("plugin_source_uri"):
            blockers.append({"mod": name, "name": "plugin_source_missing"})
        if source == "thunderstore" and not (row["ts_namespace"] and row["ts_name"] and row["ts_version"]):
            blockers.append({"mod": name, "name": "thunderstore_pin_missing"})
        elif source == "thunderstore":
            from .thunderstore import package_link
            try:
                package_link(row["ts_namespace"], row["ts_name"], row["ts_version"],
                             row["ts_community"] or "export")
            except JobError:
                blockers.append({"mod": name, "name": "thunderstore_pin_invalid"})
        if source in ("direct", "browse", "manual") and row.get("direct_url"):
            parsed = urlsplit(row["direct_url"])
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                blockers.append({"mod": name, "name": "source_url_invalid"})
        elif source == "direct":
            blockers.append({"mod": name, "name": "source_url_missing"})
    if not any(row["source"] != "ignore" for row in rows):
        blockers.append({"name": "profile_export_empty"})
    manifest = export.build_manifest(
        rows, normalise_game_domain(getattr(context["game"], "nexus_game_domain", "") or ""),
        __version__, game_name=context["game"].name, profile_dir=context["profile"])
    manifest["info"].update(name=profile.name, gameName=context["game"].name)
    archives, missing, archive_inputs = {}, [], {}
    wanted = [row for row in rows if row.get("save_edits") and row["source"] not in ("bundle", "ignore")]
    if wanted:
        from Utils.downloads.inventory import build_inventory
        from .jobs import _validated_archive_path
        inventory = build_inventory(context["game"], context["profile"])
        for row in wanted:
            context["check"]()
            if row["source"] == "thunderstore":
                matches = [item for item in inventory["archives"] if (
                    (item.get("thunderstore") or {}).get("namespace"),
                    (item.get("thunderstore") or {}).get("package"),
                    (item.get("thunderstore") or {}).get("package_version")) == (
                        row["ts_namespace"], row["ts_name"], row["ts_version"])]
            elif row["source"] == "plugin":
                record = row["plugin_source"]
                matches = [item for item in inventory["archives"] if all((item.get("provider") or {}).get(key) == record[key]
                           for key in ("plugin_id", "provider_id", "identity", "sha256"))]
            elif row["source"] == "nexus":
                matches = [item for item in inventory["archives"] if
                           normalise_game_domain((item.get("nexus") or {}).get("domain") or "") == normalise_game_domain(row.get("game_domain") or manifest["info"]["domainName"])
                           and (item.get("nexus") or {}).get("mod_id") == row["mod_id"]
                           and (item.get("nexus") or {}).get("file_id") == row["file_id"]]
            else:
                matches = [item for item in inventory["archives"] if
                           item["id"] == row.get("archive_id") or
                           (not row.get("archive_id") and item["name"] == row.get("installation_file"))]
            archive_id = row.get("archive_id")
            if archive_id:
                chosen = next((item for item in matches if item["id"] == archive_id), None)
                if chosen is None:
                    blockers.append({"mod": row["name"], "name": "patch_archive_mismatch"})
            elif len(matches) == 1:
                chosen = matches[0]
            else:
                chosen = None
                if len(matches) > 1:
                    blockers.append({"mod": row["name"], "name": "patch_archive_ambiguous",
                                     "archives": [{"id": value["id"], "name": value["name"]} for value in matches]})
            if chosen is None:
                missing.append({"mod": row["name"], "source": row["source"],
                                "mod_id": row["mod_id"], "file_id": row["file_id"],
                                "namespace": row["ts_namespace"], "package": row["ts_name"],
                                "version": row["ts_version"]})
                archives[row["name"]] = None
            else:
                path = _validated_archive_path(inventory, chosen)
                archives[row["name"]] = path
                archive_inputs[path] = _token(path)
                for suffix in (".fileid", ".nexus.json", ".thunderstore.json"):
                    _metadata(path.with_suffix(path.suffix + suffix), context["captured"])
    scratch = context["scratch"]
    try:
        progress(0, 0, "Building local-edit patches")
        jobs, warnings = export.build_patch_jobs(
            rows, manifest, context["game"], scratch_out=scratch,
            mod_roots=context["roots"], archive_paths=archives, checkpoint=context["check"])
    except BaseException:
        for folder in scratch:
            shutil.rmtree(folder, ignore_errors=True)
        raise
    bundle_names = [row["name"] for row in rows if row["source"] == "bundle"]
    members = export.amethyst_members(
        manifest, profile_dir=context["profile"], bundle_names=[name for name in bundle_names if name in context["roots"]],
        bundle_roots=context["roots"], profile_roots=context["profile_roots"],
        root_folder_root=context["root_folder"], overwrite_root=context["overwrite"],
        patch_jobs=jobs, checkpoint=context["check"])
    digest = hashlib.sha256(json.dumps(
        [options, context["configuration"], context["dependencies"], context["settings_files"],
         {name: [str(value) for value in values] if values else None for name, values in context["ownership"].items()},
         {str(root): values for root, values in context["trees"].items()},
         {str(path): [token, raw.hex() if raw is not None else None]
          for path, (token, raw) in context["captured"].items()},
         {str(path): token for path, token in archive_inputs.items()}, rows], sort_keys=True).encode())
    result = {"profile": profile.name, "revision": digest.hexdigest(), "options": options,
              "settings": context["settings"],
              "settings_files": [{key: value for key, value in row.items() if key != "token"}
                                 for row in context["settings_files"]],
              "rows": rows, "manifest": manifest, "blockers": blockers, "can_export": not blockers,
              "missing_archives": missing, "warnings": warnings,
              "requires_patch_warning_acknowledgement": bool(warnings),
              "bundled_mods": bundle_names, "bundled_downloadable_mods": export.redistributable_bundle_names(rows),
              "member_count": len(members),
              "packing_bytes": sum(len(data) if data is not None else src.stat().st_size for src, _name, data in members)}
    return result, jobs, scratch, archive_inputs


def _destination(path, context, profile, overwrite):
    if not path.is_absolute() or path.suffix.casefold() not in (".amethyst", ".zip"):
        raise JobError("unsafe_profile_export_path", "Export to an absolute .amethyst or ZIP path.")
    for parent in [path, *path.parents]:
        if parent.is_symlink():
            raise JobError("unsafe_profile_export_path", "The output path cannot cross a symbolic link.")
    if path.exists() and (not overwrite or not path.is_file()):
        raise JobError("profile_export_exists", "The output already exists or is not a regular file.", {"path": str(path)})
    roots = [Path(context["selected"].get_mod_staging_path()), profile.parent,
             Path(context["selected"].get_profile_root()) / "overwrite",
             Path(context["selected"].get_profile_root()) / "Root_Folder", *context["trees"]]
    resolved = path.resolve()
    if any(resolved.is_relative_to(root.resolve()) for root in roots):
        raise JobError("unsafe_profile_export_path", "Export outside the manager's source folders.")
    if any(resolved == source.resolve() for source in context["captured"]):
        raise JobError("unsafe_profile_export_path", "The output would replace an export input.")
    return _token(path)


def runner(game, profile, options, stop, *, revision=None, destination=None,
           overwrite=False, allow_patch_warnings=False, settings_name=None):
    def run(log, progress):
        from Utils.profiles import export
        from Utils.atomic_write import _tmp_for
        from Utils.operations import OperationBusy

        try:
            with _snapshot(game, profile, options, stop, log, progress) as context:
                result, patch_jobs, scratch, archive_inputs = _plan(context, profile, options, progress)
                try:
                    def verify():
                        context["verify"]()
                        for path, token in archive_inputs.items():
                            if _token(path) != token:
                                raise JobError("stale_profile_export", "A pristine patch archive changed.")
                    verify()
                    if revision is not None and result["revision"] != revision:
                        raise JobError("stale_profile_export", "Export inputs or options changed; preview again.",
                                       {"revision": result["revision"]})
                    if destination is None and settings_name is None:
                        return result
                    if settings_name is not None:
                        path = profile / "workshop" / settings_name
                        if path.parent.is_symlink() or path.is_symlink():
                            raise JobError("unsafe_profile_export_path", "The settings destination is a symbolic link.")
                        if path.exists() and not overwrite:
                            raise JobError("profile_export_exists", "The saved settings already exist.")
                        before = _token(path)
                        saved = export.write_settings(context["profile"] / "settings.json", context["rows"])
                        path.parent.mkdir(parents=True, exist_ok=True)
                        pending = _tmp_for(path)
                        try:
                            pending.write_bytes(saved.read_bytes())
                            verify()
                            if path.parent.is_symlink() or _token(path) != before:
                                raise JobError("profile_export_output_changed", "The settings destination changed.")
                            context["check"]()
                            if overwrite:
                                os.replace(pending, path)
                            else:
                                export._publish_new_archive(pending, path)
                        finally:
                            pending.unlink(missing_ok=True)
                        return {"profile": profile.name, "settings": settings_name,
                                "path": str(path), "rows": len(context["rows"])}
                    if result["blockers"]:
                        raise JobError("profile_export_blocked", "Resolve the preview's export blockers.",
                                       {"blockers": result["blockers"]})
                    if result["warnings"] and not allow_patch_warnings:
                        raise JobError("profile_export_patch_warnings", "Some requested edits cannot be represented.",
                                       {"warnings": result["warnings"], "missing_archives": result["missing_archives"]})
                    before = _destination(destination, context, profile, overwrite)
                    if any(destination.resolve() == path.resolve() for path in archive_inputs):
                        raise JobError("unsafe_profile_export_path", "The output would replace a pristine patch archive.")
                    def precommit():
                        verify()
                        if _destination(destination, context, profile, overwrite) != before:
                            raise JobError("profile_export_output_changed", "The output changed while packing.")
                    final = export.write_amethyst(
                        destination, result["manifest"], profile_dir=context["profile"],
                        bundle_names=result["bundled_mods"], bundle_roots=context["roots"],
                        profile_roots=context["profile_roots"], root_folder_root=context["root_folder"],
                        overwrite_root=context["overwrite"], patch_jobs=patch_jobs,
                        checkpoint=context["check"], precommit=precommit, overwrite=overwrite,
                        progress_cb=lambda done, total, member: progress(done, total, "Packing profile archive"))
                    log(f"Exported {len(result['manifest']['mods'])} mods to {final}.")
                    return {"profile": profile.name, "archive_path": str(final),
                            "archive_bytes": final.stat().st_size, "packing_bytes": result["packing_bytes"],
                            "mod_count": len(result["manifest"]["mods"]), "manifest": result["manifest"],
                            "warnings": result["warnings"], "revision": result["revision"]}
                finally:
                    for folder in scratch:
                        shutil.rmtree(folder, ignore_errors=True)
        except (JobError, JobCancelled, OperationBusy):
            raise
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            raise JobError("profile_export_failed", str(exc), {"profile": profile.name}) from exc
    return run
