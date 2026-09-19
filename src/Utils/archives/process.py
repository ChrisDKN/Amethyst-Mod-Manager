from __future__ import annotations

import errno
import os
import re
import shutil
import signal
import subprocess
import threading
from pathlib import Path


def failure_kind(error, code=None, tool="") -> str:
    message = str(error).lower()
    number = getattr(error, "errno", None)
    if number in (errno.ENOSPC, errno.EDQUOT) or any(value in message for value in (
            "no space left", "quota exceeded", "enospc", "edquot")):
        return "disk-space"
    if isinstance(error, MemoryError) or number == errno.ENOMEM or any(value in message for value in (
            "not enough memory", "cannot allocate memory", "out of memory")):
        return "memory"
    if any(value in message for value in (
            "enter password", "wrong password", "password is required", "password required",
            "password-protected", "password protected", "password is incorrect",
            "encrypted archive", "encrypted file")) or type(error).__name__ == "PasswordRequired":
        return "password"
    if number in (errno.EACCES, errno.EPERM) or any(value in message for value in (
            "permission denied", "access is denied", "read-only file system")):
        return "permission"
    if Path(tool).name in {"7z", "7za", "7zz", "7zzs"}:
        if code == 8:
            return "memory"
        if code == 7:
            return "arguments"
        if code == 255:
            return "cancelled"
    if code is not None and code < 0:
        return "terminated"
    return "archive"


def failure_message(tool, code, output) -> str:
    kind = failure_kind(output, code, tool)
    summary = {
        "disk-space": "Not enough disk space or disk quota exceeded",
        "memory": "Not enough memory to extract the archive",
        "password": "Password-protected archive requires a password; unattended extraction cannot continue",
        "permission": "Archive or destination is not accessible",
        "arguments": "Extractor rejected its command-line options",
        "cancelled": "Extraction was cancelled",
        "terminated": "Extractor was terminated by a signal",
        "archive": "Archive extraction failed",
    }[kind]
    if code == 1 and Path(tool).name in {"7z", "7za", "7zz", "7zzs"}:
        summary = "Extractor reported warnings; extraction is not verified complete"
    from Utils.processes.watch import redact_text
    detail = output.strip() or "no diagnostic output"
    return redact_text(f"{tool} (exit {code}): {summary}. {detail}")


def run_extractor(cmd, cancel=None, progress_cb=None, low_priority=False):
    """Return exit code, bounded diagnostics, and whether cancellation killed the process."""
    if cancel is not None and cancel.is_set():
        return 255, "Extraction cancelled", True
    tool = cmd[0]
    from Utils.downloads.resources import current_resources
    resources = current_resources()
    if low_priority and shutil.which("ionice"):
        cmd = ["ionice", "-c2", "-n7", *cmd]
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as exc:
        return 127, f"{tool}: {type(exc).__name__}: {exc}", False
    if low_priority:
        try:
            os.setpriority(os.PRIO_PROCESS, proc.pid, 19)
        except (OSError, AttributeError):
            pass
    if resources is not None:
        resources.emit("install.extractor.started", tool=tool, pid=proc.pid,
                       thread_option=next((arg for arg in cmd if arg.startswith("-mmt=")), ""),
                       low_priority=low_priority)
    output = [b"", b""]

    def drain(stream, index):
        last, tail = -1, b""
        try:
            while chunk := stream.read1(4096):
                output[index] = (output[index] + chunk)[-8192:]
                if index == 0 and progress_cb is not None:
                    hits = re.findall(rb"(\d{1,3})%", tail + chunk)
                    tail = chunk[-8:]
                    if hits:
                        percent = min(100, int(hits[-1]))
                        if percent != last:
                            last = percent
                            try:
                                progress_cb(percent)
                            except Exception:
                                pass
        finally:
            stream.close()

    readers = [threading.Thread(target=drain, args=(stream, index), daemon=True)
               for index, stream in enumerate((proc.stdout, proc.stderr))]
    for reader in readers:
        reader.start()
    killed = False
    paused = False

    def resume():
        nonlocal paused
        if paused:
            proc.send_signal(signal.SIGCONT)
            paused = False

    try:
        while True:
            if cancel is not None and cancel.is_set():
                resume()
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                killed = True
                break
            if resources is not None and hasattr(signal, "SIGSTOP"):
                should_pause = resources.pause_extractor()
                if should_pause != paused:
                    proc.send_signal(signal.SIGSTOP if should_pause else signal.SIGCONT)
                    paused = should_pause
            try:
                proc.wait(timeout=0.1 if resources is not None else
                          0.25 if cancel is not None else None)
                break
            except subprocess.TimeoutExpired:
                pass
    except BaseException:
        resume()
        proc.kill()
        proc.wait()
        raise
    finally:
        resume()
    for reader in readers:
        reader.join(timeout=5)
    detail = "\n".join(part.decode("utf-8", "replace").replace("\b", "")
                       for part in output if part)
    if proc.returncode:
        detail = failure_message(tool, proc.returncode, detail)
    return proc.returncode, detail, killed
