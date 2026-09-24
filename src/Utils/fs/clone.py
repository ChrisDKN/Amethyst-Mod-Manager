"""Clone mod trees without following links or copying extended attributes."""

from __future__ import annotations

import os
import shutil
from pathlib import Path


def clone_tree_hardlinked(src: Path, dst: Path, *,
                          copy_exts: frozenset[str] = frozenset(),
                          link_files: bool = True) -> None:
    """Clone a real directory; hardlink files unless a copy is requested."""
    src = Path(src)
    dst = Path(dst)
    if src.is_symlink():
        raise ValueError(f"Clone source is a symbolic link: {src}")
    if not src.is_dir():
        raise NotADirectoryError(src)
    if dst.exists() or dst.is_symlink():
        raise FileExistsError(dst)

    src_root = src.resolve()
    stack = [(src, dst)]
    while stack:
        source_dir, target_dir = stack.pop()
        target_dir.mkdir(parents=True)
        with os.scandir(source_dir) as entries:
            for entry in entries:
                source = Path(entry.path)
                target = target_dir / entry.name
                if entry.is_symlink():
                    link = os.readlink(source)
                    resolved = Path(os.path.realpath(source))
                    if resolved.is_relative_to(src_root):
                        link = os.path.relpath(
                            dst / resolved.relative_to(src_root), target.parent)
                    os.symlink(link, target)
                elif entry.is_dir(follow_symlinks=False):
                    stack.append((source, target))
                elif not link_files or source.suffix.lower() in copy_exts:
                    _copy_file(source, target)
                else:
                    try:
                        os.link(source, target)
                    except OSError:
                        _copy_file(source, target)


def _copy_file(src: Path, dst: Path) -> None:
    stat = src.stat(follow_symlinks=False)
    shutil.copyfile(src, dst)
    shutil.copymode(src, dst)
    os.utime(dst, ns=(stat.st_atime_ns, stat.st_mtime_ns))
