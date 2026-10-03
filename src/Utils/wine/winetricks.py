"""Run bundled Winetricks with the selected Wine runner and runtime."""

from __future__ import annotations

import hashlib
import os
import shlex
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace


def _host_path(path: Path) -> Path:
    if str(path).startswith("/run/host/"):
        return Path(str(path)[len("/run/host"):])
    return path


def _runtime_path(path: Path) -> Path:
    path = _host_path(path)
    if path.parts[1:2] in (("usr",), ("bin",), ("lib",), ("lib64",), ("sbin",)):
        return Path("/run/host") / path.relative_to("/")
    return path


def _find_runtime(script: Path) -> Path | None:
    from Utils.launchers.steam import find_steam_runtime_entry_point, _require_tool_appid
    runtime = find_steam_runtime_entry_point(script)
    if runtime is not None:
        return runtime
    name = {"1391110": "steamrt2", "1628350": "steamrt3",
            "4183110": "steamrt4"}.get(_require_tool_appid(script))
    if name:
        roots = [Path.home() / ".local/share"]
        for key in ("UMU_FOLDERS_PATH", "HOST_XDG_DATA_HOME", "XDG_DATA_HOME"):
            if os.environ.get(key):
                roots.insert(0, Path(os.environ[key]))
        for root in roots:
            runtime = root / "umu" / name / "_v2-entry-point"
            if os.access(runtime, os.X_OK):
                return runtime
    return None


def _write_wrapper(path: Path, content: str) -> None:
    if path.is_file() and path.read_text(encoding="utf-8") == content:
        return
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
        os.chmod(temporary, 0o700)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def build_winetricks_command(prefix_path: Path, *args: str, game=None,
                            proton_script=None, env=None, log_fn=None):
    """Return ``(command, env)`` or ``(None, None)`` after logging a failure."""
    from Utils.app_log import safe_log
    from Utils.launchers import steam
    from Utils.wine import protontricks
    from Utils.wine.prefix import resolve_compat_data
    from Utils.wine.proton import resolve_proton_env

    log = safe_log(log_fn)
    prefix_path = Path(prefix_path)
    if not prefix_path.is_dir():
        log(f"Winetricks: prefix is unavailable: {prefix_path}")
        return None, None
    try:
        if proton_script is None:
            if game is None:
                compat = resolve_compat_data(prefix_path)
                steam_id = compat.name if compat.parent.name == "compatdata" else ""
                game = SimpleNamespace(get_prefix_path=lambda: prefix_path,
                                       steam_id=steam_id)
            proton_script, env = resolve_proton_env(game, log, allow_fallback=False)
        if proton_script is None or env is None:
            return None, None
        env = protontricks.strip_appimage_env(env.copy())
        script = Path(proton_script)
        classic_wine = script.name in ("wine", "wine64")
        runtime = None
        if classic_wine:
            wine = script
        else:
            wine = next((script.parent / sub / "wine" for sub in
                         ("files/bin", "dist/bin")
                         if (script.parent / sub / "wine").is_file()), None)
            if wine is None:
                raise RuntimeError(f"Wine is missing from {script.parent}")
            runtime = _find_runtime(script)
            if runtime is None and steam._require_tool_appid(script):
                raise RuntimeError(
                    f"the Steam Linux Runtime required by {script.parent.name} "
                    "is missing; launch the game through its launcher first")

        if not protontricks.winetricks_installed():
            if not protontricks.install_winetricks(log):
                return None, None
        if not protontricks.cabextract_installed():
            if not protontricks.install_cabextract(log):
                return None, None

        tools_dir = protontricks._get_tools_dir()
        from Utils.deployment.wine_dll import set_show_dot_files
        set_show_dot_files(prefix_path, log_fn=log)
        env["WINEPREFIX"] = str(prefix_path)
        if not classic_wine:
            env.pop("WINEARCH", None)
        shared_tmp = tools_dir / "winetricks-tmp"
        shared_tmp.mkdir(mode=0o700, exist_ok=True)
        env["TMPDIR"] = str(shared_tmp)
        if runtime:
            env["STEAM_COMPAT_TOOL_PATHS"] = (
                f"{_host_path(script.parent)}:{_host_path(runtime.parent)}")

        host = steam._in_flatpak_sandbox()
        steam_flatpak = (steam._proton_script_in_steam_flatpak(script)
                         and not steam._own_process_in_steam_flatpak())
        if host and not shutil.which("flatpak-spawn"):
            raise RuntimeError("flatpak-spawn is required to run this Wine runner")

        from Utils.flatpak.env import flatpak_forward_env_args
        forwarded = {arg.split("=", 2)[1] for arg in flatpak_forward_env_args(env)}
        forwarded.update(("WINEPREFIX", "WINEARCH", "WINEDLLOVERRIDES",
                          "WINEDEBUG", "WINEESYNC", "WINEFSYNC"))
        forwarded.difference_update(("PATH", "WINE", "WINE64", "WINESERVER",
                                     "WINELOADER", "WINEDLLPATH"))
        forward_args = " ".join(
            f'"--env={name}=${{{name}-}}"' for name in sorted(forwarded)
            if name.isascii() and name.isidentifier())
        outer = ""
        if host:
            outer = f"flatpak-spawn --host --directory=/ {forward_args} "
        if steam_flatpak:
            outer += ("flatpak run --filesystem=host --command=sh "
                      f"{forward_args} {steam._STEAM_FLATPAK_ID} -c ")

        # Keep Winetricks' GUI/cabextract in the app, and run Wine in its runtime.
        contents = {}
        for name in ("wine", "wine64", "wineserver"):
            binary = wine.parent / name
            if name in ("wine", "wine64") and not binary.is_file():
                binary = wine
            if not binary.is_file():
                raise RuntimeError(f"Wine helper is missing: {binary}")
            target = _runtime_path(binary) if runtime else _host_path(binary)
            setup = ('unset WINE WINE64 WINELOADER WINESERVER; '
                     '[ -n "${WINEARCH-}" ] || unset WINEARCH; ')
            if not classic_wine:
                dist = (_runtime_path(wine.parent.parent) if runtime
                        else _host_path(wine.parent.parent))
                libraries = shlex.quote(f"{dist / 'lib64'}:{dist / 'lib'}")
                dlls = shlex.quote(":".join(str(dist / sub) for sub in (
                    "lib64/wine", "lib/wine", "lib64/vkd3d", "lib/vkd3d")))
                setup += (
                    f"export LD_LIBRARY_PATH={libraries}"
                    ':"${LD_LIBRARY_PATH-}"; '
                    f"export WINEDLLPATH={dlls}; ")
            setup += 'exec "$@"'
            command = ["/bin/sh", "-c", setup, "amethyst-winetricks", str(target)]
            if runtime:
                command = [str(_host_path(runtime)), "--verb=run", "--", *command]
            launch = shlex.join(command) + ' "$@"'
            if steam_flatpak:
                launch = shlex.quote("exec " + launch) + ' amethyst-winetricks "$@"'
            contents[name] = "#!/bin/sh\nexec " + outer + launch + "\n"

        digest = hashlib.sha256(repr(contents).encode()).hexdigest()[:20]
        wrappers = tools_dir / "winetricks-runners" / digest
        wrappers.mkdir(mode=0o700, parents=True, exist_ok=True)
        for name, content in contents.items():
            _write_wrapper(wrappers / name, content)
        env.update(WINE=str(wrappers / "wine"), WINE64=str(wrappers / "wine64"),
                   WINESERVER=str(wrappers / "wineserver"), WINE_BIN=str(wine),
                   WINESERVER_BIN=str(wine.parent / "wineserver"))
        env["PATH"] = f"{wrappers}{os.pathsep}{tools_dir}{os.pathsep}{env.get('PATH', '')}"
        log(f"Winetricks: Wine runner: {wine}")
        if runtime:
            log(f"Winetricks: Steam Linux Runtime: {runtime.parent}")
        return [str(protontricks._bundled_winetricks()), *args], env
    except (OSError, RuntimeError) as exc:
        log(f"Winetricks: {exc}")
        return None, None
