import os
from pathlib import Path
import re
import shutil
import subprocess


def wine_binary(value):
    if not value:
        return None
    path = Path(value).expanduser()
    if path.name.split("-", 1)[0] in {"wine", "wine64"} and path.is_file() and os.access(path, os.X_OK):
        return path.resolve()
    root = path.parent if path.is_file() else path
    for folder in ("files/bin", "dist/bin", "bin"):
        for name in ("wine64", "wine"):
            candidate = root / folder / name
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate.resolve()
    return None


def _system_wine():
    from .runtime import _host_launcher, _inside_flatpak
    if not _inside_flatpak():
        return [shutil.which("wine64"), shutil.which("wine")]
    result = subprocess.run([*_host_launcher(), "/bin/sh", "-c",
        'for name in wine64 wine; do path=$(command -v "$name") || continue; readlink -f -- "$path"; done'],
        capture_output=True, text=True, timeout=10)
    if result.returncode:
        raise RuntimeError("Could not discover host Wine: " + result.stderr.strip()[-1000:])
    return [Path("/run/host") / path.lstrip("/") if path.startswith("/usr/") else Path(path)
            for path in result.stdout.splitlines() if path.startswith("/")]


def _lutris_runner(game):
    from Utils.launchers import lutris
    from Utils.launchers.steam import list_installed_proton
    prefix = game.get_prefix_path()
    hit = lutris._game_for_prefix(prefix) if prefix else None
    if hit is None:
        return None
    root, _row, config = hit
    version = lutris._wine_version_for_game(root, config)
    if not version or version.lower() == "ge-proton":
        return None
    if version == "custom":
        settings_path = root.config_dir / "runners" / "wine.yml"
        settings = lutris.parse_lutris_yaml(settings_path.read_text(encoding="utf-8")) if settings_path.is_file() else {}
        custom = config.get("wine", {}).get("custom_wine_path") or settings.get("wine", {}).get("custom_wine_path")
        candidates = [custom]
    elif version == "system":
        candidates = _system_wine()
    else:
        names = {version.casefold(), (version + "-x86_64").casefold(), (version + "-i386").casefold()}
        candidates = [root.data_dir / "runners" / "wine" / name
                      for name in (version, version + "-x86_64", version + "-i386")]
        candidates += [path for path in list_installed_proton() if path.parent.name.casefold() in names]
        if Path(version).expanduser().is_absolute():
            candidates.insert(0, version)
    for candidate in candidates:
        runner = wine_binary(candidate)
        if runner is not None:
            return runner
    raise RuntimeError(f"The game's configured Lutris runner '{version}' is unavailable. "
                       "Install it or choose a Wine / Proton override in the Legacy Scripted Installers settings.")


def select_runner(game, override, log):
    if override:
        runner = wine_binary(override)
        if runner is None:
            raise RuntimeError("The Legacy Scripted Installers Wine / Proton override is unavailable. "
                               "Choose a Proton build, proton script, or Wine executable, or clear it to use the game settings.")
        log(f"Using Wine / Proton override: {runner}")
        return runner, os.environ.copy()
    from amethyst_plugins.launchers import selected_entry
    from Utils.wine.proton import resolve_proton_env
    if selected_entry(game, log=log) is None:
        runner = _lutris_runner(game)
        if runner is not None:
            log(f"Using the game's configured Lutris runner: {runner}")
            return runner, os.environ.copy()
    selected, environment = resolve_proton_env(game, log, allow_fallback=False, read_only=True)
    if selected is not None:
        runner = wine_binary(selected)
        if runner is None:
            raise RuntimeError(f"The game's runner cannot be used by legacy installers: {selected}. "
                               "Choose a Wine / Proton override in the Legacy Scripted Installers settings.")
        log(f"Using the game's runner: {runner}")
        return runner, environment or os.environ.copy()
    from Utils.launchers.steam import list_installed_proton
    from Utils.launchers.lutris import find_lutris_roots
    candidates = _system_wine()
    for root in find_lutris_roots():
        folder = root.data_dir / "runners" / "wine"
        try:
            candidates.extend(sorted(folder.iterdir(),
                key=lambda path: tuple(int(number) for number in re.findall(r"\d+", path.name)), reverse=True))
        except OSError:
            continue
    candidates.extend(list_installed_proton())
    for candidate in candidates:
        runner = wine_binary(candidate)
        if runner is not None:
            log(f"No game runner found; using installed Wine / Proton: {runner}")
            return runner, os.environ.copy()
    raise RuntimeError("Install Wine or Proton, or choose a Wine / Proton override in the Legacy Scripted Installers settings.")
