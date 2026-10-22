from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import threading
import time
import uuid
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

from .jobs import JobCancelled, JobError, _install_download, _validated_archive_path


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _source_token(path):
    from .download_archives import _token

    return _token(path, required=True)


def _binding(game, profile, prepared, archive):
    from Utils.fomod.session import config_identity
    from Utils.mods.copy import resolve_target_staging

    files = [profile / name for name in (
        "modlist.txt", "plugins.txt", "profile_state.json", "profile_settings.json")]
    paths_file = getattr(game, "_paths_file", None)
    if paths_file:
        files.append(Path(paths_file))
    return _hash({
        "archive": _source_token(archive),
        "config": config_identity(prepared.fomod_config),
        "context": [sorted(values) for values in prepared.fomod_context],
        "profile": str(profile.resolve()),
        "staging": str(Path(resolve_target_staging(game, profile)).resolve()),
        "files": {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                  if path.is_file() else None for path in files},
    })


class InstallerSessions:
    maximum = 8
    lifetime = 1800

    def __init__(self):
        self._lock = threading.RLock()
        self._sessions = {}

    def _saved_path(self, session_id):
        from Utils.config_paths import get_config_dir
        if not re.fullmatch("[0-9a-f]{32}", session_id):
            raise JobError("installer_session_not_found", "Invalid installer session ID.")
        directory = Path(os.environ.get("AMETHYST_INSTALLER_STATE_DIR") or get_config_dir() / "headless" / "installers")
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        return directory / (session_id + ".json")

    def _save(self, session):
        from Utils.atomic_write import atomic_writer
        record = {"id": session["id"], "game": session["game"].game_id,
                  "profile": session["profile"].name, "archive_id": session["archive_id"],
                  "archive_path": session.get("archive_path"),
                  "name": session["name"], "binding": session["binding"],
                  "draft": session["wizard"].draft, "selections": session["wizard"].selections}
        with atomic_writer(self._saved_path(session["id"])) as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(record, stream)

    def saved_record(self, session_id):
        path = self._saved_path(session_id)
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
            raise JobError("installer_session_not_found", "The saved installer session is unavailable.")
        try:
            record = json.loads(path.read_text())
            if (record["id"] != session_id or not isinstance(record["draft"], dict)
                    or not isinstance(record["selections"], dict)
                    or any(not isinstance(record[key], str) for key in ("game", "profile", "name", "binding"))
                    or record.get("archive_path") is not None and not isinstance(record["archive_path"], str)):
                raise ValueError()
            return record
        except (KeyError, ValueError, TypeError) as exc:
            raise JobError("installer_session_invalid", "The saved installer session could not be read.") from exc

    def saved(self):
        with self._lock:
            records = []
            for path in sorted(self._saved_path("0" * 32).parent.glob("*.json")):
                try:
                    record = self.saved_record(path.stem)
                    records.append({key: record[key] for key in ("id", "game", "profile", "archive_id", "name")})
                except (JobError, OSError):
                    continue
            return {"sessions": records}

    def resume(self, record, game, profile, log, progress, cancel):
        result = self.open(game, profile, record["archive_id"], record["name"], log, progress, cancel,
                           archive_path=record.get("archive_path"))
        session_id = result["session"]["id"]
        try:
            with self._lock:
                session = self._get(session_id)
                if session["binding"] != record["binding"]:
                    raise JobError("stale_installer_session", "The archive or profile changed; open a new installer to review its choices.")
                session["wizard"].draft = deepcopy(record["draft"])
                session["wizard"].recompute()
                if session["wizard"].selections != record["selections"]:
                    raise JobError("stale_installer_session", "The saved installer choices have changed.")
                self._save(session)
                self._saved_path(record["id"]).unlink(missing_ok=True)
                return self.get(session_id)
        except BaseException:
            self.close(session_id)
            raise

    def _get(self, session_id, revision=None):
        now = time.monotonic()
        self._expire()
        session = self._sessions.get(session_id)
        if session is None:
            raise JobError("installer_session_not_found", "The installer session expired or was closed.")
        if session["busy"]:
            raise JobError("installer_session_busy", "This installer session is being installed.")
        if revision is not None and revision != self._revision(session):
            raise JobError("stale_installer_session", "Read the current installer selections before continuing.")
        session["touched"] = now
        return session

    @staticmethod
    def _revision(session):
        return _hash([session["id"], session["binding"], session["wizard"].selections])

    def open(self, game, profile, archive_id, name, log, progress, cancel, archive_path=None):
        from Utils.downloads.inventory import build_inventory
        from Utils.fomod.session import FomodSession
        from Utils.mods.install import prepare_archive
        from Utils.operations import acquire_game_operation
        from Utils.profiles.groups import is_group

        prepared = None
        with acquire_game_operation(game, "installers.sessions.open"):
            if profile.is_symlink() or not profile.is_dir() or is_group(profile):
                raise JobError("profile_group_requires_member", "Open an installer for a real, available member profile.")
            inventory = build_inventory(game, profile)
            archive = next((item for item in inventory["archives"] if item["id"] == archive_id), None)
            if archive_path is not None:
                from .install_options import local_archive
                archive = local_archive(archive_path)
            if archive is None:
                raise JobError("archive_not_found", "The downloaded archive is unavailable.")
            path = Path(archive["path"]) if archive_path is not None else _validated_archive_path(inventory, archive)
            token = _source_token(path)
            try:
                if cancel.is_set():
                    raise JobCancelled()
                prepared = prepare_archive(str(path), game, profile, preferred_name=name,
                                           log_fn=log, progress_fn=progress, cancel=cancel)
                if cancel.is_set():
                    raise JobCancelled()
                if prepared is None:
                    raise JobError("install_failed", "The archive could not be prepared.")
                if not prepared.is_fomod() or prepared.is_scripted_fomod() or prepared.fomod_parse_failed:
                    raise JobError("installer_session_unsupported", "Sessions require a parsed XML FOMOD; use archive inspection for other installers.")
                if _source_token(path) != token:
                    raise JobError("installer_archive_changed", "The archive changed during preparation.")
                wizard = FomodSession(prepared.fomod_config, saved=prepared.saved_fomod_selections or {},
                                      context=prepared.fomod_context)
                with self._lock:
                    self._expire()
                    if len(self._sessions) >= self.maximum:
                        raise JobError("installer_session_limit", "Close an existing installer session first.")
                    session_id = uuid.uuid4().hex
                    self._sessions[session_id] = {
                        "id": session_id, "game": game, "profile": profile,
                        "prepared": prepared, "wizard": wizard, "archive_id": archive_id,
                        "archive_path": archive_path,
                        "archive": path, "name": name, "binding": _binding(game, profile, prepared, path),
                        "busy": False, "touched": time.monotonic(),
                    }
                    try:
                        result = self.get(session_id)
                        self._save(self._sessions[session_id])
                    except Exception:
                        self._sessions.pop(session_id)
                        raise
                    prepared = None
                    return result
            finally:
                if prepared is not None:
                    prepared.cleanup()

    def _expire(self):
        for key, session in tuple(self._sessions.items()):
            if not session["busy"] and time.monotonic() - session["touched"] > self.lifetime:
                self._sessions.pop(key)["prepared"].cleanup()

    def get(self, session_id):
        from Utils.fomod.installer import check_module_dependencies

        with self._lock:
            session = self._get(session_id)
            wizard = session["wizard"]
            prepared = session["prepared"]
            satisfied, missing = check_module_dependencies(prepared.fomod_config, *prepared.fomod_context)
            steps = []
            for si in wizard.visible:
                step = wizard.config.steps[si]
                groups = []
                for gi, group in enumerate(step.groups):
                    options = []
                    for option in group.plugins:
                        state = wizard.states[si, gi][option.name]
                        options.append({
                            "name": option.name, "description": option.description,
                            "selected": option.name in wizard.selections[str(si)][group.name],
                            "type": state.plugin_type, "locked": state.locked,
                            "missing_dependency": state.missing_dependency,
                            "previously_selected": state.previously_selected,
                            "image_available": bool(option.image_path),
                        })
                    groups.append({"index": gi, "name": group.name, "label": group.display_name or group.name,
                                   "type": group.group_type, "options": options})
                steps.append({"index": si, "name": step.name, "groups": groups})
            return {"session": {
                "id": session_id, "revision": self._revision(session), "profile": session["profile"].name,
                "archive_id": session["archive_id"], "mod": prepared.mod_name,
                "title": wizard.config.name, "steps": steps, "selections": deepcopy(wizard.selections),
                "dependencies_satisfied": satisfied, "missing_dependencies": missing,
                "errors": [{"step": si, "message": error} for si, error in wizard.errors],
                "expires_after_seconds": self.lifetime,
            }}

    @staticmethod
    def _option(session, step, group, option):
        wizard = session["wizard"]
        try:
            if step not in wizard.visible or step < 0 or group < 0:
                raise KeyError()
            selected_group = wizard.config.steps[step].groups[group]
            return next(value for value in selected_group.plugins if value.name == option)
        except (IndexError, KeyError, StopIteration) as exc:
            raise JobError("installer_option_not_found", "The selected visible installer option does not exist.") from exc

    def update(self, session_id, revision, step, group, option):
        with self._lock:
            session = self._get(session_id, revision)
            self._option(session, step, group, option)
            session["wizard"].toggle(step, group, option)
            self._save(session)
            return self.get(session_id)

    def image(self, session_id, step, group, option):
        from PIL import Image
        from Utils.fomod.parser import resolve_path_ci

        with self._lock:
            session = self._get(session_id)
            selected = self._option(session, step, group, option)
            base = Path(session["prepared"].fomod_base).resolve()
            resolved = resolve_path_ci(str(base), selected.image_path) if selected.image_path else None
            path = Path(resolved).resolve() if resolved else None
            if path is None or not path.is_relative_to(base) or not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
                raise JobError("installer_image_unavailable", "The installer image is unavailable.")
            try:
                with Image.open(path) as original:
                    original.thumbnail((960, 540))
                    output = io.BytesIO()
                    original.convert("RGB").save(output, format="JPEG", quality=85)
            except (OSError, ValueError, Image.DecompressionBombError) as exc:
                raise JobError("installer_image_unavailable", "The installer image could not be decoded.") from exc
            return {"image": {"mime_type": "image/jpeg", "data": base64.b64encode(output.getvalue()).decode("ascii")}}

    def target(self, session_id, revision):
        with self._lock:
            session = self._get(session_id, revision)
            return session["game"], session["profile"]

    def install(self, session_id, revision, if_exists, log, progress, cancel=None):
        with self._lock:
            session = self._get(session_id, revision)
            if session["wizard"].errors:
                raise JobError("installer_selection_invalid", "Resolve the installer errors before installing.")
            session["busy"] = True
            options = {"archive_id": session["archive_id"], "name": session["name"],
                       "archive_path": session.get("archive_path"),
                       "fomod_selections": deepcopy(session["wizard"].selections), "if_exists": if_exists}
        success = False
        try:
            def verify(prepared, path):
                if not prepared.is_fomod() or _binding(session["game"], session["profile"], prepared, path) != session["binding"]:
                    raise JobError("stale_installer_session", "The archive or profile changed; reopen the installer.")

            job = SimpleNamespace(id=session_id, kind="downloads.install", game=session["game"],
                                  profile=session["profile"].name, options=options, cancel_event=cancel)
            token = _source_token(session["archive"])
            def before_install():
                if _source_token(session["archive"]) != token:
                    raise JobError("installer_archive_changed", "The archive changed during installation preparation.")
            result = _install_download(job, log, progress, verify_prepared=verify, before_install=before_install)
            success = True
            return result
        finally:
            with self._lock:
                session["busy"] = False
                if success:
                    self.close(session_id)

    def close(self, session_id):
        with self._lock:
            self._expire()
            if session_id not in self._sessions:
                self.saved_record(session_id)
                self._saved_path(session_id).unlink()
                return {"closed": True, "session_id": session_id}
            session = self._get(session_id)
            self._sessions.pop(session_id)
            session["prepared"].cleanup()
            self._saved_path(session_id).unlink(missing_ok=True)
            return {"closed": True, "session_id": session_id}

    def close_all(self):
        with self._lock:
            for session in self._sessions.values():
                session["prepared"].cleanup()
            self._sessions.clear()
