from __future__ import annotations

import errno
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
from pathlib import Path


_IOPRIO_SCHEDULERS = {"bfq", "mq-deadline"}


def _active_scheduler(path=None):
    candidates = []
    if path is not None:
        try:
            existing = Path(path)
            while not existing.exists() and existing != existing.parent:
                existing = existing.parent
            device = existing.stat().st_dev
            node = Path(f"/sys/dev/block/{os.major(device)}:{os.minor(device)}").resolve()
            if (node / "partition").exists():
                node = node.parent
            candidates = [node / "queue/scheduler"]
        except OSError:
            pass
    fallback = list(Path("/sys/block").glob("*/queue/scheduler"))
    if not candidates:
        candidates = fallback

    def read(paths):
        found = []
        for candidate in paths:
            try:
                match = re.search(r"\[([^]]+)\]", candidate.read_text())
            except OSError:
                continue
            if match:
                found.append(match.group(1))
        return found

    active = read(candidates)
    if not active and candidates != fallback:
        active = read(fallback)
    return tuple(dict.fromkeys(active))


def low_priority_support(path=None):
    schedulers = _active_scheduler(path)
    ionice = shutil.which("ionice")
    supported = None if not schedulers else all(
        scheduler in _IOPRIO_SCHEDULERS for scheduler in schedulers)
    return {
        "cpu": hasattr(os, "setpriority"),
        "ionice": bool(ionice),
        "io_supported": bool(ionice) and supported is True,
        "io_known": supported is not None,
        "schedulers": schedulers,
    }


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


def run_extractor(cmd, cancel=None, progress_cb=None, low_priority=False,
                  priority_path=None):
    """Return exit code, bounded diagnostics, and whether cancellation killed the process."""
    if cancel is not None and cancel.is_set():
        return 255, "Extraction cancelled", True
    tool = cmd[0]
    from Utils.downloads.resources import current_resources
    resources = current_resources()
    priority = low_priority_support(priority_path) if low_priority else {}
    io_native = bool(priority.get("io_supported"))
    io_unknown = bool(priority.get("ionice") and not priority.get("io_known"))
    if low_priority and (io_native or io_unknown):
        cmd = ["ionice", "-c3", *cmd]
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as exc:
        return 127, f"{tool}: {type(exc).__name__}: {exc}", False
    cpu_priority_applied = False
    if low_priority:
        try:
            os.setpriority(os.PRIO_PROCESS, proc.pid, 19)
            cpu_priority_applied = os.getpriority(os.PRIO_PROCESS, proc.pid) == 19
        except (OSError, AttributeError):
            pass
    if resources is not None:
        resources.emit("install.extractor.started", tool=tool, pid=proc.pid,
                       thread_option=next((arg for arg in cmd if arg.startswith("-mmt=")), ""),
                       low_priority=low_priority,
                       cpu_priority_applied=cpu_priority_applied,
                       io_priority_requested=bool(low_priority and (io_native or io_unknown)),
                       io_priority_supported=io_native,
                       io_scheduler=",".join(priority.get("schedulers", ())),
                       adaptive_io_fallback=bool(low_priority and not io_native))
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
                should_pause = resources.pause_extractor(
                    low_priority=low_priority,
                    io_fallback=bool(low_priority and not io_native))
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


def run_python_extractor(kind, archive, target, cancel=None, *, low_priority=False):
    return run_extractor(
        [sys.executable, "-m", "Utils.archives.process", "--extract", kind,
         os.fspath(archive), os.fspath(target)],
        cancel, low_priority=low_priority, priority_path=target)


def _python_extract(kind, archive, target):
    target = Path(target)
    if kind == "zip":
        import zipfile
        with zipfile.ZipFile(archive, "r") as source:
            for item in source.infolist():
                extracted = source.extract(item, target)
                mode = (item.external_attr >> 16) & 0o777
                if mode:
                    try:
                        os.chmod(extracted, mode)
                    except OSError:
                        pass
        return
    if kind == "tar":
        import tarfile
        with tarfile.open(archive, "r:*") as source:
            source.extractall(target, filter="data")
        return
    if kind == "tar-zst":
        import tarfile
        try:
            from compression import zstd
        except ImportError:
            from backports import zstd
        with zstd.open(archive, "rb") as stream, \
                tarfile.open(fileobj=stream, mode="r|") as source:
            source.extractall(target, filter="data")
        return
    if kind == "7z":
        import py7zr
        with py7zr.SevenZipFile(archive, "r") as source:
            source.extractall(target)
        return
    raise ValueError(f"Unsupported Python extraction format: {kind}")


if __name__ == "__main__":
    try:
        if len(sys.argv) != 5 or sys.argv[1] != "--extract":
            raise ValueError("Invalid extraction worker arguments")
        _python_extract(sys.argv[2], sys.argv[3], sys.argv[4])
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
