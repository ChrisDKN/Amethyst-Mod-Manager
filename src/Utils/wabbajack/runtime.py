from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .paths import WabbajackError


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


@dataclass(frozen=True)
class Adjustment:
    id: str
    label: str
    required: bool = False


def adjustments(package, game):
    from Utils.wine.health import COMPONENT_SPECS, detect_component
    result = []
    paths = [d.path.casefold() for d in package.directives]
    windows = any(p.endswith((".exe", ".dll")) for p in paths)
    prefix = game.get_prefix_path() if hasattr(game, "get_prefix_path") else None
    if windows:
        for token in getattr(game, "auto_install_deps", []) or []:
            spec = COMPONENT_SPECS.get(token)
            if spec and (not prefix or detect_component(token, Path(prefix)) is not True):
                result.append(Adjustment("runtime:" + token, "Install " + spec.label + " in the game prefix", True))
    for name in ("dinput8", "version", "winhttp", "winmm"):
        if any(Path(p).name == name + ".dll" and ("/root/" in p or len(Path(p).parts) <= 2) for p in paths):
            result.append(Adjustment("dll:" + name, f"Load the provided {name}.dll before Wine's built-in DLL"))
    if any("enbseries" in p or "enblocal.ini" in p for p in paths):
        result.append(Adjustment("enb-warning", "Acknowledge ENB requires manual Linux compatibility review"))
    if any("net script framework" in p or "netscriptframework" in p for p in paths):
        result.append(Adjustment("framework-warning", "Acknowledge .NET Script Framework may require additional Wine configuration"))
    return result


def ensure_runtime(request, stop, log):
    from Utils.wine import proton
    from Utils.wine.health import detect_component
    from Utils.wine.protontricks import WINETRICKS_VERB_DEPS, install_winetricks_verb
    for item in adjustments(request.package, request.game):
        if not item.required:
            continue
        if item.id not in request.fixes:
            raise WabbajackError(f"Required runtime setup has not been accepted: {item.label}")
        if stop.is_set():
            raise InterruptedError("Runtime setup stopped")
        token = item.id.removeprefix("runtime:")
        log(item.label)
        installer = getattr(proton, "install_" + token, None)
        if token.startswith("dotnet"):
            ok = proton.install_dotnet(request.game, token.removeprefix("dotnet"), log_fn=log)
        elif token in WINETRICKS_VERB_DEPS:
            ok = install_winetricks_verb(request.game, token, log_fn=log)
        elif installer:
            ok = installer(request.game, log_fn=log)
        else:
            raise WabbajackError(f"No runtime installer is available for {token}")
        prefix = request.game.get_prefix_path()
        if not ok or not prefix or detect_component(token, Path(prefix)) is not True:
            raise WabbajackError(f"Runtime setup failed verification: {item.label}. Use Proton Tools to repair it, then resume.")


def launch_environment(game, env):
    from Utils.profiles.state import read_profile_state
    profile = getattr(game, "_active_profile_dir", None)
    if not profile:
        return
    state = read_profile_state(Path(profile))
    accepted = state.get("profile_settings", {}).get("wabbajack_adjustments", [])
    names = [item[4:] for item in accepted if item in {"dll:dinput8", "dll:version", "dll:winhttp", "dll:winmm"}]
    existing = env.get("WINEDLLOVERRIDES", "")
    configured = {name.strip().lstrip("*").casefold() for clause in existing.split(";")
                  for name in clause.partition("=")[0].split(",") if name.strip()}
    additions = [name + "=n,b" for name in names if name not in configured and "" not in configured]
    if additions:
        env["WINEDLLOVERRIDES"] = ";".join(filter(None, [existing, *additions]))


def working_directory(game, exe):
    from Utils.profiles.state import read_profile_state
    profile = getattr(game, "_active_profile_dir", None)
    if profile:
        state = read_profile_state(Path(profile))
        saved = state.get("wabbajack_working_directories", {}).get(exe.name)
        if saved and Path(saved).is_dir():
            return Path(saved)
    return exe.parent
