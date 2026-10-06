"""Per-mod Vortex instruction routing and source identity preservation."""

from __future__ import annotations

import hashlib
import json
import os
from collections import OrderedDict
from contextlib import contextmanager
from threading import RLock
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath

from Utils.atomic_write import write_atomic, write_atomic_text

INSTRUCTIONS = "vortex_override_instructions.json"
SOURCE_MAP = ".amethyst_vortex_sources.json"
FLAG_VORTEX_ROUTE = 1 << 9
METADATA_NAMES = frozenset({INSTRUCTIONS, SOURCE_MAP})


def relative_path(value, *, empty=False):
    if not isinstance(value, str):
        raise ValueError("expected a relative path")
    value = value.replace("\\", "/")
    if (value.startswith("/") or PureWindowsPath(value).drive
            or ".." in value.split("/") or any(c in value for c in "\0\r\n")):
        raise ValueError("unsafe relative path")
    value = "/".join(part for part in value.split("/") if part and part != ".")
    if not value and not empty:
        raise ValueError("empty file path")
    return value


def mod_types(game):
    result = {"dinput": {"dest": ""}, "enb": {"dest": ""}}
    declared = getattr(game, "vortex_mod_types", {})
    if not isinstance(declared, dict):
        raise ValueError("vortex_mod_types must be an object")
    for name, spec in declared.items():
        if (not isinstance(name, str) or not name or not isinstance(spec, dict)
                or set(spec) - {"dest", "to_prefix"}
                or type(spec.get("to_prefix", False)) is not bool):
            raise ValueError("invalid Vortex mod-type destination")
        result[name] = {"dest": relative_path(spec.get("dest"), empty=True),
                        "to_prefix": spec.get("to_prefix", False)}
    return result


_discovery_cache = OrderedDict()
_discovery_lock = RLock()


def _directory_signature(path):
    stat = path.stat()
    return stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns


def metadata_paths(root):
    root = Path(root)
    with _discovery_lock:
        cached = _discovery_cache.get(root)
    if cached is not None:
        directories, found = cached
        try:
            if directories and all(_directory_signature(path) == signature
                   for path, signature in directories):
                return list(found)
        except OSError:
            pass
    if not root.is_dir():
        return []
    found, directories = [], []
    for directory, dirs, files in os.walk(root, followlinks=False):
        path = Path(directory)
        try:
            directories.append((path, _directory_signature(path)))
        except OSError:
            continue
        dirs[:] = [name for name in dirs if not name.startswith(".")
                   and not Path(directory, name).is_symlink()]
        matches = [Path(directory, name) for name in files
                   if name.lower() == INSTRUCTIONS]
        if path == root and matches:
            found = matches
            break
        found.extend(matches)
    found.sort()
    with _discovery_lock:
        _discovery_cache[root] = (directories, tuple(found))
        _discovery_cache.move_to_end(root)
        if len(_discovery_cache) > 4096:
            _discovery_cache.popitem(last=False)
    return found


@dataclass
class Instructions:
    fingerprint: str = ""
    base: str = ""
    copies: dict[str, str] = field(default_factory=dict)
    mod_type: str | None = None
    sources: dict[str, str] = field(default_factory=dict)
    valid: bool = False


def read_instructions(root, log_fn=None):
    root = Path(root)
    log = log_fn or (lambda _message: None)
    result = Instructions()
    paths = metadata_paths(root)
    if not paths:
        return result
    digest = hashlib.sha256()
    try:
        for path in paths:
            digest.update(path.relative_to(root).as_posix().encode())
            if not path.resolve().is_relative_to(root.resolve()):
                raise ValueError("instruction file escapes the mod directory")
            digest.update(path.read_bytes())
        if len(paths) != 1:
            raise ValueError("multiple instruction files")
        payload = paths[0].read_bytes()
        result.base = paths[0].parent.relative_to(root).as_posix()
        if result.base == ".":
            result.base = ""
        sidecar = root / SOURCE_MAP
        if sidecar.exists():
            if not sidecar.resolve().is_relative_to(root.resolve()):
                raise ValueError("source map escapes the mod directory")
            source_data = sidecar.read_bytes()
            digest.update(source_data)
            mapping = json.loads(source_data)
            if not isinstance(mapping, dict) or mapping.get("version") != 1:
                raise ValueError("invalid source map")
            result.base = relative_path(mapping.get("base", ""), empty=True)
            result.sources = {relative_path(k).lower(): relative_path(v)
                              for k, v in mapping["sources"].items()}
        rows = json.loads(payload.decode("utf-8-sig"))
        if not isinstance(rows, list):
            raise ValueError("instructions must be an array")
        for index, row in enumerate(rows, 1):
            try:
                if not isinstance(row, dict):
                    raise ValueError("instruction must be an object")
                kind = row.get("type")
                if kind == "copy":
                    source = relative_path(row.get("source"))
                    destination = relative_path(row.get("destination"))
                    if source.rsplit("/", 1)[-1].lower() in METADATA_NAMES:
                        continue
                    if destination.rsplit("/", 1)[-1].lower() in METADATA_NAMES:
                        raise ValueError("destination is reserved routing metadata")
                    result.copies[source.lower()] = destination
                elif kind == "setmodtype":
                    if not isinstance(row.get("value"), str):
                        raise ValueError("mod type must be a string")
                    result.mod_type = row["value"]
                else:
                    raise ValueError(f"unsupported instruction type {kind!r}")
            except ValueError as exc:
                log(f"Vortex instructions ({root.name}, entry {index}): {exc}; skipped.")
        result.valid = True
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        log(f"Vortex instructions ({root.name}): {exc}; using normal routing.")
    result.fingerprint = digest.hexdigest()
    return result


def resolve_routes(root, instructions, files, types, default_domain, log_fn=None):
    """Return raw-source-keyed (target, destination) pairs."""
    if not instructions.valid:
        return {}
    log = log_fn or (lambda _message: None)
    target, prefix = default_domain
    typed = instructions.mod_type not in (None, "")
    if typed:
        spec = types.get(instructions.mod_type)
        if spec is None:
            log(f"Vortex instructions ({Path(root).name}): unknown mod type "
                f"{instructions.mod_type!r}; using normal routing.")
            return {}
        target = "prefix" if spec.get("to_prefix") else "game"
        prefix = spec["dest"]
    paths = {}
    originals = {}
    actual_paths = {}
    base = instructions.base.lower().rstrip("/")
    for raw in files:
        raw = relative_path(raw)
        if raw.rsplit("/", 1)[-1].lower() in METADATA_NAMES:
            continue
        original = instructions.sources.get(raw.lower(), raw)
        logical = (original[len(base) + 1:] if base and
                   original.lower().startswith(base + "/") else original)
        originals[raw.lower()] = logical
        actual_paths[raw.lower()] = raw
        for key in {original.lower(), logical.lower()}:
            paths.setdefault(key, set()).add(raw)
    explicit = {}
    for source, destination in instructions.copies.items():
        matches = paths.get(source, set())
        if len(matches) != 1:
            log(f"Vortex instructions ({Path(root).name}): source {source!r} "
                f"{'is ambiguous' if matches else 'was not staged'}; skipped.")
            continue
        raw = next(iter(matches))
        if not (Path(root) / raw).resolve().is_relative_to(Path(root).resolve()):
            log(f"Vortex instructions ({Path(root).name}): source escapes mod directory; skipped.")
            continue
        explicit[raw.lower()] = destination
    result = {}
    for raw, logical in originals.items():
        destination = explicit.get(raw)
        if destination is None:
            if not typed:
                continue
            destination = logical
        actual = actual_paths[raw]
        if not (Path(root) / actual).resolve().is_relative_to(Path(root).resolve()):
            continue
        result[raw] = (target, f"{prefix}/{destination}" if prefix else destination)
    return result


def _write_sources(root, base, sources):
    write_atomic_text(Path(root) / SOURCE_MAP, json.dumps(
        {"version": 1, "base": base, "sources": sources}, sort_keys=True) + "\n")


def record_install_sources(source_root, destination_root, pairs, log_fn=None):
    source_root, destination_root = Path(source_root), Path(destination_root)
    paths = metadata_paths(source_root)
    if len(paths) != 1:
        for path in paths:
            if path.resolve().is_relative_to(source_root.resolve()):
                target = destination_root / path.relative_to(source_root)
                if not target.parent.resolve().is_relative_to(destination_root.resolve()):
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                write_atomic(target, path.read_bytes())
        if paths and log_fn is not None:
            log_fn(f"Vortex instructions ({destination_root.name}): "
                   "multiple instruction files; using normal routing.")
        return
    metadata = paths[0]
    if not metadata.resolve().is_relative_to(source_root.resolve()):
        return
    sources = {}
    for source, destination in pairs:
        if Path(destination).is_file():
            sources[Path(destination).relative_to(destination_root).as_posix()] = (
                Path(source).relative_to(source_root).as_posix())
    for path in metadata_paths(destination_root):
        if path != destination_root / INSTRUCTIONS:
            path.unlink()
    write_atomic(destination_root / INSTRUCTIONS, metadata.read_bytes())
    base = metadata.parent.relative_to(source_root).as_posix()
    base = "" if base == "." else base
    if base or any(source != destination for destination, source in sources.items()):
        _write_sources(destination_root, base, sources)
    else:
        (destination_root / SOURCE_MAP).unlink(missing_ok=True)


@contextmanager
def preserve_sources(root):
    root = Path(root)
    instructions = read_instructions(root)
    if not instructions.valid:
        yield
        return
    sources = {}
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [n for n in dirs if not n.startswith(".")
                   and not Path(directory, n).is_symlink()]
        for name in files:
            path = Path(directory, name)
            if name.lower() in METADATA_NAMES or path.is_symlink():
                continue
            stat = path.stat()
            rel = path.relative_to(root).as_posix()
            sources.setdefault((stat.st_dev, stat.st_ino), []).append(
                (rel, instructions.sources.get(rel.lower(), rel)))
    try:
        yield
    finally:
        mapped = {}
        for directory, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = [n for n in dirs if not n.startswith(".")
                       and not Path(directory, n).is_symlink()]
            for name in files:
                path = Path(directory, name)
                if name.lower() in METADATA_NAMES or path.is_symlink():
                    continue
                stat = path.stat()
                rel = path.relative_to(root).as_posix()
                old = sources.get((stat.st_dev, stat.st_ino), ())
                original = next((origin for prior, origin in old if prior == rel),
                                old[0][1] if len(old) == 1 else None)
                if original is not None:
                    mapped[rel] = original
        if root.is_dir() and mapped and (mapped != {k: k for k in mapped} or instructions.sources):
            _write_sources(root, instructions.base, mapped)
