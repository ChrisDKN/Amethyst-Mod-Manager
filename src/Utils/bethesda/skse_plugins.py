"""Read SKSE/F4SE DLL compatibility declarations without loading plugin code."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

_MAX_FILE = 256 * 1024 * 1024
_ADDRESS_LIBRARY = 1
_SIGNATURES = 2
_STRUCTS_POST_629 = 4
_NO_STRUCTS = 1
_ADDRESS_LIBRARY_V5 = 2
_POST_629 = 0x01062750
_POST_17 = 0x01070630
_FO4_NEXT_GEN = 0x010A3D40
_FO4_ANNIVERSARY = 0x010B0890

_GAME_EXTENDERS = {
    "skyrim_se": ("SKSE", "SkyrimSE.exe", "skse64_loader.exe"),
    "Fallout4": ("F4SE", "Fallout4.exe", "f4se_loader.exe"),
}


@dataclass(frozen=True, slots=True)
class PluginMetadata:
    machine: int
    timestamp: int
    has_version_data: bool = False
    independence: int = 0
    independence_ex: int = 0
    compatible_versions: tuple[int, ...] = ()
    minimum_extender: int = 0
    reserved_breaking: int = 0
    extender: str = "SKSE"
    has_query: bool = False


@dataclass(frozen=True, slots=True)
class CompatibilityIssue:
    dll: str
    runtime: str
    reason: str
    versions: tuple[str, ...] = ()
    extender: str = "SKSE"


def _signature(path: Path) -> tuple:
    info = path.stat()
    return (info.st_dev, info.st_ino, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def read_plugin_metadata(path: Path, extender: str = "SKSE") -> PluginMetadata | None:
    try:
        path = Path(path)
        return _read_plugin_cached(str(path), _signature(path), extender)
    except (OSError, ValueError):
        return None


@lru_cache(maxsize=2048)
def _read_plugin_cached(path: str, signature: tuple, extender: str) -> PluginMetadata | None:
    if not 0 < signature[2] <= _MAX_FILE:
        return None
    try:
        with open(path, "rb") as stream:
            data = stream.read(_MAX_FILE + 1)
        if len(data) != signature[2] or _signature(Path(path)) != signature:
            return None
        return _parse_plugin(data, extender)
    except (OSError, ValueError, struct.error, IndexError):
        return None


def _parse_plugin(data: bytes, extender: str = "SKSE") -> PluginMetadata | None:
    if extender not in {"SKSE", "F4SE"}:
        return None
    if len(data) < 64 or data[:2] != b"MZ":
        return None
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if pe + 24 > len(data) or data[pe:pe + 4] != b"PE\0\0":
        return None
    machine, sections, timestamp = struct.unpack_from("<HHI", data, pe + 4)
    opt_size, characteristics = struct.unpack_from("<HH", data, pe + 20)
    opt = pe + 24
    if not characteristics & 0x2000 or not 0 < sections <= 96:
        return None
    if opt + opt_size + sections * 40 > len(data) or opt_size < 64:
        return None
    magic = struct.unpack_from("<H", data, opt)[0]
    if magic == 0x20B:
        count_off, directory_off = 108, 112
    elif magic == 0x10B:
        count_off, directory_off = 92, 96
    else:
        return None
    if opt_size < directory_off + 8:
        return None
    if not struct.unpack_from("<I", data, opt + count_off)[0]:
        return None
    export_rva, export_size = struct.unpack_from("<II", data, opt + directory_off)
    if not export_rva or export_size < 40:
        return None
    headers_size = struct.unpack_from("<I", data, opt + 60)[0]
    ranges = []
    for index in range(sections):
        section = opt + opt_size + index * 40
        rva, size, raw = struct.unpack_from("<III", data, section + 12)
        ranges.append((rva, size, raw))

    def offset(rva, size):
        if rva < headers_size and rva + size <= min(headers_size, len(data)):
            return rva
        for base, length, raw in ranges:
            if base <= rva and rva + size <= base + length:
                result = raw + rva - base
                if result + size <= len(data):
                    return result
        raise ValueError("PE address outside file")

    export = offset(export_rva, 40)
    functions, names, functions_rva, names_rva, ordinals_rva = struct.unpack_from(
        "<IIIII", data, export + 20)
    if not 0 < names <= functions <= 65536:
        return None
    functions_off = offset(functions_rva, functions * 4)
    names_off = offset(names_rva, names * 4)
    ordinals_off = offset(ordinals_rva, names * 2)
    exports = {}
    prefix = extender.encode("ascii") + b"Plugin_"
    wanted = {prefix + suffix for suffix in (b"Version", b"Load", b"Query")}
    for index in range(names):
        name_rva = struct.unpack_from("<I", data, names_off + index * 4)[0]
        name_off = offset(name_rva, 1)
        end = data.find(b"\0", name_off, min(name_off + 128, len(data)))
        if end < 0 or data[name_off:end] not in wanted:
            continue
        ordinal = struct.unpack_from("<H", data, ordinals_off + index * 2)[0]
        if ordinal >= functions:
            raise ValueError("Invalid PE export ordinal")
        rva = struct.unpack_from("<I", data, functions_off + ordinal * 4)[0]
        if rva and not export_rva <= rva < export_rva + export_size:
            exports[data[name_off:end]] = rva
    if not exports:
        return None
    has_query = prefix + b"Query" in exports
    metadata = PluginMetadata(machine, timestamp, extender=extender, has_query=has_query)
    version_rva = exports.get(prefix + b"Version")
    if machine != 0x8664 or not version_rva:
        return metadata
    version = offset(version_rva, 0x45C if extender == "F4SE" else 0x350)
    if struct.unpack_from("<I", data, version)[0] != 1 or not data[version + 8]:
        return metadata
    if extender == "F4SE":
        independence, independence_ex = struct.unpack_from("<II", data, version + 0x208)
        versions_offset, minimum_offset = 0x210, 0x250
        reserved_breaking = struct.unpack_from("<I", data, version + 0x258)[0]
    else:
        independence_ex, independence = struct.unpack_from("<II", data, version + 0x304)
        versions_offset, minimum_offset = 0x30C, 0x34C
        reserved_breaking = 0
    versions = []
    for value in struct.unpack_from("<16I", data, version + versions_offset):
        if not value:
            break
        versions.append(value)
    return PluginMetadata(
        machine, timestamp, True, independence, independence_ex, tuple(versions),
        struct.unpack_from("<I", data, version + minimum_offset)[0],
        reserved_breaking, extender, has_query)


def _pack_version(version: str) -> int:
    try:
        values = tuple(int(part) for part in version.split("."))
        if len(values) == 3:
            values += (0,)
        if len(values) != 4 or any(value < 0 or value > limit
                                  for value, limit in zip(values, (255, 255, 4095, 15))):
            return 0
        return values[0] << 24 | values[1] << 16 | values[2] << 4 | values[3]
    except (ValueError, AttributeError):
        return 0


def _version_text(version: int) -> str:
    return f"{version >> 24}.{(version >> 16) & 255}.{(version >> 4) & 4095}"


def compatibility_issue(metadata: PluginMetadata, dll: str, runtime: int,
                        *, address_library: bool | None = None,
                        extender_version: int = 0) -> CompatibilityIssue | None:
    runtime_text = _version_text(runtime)

    def issue(reason, versions=()):
        return CompatibilityIssue(dll, runtime_text, reason, versions, metadata.extender)

    if metadata.machine == 0x14C:
        return issue("32bit")
    if metadata.machine != 0x8664:
        return None
    if metadata.extender == "SKSE" and runtime < 0x01060000:
        # Pre-AE SKSE calls Query; it does not read SKSEPlugin_Version.
        return None if metadata.has_query else issue("runtime")
    if not metadata.has_version_data:
        return None
    flags, extended = metadata.independence, metadata.independence_ex
    if metadata.extender == "F4SE":
        if flags & ~7 or extended & ~7:
            return None
        layout = (4 if runtime >= _FO4_ANNIVERSARY
                  else 2 if runtime >= _FO4_NEXT_GEN else 0)
        independent = bool(flags & (1 | layout) and extended & (1 | layout)
                           and not metadata.reserved_breaking)
        uses_library = bool(flags & layout)
    else:
        if flags & ~7 or extended & ~3:
            return None
        independent = bool(flags & (_ADDRESS_LIBRARY | _SIGNATURES))
        uses_library = bool(flags & _ADDRESS_LIBRARY)
        if (runtime >= _POST_17 and uses_library
                and not extended & _ADDRESS_LIBRARY_V5
                and 520128000 <= metadata.timestamp < 1748217600):
            independent = False
        if independent and runtime >= _POST_629 and not (
                flags & _STRUCTS_POST_629 or extended & _NO_STRUCTS):
            return issue("pre629")
        if independent and runtime < _POST_629 and flags & _STRUCTS_POST_629 \
                and not extended & _NO_STRUCTS:
            return issue("post629")
    # The low nibble identifies the storefront, not the EXE's file revision.
    if not independent and not any(
            (value & ~15) == (runtime & ~15) for value in metadata.compatible_versions):
        versions = tuple(dict.fromkeys(map(_version_text, metadata.compatible_versions)))
        return issue("runtime", versions)
    if uses_library and address_library is False:
        return issue("address_library")
    if extender_version and metadata.minimum_extender > extender_version:
        return issue("extender", (_version_text(metadata.minimum_extender),))
    return None


@lru_cache(maxsize=32)
def _exe_version_cached(path: str, signature: tuple) -> str:
    from Utils.executables.icon import extract_exe_version
    return extract_exe_version(Path(path))


def _exe_version(path: Path | None, *, extender: str = "") -> int:
    try:
        if path is None:
            return 0
        version = _exe_version_cached(str(path), _signature(path))
        if extender == "SKSE" and version.startswith("0.") and version.count(".") == 3:
            version = version[2:]
        return _pack_version(version)
    except OSError:
        return 0


def scan_script_extender_plugins(game, snapshot, mod_names, *, adapter, enabled_mods):
    config = _GAME_EXTENDERS.get(getattr(game, "game_id", ""))
    if config is None:
        return {}, set()
    extender, game_exe, loader_exe = config
    from Utils.games.frameworks import resolve_file_ci

    roots = {"[Overwrite]": adapter.overwrite, "[Root_Folder]": adapter.root_folder}

    def source(mod_name, relative):
        root = roots.get(mod_name)
        if root is None:
            root = adapter.staging / mod_name
        return root / bytes(relative).decode("utf-8", "surrogateescape")

    game_root = game.get_game_path()

    def effective_file(relative):
        winner = (snapshot.winner("game", relative, namespace="root")
                  or snapshot.winner("game", relative))
        if winner is not None:
            return source(winner.mod_name, winner.source_rel)
        return resolve_file_ci(game_root, Path(relative)) if game_root else None

    runtime = _exe_version(effective_file(game_exe))
    if not runtime:
        return {}, set()
    loader_version = _exe_version(effective_file(loader_exe), extender=extender)
    known_loader = (loader_version >> 24 == 0 if extender == "F4SE"
                    else 2 <= loader_version >> 24 <= 3)
    extender_version = loader_version if known_loader else 0
    names = set(mod_names) | set(roots)
    plugin_folder = f"{extender.lower()}/plugins"
    copies = snapshot.asset_copies(
        names, prefixes=(f"{plugin_folder}/", f"data/{plugin_folder}/"),
        extensions=(".dll", ".bin"))
    library_name = ("versionlib" if extender == "SKSE" and runtime >= 0x01060000 else "version")
    library_relative = f"Data/{extender}/Plugins/{library_name}-{_version_text(runtime).replace('.', '-')}-0.bin"
    library_winner = (snapshot.winner("game", library_relative, namespace="root")
                      or snapshot.winner("game", library_relative))
    staged_libraries = any(
        copy.provider_kind != "archive_member"
        and copy.legacy_rel.lower().removeprefix("data/")
        == library_relative.lower().removeprefix("data/")
        for copy in copies)
    if library_winner is not None:
        address_library = source(library_winner.mod_name, library_winner.source_rel).is_file()
    elif staged_libraries:
        address_library = False
    else:
        deployed = resolve_file_ci(game_root, Path(library_relative)) if game_root else None
        address_library = bool(deployed)
        if deployed and deployed.is_symlink():
            target = deployed.resolve()
            if any(target.is_relative_to(root.resolve())
                   for root in (adapter.staging, adapter.overwrite, adapter.root_folder)):
                address_library = False
    issues = {}
    plugin_mods = set()
    enabled_mods = set(enabled_mods) | set(roots)
    for copy in copies:
        if copy.provider_kind == "archive_member":
            continue
        relative = copy.legacy_rel.replace("\\", "/").lower().removeprefix("data/")
        if relative.rsplit("/", 1)[0] != plugin_folder:
            continue
        plugin_mods.add(copy.mod_name)
        if not relative.endswith(".dll"):
            continue
        if copy.mod_name in enabled_mods:
            if not copy.winning:
                continue
            winner = (snapshot.winner("game", f"Data/{relative}", namespace="root")
                      or snapshot.winner("game", f"Data/{relative}"))
            if winner is not None and (winner.mod_name != copy.mod_name
                                       or winner.source_rel != copy.source_rel):
                continue
        path = source(copy.mod_name, copy.source_rel)
        metadata = read_plugin_metadata(path, extender)
        if metadata is None:
            continue
        issue = compatibility_issue(
            metadata, path.name, runtime,
            address_library=address_library if copy.winning else None,
            extender_version=extender_version)
        if issue is not None:
            issues.setdefault(copy.mod_name, []).append(issue)
    return {name: tuple(values) for name, values in issues.items()}, plugin_mods
