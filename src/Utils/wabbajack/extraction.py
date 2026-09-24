from __future__ import annotations

import os
import stat
import shutil
import subprocess
import tarfile
import tempfile
import zipfile
from pathlib import Path

from Utils.archives.process import failure_kind, failure_message, run_extractor
from .paths import WabbajackError, check_tree, relative_path

MIB = 1024 * 1024
LARGE_BYTES = 1024 * MIB


class ExtractionFailure(WabbajackError):
    def __init__(self, message, *, retryable):
        super().__init__(message)
        self.retryable = retryable


def archive_entries(archive, stop=None):
    archive = Path(archive)
    rows = []
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as source:
            for item in source.infolist():
                if stat.S_ISLNK(item.external_attr >> 16) or item.flag_bits & 1:
                    raise WabbajackError("Links and encrypted files are not supported in setup archives")
                if "\x00" in item.orig_filename:
                    raise WabbajackError(f"Unsafe ZIP member name: {item.orig_filename!r}")
                rows.append((item.filename, item.file_size,
                             item.is_dir() or item.filename.endswith("\\")
                             or stat.S_ISDIR(item.external_attr >> 16) or bool(item.external_attr & 0x10)))
    elif tarfile.is_tarfile(archive):
        with tarfile.open(archive) as source:
            for item in source:
                if not item.isfile() and not item.isdir():
                    raise WabbajackError("Special files are not supported in setup archives")
                rows.append((item.name, item.size, item.isdir()))
    else:
        tool = next((shutil.which(n) for n in ("7zzs", "7zz", "7z", "7za") if shutil.which(n)), None)
        if not tool:
            raise WabbajackError("7-Zip is required to inspect this setup archive")
        result = subprocess.run([tool, "l", "-slt", "-ba", "-sccUTF-8", "--", str(archive)],
                                stdin=subprocess.DEVNULL, capture_output=True,
                                encoding="utf-8", errors="replace", timeout=120)
        if result.returncode:
            detail = failure_message(tool, result.returncode,
                                     result.stdout[-2000:] + result.stderr[-2000:])
            raise WabbajackError(f"Cannot inspect {archive.name}: {detail}")
        entry = {}
        for line in [*result.stdout.splitlines(), ""]:
            key, sep, value = line.partition(" = ")
            if sep:
                entry[key] = value
            elif not line.strip() and entry:
                if entry.get("Symbolic Link") or entry.get("Hard Link") or entry.get("Encrypted") == "+":
                    raise WabbajackError("Links and encrypted files are not supported in setup archives")
                if "Path" in entry:
                    rows.append((entry["Path"], int(entry.get("Size") or 0),
                                 entry.get("Folder") == "+" or entry.get("Attributes", "").startswith("D")))
                entry = {}
    found = {}
    for raw, size, directory in rows:
        if stop is not None and stop.is_set():
            raise InterruptedError("Archive inspection stopped")
        name = raw.replace("\\", "/").rstrip("/")
        if directory and name == "." and not size:
            continue
        name = relative_path(name)
        if size < 0 or (directory and size):
            raise WabbajackError(f"Invalid archive entry: {name}")
        previous = found.get(name.casefold())
        if previous and not (directory and previous[2] and previous[0] == name):
            raise WabbajackError(f"Conflicting Windows paths in archive: {name}")
        found[name.casefold()] = name, size, directory
    return list(found.values())


def extract_selected(tool, archive, target, names, stop, log, progress=None,
                     cpu_threads=None):
    from Utils.ui.config import load_extraction_settings
    settings = load_extraction_settings()
    threads = int(settings.get("cpu_threads", 0) or 0)
    if cpu_threads is not None:
        threads = min(threads or cpu_threads, cpu_threads)
    mmt = f"-mmt={threads}" if threads > 0 else "-mmt=on"
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent,
                                     prefix="members-", suffix=".txt") as listing:
        listing.write("\n".join(names) + "\n")
        listing.flush()
        code, error, killed = run_extractor(
            [tool, "x", f"-o{target}", "-y", mmt, "-bsp1",
             "-spd", "-sccUTF-8", "-scsUTF-8", f"-i@{listing.name}", "--", str(archive)],
            stop, progress_cb=progress,
            low_priority=bool(settings.get("low_priority", False)),
            priority_path=target)
    if killed or stop.is_set():
        raise InterruptedError("Installation stopped")
    if code:
        raise ExtractionFailure(f"Extraction failed: {archive.name}: {error}",
                                retryable=failure_kind(error, code, tool) == "archive")
    finish_extraction(target, stop, log)


def finish_extraction(root, stop=None, log=None):
    root = Path(root)
    fixed = 0
    backslashes = nonutf8 = False
    def inspect(path, directory):
        nonlocal fixed, backslashes, nonutf8
        if stop is not None and stop.is_set():
            raise InterruptedError("Installation stopped")
        info = path.lstat()
        if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
            raise WabbajackError(f"Extracted entry is a link or special file: {path}")
        want = 0o700 if directory else 0o600
        if info.st_mode & want != want:
            os.chmod(path, (info.st_mode & 0o7777) | want, follow_symlinks=False)
            fixed += 1
        if path != root:
            relative_path(path.relative_to(root).as_posix())
            backslashes |= "\\" in path.name
            try:
                path.name.encode("utf-8")
            except UnicodeEncodeError:
                nonutf8 = True
    def failed(exc):
        raise exc
    inspect(root, True)
    for parent, directories, files in os.walk(root, onerror=failed, followlinks=False):
        for name in directories:
            inspect(Path(parent) / name, True)
        for name in files:
            inspect(Path(parent) / name, False)
    if fixed and log:
        log(f"Repaired unreadable permissions on {fixed} extracted entries.")
    if backslashes or nonutf8:
        from Utils.mods.install import _debackslash_extracted_tree, _fix_nonutf8_names_extracted_tree
        if backslashes:
            _debackslash_extracted_tree(str(root), log)
        if nonutf8:
            _fix_nonutf8_names_extracted_tree(str(root), log)
        check_tree(root)
