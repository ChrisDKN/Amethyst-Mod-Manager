from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path


class WabbajackError(ValueError):
    pass


def relative_path(value: str) -> str:
    value = str(value).replace("\\", "/")
    parts = value.split("/")
    if (not value or value.startswith("/") or any(
            p in ("", ".", "..") or ":" in p or "\x00" in p for p in parts)):
        raise WabbajackError(f"Unsafe relative path: {value!r}")
    return value


def within(root: Path, relative: str) -> Path:
    target = root / relative_path(relative)
    if not target.resolve().is_relative_to(root.resolve()):
        raise WabbajackError(f"Path leaves its managed directory: {relative}")
    return target


def source_path(root: Path, relative: str, *, expected="", size=None, stop=None) -> Path:
    direct = within(root, relative)
    from .hashes import file_hash, canonical_hash
    if expected:
        expected = canonical_hash(expected)
    def matches(path):
        return (path.is_file() and (size is None or path.stat().st_size == size)
                and file_hash(path, stop) == expected)
    if direct.exists() and (not expected or matches(direct)):
        return direct
    candidates = source_candidates(root, relative)
    if expected:
        for candidate in sorted(candidates):
            if candidate != direct and matches(candidate):
                return candidate
        raise WabbajackError(f"No source matches the required hash: {relative}")
    if len(candidates) != 1:
        raise WabbajackError(f"Ambiguous source: {relative}")
    return candidates[0]


def source_candidates(root: Path, relative: str) -> list[Path]:
    candidates = [root]
    for part in relative_path(relative).split("/"):
        matches = []
        for base in candidates:
            if not base.is_dir():
                continue
            for path in base.iterdir():
                if path.name.casefold() == part.casefold():
                    matches.append(path)
                    if len(matches) > 256:
                        raise WabbajackError(f"Too many case variants for source: {relative}")
        candidates = matches
        if not candidates:
            raise WabbajackError(f"Missing or ambiguous source: {relative}")
        if any(not p.resolve().is_relative_to(root.resolve()) for p in candidates):
            raise WabbajackError(f"Source leaves its directory: {relative}")
    return sorted(candidates)


def cache_path(directory: Path, identity: str, name: str) -> Path:
    from Utils.atomic_write import filename_limit
    name = relative_path(name)
    if "/" in name:
        raise WabbajackError(f"Invalid archive filename: {name}")
    limit = filename_limit(directory) - len(identity) - 1 - len(".chunks")
    if len(os.fsencode(name)) > limit:
        suffix = Path(name).suffix
        if len(os.fsencode(suffix)) > 16:
            suffix = ""
        raw = os.fsencode(name)[:max(0, limit - len(os.fsencode(suffix)))]
        name = raw.decode("utf-8", "ignore") + suffix
    return directory / f"{identity}-{name}"


def auxiliary_path(path: Path, suffix: str) -> Path:
    import hashlib
    from Utils.atomic_write import filename_limit
    name = path.name + suffix
    if len(os.fsencode(name)) > filename_limit(path.parent):
        name = ".wj-" + hashlib.sha256(os.fsencode(path.name)).hexdigest()[:32] + suffix
    return path.with_name(name)


def path_limits(root: Path) -> tuple[int, int]:
    from Utils.atomic_write import filename_limit
    try:
        length = os.pathconf(existing_parent(root), "PC_PATH_MAX")
    except (OSError, ValueError):
        length = -1
    return filename_limit(root), length


@lru_cache(maxsize=8192)
def _component_lengths(relative):
    return tuple(len(os.fsencode(component)) for component in relative.split("/"))


def check_path_length(root: Path, relative: str, limits=None):
    name_max, path_max = limits or path_limits(root)
    relative = relative_path(relative)
    counts = _component_lengths(relative)
    for count in counts:
        if count > name_max:
            raise WabbajackError(f"{relative}: filename component needs {count} bytes; the filesystem at {existing_parent(root)} allows {name_max}. Choose a filesystem with longer filename support.")
    base = os.fsencode(root)
    length = sum(counts) + len(counts) - 1
    if base and base != b".":
        length += len(base) + int(not base.endswith(b"/"))
    if path_max > 0 and length >= path_max:
        raise WabbajackError(f"{root / relative}: path exceeds the filesystem's {path_max - 1}-byte limit. Choose a shorter installation path.")


def safe_name(name: str, limit=160) -> str:
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name).strip(" .")
    return name.encode("utf-8")[:limit].decode("utf-8", "ignore").rstrip(" .") or "Modlist"


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
