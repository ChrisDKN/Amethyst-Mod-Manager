"""Reconstruct a collection's Replicate file layout from archive content."""

import hashlib
import os
import re
import shutil
import stat
import tempfile
import time
from pathlib import Path

from Utils.downloads.resources import time_phase


def validate_hashes(hashes) -> list[tuple[str, str]]:
    from Utils.mods.install import _normalise_relative_install_path

    if not isinstance(hashes, list):
        raise ValueError("Replicate hashes must be a list")
    result = {}
    for entry in hashes:
        if not isinstance(entry, dict):
            raise ValueError("Invalid Replicate file entry")
        path, digest = entry.get("path"), entry.get("md5")
        if not isinstance(path, str) or not isinstance(digest, str):
            raise ValueError("Replicate files require a path and MD5")
        path = _normalise_relative_install_path(
            path, label="Replicate destination", allow_empty=False)
        if path.endswith("/") or ":" in path:
            raise ValueError(f"Invalid Replicate file path: {path!r}")
        if path.casefold().startswith("meta.ini/"):
            raise ValueError("Replicate destination conflicts with mod metadata")
        digest = digest.lower()
        if not re.fullmatch(r"[0-9a-f]{32}", digest):
            raise ValueError(f"Invalid Replicate MD5 for {path!r}")
        key = path.casefold()
        previous = result.get(key)
        if previous is not None and previous[1] != digest:
            raise ValueError(f"Conflicting Replicate hashes for {path!r}")
        result.setdefault(key, (path, digest))
    for key in result:
        parent = key.rpartition("/")[0]
        while parent:
            if parent in result:
                raise ValueError(f"Replicate file is also a directory: {parent!r}")
            parent = parent.rpartition("/")[0]
    return list(result.values())


def _check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise InterruptedError("Replicate installation cancelled")


@time_phase("replicate")
def resolve_files(extract_dir, hashes, *, cancel=None, progress_fn=None):
    records = validate_hashes(hashes)
    wanted = {digest for _, digest in records}
    if not wanted:
        return []
    root = Path(extract_dir)
    names = {path.rsplit("/", 1)[-1].casefold() for path, _ in records}
    candidates = []
    for directory, dirs, files in os.walk(root, followlinks=False):
        _check_cancel(cancel)
        dirs[:] = sorted(d for d in dirs if not (Path(directory) / d).is_symlink())
        for name in sorted(files):
            path = Path(directory) / name
            info = path.lstat()
            if stat.S_ISREG(info.st_mode):
                candidates.append((path, info.st_size))
    candidates.sort(key=lambda item: (
        item[0].name.casefold() not in names, item[0].relative_to(root).as_posix()))
    total = sum(size for _, size in candidates)
    done = 0
    last_progress = 0.0
    sources = {}
    for path, _ in candidates:
        _check_cancel(cancel)
        digest = hashlib.md5()
        with path.open("rb") as stream:
            while chunk := stream.read(4 << 20):
                _check_cancel(cancel)
                digest.update(chunk)
                done += len(chunk)
                now = time.monotonic()
                if progress_fn is not None and now - last_progress >= 0.1:
                    progress_fn(done, total, "Matching collection files")
                    last_progress = now
        value = digest.hexdigest()
        if value in wanted:
            sources[value] = path.relative_to(root).as_posix()
            wanted.remove(value)
            if not wanted:
                break
    _check_cancel(cancel)
    if wanted:
        missing = [path for path, digest in records if digest in wanted]
        raise ValueError(
            f"Replicate could not match {len(missing)} file(s) in the archive: "
            + ", ".join(missing[:5]))
    if progress_fn is not None:
        progress_fn(total, total, "Matching collection files")
    return [(sources[digest], path, False) for path, digest in records]


def stage_files(file_list, extract_dir, dest_root, *, game=None,
                cancel=None, log_fn):
    from Utils.mods.install import _copy_file_list, _resolve_src_case

    _check_cancel(cancel)
    dest_root = Path(dest_root)
    if dest_root.is_symlink():
        raise ValueError(f"Replicate destination is a symlink: {dest_root}")
    dest_root.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".mm_replicate_", dir=dest_root.parent))
    pending, backup = temporary / "new", temporary / "old"
    preserve_backup = False
    try:
        pending.mkdir()
        # The mod's root meta.ini belongs to Amethyst and is regenerated below.
        content = [row for row in file_list if row[1].casefold() != "meta.ini"]
        _copy_file_list(content, str(extract_dir), pending, log_fn,
                        game=game, cancel=cancel)
        cache = {}
        seen_inodes = set()
        for source, destination, _ in content:
            _check_cancel(cancel)
            copied = _resolve_src_case(pending, destination, cache)
            source_size = (Path(extract_dir) / source).stat().st_size
            if not copied.is_file() or copied.stat().st_size != source_size:
                raise OSError(f"Replicate failed to stage {destination!r}")
            info = copied.stat()
            inode = (info.st_dev, info.st_ino)
            if inode in seen_inodes:
                # Identical outputs must remain independently editable.
                duplicate = temporary / "duplicate"
                shutil.copy2(copied, duplicate)
                duplicate.replace(copied)
            seen_inodes.add(inode)
        _check_cancel(cancel)
        if dest_root.exists():
            dest_root.rename(backup)
        try:
            pending.rename(dest_root)
        except BaseException:
            if backup.exists():
                try:
                    backup.rename(dest_root)
                except OSError as exc:
                    preserve_backup = True
                    raise OSError(f"Previous installation is preserved at {backup}") from exc
            raise
    finally:
        if not preserve_backup:
            try:
                shutil.rmtree(temporary)
            except OSError as exc:
                log_fn(f"Could not remove Replicate temporary folder '{temporary}': {exc}")
