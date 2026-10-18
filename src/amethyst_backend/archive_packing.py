from __future__ import annotations

import hashlib
import json
import os
import stat
import threading
from dataclasses import asdict
from pathlib import Path

from .jobs import JobCancelled, JobError
from .tool_install import _selected_game, _token


class Control:
    def __init__(self):
        self.event = threading.Event()
        self._lock = threading.Lock()
        self._token = None

    def cancel(self):
        self.event.set()
        with self._lock:
            if self._token is not None:
                self._token.cancel()

    def token(self):
        from Utils.filegraph.service import CancellationToken

        with self._lock:
            if self._token is None:
                self._token = CancellationToken()
            if self.event.is_set():
                self._token.cancel()
            return self._token

    def check(self):
        if self.event.is_set():
            raise JobCancelled("Archive operation cancelled.")


def _identity(path):
    value = _token(path)
    if value is None:
        return None
    return [value, _token(Path(path).resolve()) if stat.S_ISLNK(value[2]) else value]


def _tree(root, control, *, strict=False):
    control.check()
    digest = hashlib.sha256(json.dumps(["", _identity(root)]).encode())
    if not root.is_dir():
        return digest.hexdigest()

    def failed(exc):
        raise exc

    for current, directories, files in os.walk(root, followlinks=False, onerror=failed):
        control.check()
        directories.sort()
        folded = set()
        for name in sorted(directories + files):
            path = Path(current) / name
            relative = path.relative_to(root).as_posix()
            key = name.casefold()
            value = _identity(path)
            if strict and (key in folded or value is None
                           or not (stat.S_ISDIR(value[0][2]) or stat.S_ISREG(value[0][2]))):
                raise JobError("unsafe_archive_source", "A selected mod has links, ambiguous paths, or non-regular files.",
                               {"path": str(path)})
            folded.add(key)
            digest.update(json.dumps([relative, value]).encode())
    return digest.hexdigest()


def _profile_inputs(game, profile):
    from Utils.config_paths import get_game_config_dir
    from Utils.profiles.state import _LEGACY
    from Utils.ui.config import get_ui_config_path

    config = get_game_config_dir(game.name)
    paths = [game._paths_file, game._deploy_state_file, get_ui_config_path(),
             config / "routing_rules.json", config / "conflict_blacklist.json"]
    for path in sorted(profile.parent.iterdir()):
        if path.is_dir() and not path.is_symlink():
            paths.extend(path / name for name in ("modlist.txt", "profile_state.json", "plugins.txt", "loadorder.txt",
                                                  *_LEGACY.values()))
    return {str(path): _identity(path) for path in paths}


def _source_roots(staging, names):
    return sorted({str(path) for path in staging.iterdir() if path.is_dir() and not path.name.startswith(".")}
                  | {str(staging / name) for name in names}
                  | {str(staging.parent / "overwrite"), str(staging.parent / "Root_Folder")})


def _bindings(game, profile):
    from .mod_properties import _profiles
    from Utils.profiles.state import _LEGACY, _read_key, read_only_profile_state, read_profile_state

    selected = _selected_game(game, profile)
    with read_only_profile_state(), _profiles(selected, profile) as (records, errors):
        if errors:
            raise JobError("archive_profile_routing_unavailable", "Could not inspect profiles sharing these mods.",
                           {"profiles": errors})
        state = {}
        for name, record in records.items():
            persisted = read_profile_state(record["path"], read_only=True)
            persisted.update({key: value for key in _LEGACY if key not in persisted
                              and (value := _read_key(record["path"], persisted, key, read_only=True)) is not None})
            persisted.pop("excluded_mod_files", None)
            state[name] = [record["raw"].hex(), record["settings"], persisted]
        return [str(selected.get_game_path()), str(selected.get_profile_root()),
                str(selected.get_effective_mod_staging_path()), state]


def _targets(game, profile, names):
    from .mod_properties import _context, _profiles
    from Utils.profiles.state import read_only_profile_state

    result = {}
    with read_only_profile_state(), _profiles(game, profile) as (records, errors):
        for name in names:
            owner, owner_mod, folder, aliases, failures = _context(game, profile, name, records, errors)
            if failures:
                raise JobError("archive_profile_routing_unavailable", "Could not inspect all affected profiles.",
                               {"profiles": failures})
            result[name] = {"folder": str(folder), "owner_profile": owner["path"].name,
                            "owner_mod": owner_mod, "affected_profiles": aliases}
    if len({str(Path(value["folder"]).resolve()) for value in result.values()}) != len(result):
        raise JobError("ambiguous_archive_targets", "Select each physical mod folder once.")
    return result


def _snapshot(game, profile, control, log, progress):
    from Utils.filegraph.service import FileGraphService

    control.check()
    progress(0, 1, "refresh_archive_inventory")
    library = FileGraphService.open_library(game, profile, log_fn=log)
    library.refresh(profile, cancel=control.token())
    session = library.open_profile(profile)
    session.ensure_reconciled(operation_hint={"kind": "archive_batch"}, cancel=control.token())
    control.check()
    return library, session.snapshot()


def _inspect(game, profile, request, control, log, progress):
    from Utils.bsa import candidates, pack
    from Utils.profiles.state import read_only_profile_state

    with read_only_profile_state():
        selected = _selected_game(game, profile)
        kind = pack.archive_kind_for_game(selected)
        if kind is None:
            raise JobError("archive_packing_unavailable", "This game has no supported BSA/BA2 packing format.")
        if not selected.is_configured():
            raise JobError("game_not_configured", "Configure the selected game before working with archives.")
        before = _profile_inputs(selected, profile)
        library, snapshot = _snapshot(selected, profile, control, log, progress)
        staging = selected.get_effective_mod_staging_path()
        assessments = candidates.analyse(selected, staging, profile, None, snapshot=snapshot,
                                         progress_fn=lambda fraction: (control.check(), progress(int(fraction * 100), 100, "assess_archive_candidates")),
                                         log_fn=log)
        rows = []
        for candidate in assessments:
            control.check()
            row = {**asdict(candidate), "can_pack": candidate.can_pack, "can_unpack": candidate.can_unpack}
            row["unpack_groups"] = [{**asdict(group), "archives": [path.name for path in group.archives]}
                                    for group in pack.collect_unpack_groups(staging / candidate.mod_name)]
            rows.append(row)
        result = {"game": str(game.game_id), "profile": profile.name, "kind": kind,
                  "candidates": rows, "options": {"compress": True, "delete_loose": False,
                                                   "skip_winners": True, "split_textures": False}}
        if request is None:
            if before != _profile_inputs(selected, profile):
                raise JobError("stale_archive_plan", "Profile state changed during the scan.")
            return result, {"selected": selected, "library": library, "snapshot": snapshot}
        action, names = request["action"], request["mods"]
        targets = _targets(selected, profile, names)
        selected_rows = {row["mod_name"]: row for row in rows}
        tree_inputs = {}
        for source in _source_roots(staging, names):
            folder = Path(source)
            tree_inputs[source] = _tree(folder, control, strict=folder.parent == staging and folder.name in names)
        planned = []
        for name in names:
            folder = staging / name
            assessment = selected_rows.get(name)
            explicit = request.get("archives", {}).get(name)
            archives = []
            if action == "pack":
                if assessment is None or not assessment["can_pack"]:
                    raise JobError("archive_mod_not_packable", "Select enabled mods with packable loose files.", {"mod": name})
                plan = pack.plan_pack(selected, folder, name, kind, selected.plugin_extensions)
                split = kind == "ba2" or request["options"]["split_textures"] or assessment["needs_split"]
                paths = [path for path in (plan.archive_path, plan.archive_textures_path) if path is not None]
                archives = [path.name for path in paths if split or path == plan.archive_path]
                item = {"mod": name, **targets[name], "archives": archives,
                        "generated_stub": plan.stub_plugin_path.name if plan.stub_plugin_path else None,
                        "split_textures": split, "existing_archives": [path.name for path in paths if path.exists()],
                        "winning_files_kept_loose": assessment["winning_count"] if request["options"]["skip_winners"] else 0,
                        "existing_archive": plan.existing_any}
            else:
                from Utils.bsa.packing_meta import read_packing_state, tracked_archives
                try:
                    state = read_packing_state(folder)
                except Exception as exc:
                    raise JobError("invalid_archive_metadata", "Could not read this mod's packing ownership.",
                                   {"mod": name, "reason": str(exc)}) from exc
                archives = explicit if explicit is not None else [path.name for path in tracked_archives(folder, state)]
                if not archives:
                    raise JobError("archive_mod_not_unpackable", "No selected or recorded archives are available.", {"mod": name})
                for archive in archives:
                    path = folder / archive
                    if path.is_symlink() or not path.is_file() or path.resolve().parent != folder.resolve():
                        raise JobError("unsafe_archive_source", "Choose existing regular archives inside the selected mod.", {"path": str(path)})
                item = {"mod": name, **targets[name], "archives": archives, "tracked_only": explicit is None}
            planned.append(item)
        deployed = pack.is_profile_deployed(selected, profile, [staging / name for name in names])
        pins = {"inputs": before, "binding": _bindings(selected, profile), "sources": tree_inputs,
                "targets": targets, "rules": library.open_profile(profile).adapter.rules_hash().hex(),
                "manifests": library.manifest_fingerprints(), "request": request}
        if before != _profile_inputs(selected, profile):
            raise JobError("stale_archive_plan", "Profile state changed during planning.")
        revision = hashlib.sha256(json.dumps(pins, sort_keys=True, default=lambda value: value.hex() if isinstance(value, bytes) else str(value)).encode()).hexdigest()
        return {**result, "revision": revision, "action": action, "mods": names,
                "options": request["options"], "items": planned, "can_apply": not deployed,
                "restore_required": deployed}, {"selected": selected, "library": library,
                                                "snapshot": snapshot, "pins": pins, "staging": staging}


def _error(exc):
    from Utils.bsa.pack import ArchiveModsDeployed
    from Utils.deployment.locking import DeploymentBusy
    from Utils.filegraph.models import FileGraphBusy, FileGraphCancelled
    from Utils.operations import OperationBusy

    if isinstance(exc, (JobError, JobCancelled)):
        return exc
    if isinstance(exc, FileGraphCancelled):
        return JobCancelled("Archive operation cancelled.")
    if isinstance(exc, ArchiveModsDeployed):
        return JobError("archive_mods_deployed", str(exc))
    if isinstance(exc, (OperationBusy, DeploymentBusy, FileGraphBusy)):
        return JobError("operation_busy", str(exc), getattr(exc, "details", {}))
    return JobError("archive_operation_unavailable", "Could not prepare the archive operation.", {"reason": str(exc)})


def inspect_runner(game, profile, request, control):
    def run(log, progress):
        from Utils.operations import acquire_game_operation
        from Utils.profiles.state import read_only_profile_state

        try:
            with read_only_profile_state(), acquire_game_operation(game, "tools.archive_packing.plan" if request else "tools.archive_packing.scan"):
                result, _context = _inspect(game, profile, request, control, log, progress)
        except Exception as exc:
            raise _error(exc) from exc
        progress(1, 1, "archive_report_ready")
        return result
    return run


def _outcome(value):
    result = {key: item for key, item in value.items() if key not in ("plan", "result")}
    if "result" in value:
        packed = asdict(value["result"])
        keys = packed.pop("packed_keys")
        result["packed"] = {**packed, "loose_file_count": len(keys), "loose_files_sample": keys[:20]}
    if "plan" in value:
        result["archives"] = [str(path) for path in (value["plan"].archive_path, value["plan"].archive_textures_path)
                              if path is not None and path.is_file()]
    return result


def apply_runner(game, profile, request, revision, control):
    def run(log, progress):
        from Utils.bsa import pack
        from Utils.plugins import invalidate_plugins_cache, read_loadorder, read_plugins
        from Utils.profiles.state import read_only_profile_state
        from .mod_versions import _sync_plugins

        with read_only_profile_state():
            selected = _selected_game(game, profile)
            staging = selected.get_effective_mod_staging_path()
        context = None
        attempted = []
        stale = False
        sources = inputs = binding = targets = None
        previous = {}
        states = {}
        archives = {}
        synced = []
        outcomes = []

        def verify():
            nonlocal context, sources, inputs, binding, targets
            report, context = _inspect(game, profile, request, control, log, progress)
            if report["revision"] != revision:
                raise JobError("stale_archive_plan", "Profile, mod sources, or packing options changed; plan again.")
            if not report["can_apply"]:
                raise JobError("archive_mods_deployed", "Run Restore before packing or unpacking these mods.")
            sources = dict(context["pins"]["sources"])
            inputs = dict(context["pins"]["inputs"])
            binding = context["pins"]["binding"]
            targets = context["pins"]["targets"]
            for item in report["items"]:
                folder = staging / item["mod"]
                archives[item["mod"]] = {str(folder / name): _identity(folder / name) for name in item["archives"]}
                plugins = [path.name for path in folder.iterdir() if path.is_file() and path.suffix.lower() in selected.plugin_extensions]
                for alias in item["affected_profiles"]:
                    previous.setdefault(alias["profile"], set()).update(plugins)
                    target = profile.parent / alias["profile"]
                    for name in ("plugins.txt", "loadorder.txt"):
                        path = target / name
                        if path.is_symlink():
                            raise JobError("unsafe_profile_path", "An affected plugin list is a symbolic link.")
                        invalidate_plugins_cache(path)
                    states[alias["profile"]] = (read_plugins(target / "plugins.txt", star_prefix=selected.plugins_use_star_prefix),
                                                read_loadorder(target / "loadorder.txt"))
            return context["snapshot"]

        def stable():
            if binding != _bindings(game, profile) or targets != _targets(selected, profile, request["mods"]):
                raise JobError("stale_archive_plan", "Profile configuration or mod ownership changed during the batch.")

        def checkpoint(name):
            nonlocal stale
            try:
                control.check()
                if stale:
                    raise JobError("stale_archive_plan", "The batch inputs changed; plan the remaining mods again.")
                stable()
                if inputs != _profile_inputs(selected, profile):
                    raise JobError("stale_archive_plan", "Profile state changed during the archive operation.")
                if set(sources) != set(_source_roots(staging, request["mods"])):
                    raise JobError("stale_archive_plan", "The mod inventory changed during the archive operation.")
                for path, expected in sources.items():
                    if expected != _tree(Path(path), control, strict=Path(path).parent == staging and Path(path).name in request["mods"]):
                        raise JobError("stale_archive_plan", "A mod source changed during the archive operation.", {"path": path})
                if name not in attempted:
                    attempted.append(name)
            except JobCancelled:
                raise pack.PackCancelled()
            except Exception:
                stale = True
                raise

        def before_cleanup(name):
            nonlocal stale
            try:
                stable()
                if inputs != _profile_inputs(selected, profile):
                    raise JobError("stale_archive_plan", "Profile state changed before archive cleanup.")
                if request["action"] == "unpack" and any(expected != _identity(Path(path)) for path, expected in archives[name].items()):
                    raise JobError("stale_archive_plan", "An archive changed before unpack cleanup; it was retained.")
            except Exception:
                stale = True
                raise

        def after_item(name, outcome):
            from Utils.profiles.state import _LEGACY

            if stale or name not in attempted:
                return
            stable()
            sources[str(staging / name)] = _tree(staging / name, Control(), strict=True)
            latest = _profile_inputs(selected, profile)
            for path, expected in inputs.items():
                if Path(path).parent == profile and Path(path).name in ("profile_state.json", *_LEGACY.values()):
                    continue
                if latest.get(path) != expected:
                    raise JobError("stale_archive_plan", "Profile state changed during archive publication.")
            inputs.update(latest)

        def finish(values):
            nonlocal outcomes
            outcomes = [_outcome(value) for value in values]
            if not attempted:
                return
            try:
                stable()
                if inputs != _profile_inputs(selected, profile):
                    raise JobError("stale_archive_plan", "Profile state changed before the final refresh.")
                affected = {}
                for name in attempted:
                    for alias in targets[name]["affected_profiles"]:
                        affected.setdefault(alias["profile"], set()).update(alias["mods"])
                from Utils.filegraph.service import FileGraphService
                for profile_name, names in affected.items():
                    target = profile.parent / profile_name
                    refreshed = _selected_game(game, target)
                    library = FileGraphService.open_library(refreshed, target, log_fn=log)
                    library.refresh(target, mod_names=sorted(names))
                    session = library.open_profile(target)
                    session.ensure_reconciled(operation_hint={"kind": "archive_batch"})
                    _sync_plugins(refreshed, target, {"mods": sorted(names)}, session.snapshot(),
                                  (previous[profile_name], states))
                    session.ensure_reconciled(operation_hint={"kind": "archive_batch"})
                    synced.append({"profile": profile_name, "mods": sorted(names),
                                   "generation": session.snapshot().generation})
            except Exception as exc:
                raise JobError("archive_operation_sync_failed", "Archive files may have changed, but profile refresh did not finish.",
                               {"state_persisted": True, "outcomes": outcomes, "synced_profiles": synced,
                                "reason": str(exc)}) from exc

        def report(info):
            nonlocal outcomes
            progress(info["done"], info["file_total"], f'{info["index"]}/{info["total"]}:{info["mod_name"]}:{info["current"]}')
            if info.get("outcome"):
                value = _outcome(info["outcome"])
                outcomes.append(value)
                log(json.dumps(value, ensure_ascii=True))

        try:
            pack.run_batch(selected, staging, profile, {"action": request["action"], "mod_names": request["mods"],
                                                        "options": request["options"], "archives_by_mod": request.get("archives", {})},
                           progress=report, cancel=control.event.is_set, log_fn=log,
                           before_write=verify, checkpoint=checkpoint, before_cleanup=before_cleanup,
                           after_item=after_item, after_batch=finish)
        except Exception as exc:
            error = _error(exc)
            if attempted:
                error.details.update(state_persisted=True, attempted_mods=attempted,
                                     outcomes=outcomes, synced_profiles=synced)
            raise error from exc
        result = {"game": str(game.game_id), "profile": profile.name, "action": request["action"],
                  "outcomes": outcomes, "synced_profiles": synced, "state_persisted": bool(attempted),
                  "counts": {status: sum(row["status"] == status for row in outcomes)
                             for status in ("success", "failed", "cancelled", "skipped")}}
        if control.event.is_set():
            raise JobCancelled("Archive batch cancelled; completed items were retained.", result)
        result["ok"] = not result["counts"]["failed"]
        return result
    return run
