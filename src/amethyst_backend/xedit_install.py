from __future__ import annotations

import hashlib
import json
import stat
import tarfile
import zipfile
from pathlib import Path

from .jobs import JobCancelled, JobError
from .tool_install import _archive_path, _digest_archive, _selected_game, _source_identity, _token


def _checkpoint(cancel):
    if cancel.is_set():
        raise JobCancelled("xEdit installation cancelled.")


def _configuration(game, profile):
    return {str(path): _token(path) for path in
            (game._paths_file, profile / "profile_state.json", profile / "profile_settings.json")}


def _destination_state(target, payload, cancel):
    records, overwrites = {}, []
    for relative in (Path(), *payload.files):
        _checkpoint(cancel)
        path = target / relative
        before = _token(path)
        if relative.parts:
            if before is not None and not (stat.S_ISREG(before[2]) or stat.S_ISLNK(before[2])):
                raise JobError("unsafe_tool_destination", "An xEdit output collides with a directory or special file.",
                               {"path": str(path)})
            if before is not None:
                overwrites.append(str(relative))
        elif path.is_symlink() or (path.exists() and not path.is_dir()):
            raise JobError("unsafe_tool_destination", "The xEdit application folder is unsafe.")
        records[str(path)] = before
        parent = path.parent
        while parent != target.parent.parent:
            if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
                raise JobError("unsafe_tool_destination", "An xEdit output directory is a link or file.",
                               {"path": str(parent)})
            records[str(parent)] = _token(parent)
            parent = parent.parent
    return records, overwrites


def _plan(game, profile, tool_id, catalogue_revision, choice, archive, payload, cancel):
    from Utils.bethesda.xedit_install import installation_target
    from Utils.wizards.payload import archive_token

    selected = _selected_game(game, profile)
    if not selected.is_configured():
        raise JobError("game_not_configured", "Configure the game before installing xEdit.")
    identity = archive_token(archive)
    digest = _digest_archive(archive, cancel)
    payload.check_archive()
    target = installation_target(selected, choice)
    if archive.is_relative_to(target):
        raise JobError("tool_archive_in_destination", "Move the archive outside the xEdit application folder.")
    sidecars = {str(path): _token(path) for path in
                (archive.with_suffix(archive.suffix + ".tool.json"), archive.with_suffix(archive.suffix + ".nexus.json"))}
    verified = _source_identity(archive, tool_id, 0, choice, digest)
    records, overwrites = _destination_state(target, payload, cancel)
    snapshot = {"game": str(selected.game_id), "profile": str(profile), "tool_id": tool_id,
                "catalogue_revision": catalogue_revision, "choice": choice,
                "configuration": _configuration(selected, profile),
                "archive": [str(archive), identity, digest, sidecars],
                "target": str(target), "destination_state": records,
                "files": [str(path) for path in payload.files]}
    if sidecars != {path: _token(Path(path)) for path in sidecars}:
        raise JobError("tool_archive_changed", "The installer identity changed during inspection.")
    return {"revision": hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest(),
            "game": str(game.game_id), "profile": profile.name, "tool_id": tool_id,
            "archive": {"path": str(archive), "sha256": digest, "source_verified": verified is not None,
                        "source_identity": verified, "retained": True},
            "destination": {"path": str(target), "exe_path": str(target / choice["exe_name"]),
                            "exists": target.exists(), "overwrite_count": len(overwrites),
                            "overwrites": overwrites[:100]},
            "file_count": len(payload.files), "restore_before_install": False,
            "needs_deployment": False}


def inspect_runner(game, profile, tool_id, catalogue_revision, choice, archive_path, cancel):
    def run(log, progress):
        from Utils.bethesda.xedit_install import prepare_xedit_payload
        from Utils.wizards.payload import PayloadInstallError

        archive = _archive_path(archive_path)
        progress(0, 1, "inspect_xedit")
        try:
            with prepare_xedit_payload(archive, choice, log_fn=log,
                                       checkpoint=lambda: _checkpoint(cancel)) as payload:
                result = _plan(game, profile, tool_id, catalogue_revision, choice, archive, payload, cancel)
        except PayloadInstallError as exc:
            raise JobError(exc.name, str(exc), exc.details) from exc
        except (JobError, JobCancelled):
            raise
        except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, tarfile.TarError) as exc:
            raise JobError("xedit_inspection_failed", "Could not inspect the xEdit archive.",
                           {"reason": str(exc)}) from exc
        progress(1, 1, "inspect_xedit")
        return result
    return run


def install_runner(game, profile, tool_id, catalogue_revision, choice,
                   archive_path, revision, allow_unverified_source, cancel):
    def run(log, progress):
        from Utils.bethesda.xedit_install import install_xedit_payload, installation_target, prepare_xedit_payload
        from Utils.deployment.locking import DeploymentBusy
        from Utils.operations import OperationBusy
        from Utils.wizards.payload import PayloadInstallError
        from .tools import catalogue

        archive = _archive_path(archive_path)
        selected = _selected_game(game, profile)
        configuration = _configuration(selected, profile)
        target = installation_target(selected, choice)
        sidecars = {str(path): _token(path) for path in
                    (archive.with_suffix(archive.suffix + ".tool.json"), archive.with_suffix(archive.suffix + ".nexus.json"))}

        def checkpoint():
            _checkpoint(cancel)
            latest = _selected_game(game, profile)
            if (configuration != _configuration(latest, profile) or installation_target(latest, choice) != target
                    or sidecars != {path: _token(Path(path)) for path in sidecars}):
                raise JobError("stale_xedit_plan", "The xEdit configuration or installer identity changed.")

        progress(0, 1, "prepare_xedit")
        try:
            with prepare_xedit_payload(archive, choice, log_fn=log, checkpoint=checkpoint) as payload:
                def verify_plan():
                    checkpoint()
                    if catalogue(game, profile)["revision"] != catalogue_revision:
                        raise JobError("stale_tool_catalogue", "The xEdit choices changed; refresh tools.list.")
                    current = _plan(game, profile, tool_id, catalogue_revision, choice, archive, payload, cancel)
                    if current["revision"] != revision:
                        raise JobError("stale_xedit_plan", "The archive or destination changed; inspect again.")
                    if not current["archive"]["source_verified"] and not allow_unverified_source:
                        raise JobError("unverified_tool_source", "Confirm use of this manually selected xEdit archive.")

                target, count = install_xedit_payload(selected, payload, choice,
                                                      log_fn=log, checkpoint=checkpoint, before_write=verify_plan)
        except PayloadInstallError as exc:
            raise JobError(exc.name, str(exc), exc.details) from exc
        except (OperationBusy, DeploymentBusy) as exc:
            raise JobError("operation_busy", str(exc), getattr(exc, "details", {})) from exc
        except (JobError, JobCancelled):
            raise
        except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, tarfile.TarError) as exc:
            raise JobError("xedit_install_failed", "Could not install xEdit.", {"reason": str(exc)}) from exc
        progress(1, 1, "install_xedit")
        return {"ok": True, "game": str(game.game_id), "profile": profile.name, "tool_id": tool_id,
                "destination": {"path": str(target), "exe_path": str(target / choice["exe_name"])},
                "file_count": count, "archive": {"path": str(archive), "retained": True},
                "restore_attempted": False, "needs_deployment": False}
    return run
