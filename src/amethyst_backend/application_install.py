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
        raise JobCancelled("Application installation cancelled.")


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
                raise JobError("unsafe_tool_destination", "An application output collides with a directory or special file.",
                               {"path": str(path)})
            if before is not None:
                overwrites.append(str(relative))
        elif path.is_symlink() or (path.exists() and not path.is_dir()):
            raise JobError("unsafe_tool_destination", "The application folder is unsafe.")
        records[str(path)] = before
        parent = path.parent
        while parent != target.parent.parent:
            if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
                raise JobError("unsafe_tool_destination", "An application output directory is a link or file.",
                               {"path": str(parent)})
            records[str(parent)] = _token(parent)
            parent = parent.parent
    return records, overwrites


def _plan(game, profile, tool_id, catalogue_revision, choice, archive, payload, cancel, family="xedit"):
    from Utils.wizards.applications import installation_target
    from Utils.wizards.payload import archive_token

    selected = _selected_game(game, profile)
    if not selected.is_configured():
        raise JobError("game_not_configured", "Configure the game before installing this application.")
    identity = archive_token(archive)
    digest = _digest_archive(archive, cancel)
    payload.check_archive()
    target = installation_target(selected, choice)
    if family == "synthesis":
        from Utils.bethesda.synthesis import validate_installation_target
        validate_installation_target(selected, target)
    if family == "lod" and target.is_relative_to(selected.get_game_path().resolve()):
        raise JobError("lod_installation_in_game", "Configure staging outside the game directory before installing LOD tools.")
    if archive.is_relative_to(target):
        raise JobError("tool_archive_in_destination", "Move the archive outside the application folder.")
    sidecars = {str(path): _token(path) for path in
                (archive.with_suffix(archive.suffix + ".tool.json"), archive.with_suffix(archive.suffix + ".nexus.json"))}
    verified = _source_identity(archive, choice.get("source_tool_id", tool_id), 0, choice, digest)
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


def inspect_runner(game, profile, tool_id, catalogue_revision, choice, archive_path, cancel, *, family="xedit"):
    def run(log, progress):
        from Utils.wizards.applications import prepare_application_payload
        from Utils.wizards.payload import PayloadInstallError

        archive = _archive_path(archive_path)
        progress(0, 1, f"inspect_{family}")
        try:
            with prepare_application_payload(archive, choice, log_fn=log,
                                             checkpoint=lambda: _checkpoint(cancel), family=family) as payload:
                result = _plan(game, profile, tool_id, catalogue_revision, choice, archive, payload, cancel, family)
        except PayloadInstallError as exc:
            raise JobError(exc.name, str(exc), exc.details) from exc
        except (JobError, JobCancelled):
            raise
        except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, tarfile.TarError) as exc:
            raise JobError(f"{family}_inspection_failed", "Could not inspect the application archive.",
                           {"reason": str(exc)}) from exc
        progress(1, 1, f"inspect_{family}")
        return result
    return run


def install_runner(game, profile, tool_id, catalogue_revision, choice,
                   archive_path, revision, allow_unverified_source, cancel, *, family="xedit"):
    def run(log, progress):
        from Utils.wizards.applications import install_application_payload, installation_target, prepare_application_payload
        from Utils.deployment.locking import DeploymentBusy
        from Utils.operations import OperationBusy, OperationCancelled
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
                raise JobError(f"stale_{family}_plan", "The application configuration or installer identity changed.")

        progress(0, 1, f"prepare_{family}")
        try:
            with prepare_application_payload(archive, choice, log_fn=log, checkpoint=checkpoint, family=family) as payload:
                def verify_plan():
                    checkpoint()
                    if catalogue(game, profile)["revision"] != catalogue_revision:
                        raise JobError("stale_tool_catalogue", "The application choices changed; refresh tools.list.")
                    current = _plan(game, profile, tool_id, catalogue_revision, choice, archive, payload, cancel, family)
                    if current["revision"] != revision:
                        raise JobError(f"stale_{family}_plan", "The archive or destination changed; inspect again.")
                    if not current["archive"]["source_verified"] and not allow_unverified_source:
                        raise JobError("unverified_tool_source", "Confirm use of this manually selected application archive.")

                target, count = install_application_payload(selected, payload, choice,
                                                           log_fn=log, checkpoint=checkpoint,
                                                           before_write=verify_plan, family=family, cancel=cancel)
        except PayloadInstallError as exc:
            raise JobError(exc.name, str(exc), exc.details) from exc
        except (OperationBusy, DeploymentBusy) as exc:
            raise JobError("operation_busy", str(exc), getattr(exc, "details", {})) from exc
        except OperationCancelled as exc:
            raise JobCancelled("Application installation cancelled.") from exc
        except (JobError, JobCancelled):
            raise
        except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, tarfile.TarError) as exc:
            raise JobError(f"{family}_install_failed", "Could not install the application.", {"reason": str(exc)}) from exc
        progress(1, 1, f"install_{family}")
        return {"ok": True, "game": str(game.game_id), "profile": profile.name, "tool_id": tool_id,
                "destination": {"path": str(target), "exe_path": str(target / choice["exe_name"])},
                "file_count": count, "archive": {"path": str(archive), "retained": True},
                "restore_attempted": False, "needs_deployment": False}
    return run
