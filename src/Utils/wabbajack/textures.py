from __future__ import annotations

import hashlib
import os
import platform
import shutil
import stat
import struct
import subprocess
import tarfile
import tempfile
import threading
import time
from functools import lru_cache
from contextlib import contextmanager
from pathlib import Path

from .paths import WabbajackError
from .diagnostics import emit, emit_exception

TEXCONV_VERSION = "may2026"
TEXCONV_URL = "https://github.com/microsoft/DirectXTex/releases/download/may2026/texconv.exe"
TEXCONV_SHA256 = "dcfdec10244e02cf5037fba089c55fb7e1326b1c8181742d77d15fa5cb5eef06"
COMPRESSONATOR_VERSION = "4.5.52"
COMPRESSONATOR_URL = "https://github.com/GPUOpen-Tools/compressonator/releases/download/V4.5.52/compressonatorcli-4.5.52-Linux.tar.gz"
COMPRESSONATOR_ARCHIVE_SHA256 = "70c9cdb27a19875df03766f349864951a749a44c0f5c001c33903944465f6b97"
COMPRESSONATOR_SHA256 = "c00d88dab9c0dce00818263dc7ac9f8bc24ba5ef2ed461e56aaedad01270c239"
_CONVERSIONS = threading.BoundedSemaphore(2)
_PREPARATION = threading.Lock()
_LEASES = threading.local()
_USED_PREFIXES = set()


def configure_texture_request(request):
    mode = request.setup_options.setdefault("texture", {}).get("mode", "auto")
    if mode not in {"auto", "cpu", "compressonator"}:
        raise WabbajackError("Unknown texture conversion mode")
    if mode == "compressonator":
        tool = getattr(request, "compressonator", None) or find_compressonator()
        if not tool:
            raise WabbajackError("Native Compressonator is not installed. Use Install / repair Compressonator in setup.")
        request.compressonator = Path(tool).resolve()
        return
    from Utils.launchers.steam import find_any_installed_proton
    options = request.setup_options.setdefault("texture", {})
    selected = request.proton or options.get("proton")
    proton = Path(selected) if selected else find_any_installed_proton()
    if proton and proton.is_dir():
        proton = proton / "proton"
    if not proton or not proton.is_file() or proton.name != "proton":
        raise WabbajackError("Select an installed Proton build for the isolated texture tool")
    request.proton = proton.resolve()
    options["proton"] = str(request.proton)


def _prefix(request):
    tool = request.texconv or tool_path()
    key = hashlib.sha256(str(request.proton).encode()).hexdigest()[:16]
    return tool.parent / "prefixes" / key


@contextmanager
def _prefix_lease(request, stop=None, *, exclusive=False):
    import fcntl
    configure_texture_request(request)
    prefix = _prefix(request)
    held = getattr(_LEASES, "held", set())
    if prefix in held:
        yield
        return
    prefix.parent.mkdir(parents=True, exist_ok=True)
    with prefix.with_suffix(".lock").open("a+b") as lock:
        while True:
            try:
                fcntl.flock(lock, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if stop is not None and stop.wait(0.1):
                    raise InterruptedError("Texture setup stopped")
                if stop is None:
                    time.sleep(0.1)
        _LEASES.held = held | {prefix}
        try:
            yield
        finally:
            _LEASES.held = held
            fcntl.flock(lock, fcntl.LOCK_UN)


@lru_cache(maxsize=8)
def _verified_tool(path, stamp):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest() == TEXCONV_SHA256


def tool_path() -> Path:
    from Utils.config_paths import get_config_dir
    return get_config_dir() / "tools" / "texconv" / TEXCONV_VERSION / "texconv.exe"


def compressonator_path() -> Path:
    from Utils.config_paths import get_config_dir
    return get_config_dir() / "tools" / "compressonator" / COMPRESSONATOR_VERSION / "compressonatorcli-bin"


def compressonator_supported() -> bool:
    return platform.machine().casefold() in {"x86_64", "amd64"}


def find_compressonator() -> Path | None:
    bundled = compressonator_path()
    if bundled.is_file():
        try:
            if hashlib.sha256(bundled.read_bytes()).hexdigest() == COMPRESSONATOR_SHA256:
                return bundled
        except OSError:
            pass
    return None


def install_texture_tool(stop=None, *, request=None, log=None):
    if request and request.setup_options.get("texture", {}).get("mode") == "compressonator":
        return install_compressonator(stop, request=request, log=log)
    from .acquire import download_http
    target = tool_path()
    emit(log, "texture.tool.install.started", target=target,
         version=TEXCONV_VERSION, present=target.is_file())
    if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != TEXCONV_SHA256:
        download_http(TEXCONV_URL, target, stop=stop, log=log)
    if hashlib.sha256(target.read_bytes()).hexdigest() != TEXCONV_SHA256:
        raise WabbajackError("Texconv failed Microsoft release checksum verification")
    prepare_texture_runtime(target, stop, log, request=request)
    emit(log, "texture.tool.install.completed", target=target,
         sha256=TEXCONV_SHA256)
    return target


def install_compressonator(stop=None, *, request=None, log=None):
    if request is None:
        from types import SimpleNamespace
        request = SimpleNamespace(compressonator=None, texconv=None, proton=None,
                                  setup_options={"texture": {"mode": "compressonator"}})
    if platform.machine().casefold() not in {"x86_64", "amd64"}:
        raise WabbajackError("The portable native Compressonator build is available only for x86-64 Linux")
    from .acquire import download_http
    target = compressonator_path()
    emit(log, "texture.native.install.started", target=target,
         version=COMPRESSONATOR_VERSION, present=target.is_file())
    target.parent.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".compressonator-", dir=target.parent.parent) as tmp:
        work = Path(tmp)
        bundle = work / "compressonator.tar.gz"
        download_http(COMPRESSONATOR_URL, bundle, stop=stop, log=log)
        if hashlib.sha256(bundle.read_bytes()).hexdigest() != COMPRESSONATOR_ARCHIVE_SHA256:
            raise WabbajackError("Compressonator failed its official release checksum verification")
        extracted = work / "extracted"
        extracted.mkdir()
        with tarfile.open(bundle) as archive:
            archive.extractall(extracted, filter="data")
        root = extracted / f"compressonatorcli-{COMPRESSONATOR_VERSION}-Linux"
        binary = root / "compressonatorcli-bin"
        if not binary.is_file() or hashlib.sha256(binary.read_bytes()).hexdigest() != COMPRESSONATOR_SHA256:
            raise WabbajackError("Compressonator executable failed checksum verification")
        binary.chmod(binary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        backup = target.parent.with_name(target.parent.name + f".old-{time.time_ns()}")
        if target.parent.exists():
            target.parent.replace(backup)
        try:
            root.replace(target.parent)
        except OSError:
            if backup.exists():
                backup.replace(target.parent)
            raise
        if backup.exists():
            shutil.rmtree(backup)
    request.compressonator = target.resolve()
    probe_texture_tool(request, stop, log=log)
    emit(log, "texture.native.install.completed", target=target,
         sha256=COMPRESSONATOR_SHA256)
    return target


def prepare_texture_runtime(target, stop=None, log=None, *, request=None):
    from types import SimpleNamespace
    from Utils.wine.protontricks import install_vcredist
    request = request or SimpleNamespace(texconv=target, proton=None, setup_options={})
    request.texconv = target
    configure_texture_request(request)
    emit(log, "texture.runtime.prepare.started", texconv=target,
         proton=request.proton, prefix=_prefix(request),
         mode=request.setup_options.get("texture", {}).get("mode", "auto"))
    with _PREPARATION, _prefix_lease(request, stop, exclusive=True):
        try:
            probe_texture_tool(request, stop, log=log)
            emit(log, "texture.runtime.reused", prefix=_prefix(request))
            return
        except (WabbajackError, OSError) as exc:
            emit_exception(log, "texture.runtime.probe_failed", exc)
        from Utils.executables.launch import shutdown_prefix_wineserver
        prefix = _prefix(request)
        backup = None
        try:
            for attempt in range(2):
                try:
                    emit(log, "texture.runtime.install_attempt", attempt=attempt + 1,
                         prefix=prefix)
                    _, env = _command(request, [])
                    if stop is not None and stop.is_set():
                        raise InterruptedError("Texture setup stopped")
                    if not install_vcredist(request.proton, env, log_fn=log, prefix_path=prefix / "pfx"):
                        raise WabbajackError("Could not install the texture tool's Visual C++ runtime")
                    probe_texture_tool(request, stop, log=log)
                    if backup:
                        shutil.rmtree(backup)
                    emit(log, "texture.runtime.prepare.completed", prefix=prefix,
                         attempt=attempt + 1)
                    return
                except InterruptedError:
                    raise
                except (WabbajackError, OSError) as exc:
                    emit_exception(log, "texture.runtime.install_failed", exc,
                                   attempt=attempt + 1)
                    if attempt:
                        raise
                    shutdown_prefix_wineserver(request.proton, prefix, log_fn=log)
                    backup = prefix.with_name(prefix.name + f".failed-{time.time_ns()}")
                    if prefix.exists():
                        prefix.rename(backup)
                        emit(log, "texture.runtime.prefix_quarantined",
                             prefix=prefix, backup=backup)
        finally:
            if backup and backup.exists():
                shutdown_prefix_wineserver(request.proton, prefix, log_fn=log)
                if prefix.exists():
                    shutil.rmtree(prefix)
                backup.replace(prefix)
                emit(log, "texture.runtime.prefix_restored",
                     prefix=prefix, backup=backup)


def _command(request, arguments):
    from Utils.launchers.steam import find_steam_root_for_proton_script
    configure_texture_request(request)
    tool = request.texconv or tool_path()
    if not tool.is_file():
        raise WabbajackError("Texconv is not installed. Use Install / repair Texconv in setup.")
    stat = tool.stat()
    if not _verified_tool(str(tool), (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino)):
        raise WabbajackError(f"Select the verified Texconv {TEXCONV_VERSION} release")
    proton = request.proton
    prefix = _prefix(request)
    prefix.mkdir(parents=True, exist_ok=True)
    _USED_PREFIXES.add((proton, prefix))
    env = os.environ.copy()
    for key in ("WINEPREFIX", "WINEDLLOVERRIDES", "LD_LIBRARY_PATH", "LD_PRELOAD"):
        env.pop(key, None)
    env.update(STEAM_COMPAT_DATA_PATH=str(prefix), WINEPREFIX=str(prefix / "pfx"),
               STEAM_COMPAT_CLIENT_INSTALL_PATH=str(find_steam_root_for_proton_script(proton) or ""),
               SteamAppId="0", SteamGameId="0", STEAM_COMPAT_APP_ID="0")
    env["OMP_NUM_THREADS"] = str(max(1, min(4, (os.cpu_count() or 2) // 2)))
    from Utils.launchers.steam import proton_run_command
    verb = "runinprefix" if (prefix / "pfx" / "user.reg").is_file() else "run"
    return proton_run_command(proton, verb, str(tool), *arguments, env=env, host_cwd=tool.parent), env


class _TextureFailure(WabbajackError):
    def __init__(self, message, transient=False):
        super().__init__(message)
        self.transient = transient


def _run_once(request, arguments, stop=None, timeout=600, log=None):
    if stop is not None and stop.is_set():
        raise InterruptedError("Texture conversion stopped")
    command, env = _command(request, arguments)
    started = time.monotonic()
    emit(log, "texture.process.started", command=command,
         proton=request.proton, prefix=_prefix(request), timeout=timeout,
         mode=request.setup_options.get("texture", {}).get("mode", "auto"),
         environment={name: env.get(name) for name in (
             "STEAM_COMPAT_DATA_PATH", "STEAM_COMPAT_CLIENT_INSTALL_PATH",
             "WINEPREFIX", "WINEARCH", "WINEDLLOVERRIDES", "OMP_NUM_THREADS")
             if env.get(name)})
    import selectors
    from collections import deque
    tail = deque(maxlen=8)
    try:
        process = subprocess.Popen(command, env=env, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, start_new_session=True)
    except OSError as exc:
        raise WabbajackError(f"Could not start Texconv: {exc}") from exc
    deadline = time.monotonic() + timeout
    try:
        os.set_blocking(process.stdout.fileno(), False)
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                events = selector.select(0.2)
                for key, _ in events:
                    data = os.read(key.fd, 8192)
                    if data:
                        tail.append(data)
                    else:
                        selector.unregister(key.fileobj)
                if process.poll() is not None and not events:
                    break
                if stop is not None and stop.is_set():
                    raise InterruptedError("Texture conversion stopped")
                if time.monotonic() > deadline:
                    raise WabbajackError(f"Texconv timed out after {timeout} seconds")
        if process.returncode:
            detail = b"".join(tail).decode("utf-8", "replace")[-4000:]
            emit(log, "texture.process.failed", exit_code=process.returncode,
                 output=detail,
                 elapsed_seconds=round(time.monotonic() - started, 3))
            transient = any(word in detail.casefold() for word in ("connection reset by peer", "wine client error", "recvmsg"))
            raise _TextureFailure(f"Texconv exited with code {process.returncode}. {detail}", transient)
        detail = b"".join(tail).decode("utf-8", "replace")[-4000:]
        emit(log, "texture.process.completed", exit_code=process.returncode,
             output=detail,
             elapsed_seconds=round(time.monotonic() - started, 3))
        return detail
    finally:
        if process.poll() is None:
            import signal
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            except ProcessLookupError:
                process.wait()
        process.stdout.close()


def _run(request, arguments, stop=None, timeout=600, log=None):
    with _prefix_lease(request, stop):
        return _run_leased(request, arguments, stop, timeout, log)


def _run_leased(request, arguments, stop=None, timeout=600, log=None):
    while not _CONVERSIONS.acquire(timeout=0.1):
        if stop is not None and stop.is_set():
            raise InterruptedError("Texture conversion stopped")
    try:
        with _prefix_lease(request, stop):
            for attempt in range(3):
                try:
                    return _run_once(request, arguments, stop, timeout, log)
                except _TextureFailure as exc:
                    emit_exception(log, "texture.process.retry", exc,
                                   attempt=attempt + 1, transient=exc.transient)
                    if not exc.transient or attempt == 2:
                        raise
                    if stop is not None and stop.wait(0.2 * (attempt + 1)):
                        raise InterruptedError("Texture conversion stopped")
    finally:
        _CONVERSIONS.release()


def shutdown_texture_tools():
    import fcntl
    from Utils.executables.launch import shutdown_prefix_wineserver
    for proton, prefix in list(_USED_PREFIXES):
        with prefix.with_suffix(".lock").open("a+b") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                continue
            try:
                shutdown_prefix_wineserver(proton, prefix)
                _USED_PREFIXES.discard((proton, prefix))
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)


def probe_texture_tool(request, stop=None, formats=None, *, log=None):
    emit(log, "texture.probe.started", formats=formats)
    if request.setup_options.get("texture", {}).get("mode") == "compressonator":
        configure_texture_request(request)
        _run_compressonator(request, ["-version"], stop, 60, log)
        _probe_texture_tool(request, stop, formats, log)
        emit(log, "texture.probe.completed", formats=formats,
             converter="compressonator", tool=request.compressonator)
        return
    with _prefix_lease(request, stop, exclusive=True):
        _probe_texture_tool(request, stop, formats, log)
    emit(log, "texture.probe.completed", formats=formats,
         proton=request.proton, prefix=_prefix(request))


def _probe_texture_tool(request, stop=None, formats=None, log=None):
    configure_texture_request(request)
    if request.setup_options.get("texture", {}).get("mode") != "compressonator":
        _run(request, ["--version"], stop, timeout=60, log=log)
    from .paths import existing_parent
    root = existing_parent(getattr(request, "directory", _prefix(request)))
    with tempfile.TemporaryDirectory(prefix=".texture-probe-", dir=root) as tmp:
        work = Path(tmp)
        source = work / "input.dds"
        fields = [124, 0x100F, 16, 16, 64, 0, 1] + [0] * 11
        fields += [32, 0x41, 0, 32, 0xFF, 0xFF00, 0xFF0000, 0xFF000000, 0x1000, 0, 0, 0, 0]
        source.write_bytes(b"DDS " + struct.pack("<31I", *fields) + bytes((64, 128, 192, 255)) * 256)
        for index, name in enumerate(formats or ("BC7_UNORM", "BC3_UNORM")):
            transform_texture(request, source, work / f"probe-{index}.dds",
                              {"Width": 8, "Height": 8, "MipLevels": 4,
                               "Format": name}, stop, timeout=60, log=log)


_FORMATS = {
    10: "R16G16B16A16_FLOAT", 28: "R8G8B8A8_UNORM", 29: "R8G8B8A8_UNORM_SRGB",
    49: "R8G8_UNORM", 61: "R8_UNORM", 65: "A8_UNORM", 87: "B8G8R8A8_UNORM",
    88: "B8G8R8X8_UNORM", 91: "B8G8R8A8_UNORM_SRGB", 93: "B8G8R8X8_UNORM_SRGB",
    71: "BC1_UNORM", 72: "BC1_UNORM_SRGB", 74: "BC2_UNORM", 75: "BC2_UNORM_SRGB",
    77: "BC3_UNORM", 78: "BC3_UNORM_SRGB", 80: "BC4_UNORM", 81: "BC4_SNORM",
    83: "BC5_UNORM", 84: "BC5_SNORM", 95: "BC6H_UF16", 96: "BC6H_SF16",
    98: "BC7_UNORM", 99: "BC7_UNORM_SRGB",
}

_COMPRESSONATOR_FORMATS = {
    "BC1_UNORM": "BC1", "BC1_UNORM_SRGB": "BC1",
    "BC2_UNORM": "BC2", "BC2_UNORM_SRGB": "BC2",
    "BC3_UNORM": "BC3", "BC3_UNORM_SRGB": "BC3",
    "BC4_UNORM": "BC4", "BC4_SNORM": "BC4_S",
    "BC5_UNORM": "BC5", "BC5_SNORM": "BC5_S",
    "BC7_UNORM": "BC7", "BC7_UNORM_SRGB": "BC7",
}

_NATIVE_RAW_FORMATS = {
    "R8G8B8A8_UNORM": 28, "R8G8B8A8_UNORM_SRGB": 29,
    "R8G8_UNORM": 49, "R8_UNORM": 61, "A8_UNORM": 65,
    "B8G8R8A8_UNORM": 87, "B8G8R8X8_UNORM": 88,
    "B8G8R8A8_UNORM_SRGB": 91, "B8G8R8X8_UNORM_SRGB": 93,
}


def texture_parameters(state):
    width, height, mips = int(state["Width"]), int(state["Height"]), int(state["MipLevels"])
    raw = str(state["Format"]).removeprefix("DXGI_FORMAT_")
    format_name = _FORMATS.get(int(raw), "") if raw.isdecimal() else raw
    if format_name not in _FORMATS.values():
        raise WabbajackError(f"Unsupported texture format: {raw}")
    if min(width, height) <= 0 or not 0 <= mips <= 15 or max(width, height) > 16384:
        raise WabbajackError("Invalid texture dimensions or mip count")
    filtering = str(state.get("Filter", "CUBIC")).upper()
    if filtering not in {"POINT", "LINEAR", "CUBIC", "FANT", "BOX", "TRIANGLE"}:
        raise WabbajackError(f"Unsupported texture filtering: {filtering}")
    return width, height, mips, format_name, filtering


def _run_compressonator(request, arguments, stop=None, timeout=600, log=None):
    configure_texture_request(request)
    tool = request.compressonator
    if tool == compressonator_path():
        try:
            verified = hashlib.sha256(tool.read_bytes()).hexdigest() == COMPRESSONATOR_SHA256
        except OSError:
            verified = False
        if not verified:
            raise WabbajackError("Install / repair the verified native Compressonator release")
    env = os.environ.copy()
    libraries = str(tool.parent / "pkglibs")
    env["LD_LIBRARY_PATH"] = libraries + ((":" + env["LD_LIBRARY_PATH"]) if env.get("LD_LIBRARY_PATH") else "")
    env["OMP_NUM_THREADS"] = str(max(1, min(4, (os.cpu_count() or 2) // 2)))
    command = [str(tool), *map(str, arguments)]
    emit(log, "texture.native.process.started", command=command, timeout=timeout,
         environment={"LD_LIBRARY_PATH": libraries, "OMP_NUM_THREADS": env["OMP_NUM_THREADS"]})
    try:
        process = subprocess.Popen(command, env=env, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, start_new_session=True)
    except OSError as exc:
        raise WabbajackError(f"Could not start native Compressonator: {exc}") from exc
    deadline = time.monotonic() + timeout
    try:
        import selectors
        from collections import deque
        tail = deque(maxlen=8)
        os.set_blocking(process.stdout.fileno(), False)
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                events = selector.select(0.2)
                for key, _ in events:
                    data = os.read(key.fd, 8192)
                    if data:
                        tail.append(data)
                    else:
                        selector.unregister(key.fileobj)
                if process.poll() is not None and not events:
                    break
                if stop is not None and stop.is_set():
                    raise InterruptedError("Texture conversion stopped")
                if time.monotonic() > deadline:
                    raise WabbajackError(f"Compressonator timed out after {timeout} seconds")
        detail = b"".join(tail).decode("utf-8", "replace")[-4000:]
        if process.returncode:
            emit(log, "texture.native.process.failed", exit_code=process.returncode,
                 output=detail)
            raise WabbajackError(f"Compressonator exited with code {process.returncode}. {detail}")
        emit(log, "texture.native.process.completed", exit_code=process.returncode,
             output=detail)
        return detail
    finally:
        if process.poll() is None:
            import signal
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            except ProcessLookupError:
                process.wait()
        process.stdout.close()


def _force_dx10_format(path, dxgi_format):
    data = bytearray(Path(path).read_bytes())
    if len(data) < 128 or data[:4] != b"DDS ":
        raise WabbajackError("Compressonator did not produce a valid DDS file")
    if data[84:88] == b"DX10":
        struct.pack_into("<I", data, 128, dxgi_format)
    else:
        struct.pack_into("<I", data, 80, 4)
        data[84:108] = b"DX10" + bytes(20)
        data[128:128] = struct.pack("<5I", dxgi_format, 3, 0, 1, 0)
    Path(path).write_bytes(data)


def _write_native_dds(path, image, format_name, mip_count, resampling):
    from PIL import Image
    dxgi_format = _NATIVE_RAW_FORMATS[format_name]
    channels = {28: 4, 29: 4, 49: 2, 61: 1, 65: 1,
                87: 4, 88: 4, 91: 4, 93: 4}[dxgi_format]
    flags = 0x1 | 0x2 | 0x4 | 0x8 | 0x1000 | (0x20000 if mip_count > 1 else 0)
    caps = 0x1000 | (0x8 | 0x400000 if mip_count > 1 else 0)
    header = (b"DDS " + struct.pack("<7I", 124, flags, image.height, image.width,
              image.width * channels, 0, mip_count) + bytes(44) +
              struct.pack("<2I4s5I", 32, 4, b"DX10", 0, 0, 0, 0, 0) +
              struct.pack("<5I", caps, 0, 0, 0, 0) +
              struct.pack("<5I", dxgi_format, 3, 0, 1, 0))
    payload = bytearray()
    for level in range(mip_count):
        width, height = max(1, image.width >> level), max(1, image.height >> level)
        mip = image if level == 0 else image.resize((width, height), resampling)
        if dxgi_format in {28, 29}:
            payload.extend(mip.tobytes("raw", "RGBA"))
        elif dxgi_format == 49:
            payload.extend(Image.merge("LA", (mip.getchannel("R"), mip.getchannel("G"))).tobytes())
        elif dxgi_format == 61:
            payload.extend(mip.getchannel("R").tobytes())
        elif dxgi_format == 65:
            payload.extend(mip.getchannel("A").tobytes())
        else:
            payload.extend(mip.tobytes("raw", "BGRA"))
    Path(path).write_bytes(header + payload)


def _transform_compressonator(request, source, result, width, height, mips,
                              format_name, filtering, stop, timeout, log):
    target_format = _COMPRESSONATOR_FORMATS.get(format_name)
    if not target_format and format_name not in _NATIVE_RAW_FORMATS:
        raise WabbajackError(f"Native Compressonator does not support {format_name}; select Texconv")
    try:
        from PIL import Image
        filters = {
            "POINT": Image.Resampling.NEAREST, "LINEAR": Image.Resampling.BILINEAR,
            "CUBIC": Image.Resampling.BICUBIC, "FANT": Image.Resampling.LANCZOS,
            "BOX": Image.Resampling.BOX, "TRIANGLE": Image.Resampling.BILINEAR,
        }
        with Image.open(source) as opened:
            image = opened.convert("RGBA").resize((width, height), filters[filtering])
        expected_mips = mips or max(width, height).bit_length()
        if format_name in _NATIVE_RAW_FORMATS:
            _write_native_dds(result, image, format_name, expected_mips, filters[filtering])
            return expected_mips
        prepared = result.with_suffix(".png")
        image.save(prepared)
    except (OSError, ValueError) as exc:
        raise WabbajackError(f"Native Compressonator could not decode the source DDS: {exc}") from exc
    arguments = ["-fd", target_format, "-miplevels", str(expected_mips),
                 "-NumThreads", str(max(1, min(4, (os.cpu_count() or 2) // 2))),
                 "-silent"]
    arguments.extend([prepared, result])
    _run_compressonator(request, arguments, stop, timeout, log)
    target_dxgi = next(code for code, name in _FORMATS.items() if name == format_name)
    from Utils.ba2.writer import _DdsParseError, _parse_dds
    try:
        with result.open("rb") as stream:
            import mmap
            with mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as mapped:
                actual_dxgi = _parse_dds(mapped)["dxgi_format"]
    except (OSError, ValueError, _DdsParseError):
        actual_dxgi = None
    if actual_dxgi != target_dxgi:
        _force_dx10_format(result, target_dxgi)
    return expected_mips


def transform_texture(request, source, target, state, stop=None, *, timeout=600,
                      log=None):
    from Utils.ba2.writer import _parse_dds
    width, height, mips, format_name, filtering = texture_parameters(state)
    started = time.monotonic()
    emit(log, "texture.transform.started", source=source, target=target,
         width=width, height=height, mip_levels=mips, format=format_name,
         filtering=filtering,
         mode=request.setup_options.get("texture", {}).get("mode", "auto"))
    with tempfile.TemporaryDirectory(prefix="texture-", dir=target.parent) as tmp:
        work = Path(tmp)
        if request.setup_options.get("texture", {}).get("mode") == "compressonator":
            result = work / "converted.dds"
            try:
                while not _CONVERSIONS.acquire(timeout=0.1):
                    if stop is not None and stop.is_set():
                        raise InterruptedError("Texture conversion stopped")
                try:
                    expected_mips = _transform_compressonator(
                        request, source, result, width, height, mips, format_name,
                        filtering, stop, timeout, log)
                finally:
                    _CONVERSIONS.release()
            except WabbajackError as exc:
                raise WabbajackError(f"{target}: {width}x{height}, {format_name}, {mips} mip levels; native Compressonator. {exc}. Install / repair Compressonator or select Texconv, then resume.") from exc
        else:
            input_path = work / "source.dds"
            shutil.copyfile(source, input_path)
            output = work / "out"
            output.mkdir()
            windows = lambda p: "Z:" + str(p.resolve()).replace("/", "\\")
            flags = ["-nogpu"] if request.setup_options.get("texture", {}).get("mode") == "cpu" else []
            try:
                _run(request, [windows(input_path), "-o", windows(output), "-ft", "dds", "-f", format_name,
                               "-w", str(width), "-h", str(height), "-m", str(mips),
                               "-if", filtering, *flags, "-dx10", "-y"], stop,
                     timeout, log)
            except WabbajackError as exc:
                raise WabbajackError(f"{target}: {width}x{height}, {format_name}, {mips} mip levels; Proton {request.proton.parent.name}. {exc}. Prepare the texture tool again or select CPU conversion, then resume.") from exc
            result = output / "source.dds"
            expected_mips = mips or max(width, height).bit_length()
        with result.open("rb") as stream:
            import mmap
            with mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as mapped:
                info = _parse_dds(mapped)
        if info["width"] != width or info["height"] != height or info["mip_count"] != expected_mips or _FORMATS.get(info["dxgi_format"]) != format_name:
            raise WabbajackError("Converted texture does not match requested dimensions or mipmaps")
        result.replace(target)
    emit(log, "texture.transform.completed", target=target,
         bytes=target.stat().st_size, width=width, height=height,
         mip_levels=expected_mips, format=format_name,
         elapsed_seconds=round(time.monotonic() - started, 3))
