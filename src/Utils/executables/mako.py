from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import subprocess
import tempfile
import uuid
from pathlib import Path

# Update the release and checksum together after validating its launch/config contract.
VERSION = "4.0.0"
_SHA256 = "1258d8f73359974a4880236b1ecc5275c702960edc6e6ce0a57b27f52109ebdf"
_URL = (f"https://github.com/eugeniosegala/MAKO/releases/download/render-v{VERSION}/"
        f"MAKO-Renderer-v{VERSION}-linux.tar.xz")
ENV_KEYS = ("MAKO_CONFIG", "MAKO_PROFILE", "MAKO_ENV", "MAKO_LAUNCH_CONFIG",
            "MAKO_ALLOW_COMPETING_LAYERS", "DISABLE_MAKO", "AMM_MAKO_LAUNCH")
_MANIFESTS = (
    "share/mako-render/vulkan/implicit_layer.d/VkLayer_MAKO_render.json",
    "share/mako-render/vulkan/implicit_layer.d/VkLayer_MAKO_render.x86.json",
)


def _root() -> Path:
    from Utils.config_paths import get_tools_dir
    return get_tools_dir() / "mako"


def _validate_payload(prefix: Path) -> None:
    for relative in ("bin/mako-launch", "bin/mako-cli", "bin/mako-vrr-lease",
                     "lib/libmako-render.so", "lib32/libmako-render.so",
                     "MAKO-Renderer-version.txt", *_MANIFESTS):
        if not (prefix / relative).is_file():
            raise RuntimeError(f"MAKO release is missing {relative}")
    for relative in _MANIFESTS:
        manifest = prefix / relative
        layer = json.loads(manifest.read_text())["layer"]
        library = (manifest.parent / layer["library_path"]).resolve()
        if (layer["name"] != "VK_LAYER_MAKO_render"
                or not library.is_relative_to(prefix.resolve())
                or not library.is_file()):
            raise RuntimeError(f"Invalid MAKO Vulkan manifest: {relative}")
    for name in ("mako-launch", "mako-cli", "mako-vrr-lease"):
        if not os.access(prefix / "bin" / name, os.X_OK):
            raise RuntimeError(f"MAKO executable is not runnable: {name}")


def _selection() -> dict:
    path = _root() / "selection.json"
    if path.exists():
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            raise ValueError("Invalid MAKO installation selection")
        return data
    current = _root() / "current"
    return {"current": current.read_text().strip() if current.exists() else ""}


def _selected_prefix(key: str) -> Path | None:
    try:
        version = _selection().get(key, "")
        if not re.fullmatch(r"\d+\.\d+\.\d+(?:-[0-9a-f]{8})?", version):
            return None
        prefix = _root() / version
        _validate_payload(prefix)
        return prefix
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        return None


def installed_prefix() -> Path | None:
    return _selected_prefix("current")


def rollback_prefix() -> Path | None:
    previous = _selected_prefix("previous")
    return previous if previous != installed_prefix() else None


def _activate(target: Path) -> None:
    from Utils.atomic_write import write_atomic_text
    _validate_payload(target)
    current = installed_prefix()
    if target == current:
        return
    write_atomic_text(_root() / "selection.json", json.dumps({
        "current": target.name, "previous": current.name if current else "",
    }) + "\n")


def rollback() -> tuple[bool, str]:
    try:
        previous = rollback_prefix()
        if previous is None:
            raise RuntimeError("No valid previous MAKO installation is available")
        version = (previous / "MAKO-Renderer-version.txt").read_text().strip()
        _activate(previous)
        return True, version
    except Exception as exc:
        return False, str(exc)


def check_updates() -> dict:
    from urllib.request import Request, urlopen
    from Utils.ca_bundle import get_ssl_context
    request = Request("https://api.github.com/repos/eugeniosegala/MAKO/releases?per_page=100",
                      headers={"Accept": "application/vnd.github+json",
                               "User-Agent": "AmethystModManager"})
    with urlopen(request, timeout=20, context=get_ssl_context()) as response:
        releases = json.load(response)
    versions = []
    for release in releases:
        match = re.fullmatch(r"render-v(\d+)\.(\d+)\.(\d+)", release.get("tag_name", ""))
        if match and not release.get("prerelease") and not release.get("draft"):
            versions.append(tuple(map(int, match.groups())))
    if not versions:
        raise RuntimeError("No stable MAKO Renderer releases were returned")
    return {"latest": ".".join(map(str, max(versions))), "supported": VERSION,
            "installed": installed_version()}


def installed_version() -> str:
    prefix = installed_prefix()
    if prefix is None:
        return ""
    try:
        return (prefix / "MAKO-Renderer-version.txt").read_text().strip()
    except OSError:
        return ""


def install_latest(log_fn=None, *, force=False) -> tuple[bool, str]:
    log = log_fn or (lambda _message: None)
    try:
        if platform.machine().lower() not in ("x86_64", "amd64"):
            raise RuntimeError("MAKO's release requires an x86_64 Linux host")
        from Utils.ca_bundle import download_file
        from Utils.executables.lsfg import _extract_archive

        root = _root()
        root.mkdir(parents=True, exist_ok=True)
        target = installed_prefix()
        if force or target is None or installed_version() != VERSION:
            target = root / f"{VERSION}-{uuid.uuid4().hex[:8]}"
            log(f"MAKO: downloading supported Renderer {VERSION}.")
            with tempfile.TemporaryDirectory(prefix=".install-", dir=root) as tmp:
                archive = Path(tmp) / "renderer.tar.xz"
                download_file(_URL, archive, timeout=180)
                with archive.open("rb") as stream:
                    digest = hashlib.file_digest(stream, "sha256").hexdigest()
                if digest != _SHA256:
                    raise RuntimeError("MAKO release checksum did not match")
                payload = Path(tmp) / "payload"
                payload.mkdir()
                _extract_archive(archive, payload)
                _validate_payload(payload)
                if (payload / "MAKO-Renderer-version.txt").read_text().strip() != VERSION:
                    raise RuntimeError("MAKO release version did not match")
                payload.rename(target)
        _validate_payload(target)
        _activate(target)
        log(f"MAKO: Renderer {VERSION} ready at {target}.")
        return True, VERSION
    except Exception as exc:
        log(f"MAKO: setup failed: {exc}")
        return False, str(exc)


def config_path(game_or_name) -> Path:
    from Utils.config_paths import get_game_config_dir
    name = getattr(game_or_name, "name", game_or_name)
    return get_game_config_dir(str(name)) / "mako.toml"


def _config_text(settings: dict) -> str:
    from Utils.executables.launch import _normalize_lsfg_settings, detect_lsfg_dll

    values = _normalize_lsfg_settings(settings)
    dll = values["mako_dll_path"] or detect_lsfg_dll(mako=True)
    lines = ["version = 2", "", "[global]",
             f"allow_fp16 = {str(values['allow_fp16']).lower()}"]
    if dll:
        dll = os.path.expandvars(os.path.expanduser(dll))
        lines.append(f"dll = {json.dumps(dll, ensure_ascii=False)}")
    profile = {
        "frame_generation_provisioned": True,
        "frame_generation_enabled": values["mako_frame_generation"],
        "multiplier": values["mako_multiplier"],
        "adaptive": values["mako_adaptive"],
        "target_fps": values["mako_target_fps"],
        "base_fps_cap": (0 if values["mako_adaptive"] and (
                             values["mako_steady_base"] or values["mako_real_frame_priority"] != "auto")
                         else values["mako_base_fps_cap"]),
        "adaptive_max_multiplier": values["mako_max_multiplier"],
        "adaptive_auto_base_fps_cap": values["mako_steady_base"],
        "adaptive_stable_cadence": values["mako_smooth_cadence"],
        "adaptive_fractional_real_frame_priority": values["mako_real_frame_priority"],
        "dynamic_cadence_recovery": values["mako_cadence_recovery"],
        "dynamic_cadence_probe_interval_seconds": values["mako_probe_interval"],
        "flow_scale": values["flow_scale"],
        "performance_mode": values["performance_mode"],
        "swapchain_image_count_compatibility": values["preserve_swapchain_image_count"],
        "scaling_enabled": False,
    }
    lines.extend(["", "[[profile]]", 'name = "Amethyst"'])
    lines.extend(f"{key} = {json.dumps(value)}" for key, value in profile.items())
    return "\n".join(lines) + "\n"


def write_config(game_or_name, settings: dict) -> Path:
    from Utils.atomic_write import write_atomic_text
    path = config_path(game_or_name)
    write_atomic_text(path, _config_text(settings))
    return path


def _run_cli(prefix: Path, arguments: list[str]) -> subprocess.CompletedProcess:
    command = [str(prefix / "bin/mako-cli"), *arguments]
    env = os.environ.copy()
    if env.get("FLATPAK_ID") == "io.github.Amethyst.ModManager":
        from Utils.environment.xdg import host_env
        env = host_env()
        command = ["flatpak-spawn", "--host", *command]
    return subprocess.run(command, capture_output=True, text=True, timeout=30, env=env)


def check_setup(settings: dict) -> list[tuple[str, bool, str]]:
    from Utils.executables.launch import _normalize_lsfg_settings, detect_lsfg_dll
    values = _normalize_lsfg_settings(settings)
    results = []
    prefix = installed_prefix()
    results.append(("renderer", prefix is not None, installed_version() if prefix else ""))
    dll = values["mako_dll_path"] or detect_lsfg_dll(mako=True)
    dll = os.path.expandvars(os.path.expanduser(dll))
    try:
        exists = bool(dll) and _host_file(Path(dll), 2) == b"MZ"
    except (OSError, subprocess.SubprocessError):
        exists = False
    results.append(("dll", exists, dll))
    if prefix:
        try:
            with tempfile.TemporaryDirectory(prefix=".check-", dir=_root()) as tmp:
                config = Path(tmp) / "mako.toml"
                config.write_text(_config_text(values))
                result = _run_cli(prefix, ["validate", "--config", str(config)])
                results.append(("config", result.returncode == 0,
                                (result.stderr or result.stdout).strip()[:1000]))
        except (OSError, subprocess.SubprocessError) as exc:
            results.append(("config", False, str(exc)))
        if exists:
            try:
                arguments = ["inspect-dll", "--dll", dll, "--lsfg"]
                if not values["allow_fp16"]:
                    arguments.append("--no-fp16")
                result = _run_cli(prefix, arguments)
                compatible = result.returncode == 0 and json.loads(result.stdout).get("compatible") is True
                results.append(("models", compatible,
                                "" if compatible else (result.stderr or result.stdout).strip()[:1000]))
            except (OSError, ValueError, AttributeError, subprocess.SubprocessError) as exc:
                results.append(("models", False, str(exc)))
    architectures = sorted(m["layer"]["library_arch"] for m in _mangohud_manifests())
    results.append(("mangohud", bool(architectures), ", ".join(architectures)))
    return results


def apply_environment(game, settings: dict, env: dict) -> None:
    env.pop("MAKO_ENV", None)
    env.update(AMM_MAKO_LAUNCH="1", MAKO_CONFIG=str(write_config(game, settings)),
               MAKO_PROFILE="Amethyst",
               DISABLE_MAKO="0", MAKO_ALLOW_COMPETING_LAYERS="0",
               MAKO_LAUNCH_CONFIG="/dev/null")


def _host_file(path: Path, limit: int = 65536) -> bytes:
    if os.environ.get("FLATPAK_ID") == "io.github.Amethyst.ModManager":
        from Utils.environment.xdg import host_env
        result = subprocess.run(
            ["flatpak-spawn", "--host", "head", "-c", str(limit), "--", str(path)],
            capture_output=True, timeout=5, env=host_env(), check=True)
        return result.stdout
    with path.open("rb") as stream:
        return stream.read(limit)


def _mangohud_manifests() -> list[dict]:
    home = Path.home()
    if os.environ.get("FLATPAK_ID") == "io.github.Amethyst.ModManager":
        roots = [home / ".config", Path("/etc"), home / ".local/share",
                 Path("/usr/local/share"), Path("/usr/share")]
    else:
        roots = [Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config"),
                 *(Path(p) for p in (os.environ.get("XDG_CONFIG_DIRS") or "/etc/xdg").split(":") if p),
                 Path("/etc"), Path(os.environ.get("XDG_DATA_HOME") or home / ".local/share"),
                 *(Path(p) for p in (os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":") if p)]
    manifests = {}
    identities = {"VK_LAYER_MANGOHUD_overlay_x86_64": "64",
                  "VK_LAYER_MANGOHUD_overlay_x86": "32",
                  "VK_LAYER_MANGOHUD_overlay": None}
    for root in dict.fromkeys(roots):
        for filename in ("MangoHud.x86_64.json", "MangoHud.x86.json", "MangoHud.json"):
            source = root / "vulkan/implicit_layer.d" / filename
            try:
                manifest = json.loads(_host_file(source))
                layer = manifest["layer"]
                name = layer["name"]
                if name not in identities or layer.get("type") != "GLOBAL":
                    continue
                library = Path(layer["library_path"])
                if not library.is_absolute():
                    library = Path(os.path.normpath(source.parent / library))
                header = _host_file(library, 20)
                arch = {1: "32", 2: "64"}.get(header[4] if len(header) >= 20 else 0)
                if (header[:4] != b"\x7fELF" or arch is None
                        or identities[name] not in (None, arch)
                        or layer.get("library_arch") not in (None, arch)):
                    continue
                if arch in manifests:
                    continue
                manifest["file_format_version"] = "1.2.1"
                # One explicit name lets each Wine process select its own architecture.
                layer["name"] = "VK_LAYER_MANGOHUD_overlay"
                layer["library_path"] = str(library)
                layer["library_arch"] = arch
                manifests[arch] = manifest
            except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
                continue
    return list(manifests.values())


def _mangohud_arguments(env: dict, log_fn) -> list[str]:
    if env.get("MANGOHUD") != "1":
        return []
    from Utils.atomic_write import write_atomic_text

    manifests = _mangohud_manifests()
    if not manifests:
        log_fn("MAKO: MangoHud was requested, but no usable host MangoHud Vulkan "
               "layer was found. Install MangoHud on the host and restart the game.")
        return []
    contents = [json.dumps(manifest, sort_keys=True, indent=2) + "\n"
                for manifest in manifests]
    digest = hashlib.sha256("".join(contents).encode()).hexdigest()[:16]
    directory = _root() / "mangohud" / digest
    try:
        for index, content in enumerate(contents):
            path = directory / f"MangoHud-{index}.json"
            if not path.is_file() or path.read_text() != content:
                write_atomic_text(path, content)
    except OSError as exc:
        log_fn(f"MAKO: could not prepare MangoHud's Vulkan manifests: {exc}")
        return []
    names = list(dict.fromkeys(manifest["layer"]["name"] for manifest in manifests))
    existing = [name for name in env.get("VK_INSTANCE_LAYERS", "").split(":")
                if name and name != "VK_LAYER_MAKO_render"
                and not name.startswith("VK_LAYER_MANGOHUD_")
                and name != "VK_LAYER_VKBASALT_post_processing"]
    layers = ":".join(["VK_LAYER_MAKO_render", *names, *existing])
    paths = ":".join(filter(None, (str(directory), env.get("VK_LAYER_PATH", ""))))
    log_fn("MAKO: loading host MangoHud after the renderer "
           f"({', '.join(manifest['layer']['library_arch'] + '-bit' for manifest in manifests)}).")
    options = [f"{key}={env[key]}" for key in (
        "MANGOHUD_CONFIG", "MANGOHUD_CONFIGFILE", "MANGOHUD_DLSYM") if key in env]
    return ["-u", "DISABLE_MANGOHUD", f"VK_LAYER_PATH={paths}",
            f"VK_INSTANCE_LAYERS={layers}", "MANGOHUD=1", "ENABLE_VKBASALT=0",
            *options]


def wrap_command(command: list[str], env: dict, *, sandbox_bridge=False,
                 log_fn=None) -> list[str]:
    if env.get("AMM_MAKO_LAUNCH") != "1":
        return list(command)
    prefix = installed_prefix()
    if prefix is None:
        raise RuntimeError("Install MAKO from the frame generation settings before launching.")
    if sandbox_bridge or any(
            Path(part).name == "flatpak" and command[index + 1:index + 2] == ["run"]
            for index, part in enumerate(command)):
        raise RuntimeError(
            "MAKO's managed renderer supports host games. Games running inside a "
            "launcher Flatpak need MAKO's matching runtime extension; disable MAKO "
            "here and configure that launcher using MAKO's Flatpak setup.")
    launcher = str(prefix / "bin/mako-launch")
    if launcher in command:
        return list(command)
    if log_fn is None:
        from Utils.app_log import app_log
        log_fn = app_log
    wrapper = ["/usr/bin/env", "-u", "MAKO_ENV",
               *_mangohud_arguments(env, log_fn), launcher]
    portal = next((index for index, part in enumerate(command)
                   if Path(part).name == "flatpak-spawn"
                   and command[index + 1:index + 2] == ["--host"]), None)
    if portal is not None:
        index = portal + 2
        while index < len(command) and command[index].startswith("--"):
            index += 1
        forwarded = [f"--env={key}={env[key]}" for key in ENV_KEYS
                     if key in env and key != "MAKO_ENV"]
        flags = [part for part in command[portal + 2:index]
                 if not (part.startswith("--env=")
                         and part[6:].split("=", 1)[0] in ENV_KEYS)]
        return [*command[:portal + 2], *forwarded,
                *flags, *wrapper, *command[index:]]
    if os.environ.get("FLATPAK_ID") == "io.github.Amethyst.ModManager":
        from Utils.flatpak.env import flatpak_forward_env_args
        return ["flatpak-spawn", "--host", *flatpak_forward_env_args(env),
                *wrapper, *command]
    return [*wrapper, *command]
