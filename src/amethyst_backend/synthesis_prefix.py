from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from .jobs import JobCancelled, JobError
from .tool_install import _selected_game
from .xedit_run import _identity


def _configuration(game, profile):
    from Utils.bethesda.synthesis import _settings_path

    return {str(path): _identity(path) for path in (
        game._paths_file, profile / "profile_state.json", profile / "profile_settings.json", _settings_path(game))}


def _directory(path):
    value = _identity(path)
    return [str(path.resolve()), value[0][:3] if value else None]


def _sources(script, *, include_fonts=True):
    from Utils.bethesda.synthesis import _wine_bin
    from Utils.wizards.payload import tree_identity
    from Utils.ca_bundle import resolve_ca_bundle

    wine = _wine_bin(script)
    bundle = resolve_ca_bundle()
    result = {"binaries": {str(path): _identity(path) for path in (script, wine, wine.parent / "wineserver")},
              "ca_bundle": [bundle, _identity(Path(bundle))] if bundle else None}
    if include_fonts:
        result["fonts"] = {str(wine.parent.parent / relative): tree_identity(wine.parent.parent / relative)
                           for relative in ("share/fonts", "share/wine/fonts")}
    return result


def _plan(game, profile, tool_id, catalogue_revision, choice, request, cancel):
    from Utils.bethesda import synthesis
    from Utils.bethesda.registry import registration_state
    from Utils.bethesda.synthesis_setup import dotnet_present, validate_prefix_paths
    from Utils.bethesda.xedit_run import executable
    from Utils.wizards.payload import tree_identity
    from Utils.wine.registry import prefix_architecture
    from Utils.wine.repair import prefix_usage
    from .tools import catalogue

    if cancel.is_set():
        raise JobCancelled("Synthesis prefix planning cancelled.")
    selected = _selected_game(game, profile)
    if not selected.is_configured():
        raise JobError("game_not_configured", "Configure the selected game before preparing Synthesis.")
    if catalogue(game, profile)["revision"] != catalogue_revision:
        raise JobError("stale_tool_catalogue", "Refresh the selected profile's tools.list catalogue.")
    exe = executable(selected, choice)
    synthesis.validate_installation_target(selected, exe.parent)
    name = request.get("proton_name") or synthesis.load_saved_proton(selected)
    candidates = synthesis.list_proton()
    matches = [path for path in candidates if path.parent.name == name] if name else candidates[:1]
    if not matches:
        raise JobError("synthesis_runner_unavailable", "Select an installed complete Proton build for Synthesis.", {"proton_name": name})
    if len(matches) != 1:
        raise JobError("ambiguous_synthesis_runner", "More than one installed Proton build has the selected name.")
    script = matches[0]
    wine = synthesis._wine_bin(script)
    if not all(path.is_file() for path in (script, wine, wine.parent / "wineserver")) or not os.access(wine, os.X_OK):
        raise JobError("synthesis_runner_unavailable", "The selected Proton build has no usable Wine and wineserver binaries.")
    prefix = synthesis.synthesis_pfx(selected)
    validate_prefix_paths(prefix, deep=True)
    if prefix_architecture(prefix) == "win32":
        raise JobError("synthesis_prefix_architecture", "Synthesis requires its dedicated prefix to be 64-bit.")
    configuration = _configuration(selected, profile)
    sources = _sources(script)
    state = {"directories": {str(path): _directory(path) for path in (exe.parent, prefix.parent, prefix, prefix / "drive_c")},
             "hives": {str(prefix / name): _identity(prefix / name) for name in ("system.reg", "user.reg", "userdef.reg")},
             "markers": tree_identity(prefix / ".synthesis_setup"),
             "runtime_files": tree_identity(prefix / "drive_c/Program Files/dotnet", lambda: _cancel(cancel)),
             "nuget": _identity(prefix / "drive_c/users/steamuser/AppData/Roaming/NuGet/NuGet.Config")}
    pids = prefix_usage(prefix)
    components = {name: dotnet_present(prefix, name) for name in (
        "dotnet10_sdk", "dotnet10_desktop", "dotnet8_desktop", "dotnet7_desktop", "dotnet6_desktop")}
    snapshot = {"game": str(game.game_id), "profile": str(profile), "tool_id": tool_id,
                "catalogue_revision": catalogue_revision, "choice": choice, "request": request,
                "configuration": configuration, "exe": [str(exe), _identity(exe)], "sources": sources,
                "game_path": _directory(selected.get_game_path()), "prefix": str(prefix), "prefix_state": state}
    if configuration != _configuration(_selected_game(game, profile), profile):
        raise JobError("stale_synthesis_prefix", "The Synthesis configuration changed during planning.")
    _cancel(cancel)
    return {"revision": hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest(),
            "game": str(game.game_id), "profile": profile.name, "tool_id": tool_id,
            "application_path": str(exe.parent), "prefix_path": str(prefix), "proton_name": script.parent.name,
            "runner_path": str(script), "wine_path": str(wine), "components": components,
            "initialized": (prefix / "drive_c").is_dir() and all((prefix / name).is_file() for name in ("system.reg", "user.reg")),
            "can_setup": pids == set(), "prefix_pids": sorted(pids) if pids is not None else None,
            "process_scan_available": pids is not None, "save_settings": request.get("save_settings", False),
            "registration": registration_state(prefix, selected.synthesis_registry_name, selected.get_game_path()),
            "runtime_execution_verified": False, "needs_deployment": False}


def _cancel(cancel):
    if cancel.is_set():
        raise JobCancelled("Synthesis prefix planning cancelled.")


def _error(exc):
    name = exc.name.replace("xedit_", "synthesis_")
    return JobError(name, str(exc).replace("xEdit", "Synthesis"), exc.details)


def plan_runner(game, profile, tool_id, catalogue_revision, choice, request, cancel):
    def run(log, progress):
        from Utils.bethesda.synthesis_setup import SynthesisSetupError
        from Utils.bethesda.tool_run import ToolRunError
        from Utils.wizards.payload import PayloadInstallError

        try:
            progress(0, 1, "plan_synthesis_prefix")
            result = _plan(game, profile, tool_id, catalogue_revision, choice, request, cancel)
            progress(1, 1, "plan_synthesis_prefix")
            return result
        except (SynthesisSetupError, ToolRunError, PayloadInstallError) as exc:
            raise _error(exc) from exc
        except (OSError, RuntimeError) as exc:
            if isinstance(exc, (JobError, JobCancelled)):
                raise
            raise JobError("synthesis_prefix_unavailable", "Could not inspect the Synthesis prefix.", {"reason": str(exc)}) from exc
    return run


def setup_runner(game, profile, tool_id, catalogue_revision, choice, request, revision, cancel):
    def run(log, progress):
        from Utils.bethesda.synthesis_setup import SynthesisSetupCancelled, SynthesisSetupError, prepare_prefix
        from Utils.bethesda.tool_run import ToolRunError
        from Utils.deployment.locking import DeploymentBusy
        from Utils.operations import OperationBusy, OperationCancelled
        from Utils.wizards.payload import PayloadInstallError

        try:
            initial = _plan(game, profile, tool_id, catalogue_revision, choice, request, cancel)
            if initial["revision"] != revision:
                raise JobError("stale_synthesis_prefix", "The Synthesis application, prefix or runner changed; plan setup again.")
            selected = _selected_game(game, profile)
            script = Path(initial["runner_path"])
            prefix = Path(initial["prefix_path"])
            sources = _sources(script)
            configuration = _configuration(selected, profile)
            binding = {str(path): _directory(path) for path in (selected.get_game_path(), Path(initial["application_path"]), prefix.parent, prefix, prefix / "drive_c")}
            exe = Path(initial["application_path"]) / choice["exe_name"]
            exe_identity = _identity(exe)

            def checkpoint():
                current = _selected_game(game, profile)
                from Utils.bethesda.synthesis import synthesis_pfx
                if (synthesis_pfx(current) != prefix or current.get_game_path() != selected.get_game_path()
                        or _configuration(current, profile) != configuration
                        or any(value != sources[key] for key, value in _sources(script, include_fonts=False).items())
                        or _identity(exe) != exe_identity or any(
                            value[1] is not None and _directory(Path(path)) != value for path, value in binding.items())):
                    raise JobError("stale_synthesis_prefix", "Synthesis configuration, runner or directory ownership changed during setup.")

            def verified():
                if _plan(game, profile, tool_id, catalogue_revision, choice, request, cancel)["revision"] != revision:
                    raise JobError("stale_synthesis_prefix", "The Synthesis prefix plan changed while waiting.")

            def initialized():
                checkpoint()
                for path in (prefix.parent, prefix, prefix / "drive_c"):
                    binding[str(path)] = _directory(path)

            def settings_saved():
                from Utils.bethesda.synthesis import _settings_path, load_saved_proton
                path = _settings_path(selected)
                if load_saved_proton(selected) != initial["proton_name"]:
                    raise JobError("synthesis_settings_save_failed", "Could not verify the saved Synthesis Proton selection.")
                configuration[str(path)] = _identity(path)

            def final_check():
                checkpoint()
                if _sources(script) != sources:
                    raise JobError("stale_synthesis_prefix", "The Proton setup sources changed during setup.")

            def cleanup_check():
                if _sources(script, include_fonts=False)["binaries"] != sources["binaries"]:
                    raise SynthesisSetupError("synthesis_prefix_cleanup_failed", "The Wine cleanup binaries changed during setup.")
                for path in (Path(initial["application_path"]), prefix.parent, prefix, prefix / "drive_c"):
                    expected = binding[str(path)]
                    if expected[1] is not None and _directory(path) != expected:
                        raise SynthesisSetupError("synthesis_prefix_cleanup_failed", "The dedicated prefix changed ownership; cleanup cannot target it safely.")

            result = prepare_prefix(selected, script, cancel=cancel, before_write=verified, checkpoint=checkpoint,
                                    on_initialized=initialized, on_settings_saved=settings_saved,
                                    final_check=final_check, cleanup_check=cleanup_check,
                                    save_settings=initial["save_settings"], log_fn=log, progress_fn=progress)
            return {**result, "game": str(game.game_id), "profile": profile.name, "tool_id": tool_id,
                    "proton_name": initial["proton_name"], "runner_path": initial["runner_path"]}
        except SynthesisSetupCancelled as exc:
            raise JobCancelled(str(exc), exc.details) from exc
        except (SynthesisSetupError, ToolRunError, PayloadInstallError) as exc:
            raise _error(exc) from exc
        except (OperationBusy, DeploymentBusy) as exc:
            raise JobError("operation_busy", str(exc), getattr(exc, "details", {})) from exc
        except OperationCancelled as exc:
            raise JobCancelled("Synthesis prefix setup cancelled.") from exc
        except (OSError, RuntimeError) as exc:
            if isinstance(exc, (JobError, JobCancelled)):
                raise
            raise JobError("synthesis_prefix_setup_failed", "Could not prepare the Synthesis prefix.", {"reason": str(exc)}) from exc
    return run
