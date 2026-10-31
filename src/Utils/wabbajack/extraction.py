from __future__ import annotations

import os
import stat
import shutil
import tarfile
import tempfile
import zipfile
from pathlib import Path

from Utils.archives.identify import TAR_TYPES, identify_archive, may_be_zip
from Utils.archives.process import failure_kind, run_extractor, run_listing
from .paths import WabbajackError, check_tree, relative_path

MIB = 1024 * 1024
LARGE_BYTES = 1024 * MIB


class ExtractionFailure(WabbajackError):
    def __init__(self, message, *, retryable):
        super().__init__(message)
        self.retryable = retryable


def rar_extraction_available(stop=None):
    if shutil.which("unrar") or shutil.which("bsdtar"):
        return True
    tool = next((path for name in ("7zzs", "7zz", "7z", "7za")
                 if (path := shutil.which(name))), None)
    if not tool:
        return False
    result = run_listing([tool, "i"], stop)
    return result.returncode == 0 and any(
        "Rar" in line.split() or "Rar5" in line.split()
        for line in result.stdout.splitlines())


def archive_entries(archive, stop=None):
    archive = Path(archive)
    if stop is not None and stop.is_set():
        raise InterruptedError("Archive inspection stopped")
    archive_type = identify_archive(archive)
    rows = []
    try:
        source = zipfile.ZipFile(archive) if may_be_zip(archive, archive_type) else None
    except zipfile.BadZipFile:
        source = None
    if source is not None:
        with source:
            for item in source.infolist():
                if stat.S_ISLNK(item.external_attr >> 16) or item.flag_bits & 1:
                    raise WabbajackError("Links and encrypted files are not supported in setup archives")
                if "\x00" in item.orig_filename:
                    raise WabbajackError(f"Unsafe ZIP member name: {item.orig_filename!r}")
                rows.append((item.filename, item.file_size,
                             item.is_dir() or item.filename.endswith("\\")
                             or stat.S_ISDIR(item.external_attr >> 16) or bool(item.external_attr & 0x10)))
    elif archive_type in TAR_TYPES and tarfile.is_tarfile(archive):
        with tarfile.open(archive) as source:
            for item in source:
                if stop is not None and stop.is_set():
                    raise InterruptedError("Archive inspection stopped")
                if not item.isfile() and not item.isdir():
                    raise WabbajackError("Special files are not supported in setup archives")
                rows.append((item.name, item.size, item.isdir()))
    else:
        from Utils.archives.listing import list_archive
        try:
            entries = list_archive(archive, stop)
        except RuntimeError as exc:
            raise WabbajackError(str(exc)) from exc
        for item in entries:
            if item.is_link or item.encrypted:
                raise WabbajackError("Links and encrypted files are not supported in setup archives")
            if item.size is None and not item.is_dir:
                raise WabbajackError(f"Unknown archive member size: {item.path}")
            rows.append((item.path, item.size or 0, item.is_dir))
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


def extract_rar_fallback(archive, target, stop, log, budget=None, progress=None,
                         *, members=None, resources=None, cpu_threads=None):
    from contextlib import nullcontext
    from Utils.mods.install import _extract_archive
    from .paths import within
    entries = archive_entries(archive, stop)
    files = {name.casefold(): (name, size) for name, size, directory in entries if not directory}
    missing = members - files.keys() if members is not None else set()
    if missing:
        raise WabbajackError(f"Archive {archive.name} is missing requested member: {min(missing)}")
    total = sum(size for _, size in files.values())
    if budget:
        budget(total)
    log(f"RAR fallback: extracting {archive.name} with full-archive space reserved.")
    errors = []
    with resources(256 * MIB, total) if resources else nullcontext():
        if not _extract_archive(str(archive), str(target), log, stop, errors,
                                progress_cb=(lambda pct: progress(pct, 100)) if progress else None,
                                cpu_threads=cpu_threads,
                                finalize=lambda folder: finish_extraction(folder, stop, log)):
            if stop.is_set():
                raise InterruptedError("Installation stopped")
            raise WabbajackError(
                f"RAR extraction failed: {archive.name}: {'; '.join(errors)}. "
                "Install RAR-capable 7-Zip, unrar or bsdtar and ensure it is on Amethyst's PATH.")
        for name, size in files.values():
            if stop.is_set():
                raise InterruptedError("Installation stopped")
            path = within(target, name)
            if not path.is_file() or path.stat().st_size != size:
                raise WabbajackError(f"RAR extraction produced a missing or incomplete member: {name}")


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
