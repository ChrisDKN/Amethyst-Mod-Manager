from __future__ import annotations

import time
from pathlib import Path

from .diagnostics import emit, emit_exception
from .paths import WabbajackError
from .post_install_rules import (Adjustment as Adjustment, DLL_OVERRIDES, compatibility_adjustments,
                                 runtime_dependencies, stock_file_adjustments)


def uses_native_runtime(game):
    mode = getattr(game, "get_runtime_mode", lambda: "")()
    if mode in {"native", "proton"}:
        return mode == "native"
    from Utils.executables.launch import resolve_game_exe
    executable = resolve_game_exe(game) or getattr(game, "exe_name", "")
    return bool(executable) and Path(executable).suffix.casefold() not in {".exe", ".bat", ".cmd", ".com", ".msi"}


def windows_path(game, path):
    path = Path(path).resolve()
    prefix = game.get_prefix_path() if hasattr(game, "get_prefix_path") else None
    devices = Path(prefix) / "dosdevices" if prefix else None
    if devices and devices.is_dir():
        links = sorted(devices.iterdir(), key=lambda p: p.name != "z:")
        for link in links:
            if len(link.name) == 2 and link.name[0].isalpha() and link.name[1] == ":" and link.is_symlink():
                base = link.resolve()
                if path.is_relative_to(base):
                    rel = path.relative_to(base).as_posix()
                    return link.name.upper() + "\\" + ("" if rel == "." else rel.replace("/", "\\"))
        raise WabbajackError(f"The selected prefix has no Windows drive mapping for {path}. Configure its drive mappings before installing.")
    return "Z:" + str(path).replace("/", "\\")


def host_path(game, value):
    value = value.replace("\\", "/")
    if len(value) > 2 and value[1] == ":":
        prefix = game.get_prefix_path() if hasattr(game, "get_prefix_path") else None
        link = Path(prefix) / "dosdevices" / value[:2].lower() if prefix else None
        if link and link.is_symlink():
            return link.resolve() / value[2:].lstrip("/")
        return Path(value[2:]) if value[:2].upper() == "Z:" else None
    return Path(value)


def uses_stock_game(game):
    from Utils.profiles.state import read_profile_settings
    profile = getattr(game, "_active_profile_dir", None)
    if not profile:
        return False
    settings = read_profile_settings(Path(profile))
    directory, path = settings.get("wabbajack_directory"), settings.get("game_path")
    current = game.get_game_path()
    return bool(directory and path and current and settings.get("wabbajack_install_id")
                and Path(path).resolve().is_relative_to((Path(directory) / "root").resolve())
                and Path(current).resolve() == Path(path).resolve())


def adjustments(package, game, profiles=None, *, configuration=None):
    from Utils.wine.health import COMPONENT_SPECS, detect_component
    result = []
    paths = [d.path.casefold() for d in package.directives]
    from .requirements import profile_configuration
    if configuration is None:
        configuration = profile_configuration(package, profiles)
    enabled = {mod.casefold() for profile in configuration.values() for mod in profile.mods}
    active_paths = [path for path in paths if not path.startswith("mods/") or path.split("/")[1] in enabled]
    active_names = {path.rsplit("/", 1)[-1] for path in active_paths}
    native = uses_native_runtime(game)
    windows = not native and any(p.endswith((".exe", ".dll")) for p in paths)
    prefix = game.get_prefix_path() if hasattr(game, "get_prefix_path") else None
    if windows:
        result.extend(stock_file_adjustments(package, game))
        for token, reason in runtime_dependencies(package, game, paths, active_names):
            spec = COMPONENT_SPECS.get(token)
            if spec and (not prefix or detect_component(token, Path(prefix)) is not True):
                result.append(Adjustment("runtime:" + token, "Install " + spec.label + " in the game prefix" + reason, True))
    result.extend(compatibility_adjustments(paths, native))
    return result


def ensure_runtime(request, stop, log):
    from Utils.wine import proton
    from Utils.wine.health import detect_component
    from Utils.wine.protontricks import WINETRICKS_VERB_DEPS, install_winetricks_verb
    required = [item for item in adjustments(request.package, request.game, request.profiles)
                if item.required]
    emit(log, "runtime.setup.started", required=[item.id for item in required],
         accepted=request.fixes, prefix=request.game.get_prefix_path()
         if hasattr(request.game, "get_prefix_path") else None)
    for item in required:
        if item.id not in request.fixes:
            emit(log, "runtime.setup.not_accepted", adjustment=item.id, label=item.label)
            raise WabbajackError(f"Required runtime setup has not been accepted: {item.label}")
        if stop.is_set():
            raise InterruptedError("Runtime setup stopped")
        token = item.id.removeprefix("runtime:")
        log(item.label)
        prefix = request.game.get_prefix_path()
        try:
            before = detect_component(token, Path(prefix)) if prefix else None
        except Exception as exc:
            before = f"<{type(exc).__name__}: {exc}>"
            emit_exception(log, "runtime.component.precheck_failed", exc,
                           component=token, prefix=prefix)
        started = time.monotonic()
        emit(log, "runtime.component.started", component=token, label=item.label,
             prefix=prefix, detected_before=before)
        installer = getattr(proton, "install_" + token, None)
        try:
            if token == "dotnet48":
                method = "proton.install_dotnet48"
                ok = proton.install_dotnet48(request.game, log_fn=log)
            elif token.startswith("dotnet"):
                method = "proton.install_dotnet"
                ok = proton.install_dotnet(request.game, token.removeprefix("dotnet"), log_fn=log)
            elif token in WINETRICKS_VERB_DEPS:
                method = "protontricks"
                ok = install_winetricks_verb(request.game, token, log_fn=log,
                                             strict_prefix=True)
            elif installer:
                method = installer.__name__
                ok = installer(request.game, log_fn=log)
            else:
                raise WabbajackError(f"No runtime installer is available for {token}")
            prefix = request.game.get_prefix_path()
            after = detect_component(token, Path(prefix)) if prefix else None
            emit(log, "runtime.component.completed", component=token, method=method,
                 installer_result=ok, prefix=prefix, detected_after=after,
                 elapsed_seconds=round(time.monotonic() - started, 3))
        except BaseException as exc:
            emit_exception(log, "runtime.component.failed", exc, component=token,
                           prefix=prefix,
                           elapsed_seconds=round(time.monotonic() - started, 3))
            raise
        if not ok or not prefix or after is not True:
            raise WabbajackError(f"Runtime setup failed verification: {item.label}. Use Proton Tools to repair it, then resume.")
    emit(log, "runtime.setup.completed", components=len(required))


def launch_environment(game, env):
    if uses_native_runtime(game):
        return
    from Utils.profiles.state import read_profile_state
    profile = getattr(game, "_active_profile_dir", None)
    if not profile:
        return
    state = read_profile_state(Path(profile))
    accepted = state.get("profile_settings", {}).get("wabbajack_adjustments", [])
    names = [item[4:] for item in accepted if item in {"dll:" + name for name in DLL_OVERRIDES}]
    existing = env.get("WINEDLLOVERRIDES", "")
    configured = {name.strip().lstrip("*").casefold() for clause in existing.split(";")
                  for name in clause.partition("=")[0].split(",") if name.strip()}
    additions = [name + "=n,b" for name in names if name not in configured and "" not in configured]
    if additions:
        env["WINEDLLOVERRIDES"] = ";".join(filter(None, [existing, *additions]))


def working_directory(game, exe):
    from Utils.profiles.state import read_profile_state
    exe = Path(exe)
    profile = getattr(game, "_active_profile_dir", None)
    if profile:
        state = read_profile_state(Path(profile))
        directories = state.get("wabbajack_working_directories", {})
        saved = directories.get(str(exe))
        if saved is None:
            matching_exes = {path for path in state.get("custom_exes", [])
                             if Path(path).name.casefold() == exe.name.casefold()}
            if matching_exes == {str(exe)}:
                saved = directories.get(exe.name)
        if saved and Path(saved).is_dir():
            return Path(saved)
    return exe.parent


def authored_executable(game, exe):
    from Utils.profiles.state import read_profile_state
    profile = getattr(game, "_active_profile_dir", None)
    if not profile:
        return False
    state = read_profile_state(Path(profile))
    directory = state.get("profile_settings", {}).get("wabbajack_directory")
    return bool(directory and str(exe) in state.get("custom_exes", [])
                and Path(exe).resolve().is_relative_to((Path(directory) / "root").resolve()))


def output_directory(game, executable):
    from Utils.profiles.state import read_profile_state
    from .paths import relative_path, within
    profile = getattr(game, "_active_profile_dir", None)
    if not profile:
        return None
    state = read_profile_state(Path(profile))
    if not state.get("profile_settings", {}).get("wabbajack_install_id"):
        return None
    names = {str(key).casefold(): value for key, value in state.get("wabbajack_output_mods", {}).items()}
    name = names.get(Path(executable).name.casefold())
    if not name:
        return None
    if "/" in relative_path(name):
        raise WabbajackError("Invalid authored tool output mod")
    return within(Path(game.get_effective_mod_staging_path()), name)


def configure_tool_output(game, exe, log):
    output = output_directory(game, exe)
    if output is None:
        return
    output.mkdir(parents=True, exist_ok=True)
    if "pandora" in exe.name.casefold():
        from Utils.executables.arguments import _bootstrap_pandora_settings
        _bootstrap_pandora_settings(getattr(game, "game_id", None), game.get_game_path(),
                                   game.get_effective_mod_staging_path(), game.get_prefix_path(),
                                   log, exe_path=exe, output_mod=output)
    elif exe.name.casefold() == "pgpatcher.exe":
        import json
        from Utils.atomic_write import write_atomic_text
        settings = exe.parent / "cfg/settings.json"
        content = json.loads(settings.read_text()) if settings.is_file() else {}
        content.setdefault("params", {}).setdefault("output", {})["dir"] = windows_path(game, output)
        write_atomic_text(settings, json.dumps(content, indent=2))
    else:
        log(f"Authored output mod for {exe.name}: {output}. Select this output directory in the tool if it is not already configured.")
