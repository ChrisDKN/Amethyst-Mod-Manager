"""Shared Proton/DXVK runner for the Windows texture-processing tools."""

from __future__ import annotations

import os
import pty
import re
import select
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from Utils.config_paths import get_wine_prefixes_dir
from Utils.texture_tools import (
    TextureToolCancelled, apply_discrete_gpu_environment, kill_process_group,
)


_ANSI_RE = re.compile(
    r"\x1b\[[0-9;]*[A-Za-z]|\x1b\[[?][0-9;]*[A-Za-z]|\x1b[A-Za-z]|\r"
)


def linux_to_wine(path: str | Path) -> str:
    r"""Convert a Linux absolute path to its Wine ``Z:\`` path."""
    return "Z:" + str(path).replace("/", "\\")


@dataclass(frozen=True)
class ProtonTextureRunner:
    proton_script: Path
    compat_data: Path
    env: dict[str, str]


def _patch_prefix_utf8(prefix: Path) -> None:
    """Set an existing Proton prefix's ANSI/OEM code pages to UTF-8."""
    reg_file = prefix / "system.reg"
    if not reg_file.is_file():
        return
    try:
        content = reg_file.read_text(errors="replace")
        if '"ACP"="65001"' in content and '"OEMCP"="65001"' in content:
            return
        content = re.sub(r'"ACP"="[^"]*"', '"ACP"="65001"', content)
        content = re.sub(r'"OEMCP"="[^"]*"', '"OEMCP"="65001"', content)
        reg_file.write_text(content)
    except OSError:
        pass


def prepare_proton_texture_runner(
    exe: Path,
    prefix_name: str,
    log_fn: Callable[[str], None],
    *,
    prefer_discrete_gpu: bool = False,
) -> ProtonTextureRunner:
    """Create/update an isolated Proton prefix with DXVK enabled."""
    from Utils.exe_launch import get_tool_prefix_env
    from Utils.steam_finder import find_any_installed_proton

    proton_script = find_any_installed_proton()
    if proton_script is None:
        raise RuntimeError("No Proton installation found. Install Proton via Steam.")

    # Keep prefixes tied to their Proton version. ``runinprefix`` intentionally
    # skips Proton's full upgrade setup, so reusing one fixed prefix after the
    # selected Proton changes can leave stale DXVK DLLs and overrides behind.
    compat_data = (
        get_wine_prefixes_dir()
        / f"{prefix_name}_{proton_script.parent.name}"
    )
    resolved = get_tool_prefix_env(
        exe,
        proton_script.parent.name,
        prefix_dir=compat_data,
    )
    if resolved is None:
        raise RuntimeError(
            f"Could not prepare the Proton prefix for {exe.name}."
        )

    proton_script, compat_data, env = resolved
    env = dict(env)
    env["WINEDEBUG"] = "-all"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    selection = apply_discrete_gpu_environment(env, prefer_discrete_gpu)
    _patch_prefix_utf8(compat_data / "pfx")

    log_fn(f"  Proton: {proton_script.parent.name}")
    log_fn(f"  Prefix: {compat_data}")
    log_fn(f"  GPU: {selection}")
    return ProtonTextureRunner(proton_script, compat_data, env)


def run_proton_texture_tool(
    runner: ProtonTextureRunner,
    exe: str | Path,
    args: list[str],
    log_fn: Callable[[str], None],
    *,
    label: str = "",
    cancel_evt: "threading.Event | None" = None,
) -> int:
    """Run a texture tool through Proton/DXVK and stream its PTY output.

    When *cancel_evt* is set the tool's whole process group is killed and
    ``TextureToolCancelled`` is raised - the select() timeout below doubles as
    the cancel poll, so a stop lands within ~0.1s even while the tool is silent.
    """
    from Utils.steam_finder import proton_run_command

    exe_path = Path(exe)
    display = label or exe_path.name
    # Don't start another step if the user already pressed Stop.
    if cancel_evt is not None and cancel_evt.is_set():
        raise TextureToolCancelled(f"{display} cancelled")
    log_fn(f"── {display} ──")
    cmd = proton_run_command(
        runner.proton_script,
        "runinprefix",
        str(exe_path),
        env=runner.env,
        host_cwd=exe_path.parent,
    ) + list(args)

    master_fd, slave_fd = pty.openpty()
    proc: subprocess.Popen | None = None
    cancelled = False
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=slave_fd,
            stderr=slave_fd,
            env=runner.env,
            cwd=exe_path.parent,
            # Lead a new process group so a cancel can killpg the whole Proton
            # tree (wineserver + the tool) rather than orphaning the workers.
            start_new_session=True,
        )
        os.close(slave_fd)
        slave_fd = -1

        buf = b""
        while True:
            if cancel_evt is not None and cancel_evt.is_set():
                log_fn(f"  {display}: stopping…")
                kill_process_group(proc, log_fn)
                cancelled = True
                break
            try:
                readable, _, _ = select.select([master_fd], [], [], 0.1)
            except (ValueError, OSError):
                break
            if readable:
                try:
                    chunk = os.read(master_fd, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line_bytes, buf = buf.split(b"\n", 1)
                    line = line_bytes.decode("utf-8", errors="replace")
                    stripped = _ANSI_RE.sub("", line).rstrip("\r")
                    if stripped.strip():
                        log_fn(f"  {stripped}")
            elif proc.poll() is not None:
                break

        # A cancelled run's tail output is noise - the process group is already
        # dead and the partial output is about to be discarded.
        if not cancelled:
            try:
                while True:
                    chunk = os.read(master_fd, 4096)
                    if not chunk:
                        break
                    buf += chunk
            except OSError:
                pass
            if buf:
                line = buf.decode("utf-8", errors="replace")
                stripped = _ANSI_RE.sub("", line).rstrip("\r\n")
                if stripped.strip():
                    log_fn(f"  {stripped}")

            proc.wait()
    finally:
        try:
            os.close(master_fd)
        except OSError:
            pass
        if slave_fd != -1:
            try:
                os.close(slave_fd)
            except OSError:
                pass

    if cancelled:
        raise TextureToolCancelled(f"{display} cancelled")

    returncode = proc.returncode if proc is not None else -1
    if returncode != 0:
        log_fn(f"  WARNING: {display} exited with code {returncode}")
    return returncode
