from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

from .jobs import JobCancelled, JobError
from .tool_install import _archive_path, _destination_state, _digest_archive, _selected_game, _source_identity, _token


def source_choice():
    from Utils.wizards.smapi import _GITHUB_API_URL

    return {"label": "SMAPI", "archive_keywords": ["smapi", "installer"],
            "source": {"kind": "github_release", "url": _GITHUB_API_URL}}


def _supported(game):
    from Utils.wizards.plugins import get_all_wizard_tools

    if not any(tool.dialog_class_path == "wizards.sdv_smapi.SmapiWizard"
               for tool in get_all_wizard_tools(game, include_plugins=False)):
        raise JobError("smapi_unavailable", "This game has no SMAPI installation wizard.")


def _checkpoint(cancel):
    if cancel.is_set():
        raise JobCancelled("SMAPI installation cancelled.")


def acquire_runner(game, control):
    def run(log, progress):
        from Utils.operations import OperationCancelled
        from Utils.wizards.smapi import fetch_latest_smapi_asset
        from .tool_acquisition import _checkpoint as check_download, _download

        _supported(game)
        check_download(control)
        try:
            tag, url = fetch_latest_smapi_asset()
            check_download(control)
            parsed = urlsplit(url)
            if (parsed.scheme != "https" or parsed.hostname != "github.com"
                    or not parsed.path.startswith("/Pathoschild/SMAPI/releases/download/")):
                raise JobError("smapi_source_invalid", "The SMAPI release asset is outside its official repository.")
            result = _download(game, "sdv_smapi", 0, source_choice(), url, tag, control, progress)
        except OperationCancelled as exc:
            raise JobCancelled("SMAPI download cancelled.") from exc
        except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
            if isinstance(exc, (JobError, JobCancelled)):
                raise
            raise JobError("smapi_acquisition_failed", "Could not acquire the SMAPI installer.",
                           {"reason": str(exc)}) from exc
        return {**result, "tool_id": "sdv_smapi", "version_index": 0}
    return run


def _configuration(game, profile):
    return {str(path): _token(path) for path in
            (game._paths_file, profile / "profile_state.json", profile / "profile_settings.json")}


def _plan(game, profile, archive, mode, mod_name, payload, sources, cancel):
    from Utils.wizards.payload import archive_token

    selected = _selected_game(game, profile)
    _supported(selected)
    if not selected.is_configured():
        raise JobError("game_not_configured", "Configure Stardew Valley before installing SMAPI.")
    before = archive_token(archive)
    digest = _digest_archive(archive, cancel)
    payload.check_archive()
    if before != archive_token(archive):
        raise JobError("smapi_archive_changed", "The SMAPI archive changed during inspection.")
    sidecar = archive.with_suffix(archive.suffix + ".tool.json")
    sidecars = {str(path): _token(path) for path in
                (sidecar, archive.with_suffix(archive.suffix + ".nexus.json"))}
    verified = _source_identity(archive, "sdv_smapi", 0, source_choice(), digest)
    target, label, name, records, overwritten, existing = _destination_state(
        selected, payload, mode, cancel, 0, mod_name_override=mod_name,
        allow_restore_links=mode == "game" and selected.get_deploy_active())
    snapshot = {"game": str(selected.game_id), "profile": str(profile), "mode": mode,
                "archive": [str(archive), before, digest, sidecars], "mod_name": name,
                "configuration": _configuration(selected, profile),
                "deployment": _token(selected._deploy_state_file), "game_sources": sources,
                "target": str(target), "destination_state": records,
                "files": [str(path) for path in payload.files]}
    if sidecars != {path: _token(Path(path)) for path in sidecars}:
        raise JobError("smapi_archive_changed", "The installer identity changed during inspection.")
    return {
        "revision": hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest(),
        "game": str(selected.game_id), "profile": profile.name, "mode": mode,
        "archive": {"path": str(archive), "sha256": digest, "source_verified": verified is not None,
                    "source_identity": verified},
        "destination": {"path": str(target), "label": label, "mod_name": name,
                        "exists": target.exists(), "existing_entries": existing,
                        "overwrites": overwritten[:100], "overwrite_count": len(overwritten),
                        "removes": [str(path) for path in payload.remove_files
                                    if mode != "mod" and records.get(str(target / path)) is not None],
                        "preserves": [str(path) for path in payload.keep_existing
                                      if mode != "mod" and records.get(str(target / path)) is not None]},
        "game_sources": sources.copy(), "file_count": len(payload.files),
        "restore_before_install": mode == "game", "archive_retained": True,
        "needs_deployment": mode != "game",
    }


def inspect_runner(game, profile, archive_path, mode, mod_name, cancel):
    def run(log, progress):
        from Utils.wizards.payload import PayloadInstallError
        from Utils.wizards.smapi import prepare_smapi_payload

        _supported(game)
        archive = _archive_path(archive_path)
        selected = _selected_game(game, profile)
        progress(0, 1, "inspect_smapi")
        try:
            with prepare_smapi_payload(selected, archive, mode, log_fn=log,
                                       checkpoint=lambda: _checkpoint(cancel)) as (payload, sources, _refresh):
                result = _plan(game, profile, archive, mode, mod_name, payload, sources, cancel)
        except PayloadInstallError as exc:
            raise JobError(exc.name, str(exc), exc.details) from exc
        except (JobError, JobCancelled):
            raise
        except (OSError, ValueError, RuntimeError, zipfile.BadZipFile) as exc:
            raise JobError("smapi_inspection_failed", "Could not prepare the SMAPI installer.",
                           {"reason": str(exc)}) from exc
        progress(1, 1, "inspect_smapi")
        return result
    return run


def install_runner(game, profile, archive_path, mode, mod_name, revision,
                   allow_unverified_source, replace_existing, cancel):
    def run(log, progress):
        from Utils.profiles.groups import materialize_group, member_of_groups
        from Utils.deployment.locking import DeploymentBusy
        from Utils.operations import OperationBusy
        from Utils.wizards.payload import PayloadInstallError, install_prepared_payload
        from Utils.wizards.smapi import game_sources, prepare_smapi_payload

        _supported(game)
        archive = _archive_path(archive_path)
        selected = _selected_game(game, profile)
        configuration = _configuration(selected, profile)
        sidecars = {str(path): _token(path) for path in
                    (archive.with_suffix(archive.suffix + ".tool.json"),
                     archive.with_suffix(archive.suffix + ".nexus.json"))}
        restore_started = False
        synced_groups = []

        def mark_restore():
            nonlocal restore_started
            restore_started = True

        def checkpoint():
            _checkpoint(cancel)
            latest = _selected_game(game, profile)
            if (configuration != _configuration(latest, profile)
                    or latest.get_game_path() != selected.get_game_path()
                    or sidecars != {path: _token(Path(path)) for path in sidecars}):
                raise JobError("stale_smapi_plan", "The game configuration or installer identity changed.")

        progress(0, 1, "prepare_smapi")
        try:
            with prepare_smapi_payload(selected, archive, mode, log_fn=log,
                                       checkpoint=checkpoint) as (payload, sources, refresh):
                def verify_plan():
                    checkpoint()
                    current = _plan(game, profile, archive, mode, mod_name, payload, sources, cancel)
                    if current["revision"] != revision or sources != game_sources(selected, mode):
                        raise JobError("stale_smapi_plan", "The installer, native game files, or destination changed; inspect again.")
                    if not current["archive"]["source_verified"] and not allow_unverified_source:
                        raise JobError("smapi_source_unverified", "Confirm use of this manually selected installer archive.")
                    if mode == "mod" and current["destination"]["exists"] and not replace_existing:
                        raise JobError("smapi_mod_already_exists", "Confirm replacement of the existing SMAPI mod.")
                    if mode == "game" and selected.get_deploy_active():
                        recovery = profile.parent / selected.get_last_deployed_profile()
                        if (recovery.parent != profile.parent or
                                _selected_game(game, recovery).get_game_path() != selected.get_game_path()):
                            raise JobError("smapi_restore_target_mismatch", "Restore the deployed profile's different game folder first.")
                    return current

                current = verify_plan()

                def source_checkpoint():
                    checkpoint()
                    current_sources = game_sources(selected, mode)
                    if sources.get("deps") != current_sources.get("deps"):
                        raise JobError("smapi_game_files_changed", "The game's dependency manifest changed during installation.")

                def after_restore():
                    checkpoint()
                    refresh()

                def sync_groups():
                    if mode != "mod":
                        return
                    failures = []
                    try:
                        names = member_of_groups(game, profile.name)
                    except Exception as exc:
                        names = []
                        failures.append({"profile": None, "reason": str(exc)})
                    for name in names:
                        group = profile.parent / name
                        try:
                            if group.is_symlink() or not group.is_dir():
                                raise OSError("The profile group is unavailable or unsafe.")
                            materialize_group(_selected_game(game, group), group, log_fn=log)
                            synced_groups.append(name)
                        except Exception as exc:
                            failures.append({"profile": name, "reason": str(exc)})
                    if failures:
                        raise JobError("profile_group_refresh_failed", "SMAPI was installed but profile groups could not be refreshed.",
                                       {"failures": failures, "synced_groups": synced_groups, "state_persisted": True})

                label, count, name = install_prepared_payload(
                    selected, payload, mode, mod_fallback_name="SMAPI", mod_name_override=mod_name,
                    modlist_path=profile / "modlist.txt", restore_first=True, delete_archive=False,
                    log_fn=log, before_write=verify_plan, checkpoint=source_checkpoint,
                    on_restore_start=mark_restore, after_restore=after_restore,
                    after_write=sync_groups, strict_index=True)
        except PayloadInstallError as exc:
            raise JobError(exc.name, str(exc), {**exc.details, "restore_attempted": restore_started,
                           "state_persisted": restore_started or exc.details.get("state_persisted", False)}) from exc
        except (JobError, JobCancelled) as exc:
            exc.details.update(restore_attempted=restore_started,
                               state_persisted=restore_started or exc.details.get("state_persisted", False))
            raise
        except (OperationBusy, DeploymentBusy) as exc:
            raise JobError("operation_busy", str(exc),
                           {**getattr(exc, "details", {}), "restore_attempted": restore_started,
                            "state_persisted": restore_started}) from exc
        except (OSError, ValueError, RuntimeError, zipfile.BadZipFile) as exc:
            raise JobError("smapi_install_failed", "Could not install SMAPI.",
                           {"reason": str(exc), "restore_attempted": restore_started,
                            "state_persisted": restore_started}) from exc
        progress(1, 1, "install_smapi")
        return {"ok": True, "game": str(game.game_id), "profile": profile.name,
                "destination": {"path": current["destination"]["path"], "label": label, "mod_name": name},
                "file_count": count, "archive": {"path": str(archive), "retained": True},
                "restore_attempted": restore_started, "needs_deployment": mode != "game",
                "synced_groups": synced_groups}
    return run
