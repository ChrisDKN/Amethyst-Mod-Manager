from __future__ import annotations

import copy
import hashlib
import json
import stat
import tempfile
from pathlib import Path

from .jobs import JobCancelled, JobError


def _name(value):
    if (not isinstance(value, str) or not value or value in (".", "..")
            or Path(value).name != value or "\\" in value
            or value.startswith("@group:")
            or any(ord(char) < 32 or 0xd800 <= ord(char) <= 0xdfff for char in value)):
        raise ValueError("A share-code mod needs a safe name.")
    return value


def inspect_code(code):
    from Utils.profiles.export import decode_manifest, manifest_install_mods, thunderstore_entries

    try:
        manifest = decode_manifest(code)
        info = manifest.get("info", {})
        if not isinstance(info, dict):
            raise ValueError("The share-code info must be an object.")
        if not isinstance(manifest["mods"], list):
            raise ValueError("The share-code mods must be an array.")
        seen, file_ids = set(), set()
        for entry in manifest["mods"]:
            if not isinstance(entry, dict):
                raise ValueError("A share-code mod must be an object.")
            name = _name(entry.get("name"))
            if name.casefold() in seen:
                raise ValueError("Share-code mod names must be unique.")
            seen.add(name.casefold())
            source = entry.get("source")
            if not isinstance(source, dict):
                raise ValueError("A share-code mod needs a source.")
            provider = source.get("type") or "nexus"
            if provider not in ("nexus", "thunderstore", "plugin") or source.get("bundle"):
                raise ValueError("A share code requires Nexus, Thunderstore or plugin-provider download pins.")
            if provider == "nexus":
                for key in ("modId", "fileId"):
                    value = source.get(key)
                    if (isinstance(value, bool) or not isinstance(value, (int, str))
                            or not str(value).isdigit() or int(value) <= 0):
                        raise ValueError("Nexus share-code entries need positive mod and file IDs.")
                file_id = int(source["fileId"])
                if file_id in file_ids:
                    raise ValueError("The profile importer requires distinct Nexus file IDs.")
                file_ids.add(file_id)
            for key in ("enabled", "optional"):
                if key in entry and not isinstance(entry[key], bool):
                    raise ValueError(f"Share-code {key} flags must be boolean.")
        mods, total = manifest_install_mods(manifest)
        thunderstore = thunderstore_entries(manifest)
        if len(thunderstore) != sum(mod.source_type == "thunderstore" for mod in mods):
            raise ValueError("A Thunderstore share-code entry has an incomplete package pin.")
        sources = {
            "nexus": sum(mod.source_type == "nexus" for mod in mods),
            "thunderstore": len(thunderstore),
        }
        from Utils.collections.providers import entries as provider_entries
        sources["plugin"] = len(provider_entries(manifest))
        return {"manifest": manifest, "sources": sources, "mod_count": len(mods) + sources["plugin"],
                "known_download_bytes": total, "requires_nexus": bool(sources["nexus"])}
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError) as exc:
        raise JobError("share_code_invalid", str(exc)) from exc


def _capture(path, captured):
    from Utils.profiles.export import MAX_CODE_MANIFEST_BYTES

    path = Path(path)
    if path in captured:
        return captured[path][1]
    if path.is_symlink() or path.parent.is_symlink():
        raise JobError("unsafe_share_input", "A share-code input cannot be a symlink.",
                       {"path": str(path)})
    try:
        info = path.stat()
    except FileNotFoundError:
        captured[path] = (None, None)
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_CODE_MANIFEST_BYTES:
        raise JobError("share_input_unavailable", "A share-code input is not a regular file of at most 16 MiB.",
                       {"path": str(path)})
    token = (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns, info.st_size)
    with path.open("rb") as stream:
        raw = stream.read(MAX_CODE_MANIFEST_BYTES + 1)
    if len(raw) > MAX_CODE_MANIFEST_BYTES:
        raise JobError("share_input_unavailable", "A share-code input exceeds 16 MiB.")
    captured[path] = (token, raw)
    return raw


def _check_inputs(captured):
    for path, expected in captured.items():
        actual = {}
        _capture(path, actual)
        if actual[path] != expected:
            raise JobError("stale_profile_share", "Profile metadata changed while creating the share code.",
                           {"path": str(path)})


def code_runner(game, profile, stop):
    def build(log, progress):
        from Utils.operations import acquire_game_operation
        from Utils.profiles import export
        from version import __version__
        from .mod_properties import _context, _profiles, _record
        from Utils.config_paths import get_bain_selections_path, get_fomod_selections_path

        def checkpoint():
            if stop.is_set():
                raise JobCancelled("Profile share-code creation cancelled.")

        selected = copy.copy(game)
        selected.set_active_profile_dir(None)
        if not selected.load_paths(read_only=True):
            raise JobError("game_not_configured", "The selected game's paths are unavailable.")
        with acquire_game_operation(selected, "profiles.share"):
            checkpoint()
            configuration = [str(selected.get_game_path()), str(selected.get_prefix_path()),
                             str(selected.get_mod_staging_path()), str(selected.get_profile_root())]
            with _profiles(selected, profile) as (records, errors):
                record = records[profile.name]
                entries = record["entries"]
                names = set()
                for entry in entries:
                    if not entry.is_separator and not entry.is_group_header:
                        name = _name(entry.name)
                        if name.casefold() in names:
                            raise JobError("ambiguous_mod", "The profile contains duplicate mod names.")
                        names.add(name.casefold())
                captured, ownership = {}, {}
                with tempfile.TemporaryDirectory(prefix="amethyst-share-") as temporary:
                    snapshot = Path(temporary)
                    staging = snapshot / "mods"
                    staging.mkdir()
                    state = {}
                    for key in ("separator_colors", "separator_locks"):
                        if key in record["state"]:
                            state[key] = record["state"][key]
                        else:
                            raw = _capture(profile / f"{key}.json", captured)
                            state[key] = json.loads(raw) if raw is not None else {}
                    if any(not isinstance(value, dict) for value in state.values()):
                        raise JobError("invalid_profile_state", "Separator settings must be objects.")
                    (snapshot / "profile_state.json").write_text(json.dumps(state), encoding="utf-8")
                    real = [entry for entry in entries if not entry.is_separator and not entry.is_group_header]
                    for index, entry in enumerate(real):
                        checkpoint()
                        progress(index, len(real), "Reading share-code sources")
                        owner, owner_mod, folder, _aliases, _errors = _context(
                            selected, profile, entry.name, records, errors)
                        ownership[entry.name] = (owner["path"], owner_mod, folder)
                        meta = _capture(folder / "meta.ini", captured)
                        target = staging / entry.name
                        target.mkdir()
                        if meta is not None:
                            (target / "meta.ini").write_bytes(meta)
                        for sub, saved in (("fomod", get_fomod_selections_path),
                                           ("bain", get_bain_selections_path)):
                            choices = None
                            candidates = [profile / sub / f"{entry.name}.json",
                                          owner["path"] / sub / f"{owner_mod}.json",
                                          saved(game.name, owner_mod)]
                            for candidate in dict.fromkeys(candidates):
                                choices = _capture(candidate, captured)
                                if choices is not None:
                                    json.loads(choices)
                                    break
                            if choices is not None:
                                destination = snapshot / sub / f"{entry.name}.json"
                                destination.parent.mkdir(exist_ok=True)
                                destination.write_bytes(choices)
                    proxy = copy.copy(selected)
                    proxy.set_active_profile_dir(snapshot)
                    proxy.get_effective_mod_staging_path = lambda: staging
                    checkpoint()
                    manifest = export.build_code_manifest(
                        entries, proxy, __version__, profile_name=profile.name)
                    included = {entry["name"] for entry in manifest["mods"]}
                    skipped = [{"mod": entry.name, "reason": "no_download_pin"}
                               for entry in real if entry.name not in included]
                    if not included:
                        raise JobError("profile_share_empty", "No mods have a downloadable share-code pin.",
                                       {"skipped": skipped})
                    code = export.encode_manifest(manifest)
                    summary = inspect_code(code)
                    _check_inputs(captured)
                    for name, expected in ownership.items():
                        owner, owner_mod, folder, _aliases, _errors = _context(
                            selected, profile, name, records, errors)
                        if (owner["path"], owner_mod, folder) != expected:
                            raise JobError("stale_profile_share", "Profile Group ownership changed while creating the code.")
                    for current in records.values():
                        if _record(current["path"])["inputs"] != current["inputs"]:
                            raise JobError("stale_profile_share", "Profile routing changed while creating the code.")
                    latest = copy.copy(game)
                    latest.set_active_profile_dir(None)
                    if not latest.load_paths(read_only=True):
                        raise JobError("stale_profile_share", "Game configuration is no longer available.")
                    current_configuration = [
                        str(latest.get_game_path()), str(latest.get_prefix_path()),
                        str(latest.get_mod_staging_path()), str(latest.get_profile_root())]
                    if current_configuration != configuration:
                        raise JobError("stale_profile_share", "Game configuration changed while creating the code.")
                    digest = hashlib.sha256(json.dumps(
                        [configuration, {name: value["inputs"] for name, value in records.items()}],
                        sort_keys=True).encode())
                    digest.update(json.dumps({name: [str(path), mod, str(folder)]
                                              for name, (path, mod, folder) in ownership.items()},
                                             sort_keys=True).encode())
                    for path, (token, raw) in sorted(captured.items(), key=lambda item: str(item[0])):
                        digest.update(str(path).encode("utf-8", "surrogateescape"))
                        digest.update(json.dumps(token).encode())
                        digest.update(raw if raw is not None else b"<missing>")
                    checkpoint()
                    progress(len(real), len(real), "Profile share code ready")
                    log(f"Created a share code for {len(included)} mods; skipped {len(skipped)} without download pins.")
                    return {"profile": profile.name, "code": code, "revision": digest.hexdigest(),
                            "skipped": skipped, **summary}

    def run(log, progress):
        from Utils.operations import OperationBusy

        try:
            return build(log, progress)
        except (JobError, JobCancelled, OperationBusy):
            raise
        except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError) as exc:
            raise JobError("profile_share_failed", str(exc), {"profile": profile.name}) from exc
    return run


def import_runner(manifest, game, profile_name, skipped, accept_mismatch,
                  session, control, owner, *, manual_archive_paths=None, manual_session=None, installer_requests=None):
    def run(log, progress):
        from .collections import import_local_runner

        try:
            with tempfile.TemporaryDirectory(prefix="amethyst-share-import-") as temporary:
                path = Path(temporary) / "shared-profile.json"
                path.write_text(json.dumps(manifest), encoding="utf-8")
                return import_local_runner(
                    game, profile_name, path, skipped, accept_mismatch, session, control, owner,
                    manual_archive_paths=manual_archive_paths, manual_session=manual_session, installer_requests=installer_requests)(log, progress)
        except OSError as exc:
            raise JobError("collection_import_failed", str(exc), {"profile": profile_name}) from exc
    return run
