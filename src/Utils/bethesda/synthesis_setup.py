from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import threading
from contextlib import ExitStack
from contextvars import ContextVar
from pathlib import Path
from urllib.parse import urlsplit
import urllib.request


_execution = ContextVar("synthesis_setup_execution", default=None)


class SynthesisSetupError(RuntimeError):
    def __init__(self, name, message, details=None):
        super().__init__(message)
        self.name = name
        self.details = details or {}


class SynthesisSetupCancelled(SynthesisSetupError):
    def __init__(self):
        super().__init__("job_cancelled", "Synthesis prefix setup cancelled.")


def check():
    context = _execution.get()
    if context is not None:
        if context["cancel"].is_set():
            raise SynthesisSetupCancelled()
        context["checkpoint"]()
        validate_prefix_paths(context["prefix"])


def validate_prefix_paths(prefix, *, deep=False):
    from Utils.wizards.payload import PayloadInstallError

    paths = [prefix, prefix.parent, prefix.parent.parent, prefix.parent.parent.parent,
             prefix / ".synthesis_setup", prefix / ".bethesda_registry",
             prefix / "dosdevices", prefix / "drive_c/Program Files/dotnet",
             prefix / "drive_c/Program Files (x86)", prefix / "drive_c/ProgramData",
             *(prefix / "drive_c/windows" / name for name in ("Fonts", "system32", "syswow64", "winsxs", "temp")),
             prefix / "drive_c/users/steamuser/AppData/Roaming/NuGet"]
    for path in paths:
        while path.is_relative_to(prefix):
            if path.is_symlink() or (path.exists() and not path.is_dir()):
                raise PayloadInstallError("unsafe_synthesis_prefix", "A Synthesis setup directory is a link or file.", {"path": str(path)})
            if path == prefix:
                break
            path = path.parent
    for path in (prefix.parent, prefix.parent.parent, prefix.parent.parent.parent, prefix.parent / ".bethesda_registry"):
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise PayloadInstallError("unsafe_synthesis_prefix", "A Synthesis application directory is a link or file.")
    for path in [*(prefix / name for name in ("system.reg", "user.reg", "userdef.reg")),
                 prefix / "drive_c/users/steamuser/AppData/Roaming/NuGet/NuGet.Config"]:
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise PayloadInstallError("unsafe_synthesis_prefix", "A Synthesis setup file is a link or special file.", {"path": str(path)})
    drive = prefix / "dosdevices/c:"
    if (drive.exists() or drive.is_symlink()) and (not drive.is_symlink() or drive.resolve() != (prefix / "drive_c").resolve()):
        raise PayloadInstallError("unsafe_synthesis_prefix", "The Synthesis C drive targets another directory.")
    if deep:
        for root in (prefix / "drive_c/Program Files/dotnet", prefix / ".synthesis_setup"):
            if root.is_dir():
                for current, directories, files in os.walk(root, followlinks=False):
                    for name in directories + files:
                        if (Path(current) / name).is_symlink():
                            raise PayloadInstallError("unsafe_synthesis_prefix", "A Synthesis runtime or setup marker is a symbolic link.")


def run_command(command, **options):
    from Utils.wine.protontricks import run_prefix_installer

    from Utils.launchers.nixos import wrap_nixos_command

    check()
    command = wrap_nixos_command(command, env=options["env"])
    context = _execution.get()
    code, output = run_prefix_installer(
        command, options["env"], options.get("cwd", context["prefix"].parent if context else Path.cwd()),
        label="Synthesis setup", timeout=options["timeout"],
        cancel=context["cancel"] if context else None,
        log_fn=context["log"] if context else None)
    check()
    if code is None:
        raise subprocess.TimeoutExpired(command, options["timeout"], output=output)
    return subprocess.CompletedProcess(command, code, stdout=output, stderr=output)


def flush_prefix(prefix, wine, log):
    from .synthesis import _base_env
    from Utils.wine.protontricks import run_prefix_installer

    from Utils.launchers.nixos import wrap_nixos_command

    for option in ("-k", "-w"):
        validate_prefix_paths(prefix)
        context = _execution.get()
        if context is not None:
            context["cleanup_check"]()
        code, output = run_prefix_installer(wrap_nixos_command([str(wine.parent / "wineserver"), option]), _base_env(prefix, wine),
                                            prefix.parent, label="Synthesis prefix cleanup", timeout=30, log_fn=log)
        if code != 0:
            raise SynthesisSetupError("synthesis_prefix_cleanup_failed", "Could not finish Synthesis prefix cleanup.",
                                      {"exit_code": code, "output": output})


def _digest(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            check()
            digest.update(chunk)
    return digest.hexdigest()


def download_installer(url, destination, log):
    from Utils.atomic_write import write_atomic_text
    from Utils.ca_bundle import get_ssl_context
    from Utils.operations import acquire_resource_operation

    context = _execution.get()
    check()
    sidecar = destination.with_suffix(destination.suffix + ".synthesis.json")
    with acquire_resource_operation(f"synthesis-installer:{destination.resolve()}", "Synthesis installer download",
                                    cancel=context["cancel"] if context else None):
        for path in (destination, sidecar):
            if path.is_symlink() or (path.exists() and not path.is_file()):
                raise SynthesisSetupError("unsafe_synthesis_installer", "The installer cache contains a link or special file.")
        try:
            identity = json.loads(sidecar.read_text()) if sidecar.is_file() and sidecar.stat().st_size <= 65536 else None
            if (isinstance(identity, dict) and destination.is_file() and identity.get("url") == url
                    and identity.get("size") == destination.stat().st_size and identity.get("sha256") == _digest(destination)):
                log(f"  cached: {destination.name}")
                return True
        except (OSError, ValueError):
            pass
        check()
        if urlsplit(url).scheme != "https":
            raise SynthesisSetupError("invalid_synthesis_installer", "The Synthesis installer source must use HTTPS.")
        log(f"  downloading {destination.name} …")
        fd, raw = tempfile.mkstemp(prefix=".synthesis-installer-", suffix=".part", dir=destination.parent)
        temporary = Path(raw)
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "ModManager/1.0"})
            with os.fdopen(fd, "wb") as output, urllib.request.urlopen(request, timeout=30, context=get_ssl_context()) as response:
                if urlsplit(response.url).scheme != "https":
                    raise SynthesisSetupError("invalid_synthesis_installer", "The installer redirected away from HTTPS.")
                total = int(response.headers.get("Content-Length") or 0)
                if total < 0:
                    raise SynthesisSetupError("invalid_synthesis_installer", "The installer download has an invalid size.")
                size = 0
                digest = hashlib.sha256()
                while True:
                    check()
                    chunk = response.read(65536)
                    if not chunk:
                        break
                    output.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                    if context:
                        context["progress"](size, total, f"synthesis_download:{destination.name}")
            check()
            with temporary.open("rb") as stream:
                header = stream.read(64)
                valid = len(header) == 64 and header[:2] == b"MZ"
                if valid:
                    offset = int.from_bytes(header[60:64], "little")
                    stream.seek(offset)
                    valid = offset >= 64 and stream.read(4) == b"PE\0\0"
            if not valid or not size or (total and size != total):
                raise SynthesisSetupError("invalid_synthesis_installer", "The installer download is incomplete or is not a Windows executable.")
            temporary.replace(destination)
            write_atomic_text(sidecar, json.dumps({"url": url, "size": size, "sha256": digest.hexdigest()}, sort_keys=True))
            return True
        finally:
            temporary.unlink(missing_ok=True)


def dotnet_present(prefix, marker):
    root = prefix / "drive_c/Program Files/dotnet"
    if not (root / "dotnet.exe").is_file():
        return False
    sdk = marker == "dotnet10_sdk"
    minimum = 10 if sdk else int(marker.split("_", 1)[0][6:])
    directory = root / "sdk" if sdk else root / "shared/Microsoft.WindowsDesktop.App"
    required = "dotnet.dll" if sdk else "PresentationFramework.dll"
    if not directory.is_dir():
        return False
    majors = {int(p.name.split(".")[0]) for p in directory.iterdir()
              if re.fullmatch(r"\d+\.\d+\.\d+", p.name) and int(p.name.split(".")[0]) >= minimum
              and (sdk or int(p.name.split(".")[0]) == minimum) and (p / required).is_file()}
    core = root / "shared/Microsoft.NETCore.App"
    return bool(majors and core.is_dir() and any(re.fullmatch(r"\d+\.\d+\.\d+", p.name)
                and int(p.name.split(".")[0]) in majors and (p / "coreclr.dll").is_file() for p in core.iterdir()))


def verify_dotnet(prefix, wine):
    from .synthesis import _base_env

    executable = prefix / "drive_c/Program Files/dotnet/dotnet.exe"
    results = [run_command([str(wine), str(executable), option], env=_base_env(prefix, wine), timeout=60)
               for option in ("--list-sdks", "--list-runtimes")]
    sdk = bool(results[0].returncode == 0 and any(
        int(line.split(".", 1)[0]) >= 10 for line in results[0].stdout.splitlines()
        if line.split(".", 1)[0].isdigit()))
    runtimes = {int(parts[1].split(".")[0]) for line in results[1].stdout.splitlines()
                if len(parts := line.split()) >= 2 and parts[0] == "Microsoft.WindowsDesktop.App"
                and parts[1].split(".")[0].isdigit()} if results[1].returncode == 0 else set()
    return {"sdk_available": sdk, "desktop_runtime_majors": sorted(runtimes),
            "verified": sdk and {6, 7, 8, 10}.issubset(runtimes)}


def registry_patches_present(prefix):
    from .synthesis import _DLL_OVERRIDES, _XEDIT_EXECUTABLES
    from Utils.wine.registry import read_value

    expected = [(r"Software\Wine\DllOverrides", name, "native,builtin") for name in _DLL_OVERRIDES]
    expected += [(rf"Software\Wine\AppDefaults\{name}", "Version", "winxp") for name in _XEDIT_EXECUTABLES]
    expected += [(r"Software\Wine\X11 Driver", "UseTakeFocus", "N"),
                 (r"Software\Wine\AppDefaults\Pandora Behaviour Engine+.exe\X11 Driver", "Decorated", "N")]
    return all(read_value(prefix, key, name, hive="user.reg") == value for key, name, value in expected)


def prepare_prefix(game, script, *, cancel=None, before_write=lambda: None, checkpoint=lambda: None,
                   on_initialized=lambda: None, on_settings_saved=lambda: None, save_settings=False,
                   final_check=lambda: None, cleanup_check=lambda: None,
                   log_fn=lambda _m: None, progress_fn=lambda *_a: None):
    from . import synthesis
    from Utils.deployment.locking import game_mutation_lock
    from Utils.operations import OperationCancelled, acquire_game_operation, acquire_resource_operation
    from Utils.profiles.state import read_only_profile_state

    cancel = cancel or threading.Event()
    target = synthesis.synthesis_dir(game)
    prefix = synthesis.synthesis_pfx(game)
    synthesis.validate_installation_target(game, target.resolve())
    validate_prefix_paths(prefix, deep=True)
    report = {"ok": False, "completed_steps": [], "failed_steps": [], "state_persisted": False,
              "settings_persisted": False, "prefix_path": str(prefix), "needs_deployment": False}
    with read_only_profile_state(), acquire_game_operation(game, "tools.synthesis.prefix.setup"), game_mutation_lock(game), ExitStack() as locks:
        locks.enter_context(synthesis.installation_guard(target, cancel, operation="tools.synthesis.prefix.setup"))
        for name in ("system.reg", "user.reg", "userdef.reg"):
            locks.enter_context(acquire_resource_operation(f"wine-state:{(prefix / name).resolve()}",
                                                           "tools.synthesis.prefix.setup", cancel=cancel))
        token = _execution.set({"cancel": cancel, "checkpoint": checkpoint, "cleanup_check": cleanup_check,
                                "prefix": prefix, "log": log_fn, "progress": progress_fn})
        pending = None
        wine = synthesis._wine_bin(script)
        try:
            check()
            before_write()
            check()
            if save_settings:
                synthesis.save_proton(game, script.parent.name)
                report["settings_persisted"] = True
                on_settings_saved()
            report["state_persisted"] = True
            prefix.mkdir(parents=True, exist_ok=True)
            on_initialized()
            synthesis.setup_synthesis_prefix(target, script, game.get_game_path(), log_fn,
                                              registry_game_name=game.synthesis_registry_name,
                                              report=report, on_initialized=on_initialized, progress_fn=progress_fn)
            check()
            if not report["failed_steps"]:
                report["dotnet"] = verify_dotnet(prefix, wine)
                if not report["dotnet"]["verified"]:
                    report["failed_steps"].append("dotnet_readback")
        except Exception as exc:
            pending = SynthesisSetupCancelled() if isinstance(exc, OperationCancelled) else exc
        finally:
            if report["state_persisted"]:
                try:
                    flush_prefix(prefix, wine, log_fn)
                    from Utils.wine.repair import prefix_usage
                    if prefix_usage(prefix) != set():
                        raise SynthesisSetupError("synthesis_prefix_cleanup_failed", "Could not verify that the Synthesis prefix is idle.")
                    if pending is None:
                        check()
                        final_check()
                except Exception as exc:
                    if isinstance(exc, (SynthesisSetupCancelled,)) or getattr(exc, "name", "") == "stale_synthesis_prefix":
                        pending = exc
                    else:
                        pending = SynthesisSetupError("synthesis_prefix_cleanup_failed", "Synthesis prefix cleanup failed.",
                                                       {"reason": str(exc), "original_error": str(pending) if pending else None})
            _execution.reset(token)
        if pending is not None:
            if hasattr(pending, "details"):
                pending.details.update(report)
                raise pending
            raise SynthesisSetupError("synthesis_prefix_setup_failed", "Synthesis prefix setup failed.",
                                      {**report, "reason": str(pending)}) from pending
        from .registry import registration_state
        report["registration"] = registration_state(prefix, game.synthesis_registry_name, game.get_game_path())
        if not report["registration"]["registered"]:
            report["failed_steps"].append("game_registration_readback")
        if report["failed_steps"]:
            raise SynthesisSetupError("synthesis_prefix_setup_failed", "Some Synthesis setup steps could not be verified.", report)
        report["ok"] = True
        report["runtime_execution_verified"] = True
        log_fn("Synthesis prefix ready.")
        progress_fn(1, 1, "synthesis_prefix_ready")
        return report
