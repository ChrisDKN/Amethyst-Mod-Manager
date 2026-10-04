"""Run Wine tools through the owning Bottles installation."""

from __future__ import annotations

import hashlib
import os
import shlex
from pathlib import Path


_HELPER = r'''
import os
import runpy
import shlex
import shutil
import sys

status, bottle, mode, cwd, host_data, *args = sys.argv[1:]
environment = {}
while args and args[0] == "--env":
    _, key, value, *args = args
    environment[key] = value
if args and args[0] == "--":
    args.pop(0)
if os.environ.get("FLATPAK_ID") == "com.usebottles.bottles":
    def mapped(value):
        return "/var/data" + value[len(host_data):] if value == host_data or value.startswith(host_data + "/") else value
    cwd = mapped(cwd)
    args = [mapped(value) for value in args]
for key in ("WINE", "WINE64", "WINESERVER", "WINELOADER", "WINEDLLPATH", "PYTHONPATH", "PYTHONHOME"):
    os.environ.pop(key, None)
cli = shutil.which("bottles-cli") or os.path.expanduser("~/.local/bin/bottles-cli")
namespace = runpy.run_path(cli, run_name="amethyst_bottles")
manager = namespace["Manager"](g_settings=namespace["CLI"].settings, is_cli=True)
manager.checks()
config = manager.local_bottles[bottle]
wine = namespace["WineCommand"](
    config=config, command=shlex.join(args), environment=environment,
    cwd=cwd, communicate=True, minimal=mode.startswith("shell-") or mode == "wineserver")
mode = mode.removeprefix("shell-")
if mode == "wineserver":
    runner = shlex.split(wine.runner)[0]
    command = shlex.join([os.path.join(os.path.dirname(runner), "wineserver"), *args])
    wine.command = wine.get_cmd(command, return_steam_cmd=True)
wine.command += '; amm_status=$?; printf "%s" "$amm_status" > ' + shlex.quote(status) + '; exit "$amm_status"'
result = wine.run()
if isinstance(result.data, str):
    sys.stdout.write(result.data)
if not result.ok:
    sys.stderr.write(str(result.message) + "\n")
'''

_SHELL_QUOTE = r'''amm_quote() { printf '%s\n' "$1" | sed "s/'/'\\\\''/g; 1s/^/'/; \$s/\$/'/"; }
'''


def _runtime_path(root, path: Path) -> Path:
    if root.is_flatpak and path.is_relative_to(root.data_dir.parent):
        return Path("/var/data") / path.relative_to(root.data_dir.parent)
    return path


def _forwarded_names(env):
    from Utils.flatpak.env import flatpak_forward_env_args
    names = {arg.split("=", 2)[1] for arg in flatpak_forward_env_args(env)}
    names.update(("WINEDEBUG", "WINEDLLOVERRIDES", "WINEESYNC", "WINEFSYNC"))
    names.difference_update((
        "PATH", "LD_LIBRARY_PATH", "LD_PRELOAD", "PYTHONPATH", "PYTHONHOME",
        "WINE", "WINE64", "WINESERVER", "WINELOADER", "WINEDLLPATH",
        "WINEPREFIX", "WINEARCH", "WINE_BIN", "WINESERVER_BIN", "TMPDIR",
        "_AMM_BOTTLES_PREFIX", "_AMM_BOTTLES_CWD", "PROTONPATH", "GAMEID", "SteamAppId",
        "SteamGameId", "SteamOverlayGameId",
    ))
    return sorted(name for name in names if name.isascii() and name.isidentifier()
                  and not name.startswith(("STEAM_COMPAT_", "XDG_")))


def bottles_tool_runner(prefix, env):
    from Utils.launchers.bottles import (
        BOTTLES_CPAK_ORIGIN, BOTTLES_FLATPAK_ID,
        find_bottles_prefix, bottles_cli_commands,
    )
    from Utils.flatpak.sandbox import _host_cmd
    from Utils.wine.winetricks import _write_wrapper

    info = find_bottles_prefix(prefix)
    if info is None:
        raise RuntimeError("the Bottles installation owning this prefix was not found")
    root, prefix, config = info
    directory = prefix / ".amethyst-tools"
    directory.mkdir(mode=0o700, exist_ok=True)
    helper = directory / "runtime.py"
    _write_wrapper(helper, _HELPER)
    runtime_dir = _runtime_path(root, directory)
    package = root.package
    if package == "flatpak":
        commands = [_host_cmd(["flatpak", "run", "--command=sh", BOTTLES_FLATPAK_ID, "-c"])]
    elif package == "cpak":
        commands = [_host_cmd(["cpak", "run", BOTTLES_CPAK_ORIGIN, "@sh", "-c"])]
    elif package == "appimage":
        commands = bottles_cli_commands(root, ["shell", "-b", config["Name"], "-i"])
    else:
        commands = [_host_cmd(["sh", "-c"])]
    names = _forwarded_names(env)
    contents = {}
    for mode in ("wine", "wine64", "wineserver"):
        # CLI shell does not return the child's status; keep it in the shared prefix.
        setup = (
            "#!/bin/sh\n"
            f"amm_status=$(mktemp {shlex.quote(str(directory / 'status-XXXXXXXX'))}) || exit 1\n"
            "trap 'rm -f -- \"$amm_status\"' EXIT\n"
            "trap 'exit 130' INT\ntrap 'exit 143' TERM HUP\n"
            + _SHELL_QUOTE +
            f"amm_inner_status={shlex.quote(str(runtime_dir))}/\"${{amm_status##*/}}\"\n"
        )
        prelude = "--version >/dev/null; exec python3 " if package == "appimage" else "exec python3 "
        helper_mode = "shell-" + mode if package == "appimage" else mode
        setup += (
            f"amm_input={shlex.quote(prelude)}$(amm_quote {shlex.quote(str(runtime_dir / 'runtime.py'))})\n"
            f"for amm_arg in \"$amm_inner_status\" {shlex.quote(config['Name'])} {shlex.quote(helper_mode)} \"${{_AMM_BOTTLES_CWD:-$PWD}}\" {shlex.quote(str(root.data_dir.parent))}; do\n"
            '  amm_input="$amm_input $(amm_quote "$amm_arg")"\n'
            "done\n"
        )
        for name in names:
            setup += (
                f'if [ "${{{name}+set}}" = set ]; then\n'
                f'  amm_input="$amm_input --env {name} $(amm_quote "${{{name}}}")"\n'
                "fi\n"
            )
        setup += (
            'amm_input="$amm_input --"\n'
            "for amm_arg do\n"
            '  amm_input="$amm_input $(amm_quote "$amm_arg")"\n'
            "done\n"
        )
        for command in commands:
            setup += shlex.join(command) + ' "$amm_input"\n'
            setup += (
                'if [ -s "$amm_status" ]; then\n'
                '  amm_result=$(cat "$amm_status")\n'
                '  case "$amm_result" in ""|*[!0-9]*) exit 1;; esac\n'
                '  exit "$amm_result"\n'
                "fi\n"
            )
        setup += "exit 1\n"
        contents[mode] = setup
    digest = hashlib.sha256(repr(contents).encode()).hexdigest()[:16]
    wrappers = directory / digest
    wrappers.mkdir(mode=0o700, exist_ok=True)
    for name, content in contents.items():
        _write_wrapper(wrappers / name, content)
    env["WINEPREFIX"] = str(prefix)
    env["_AMM_BOTTLES_PREFIX"] = str(prefix)
    return wrappers / "wine"


def bottles_tool_command(prefix, args, env, *, cwd=None):
    from Utils.launchers.bottles import find_bottles_prefix
    from Utils.launchers.bottles_sandbox import ensure_bottles_paths

    info = find_bottles_prefix(prefix)
    if info is None:
        raise RuntimeError("the Bottles installation owning this prefix was not found")
    paths = [Path(prefix)]
    if cwd:
        paths.append(Path(cwd))
        env["_AMM_BOTTLES_CWD"] = str(cwd)
    else:
        env.pop("_AMM_BOTTLES_CWD", None)
    for arg in args:
        path = Path(arg)
        try:
            if path.is_absolute() and path.exists():
                paths.append(path if path.is_dir() else path.parent)
        except OSError:
            continue
    ensure_bottles_paths(info[0], paths)
    runner = bottles_tool_runner(prefix, env)
    return [str(runner), *args]


def bottles_winetricks_command(prefix, args, env, log):
    from Utils.launchers.bottles import find_bottles_prefix
    from Utils.launchers.bottles_sandbox import ensure_bottles_paths
    from Utils.wine import protontricks

    if not protontricks.winetricks_installed() and not protontricks.install_winetricks(log):
        return None, None
    if not protontricks.cabextract_installed() and not protontricks.install_cabextract(log):
        return None, None
    info = find_bottles_prefix(prefix)
    if info is None:
        raise RuntimeError("the Bottles installation owning this prefix was not found")
    tools = protontricks._get_tools_dir()
    cache = Path(env.get("W_CACHE", str(Path(env.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "winetricks")))
    temporary = tools / "winetricks-tmp"
    for path in (cache, temporary):
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    ensure_bottles_paths(info[0], [prefix, tools, cache], log)
    env["TMPDIR"] = str(temporary)
    wine = bottles_tool_runner(prefix, env)
    env.update(WINE=str(wine), WINE64=str(wine.parent / "wine64"),
               WINESERVER=str(wine.parent / "wineserver"), WINE_BIN=str(wine),
               WINESERVER_BIN=str(wine.parent / "wineserver"))
    env["PATH"] = f"{wine.parent}{os.pathsep}{tools}{os.pathsep}{env.get('PATH', '')}"
    log("Winetricks: using the bottle's runner inside Bottles.")
    return [str(protontricks._bundled_winetricks()), *args], env
