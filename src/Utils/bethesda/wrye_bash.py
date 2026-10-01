from __future__ import annotations

import hashlib
from pathlib import Path

from .tool_run import ToolRunError


WIZARD_CLASS = "wizards.wrye_bash.WryeBashWizard"
GITHUB_API = "https://api.github.com/repos/wrye-bash/wrye-bash/releases/latest"
EXE_NAME = "Wrye Bash.exe"
APP_DIR = "Wrye Bash"


def options():
    return {"label": APP_DIR, "app_dir": APP_DIR, "exe_name": EXE_NAME,
            "optional_64bit_exe": None, "archive_keywords": ["standalone", "executable"],
            "source_tool_id": "wrye_bash", "source": {"kind": "github_release", "url": GITHUB_API,
            "manual_url": "https://github.com/wrye-bash/wrye-bash/releases/latest"}}


def executable(game, choice):
    from .xedit_run import executable as find_executable

    try:
        return find_executable(game, choice)
    except ToolRunError as exc:
        raise ToolRunError(exc.name.replace("xedit_", "wrye_bash_"), str(exc).replace("xEdit", "Wrye Bash"), exc.details) from exc


def saved_options(game, choice):
    from .xedit_run import saved_options as read_options

    try:
        result = read_options(game, choice, False)
    except ToolRunError as exc:
        raise ToolRunError(exc.name.replace("xedit_", "wrye_bash_"), str(exc).replace("xEdit", "Wrye Bash"), exc.details) from exc
    result.pop("use_64bit")
    return result


def game_link(game, prefix):
    root = Path(game.get_game_path()).resolve()
    name = "game_" + hashlib.sha256(str(root).encode()).hexdigest()[:16]
    parent = Path(prefix) / "drive_c" / "wb_games"
    for path in (parent, parent.parent):
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise ToolRunError("unsafe_wrye_bash_game_link", "The Wrye Bash game-link directory is a link or file.")
    link = parent / name
    if link.is_symlink():
        if link.resolve() != root:
            raise ToolRunError("unsafe_wrye_bash_game_link", "The existing Wrye Bash game link targets another installation.")
    elif link.exists():
        raise ToolRunError("unsafe_wrye_bash_game_link", "A file or directory occupies the Wrye Bash game link.")
    if parent.is_dir() and any(path.name.casefold() == name.casefold() and path.name != name for path in parent.iterdir()):
        raise ToolRunError("unsafe_wrye_bash_game_link", "A Wrye Bash game link differs only in letter case.")
    return link, root


def arguments(game, prefix, launch_args=""):
    from Utils.executables.launch import parse_launch_args

    link, _root = game_link(game, prefix)
    extra = parse_launch_args(launch_args)
    for arg in extra:
        key = arg.casefold().split("=", 1)[0]
        if key.startswith("-o") or (key.startswith("--") and "--oblivionpath".startswith(key)):
            raise ToolRunError("wrye_bash_argument_override", "The manager supplies Wrye Bash's game path.")
    return ["-o", "C:\\wb_games\\" + link.name, *extra]


def run_wrye_bash(game, choice, exe, settings, *, prefix=None, **kwargs):
    from .tool_run import run_bethesda_tool
    from .xedit_run import preview_prefix
    from .xedit import capture_wrye_bash_patch, wrye_bash_patch_digests

    prefix = prefix or preview_prefix(game, exe, settings["proton_name"], settings["prefix_mode"])
    log = kwargs.get("log_fn", lambda _m: None)
    baseline = {}

    def prepare_arguments(_compat):
        nonlocal baseline
        args = arguments(game, prefix["prefix_path"], settings["launch_args"])
        link, root = game_link(game, prefix["prefix_path"])
        link.parent.mkdir(parents=True, exist_ok=True)
        if not link.is_symlink():
            link.symlink_to(root, target_is_directory=True)
        baseline = wrye_bash_patch_digests(game)
        return args

    def capture(session):
        nonlocal baseline
        if session is not None:
            return 0
        count = capture_wrye_bash_patch(game, baseline, log_fn=log)
        baseline = wrye_bash_patch_digests(game)
        return count

    return run_bethesda_tool(game, choice, exe, settings["proton_name"], settings["prefix_mode"],
                             family="wrye_bash", prefix=prefix, launch_args=settings["launch_args"],
                             launch_env=settings["launch_env"], winetricks_style=settings["winetricks_style"],
                             build_arguments=prepare_arguments, capture_changes=capture,
                             complete=lambda result: {"needs_deployment": bool(result.get("captured_plugins")
                                                       or result["saved_vfs_plugins"])}, **kwargs)
