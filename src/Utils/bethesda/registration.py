from __future__ import annotations

import subprocess
import threading
from contextlib import ExitStack
from pathlib import Path

from .registry import (
    RegistryRegistrationCancelled, RegistryRegistrationError, _marker_path,
    register_bethesda_game_path, registration_state,
)


def write_game_registration(game, script, env, *, force=True, cancel=None,
                            before_write=lambda: None, checkpoint=lambda: None,
                            log_fn=lambda _m: None, progress_fn=lambda *_a: None):
    from Utils.executables.launch import shutdown_prefix_wineserver
    from Utils.operations import acquire_resource_operation
    from Utils.profiles.state import read_only_profile_state
    from Utils.wine.prefix import resolve_compat_data
    from Utils.wine.protontricks import run_prefix_installer
    from Utils.wine.repair import prefix_repair_operation

    cancel = cancel or threading.Event()
    registry_name = getattr(game, "synthesis_registry_name", None)
    if not registry_name:
        raise RegistryRegistrationError("game_registry_unavailable", "This game has no Bethesda registry registration.")
    attempted, written = [], []

    def check():
        if cancel.is_set():
            raise RegistryRegistrationCancelled()
        try:
            checkpoint()
        except RegistryRegistrationError:
            raise
        except Exception as exc:
            raise RegistryRegistrationError(getattr(exc, "name", "game_registry_write_failed"),
                                             str(exc), getattr(exc, "details", {})) from exc

    with read_only_profile_state(), prefix_repair_operation(game, stop=cancel, operation="wine.game_registry.write") as prefix, ExitStack() as locks:
        check()
        for name in ("system.reg", "user.reg", "userdef.reg"):
            locks.enter_context(acquire_resource_operation(f"wine-state:{(prefix / name).resolve()}",
                                                           "wine.game_registry.write", cancel=cancel))
        compat = prefix if Path(script).name in ("wine", "wine64") else resolve_compat_data(prefix)
        for key, expected in (("WINEPREFIX", prefix), ("STEAM_COMPAT_DATA_PATH", compat)):
            if key in env and Path(env[key]).expanduser().resolve() != expected.resolve():
                raise RegistryRegistrationError("wine_prefix_binding_mismatch", "The runner environment targets a different prefix.")
        before_write()
        check()
        state = registration_state(prefix, registry_name, game.get_game_path())
        if not force and state["registered"]:
            return {"ok": True, "no_op": True, "attempted_views": [], "written_views": [],
                    "state_persisted": False, "registration": state}
        marker = _marker_path(compat, registry_name)
        if (marker.parent.is_symlink() or (marker.parent.exists() and not marker.parent.is_dir())
                or marker.is_symlink() or (marker.exists() and not marker.is_file())):
            raise RegistryRegistrationError("unsafe_registry_marker", "The registry marker is a link or special file.")
        marker_removed = marker.exists()
        marker.unlink(missing_ok=True)
        env = {**env, "WINEDEBUG": "-all", "WINEPREFIX": str(prefix), "STEAM_COMPAT_DATA_PATH": str(compat)}
        total = 1 if state["architecture"] == "win32" else 2

        def execute(command, **options):
            check()
            view = next(value.split(":", 1)[1] for value in command if value.startswith("/reg:"))
            attempted.append(view)
            progress_fn(len(written), total, f"write_game_registry:{view}")
            code, output = run_prefix_installer(command, env, compat, label="Game registry",
                                                timeout=options["timeout"], proton_script=script,
                                                compat_data=compat, cancel=cancel, log_fn=log_fn)
            if code == 0:
                written.append(view)
            check()
            return subprocess.CompletedProcess(command, code if code is not None else 1, stdout=output, stderr="")

        pending = None
        try:
            succeeded = register_bethesda_game_path(compat, script, env, game.get_game_path(), registry_name,
                                                   log_fn=log_fn, force=True, checkpoint=check, run_command=execute)
        except Exception as exc:
            pending = exc
            succeeded = False
        finally:
            try:
                if attempted:
                    shutdown_prefix_wineserver(script, compat, log_fn=log_fn, wait=True)
            except Exception as exc:
                pending = RegistryRegistrationError("game_registry_cleanup_failed", "Could not finish registry process cleanup.",
                                                     {"reason": str(exc)})
        details = {"attempted_views": attempted, "written_views": written, "state_persisted": bool(attempted or marker_removed),
                   "prefix_path": str(prefix), "registration": registration_state(prefix, registry_name, game.get_game_path())}
        if pending is None:
            try:
                check()
            except Exception as exc:
                pending = exc
        if pending is not None:
            marker.unlink(missing_ok=True)
            if hasattr(pending, "details"):
                pending.details.update(details)
                raise pending
            raise RegistryRegistrationError("game_registry_write_failed", "Could not write the game registry.",
                                             {**details, "reason": str(pending)}) from pending
        if not succeeded or not details["registration"]["registered"]:
            marker.unlink(missing_ok=True)
            raise RegistryRegistrationError("game_registry_write_failed", "The requested registry values could not be verified.", details)
        progress_fn(total, total, "game_registry_written")
        return {"ok": True, "no_op": False, **details}
