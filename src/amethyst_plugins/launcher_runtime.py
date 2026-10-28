from __future__ import annotations

from pathlib import Path
import hashlib
import os
import shlex
from types import SimpleNamespace

from .launchers import ProviderError, assert_idle, matches_game_path, prefix_lock


def prefix_environment(entry, log=lambda message: None, *, read_only=False):
    from Utils.environment.xdg import host_env
    from Utils.wine.protontricks import strip_appimage_env
    runner = entry["runner"]
    if runner["kind"] == "native":
        raise ProviderError("Native applications do not have a Wine prefix.")
    script = Path(runner["path"])
    if script.is_dir() and runner["kind"] == "proton":
        script = script / "proton"
    if not script.is_file():
        raise ProviderError(f"The selected runner is unavailable: {script}")
    prefix = Path(entry["prefix_path"])
    compat = Path(entry["compat_data_path"] or prefix)
    env = strip_appimage_env(host_env())
    for key in list(env):
        if key.startswith("STEAM_COMPAT_") or key in {"SteamAppId", "SteamGameId", "GAMEID", "PROTONPATH", "WINEPREFIX"}:
            env.pop(key, None)
    env.update(WINEPREFIX=str(prefix), STEAM_COMPAT_DATA_PATH=str(compat),
               _AMM_PLUGIN_PREFIX="1", PROTON_DISABLE_XALIA="1", PROTON_USE_XALIA="0")
    if runner["kind"] == "proton":
        env.update(PROTONPATH=str(script.parent), GAMEID=entry.get("umu_id") or "umu-default")
        if not read_only:
            from Utils.launchers.umu import ensure_umu_run
            if ensure_umu_run(log, required=True) is None:
                raise ProviderError("UMU is unavailable. Install umu-launcher and try again.")
    return script, compat, env


def launch_entry(entry, *, game=None, executable=None, arguments=None, log=lambda message: None,
                 check_cancelled=lambda: None):
    from Utils.executables import launch as shared
    from Utils.processes import game as processes
    from Utils.environment.xdg import host_env
    from Utils.executables.mako import wrap_command
    exe = Path(executable or entry["executable"])
    check_cancelled()
    if game is not None and getattr(game, "vfs_launch_enabled", False):
        resolved = shared.resolve_deployed_exe(game, exe)
        if resolved is None:
            raise ProviderError(f"Application executable is unavailable in the deployed VFS: {exe}")
        exe = resolved
    elif not exe.is_file():
        raise ProviderError(f"Application executable is unavailable: {exe}")
    if game is not None:
        if not matches_game_path(game, entry["game_path"]):
            raise ProviderError("The launcher's game path changed. Restore and reconfigure this installation.")
        saved_prefix = game.get_prefix_path()
        if str(saved_prefix or "") != str(entry["prefix_path"] or ""):
            raise ProviderError("The launcher's prefix changed. Restore and reconfigure this installation.")
    else:
        from .launchers import linked_profiles
        if any(linked.get_deploy_active() for linked, _ in linked_profiles(
                entry["provider_id"], [entry["id"]], installation=entry)):
            raise ProviderError("This application has a deployed Amethyst profile. Launch it from Amethyst's Play control.")
    lock = prefix_lock(entry["prefix_path"] or entry["game_path"])
    lock.__enter__()
    session = processes.current()
    handed_off = False
    try:
        assert_idle(entry)
        if session is not None:
            session.begin_preparation()
        native = entry["runner"]["kind"] == "native"
        env = host_env() if native else prefix_environment(entry, log)[2]
        check_cancelled()
        if not native:
            Path(entry["prefix_path"]).mkdir(parents=True, exist_ok=True)
        args = shlex.split(entry["arguments"] if arguments is None else arguments)
        plain = entry["plain_wine"] and not native
        opts = entry["launch_options"]
        extra, _ = shared.parse_launch_options(opts, [])
        if not native and any(key in extra and extra[key] != env.get(key)
                              for key in ("WINEPREFIX", "PROTONPATH", "STEAM_COMPAT_DATA_PATH", "_AMM_PLUGIN_PREFIX")):
            raise ProviderError("Change the prefix or runner in its settings rather than overriding it in Launch Options.")
        env.update(extra)
        if native:
            command = [str(exe), *args]
        else:
            from Utils.launchers.steam import proton_run_command
            script = Path(entry["runner"]["path"])
            script = script / "proton" if script.is_dir() else script
            if plain and entry["runner"]["kind"] == "proton":
                if not (Path(entry["prefix_path"]) / "drive_c").is_dir():
                    raise ProviderError("Run this prefix with Proton once before enabling plain Wine.")
                wine = next((script.parent / d / "bin" / "wine" for d in ("files", "dist")
                             if (script.parent / d / "bin" / "wine").is_file()), None)
                if wine is None:
                    raise ProviderError("The selected Proton build has no plain Wine binary.")
                script = wine
                env["PATH"] = str(wine.parent) + os.pathsep + env.get("PATH", "")
                for key in list(env):
                    if key.startswith("STEAM_COMPAT_") or key in {"PROTONPATH", "GAMEID", "_AMM_PLUGIN_PREFIX"}:
                        env.pop(key, None)
            payload = ["msiexec", "/i", str(exe)] if exe.suffix.casefold() == ".msi" else [str(exe)]
            command = proton_run_command(script, "run", *payload, *args, env=env, host_cwd=exe.parent)
        if not plain:
            _, command = shared.parse_launch_options(opts, command)
        if not command:
            raise ProviderError("Launch options produced no command.")
        config_name = "launcher_" + hashlib.sha256(
            f"{entry['provider_id']}/{entry['id']}".encode()).hexdigest()[:24]
        target = game or SimpleNamespace(name=config_name)
        command = shared.apply_wayland_launch_setting(target, env, command,
            native=native, exe_path=exe, enabled=entry["wayland"], log_fn=log)
        shared.apply_lsfg_launch_setting(target, env, settings=entry["lsfg"], log_fn=log)
        shared.apply_mangohud_launch_setting(target, env, settings=entry["mangohud"], log_fn=log)
        command = shared.forward_manager_env_through_flatpak_spawn(command, env)
        command = wrap_command(command, env, log_fn=log)
        if game is not None:
            if getattr(game, "vfs_launch_enabled", False):
                from Utils.vfs import wrap_command as wrap_vfs_command
                command = wrap_vfs_command(game, command, env=env, log_fn=log, cwd=exe.parent)
            else:
                command = shared.wrap_tool_command(game, command, env, log_fn=log, cwd=exe.parent)
        if session is not None:
            if session.stopping or session.finished:
                raise ProviderError("Application launch was cancelled.")
            session.add_finish_callback(lambda completion: lock.__exit__(None, None, None))
            handed_off = True
        check_cancelled()
        log(f"Launching {entry['name']} with {entry['runner']['kind']} in {entry['prefix_path'] or 'native mode'}.")
        cwd = exe.parent
        if game is not None and getattr(game, "vfs_launch_enabled", False) and not cwd.is_dir():
            cwd = Path(game.get_game_path())
        shared.spawn_process_watched(command, env=env, cwd=cwd, label=entry["name"], log_fn=log)
    finally:
        if session is not None:
            session.end_preparation()
            session.finish_dispatch()
        if not handed_off:
            lock.__exit__(None, None, None)


def tools_target(entry, log=lambda message: None):
    from Utils.wine.proton import PrefixToolsTarget
    script, compat, env = prefix_environment(entry, log)
    if not (Path(entry["prefix_path"]) / "drive_c").is_dir():
        raise ProviderError("Launch an application or installer with Proton to initialize this prefix first.")
    return PrefixToolsTarget(entry["name"], Path(entry["prefix_path"]), script, env)
