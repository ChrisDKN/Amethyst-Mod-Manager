from __future__ import annotations

import os
import re
import stat
import struct
import tempfile
import zipfile
from pathlib import Path

from .paths import WabbajackError, check_tree, relative_path

MIB = 1024 * 1024
LARGE_BYTES = 1024 * MIB


def working_memory(methods, threads=2):
    dictionary = 0
    for method in methods:
        for value, unit in re.findall(
                r"(?:LZMA2?|PPMd|(?:Rar\d?|v\d+):m\d+):(\d+)([bkmg]?)", method, re.I):
            number = int(value)
            if unit:
                size = number * 1024 ** "bkmg".index(unit.lower())
            else:
                size = 1 << min(number, 40)
            dictionary = max(dictionary, size)
    return max(256 * MIB, 64 * MIB + dictionary * 2 * threads)


def zip_memory(source, items):
    dictionary = 0
    with open(source.filename, "rb") as stream:
        for item in items:
            if item.compress_type != zipfile.ZIP_LZMA:
                continue
            stream.seek(item.header_offset)
            header = stream.read(30)
            if len(header) != 30:
                raise zipfile.BadZipFile("Truncated ZIP local header")
            name_length, extra_length = struct.unpack_from("<HH", header, 26)
            stream.seek(name_length + extra_length, 1)
            properties = stream.read(9)
            if len(properties) == 9 and properties[2:4] == b"\x05\x00":
                dictionary = max(dictionary, struct.unpack_from("<I", properties, 5)[0])
            else:
                raise zipfile.BadZipFile("Invalid ZIP LZMA properties")
    return 64 * MIB + dictionary * 2


def extract_selected(tool, archive, target, names, stop, log, progress=None):
    from Utils.mods.install import _run_extractor_cancellable
    from Utils.ui.config import load_extraction_settings
    settings = load_extraction_settings()
    threads = min(2, int(settings.get("cpu_threads", 0) or 2))
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent,
                                     prefix="members-", suffix=".txt") as listing:
        listing.write("\n".join(names) + "\n")
        listing.flush()
        code, error, killed = _run_extractor_cancellable(
            [tool, "x", f"-o{target}", "-y", f"-mmt={threads}", "-bsp1",
             "-spd", "-scsUTF-8", f"-i@{listing.name}", "--", str(archive)],
            stop, progress_cb=progress,
            low_priority=bool(settings.get("low_priority", False)))
    if killed or stop.is_set():
        raise InterruptedError("Installation stopped")
    if code:
        raise WabbajackError(f"Extraction failed: {archive.name}: {error[:1000]}")
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
