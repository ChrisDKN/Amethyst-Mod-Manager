from __future__ import annotations

from pathlib import Path

from .tool_run import ToolRunError as XEditRunError, ToolRunCancelled as XEditRunCancelled


def executable(game, choice, use_64bit=False):
    from Utils.bethesda.xedit_install import installation_target

    relative = choice["optional_64bit_exe"] if use_64bit else choice["exe_name"]
    if not relative:
        raise XEditRunError("xedit_64bit_unavailable", "This build has no separate optional 64-bit executable.")
    path = installation_target(game, choice) / relative
    parent = path.parent
    anchor = installation_target(game, choice).parent.parent
    while parent != anchor:
        if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
            raise XEditRunError("unsafe_tool_destination", "An xEdit application directory is a link or file.")
        parent = parent.parent
    if path.is_symlink() or not path.is_file():
        raise XEditRunError("xedit_executable_missing", "Install the selected xEdit executable first.",
                            {"path": str(path)})
    return path


def saved_options(game, choice, use_64bit=None):
    from Utils.executables.launch import (
        load_prefix_mode, load_proton_override, load_tool_launch_args,
        load_tool_launch_env, load_tool_use_64bit, load_winetricks_style,
    )

    if use_64bit is None:
        use_64bit = bool(choice["optional_64bit_exe"] and load_tool_use_64bit(game, choice["exe_name"]))
    exe = executable(game, choice, use_64bit)
    return {"use_64bit": use_64bit, "proton_name": load_proton_override(game, exe.name) or "",
            "prefix_mode": load_prefix_mode(game, exe.name), "launch_args": load_tool_launch_args(exe),
            "launch_env": load_tool_launch_env(exe), "winetricks_style": load_winetricks_style(game, exe.name)}


def preview_prefix(game, exe, proton_name, prefix_mode):
    from Utils.executables.launch import PREFIX_MODE_GAME, PREFIX_MODE_SHARED, shared_prefix_dir
    from Utils.wine.registry import normalize_pfx

    if prefix_mode == PREFIX_MODE_GAME:
        configured = game.get_prefix_path()
        if configured is None or not Path(configured).is_dir():
            raise XEditRunError("wine_prefix_unavailable", "The selected game prefix is unavailable.")
        from Utils.executables.launch import get_game_prefix_env
        resolved = get_game_prefix_env(game, allow_runner_fallback=True, read_only=True)
        if resolved is None:
            raise XEditRunError("xedit_runner_unavailable", "The game's prefix has no available Wine/Proton runner.")
        script, compat, _env = resolved
        prefix = normalize_pfx(Path(configured))
    else:
        from Utils.launchers.steam import find_any_installed_proton
        script = find_any_installed_proton(proton_name)
        if script is None:
            raise XEditRunError("xedit_runner_unavailable", "No installed Proton runner is available.")
        compat = (shared_prefix_dir(script.parent.name) if prefix_mode == PREFIX_MODE_SHARED
                  else exe.parent / f"prefix_{script.parent.name}")
        prefix = compat / "pfx"
    if script is None or not Path(script).is_file():
        raise XEditRunError("xedit_runner_unavailable", "The selected prefix has no available Wine/Proton runner.")
    return {"script": str(script), "compat_data": str(compat), "prefix_path": str(prefix)}


def arguments(game, choice, exe, compat_data, mode, launch_args=""):
    from Utils.executables.launch import parse_launch_args
    from Utils.wine.paths import to_wine_path
    from Utils.wine.registry import normalize_pfx

    pfx = normalize_pfx(compat_data)
    args = [f'-d:{to_wine_path(game.get_game_path() / "Data", pfx)}']
    if mode == "qac_all":
        args.extend(("-autoload", "-autoexit"))
    optional = choice["optional_64bit_exe"]
    use_64bit = bool(optional and exe.name == Path(optional).name)
    if use_64bit:
        args.append(f'-s:{to_wine_path(exe.parent.parent / "Edit Scripts", pfx)}\\')
    if choice["qac"] and (choice["discord"] or use_64bit):
        args.insert(0, "-quickautoclean")
    if choice["discord"]:
        args.insert(0, f'-{choice["discord_mode"]}')
    args.extend(parse_launch_args(launch_args))
    return args


def _check_prefix(prefix, launch_env):
    from Utils.executables.launch import parse_env_overrides
    from Utils.wine.repair import prefix_usage

    expected = Path(prefix["prefix_path"]).resolve()
    overrides = parse_env_overrides(launch_env)
    for key, value in (("WINEPREFIX", prefix["prefix_path"]),
                       ("STEAM_COMPAT_DATA_PATH", prefix["compat_data"])):
        if key in overrides and Path(overrides[key]).expanduser().resolve() != Path(value).resolve():
            raise XEditRunError("xedit_prefix_override", "Choose the prefix through prefix_mode instead of redirecting it in launch_env.")
    pids = prefix_usage(expected)
    if pids is None:
        raise XEditRunError("wine_prefix_scan_unavailable", "Could not check whether the selected prefix is in use.")
    if pids:
        raise XEditRunError("wine_prefix_in_use", "Close other processes using the selected prefix first.",
                            {"pids": sorted(pids)})


from .tool_run import run_bethesda_tool as run_xedit
