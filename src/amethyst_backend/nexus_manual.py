from __future__ import annotations

import json
import queue
from contextlib import nullcontext
from pathlib import Path

from .jobs import JobCancelled, JobError, _validated_archive_path
from .nexus import _call, _file


def browser_url(domain, mod_id, file_id):
    return f"https://www.nexusmods.com/{domain}/mods/{mod_id}?tab=files&file_id={file_id}"


def runner(session, game, profile, domain, mod_id, file_id, cancel, *, input_wait=nullcontext):
    def run(log, progress):
        def operation():
            from Nexus.manual_download_watch import ManualDownloadWatcher
            from Utils.atomic_write import write_atomic_text
            from Utils.downloads.inventory import build_inventory
            from Utils.operations import OperationCancelled, acquire_resource_operation
            from .download_archives import _token

            if cancel.is_set():
                raise JobCancelled()
            api = session.require_api()
            file = api.get_file_info(domain, mod_id, file_id)
            if file is None or int(file.file_id) != file_id:
                raise JobError("nexus_file_not_found", "The requested Nexus file is unavailable.")
            arrived = queue.Queue(maxsize=1)
            watcher = ManualDownloadWatcher(
                mod_id=mod_id, files=[file],
                on_found=lambda path, found: arrived.put((path, found)),
                on_progress=lambda done, total: progress(done, total, "browser_download"),
                on_timeout=lambda: arrived.put(None))
            if cancel.is_set():
                raise JobCancelled()
            log("Waiting for the selected Nexus file in enabled browser download locations.")
            progress(0, 1, "waiting_for_browser_download")
            watcher.start()
            try:
                with input_wait():
                    while True:
                        if cancel.is_set():
                            raise JobCancelled("Browser download handoff cancelled; browser files were retained.")
                        try:
                            found = arrived.get(timeout=0.25)
                            break
                        except queue.Empty:
                            continue
                if found is None:
                    raise JobError("nexus_manual_download_timeout", "No matching browser download arrived.")
                path = Path(found[0])
                inventory = build_inventory(game, profile)
                archive = next((item for item in inventory["archives"]
                                if Path(item["path"]).absolute() == path.absolute()), None)
                if archive is None:
                    raise JobError("archive_not_found", "The browser archive is no longer in an enabled download location.")
                path = _validated_archive_path(inventory, archive)
                with acquire_resource_operation(f"nexus.manual:{path}", "nexus.download.manual", cancel=cancel):
                    if cancel.is_set():
                        raise JobCancelled()
                    token = _token(path, required=True)
                    identity = {"version": 1, "game_domain": domain, "mod_id": mod_id, "file_id": file_id}
                    sidecar = path.with_suffix(path.suffix + ".nexus.json")
                    if sidecar.is_symlink():
                        raise JobError("unsafe_archive", "The archive identity sidecar is a symbolic link.")
                    if sidecar.exists():
                        try:
                            existing = json.loads(sidecar.read_text())
                        except (OSError, ValueError) as exc:
                            raise JobError("nexus_archive_identity_conflict", "The archive has an unreadable identity sidecar.") from exc
                        if existing != identity:
                            raise JobError("nexus_archive_identity_conflict", "The archive belongs to another Nexus file.")
                    if _token(path, required=True) != token:
                        raise JobError("nexus_archive_changed", "The browser archive changed during verification.")
                    write_atomic_text(sidecar, json.dumps(identity))
                progress(1, 1, "browser_download_ready")
                return {"source": {"provider": "nexus", "mode": "browser", "domain": domain,
                                   "mod_id": mod_id, "file_id": file_id},
                        "archive": {"id": archive["id"], "path": str(path), "name": path.name,
                                    "size_bytes": path.stat().st_size, "retained": True},
                        "file": _file(file)}
            except OperationCancelled as exc:
                raise JobCancelled("Browser download handoff cancelled; browser files were retained.") from exc
            finally:
                watcher.stop()
                thread = getattr(watcher, "_thread", None)
                if thread is not None:
                    thread.join(timeout=2)

        return _call(operation)
    return run
