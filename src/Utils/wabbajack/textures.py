from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from .paths import WabbajackError

TEXCONV_VERSION = "may2026"
TEXCONV_URL = "https://github.com/microsoft/DirectXTex/releases/download/may2026/texconv.exe"
TEXCONV_SHA256 = "dcfdec10244e02cf5037fba089c55fb7e1326b1c8181742d77d15fa5cb5eef06"


def tool_path() -> Path:
    from Utils.config_paths import get_config_dir
    return get_config_dir() / "tools" / "texconv" / TEXCONV_VERSION / "texconv.exe"


def install_texture_tool(stop=None):
    from .acquire import download_http
    target = tool_path()
    if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != TEXCONV_SHA256:
        download_http(TEXCONV_URL, target, stop=stop)
    if hashlib.sha256(target.read_bytes()).hexdigest() != TEXCONV_SHA256:
        raise WabbajackError("Texconv failed Microsoft release checksum verification")
    prepare_texture_runtime(target, stop)
    return target


def prepare_texture_runtime(target, stop=None, log=None):
    from types import SimpleNamespace
    from Utils.wine.protontricks import install_vcredist
    from Utils.launchers.steam import find_any_installed_proton
    request = SimpleNamespace(texconv=target, proton=None)
    try:
        probe_texture_tool(request, stop)
        return
    except WabbajackError:
        pass
    _, env = _command(request, [])
    proton = find_any_installed_proton()
    if not install_vcredist(proton, env, log_fn=log, prefix_path=target.parent / "prefix" / "pfx"):
        raise WabbajackError("Could not prepare the isolated Texconv runtime. Install VC++ Redistributable in its tool prefix and retry.")
    probe_texture_tool(request, stop)


def _command(request, arguments):
    from Utils.launchers.steam import find_any_installed_proton, find_steam_root_for_proton_script
    tool = request.texconv or tool_path()
    if not tool.is_file():
        raise WabbajackError("Texture conversion requires Texconv. Use Install Texture Tool in setup.")
    if hashlib.sha256(tool.read_bytes()).hexdigest() != TEXCONV_SHA256:
        raise WabbajackError(f"Select the verified Texconv {TEXCONV_VERSION} release")
    proton = request.proton or find_any_installed_proton()
    if proton and proton.is_dir():
        proton = proton / "proton"
    if not proton or not proton.is_file():
        raise WabbajackError("Texture conversion requires an installed Proton runtime")
    prefix = tool.parent / "prefix"
    prefix.mkdir(exist_ok=True)
    env = os.environ.copy()
    for key in ("WINEPREFIX", "WINEDLLOVERRIDES", "LD_LIBRARY_PATH", "LD_PRELOAD"):
        env.pop(key, None)
    env.update(STEAM_COMPAT_DATA_PATH=str(prefix), WINEPREFIX=str(prefix / "pfx"),
               STEAM_COMPAT_CLIENT_INSTALL_PATH=str(find_steam_root_for_proton_script(proton) or ""),
               SteamAppId="0", SteamGameId="0", STEAM_COMPAT_APP_ID="0")
    from Utils.launchers.steam import proton_run_command
    verb = "runinprefix" if (prefix / "pfx" / "user.reg").is_file() else "run"
    return proton_run_command(proton, verb, str(tool), *arguments, env=env, host_cwd=tool.parent), env


def _run(request, arguments, stop=None, timeout=600):
    command, env = _command(request, arguments)
    import selectors
    from collections import deque
    tail = deque(maxlen=8)
    process = subprocess.Popen(command, env=env, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, start_new_session=True)
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
                if (stop is not None and stop.is_set()) or time.monotonic() > deadline:
                    raise InterruptedError("Texture conversion stopped or timed out")
        if process.returncode:
            detail = b"".join(tail).decode("utf-8", "replace")[-4000:]
            raise WabbajackError(f"Texconv exited with code {process.returncode}. Use Install Texture Tool to prepare its runtime. {detail}")
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


def probe_texture_tool(request, stop=None):
    _run(request, ["--version"], stop, timeout=60)


_FORMATS = {
    10: "R16G16B16A16_FLOAT", 28: "R8G8B8A8_UNORM", 29: "R8G8B8A8_UNORM_SRGB",
    49: "R8G8_UNORM", 61: "R8_UNORM", 65: "A8_UNORM", 87: "B8G8R8A8_UNORM",
    88: "B8G8R8X8_UNORM", 91: "B8G8R8A8_UNORM_SRGB", 93: "B8G8R8X8_UNORM_SRGB",
    71: "BC1_UNORM", 72: "BC1_UNORM_SRGB", 74: "BC2_UNORM", 75: "BC2_UNORM_SRGB",
    77: "BC3_UNORM", 78: "BC3_UNORM_SRGB", 80: "BC4_UNORM", 81: "BC4_SNORM",
    83: "BC5_UNORM", 84: "BC5_SNORM", 95: "BC6H_UF16", 96: "BC6H_SF16",
    98: "BC7_UNORM", 99: "BC7_UNORM_SRGB",
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


def transform_texture(request, source, target, state, stop=None):
    from Utils.ba2.writer import _parse_dds
    width, height, mips, format_name, filtering = texture_parameters(state)
    with tempfile.TemporaryDirectory(prefix="texture-", dir=target.parent) as tmp:
        work = Path(tmp)
        input_path = work / "source.dds"
        shutil.copyfile(source, input_path)
        output = work / "out"
        output.mkdir()
        windows = lambda p: "Z:" + str(p.resolve()).replace("/", "\\")
        _run(request, [windows(input_path), "-o", windows(output), "-ft", "dds", "-f", format_name,
                       "-w", str(width), "-h", str(height), "-m", str(mips),
                       "-if", filtering, "-singleproc", "-nogpu", "-dx10", "-y"], stop)
        result = output / "source.dds"
        with result.open("rb") as stream:
            import mmap
            with mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as mapped:
                info = _parse_dds(mapped)
        expected_mips = mips or max(width, height).bit_length()
        if info["width"] != width or info["height"] != height or info["mip_count"] != expected_mips or _FORMATS.get(info["dxgi_format"]) != format_name:
            raise WabbajackError("Converted texture does not match requested dimensions or mipmaps")
        result.replace(target)
