from __future__ import annotations

import os
import re
from pathlib import Path, PurePosixPath


class WabbajackError(ValueError):
    pass


def relative_path(value: str) -> str:
    value = str(value).replace("\\", "/")
    parts = value.split("/")
    if (not value or value.startswith("/") or any(
            p in ("", ".", "..") or ":" in p or "\x00" in p for p in parts)):
        raise WabbajackError(f"Unsafe relative path: {value!r}")
    return str(PurePosixPath(value))


def within(root: Path, relative: str) -> Path:
    target = root / relative_path(relative)
    if not target.resolve().is_relative_to(root.resolve()):
        raise WabbajackError(f"Path leaves its managed directory: {relative}")
    return target


def source_path(root: Path, relative: str) -> Path:
    parts = relative_path(relative).split("/")
    direct = within(root, relative)
    if direct.exists():
        return direct
    candidates = [root]
    for part in parts:
        candidates = [p for base in candidates if base.is_dir() for p in base.iterdir()
                      if p.name.casefold() == part.casefold()]
        if not candidates or len(candidates) > 256:
            raise WabbajackError(f"Missing or ambiguous source: {relative}")
        if any(not p.resolve().is_relative_to(root.resolve()) for p in candidates):
            raise WabbajackError(f"Source leaves its directory: {relative}")
    if len(candidates) != 1:
        raise WabbajackError(f"Ambiguous source: {relative}")
    return candidates[0]


def safe_name(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name).strip(" .")
    return name[:160] or "Modlist"


def existing_parent(path: Path) -> Path:
    while not path.exists() and path != path.parent:
        path = path.parent
    return path


def check_tree(root: Path) -> None:
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(directory) / name
            if path.is_symlink():
                raise WabbajackError(f"Archive contains a symbolic link: {path.relative_to(root)}")
            within(root, path.relative_to(root).as_posix())
