"""Path rules shared by catalog ingestion, install, and game adapters.

This module deliberately has no dependency on the retired text-map resolver.
"""

from __future__ import annotations

import fnmatch
import os
import re
from functools import lru_cache
from pathlib import Path


EXCLUDE_NAMES = frozenset({
    "meta.ini", ".mm_overwrite_log.txt", ".mm_merge_inventory.xml",
})

CASING_UPPER = "upper"
CASING_LOWER = "lower"
CASING_FORCE_LOWER = "force_lower"
CASING_FORCE_UPPER = "force_upper"
_VALID_CASINGS = frozenset({
    CASING_UPPER, CASING_LOWER, CASING_FORCE_LOWER, CASING_FORCE_UPPER,
})


def is_macos_junk(name: str) -> bool:
    return name.startswith("._") or name in {".DS_Store", "__MACOSX"}


def is_utf8_safe(value: str) -> bool:
    try:
        value.encode("utf-8")
        return True
    except UnicodeEncodeError:
        return False


def repair_nonutf8_names(root: Path | str, log_fn=None) -> int:
    root = Path(root)
    try:
        offenders = [path for path in root.rglob("*")
                     if not is_utf8_safe(path.name)]
    except OSError:
        return 0
    renamed = 0
    for entry in sorted(offenders, key=lambda path: len(path.parts), reverse=True):
        try:
            raw = entry.name.encode("utf-8", "surrogateescape")
        except Exception:
            continue
        fixed = None
        for encoding in ("cp1252", "cp437"):
            try:
                fixed = raw.decode(encoding)
                fixed.encode("utf-8")
                break
            except (UnicodeDecodeError, UnicodeEncodeError):
                fixed = None
        fixed = fixed or raw.decode("utf-8", "backslashreplace")
        target = entry.parent / fixed
        try:
            if fixed == entry.name or target.exists():
                continue
            os.rename(os.fsencode(entry), os.fsencode(target))
            renamed += 1
        except OSError:
            continue
    if renamed and log_fn is not None:
        log_fn(
            f"Repaired {renamed} non-UTF-8 file name(s) on disk so the "
            "catalog can preserve them safely."
        )
    return renamed


def index_key_for_raw(
    raw_key: str,
    strip_prefixes=None,
    strip_path_prefixes: list[str] | None = None,
) -> str:
    relative = raw_key.replace("\\", "/")
    lower = relative.lower()
    for prefix in sorted(strip_path_prefixes or (), key=len, reverse=True):
        prefix_lower = prefix.lower().strip("/")
        if lower == prefix_lower or lower.startswith(prefix_lower + "/"):
            relative = relative[len(prefix.strip("/")):].lstrip("/")
            break
    strips = {str(value).lower() for value in (strip_prefixes or ())}
    while strips and "/" in relative:
        head, tail = relative.split("/", 1)
        if head.lower() not in strips:
            break
        relative = tail
    return relative.lower()


def mod_strip_args(
    mod_name: str,
    strip_prefixes=None,
    per_mod_strip_prefixes: dict | None = None,
    root_folder_mods=None,
) -> tuple[frozenset[str], list[str]]:
    if root_folder_mods and mod_name in root_folder_mods:
        return frozenset(), []
    base = frozenset(str(value).lower() for value in (strip_prefixes or ()))
    configured = (per_mod_strip_prefixes or {}).get(mod_name) or ()
    segments = frozenset(
        str(value).lower() for value in configured if "/" not in str(value))
    paths = [str(value) for value in configured if "/" in str(value)]
    return base | segments, paths


def scan_dir(
    source_name: str,
    source_dir: str,
    strip_prefixes: frozenset[str] = frozenset(),
    allowed_extensions: frozenset[str] = frozenset(),
    _unused_root_deploy_folders: frozenset[str] = frozenset(),
    strip_path_prefixes: list[str] | None = None,
    exclude_dirs: frozenset[str] = frozenset(),
) -> tuple[str, dict[str, str], dict[str, str], list[str]]:
    """Compatibility-shaped raw scan used by per-file rule editors only."""
    del _unused_root_deploy_folders
    result: dict[str, str] = {}
    invalid: list[str] = []
    path_prefixes = sorted(
        ((value.lower(), len(value)) for value in strip_path_prefixes or ()),
        key=lambda value: -value[1],
    )
    stack = [("", source_dir)]
    while stack:
        prefix, current = stack.pop()
        try:
            iterator = os.scandir(current)
        except OSError:
            continue
        with iterator:
            for entry in iterator:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if (entry.name.lower() in exclude_dirs
                                or is_macos_junk(entry.name)
                                or entry.name.startswith("prefix_")
                                or entry.name == ".mm_bundle"):
                            continue
                        if not is_utf8_safe(entry.name):
                            invalid.append(prefix + entry.name + "/")
                            continue
                        stack.append((prefix + entry.name + "/", entry.path))
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    if entry.name in EXCLUDE_NAMES or is_macos_junk(entry.name):
                        continue
                    if not is_utf8_safe(entry.name):
                        invalid.append(prefix + entry.name)
                        continue
                    relative = prefix + entry.name
                    lower = relative.lower()
                    for path_lower, path_length in path_prefixes:
                        if lower == path_lower or lower.startswith(path_lower + "/"):
                            relative = relative[path_length:].lstrip("/")
                            break
                    while strip_prefixes and "/" in relative:
                        head, tail = relative.split("/", 1)
                        if head.lower() not in strip_prefixes:
                            break
                        relative = tail
                    if allowed_extensions and not any(
                        entry.name.lower().endswith(extension)
                        and len(entry.name) > len(extension)
                        for extension in allowed_extensions
                    ):
                        continue
                    key = relative.lower()
                    previous = result.get(key)
                    relative_slash = relative.rfind("/")
                    previous_slash = previous.rfind("/") if previous else -1
                    relative_folders = (
                        relative[:relative_slash] if relative_slash >= 0 else "")
                    previous_folders = (
                        previous[:previous_slash] if previous_slash >= 0 else "")
                    if previous is None or _upper_count(
                            relative_folders) > _upper_count(previous_folders):
                        result[key] = relative
                except OSError:
                    continue
    return source_name, result, {}, invalid


@lru_cache(maxsize=2048)
def _upper_count(value: str) -> int:
    return sum(character.isupper() for character in value)


def _canonical_segment(first: str, second: str, strategy: str) -> str:
    if strategy == CASING_LOWER:
        return first if _upper_count(first) <= _upper_count(second) else second
    return first if _upper_count(first) >= _upper_count(second) else second


def canonicalize_dir_casing(
    relative_paths: list[str],
    strategy: str = CASING_UPPER,
    pins: dict[str, str] | None = None,
) -> dict[str, str]:
    if strategy == CASING_FORCE_LOWER:
        strategy = CASING_LOWER
    elif strategy == CASING_FORCE_UPPER:
        strategy = CASING_UPPER
    elif strategy not in _VALID_CASINGS:
        strategy = CASING_UPPER
    canonical: dict[tuple[str, str], str] = {}
    for relative in relative_paths:
        parent = ""
        for segment in relative.rsplit("/", 1)[0].split("/") \
                if "/" in relative else ():
            key = (parent, segment.lower())
            current = canonical.get(key)
            canonical[key] = segment if current is None else _canonical_segment(
                current, segment, strategy)
            parent += segment.lower() + "/"
    result = {}
    for relative in relative_paths:
        if "/" not in relative:
            result[relative] = relative
            continue
        parts = relative.split("/")
        parent = ""
        for index, segment in enumerate(parts[:-1]):
            replacement = canonical.get((parent, segment.lower()), segment)
            if pins:
                replacement = pins.get(replacement.lower(), replacement)
            parts[index] = replacement
            parent += segment.lower() + "/"
        result[relative] = "/".join(parts)
    return result


class PathFilters:
    __slots__ = (
        "ignore_re", "loose_excl_re", "allowed_top", "excluded",
        "folder_ignore_re", "allowed_top_exempt", "_dir_cache",
    )

    def __init__(self, ignore_re, loose_excl_re, allowed_top, excluded,
                 folder_ignore_re=None, allowed_top_exempt=frozenset()):
        self.ignore_re = ignore_re
        self.loose_excl_re = loose_excl_re
        self.allowed_top = allowed_top
        self.excluded = excluded
        self.folder_ignore_re = folder_ignore_re
        self.allowed_top_exempt = allowed_top_exempt
        self._dir_cache = {}

    def accepts(self, mod: str, relative_key: str) -> bool:
        if relative_key in self.excluded.get(mod, ()):
            return False
        if (self.loose_excl_re is not None and "/" not in relative_key
                and self.loose_excl_re.match(relative_key)):
            return False
        if self.allowed_top is not None and mod not in self.allowed_top_exempt:
            slash = relative_key.find("/")
            if slash >= 0 and relative_key[:slash] not in self.allowed_top:
                return False
        if (self.ignore_re is not None
                and self.ignore_re.match(relative_key.rsplit("/", 1)[-1])):
            return False
        if self.folder_ignore_re is not None and "/" in relative_key:
            directory = relative_key.rsplit("/", 1)[0]
            ignored = self._dir_cache.get(directory)
            if ignored is None:
                ignored = any(
                    self.folder_ignore_re.match(segment)
                    for segment in directory.split("/"))
                self._dir_cache[directory] = ignored
            if ignored:
                return False
        return True


def build_path_filters(
    conflict_ignore_filenames: set[str] | None,
    excluded_loose_filenames: set[str] | None,
    allowed_top_level_folders: set[str] | None,
    excluded_mod_files: dict[str, set[str]] | None,
    conflict_ignore_foldernames: set[str] | None = None,
    allowed_top_level_exempt_mods: set[str] | None = None,
) -> PathFilters:
    def compile_patterns(patterns, expand_extensionless=False):
        translated = []
        for pattern in patterns or ():
            lower = pattern.lower()
            translated.append(fnmatch.translate(lower))
            if (expand_extensionless and lower.endswith(".*")
                    and "*" not in lower[:-2] and "?" not in lower[:-2]):
                translated.append(fnmatch.translate(lower[:-2]))
        return re.compile("|".join(translated)) if translated else None

    return PathFilters(
        compile_patterns(conflict_ignore_filenames, True),
        compile_patterns(excluded_loose_filenames),
        ({value.lower() for value in allowed_top_level_folders}
         if allowed_top_level_folders else None),
        excluded_mod_files or {},
        compile_patterns(conflict_ignore_foldernames),
        frozenset(allowed_top_level_exempt_mods or ()),
    )


# Transitional aliases used by callers while the old resolver remains a test
# oracle. New code should prefer the public names above.
_EXCLUDE_NAMES = EXCLUDE_NAMES
_is_macos_junk = is_macos_junk
_scan_dir = scan_dir
_build_path_filters = build_path_filters
