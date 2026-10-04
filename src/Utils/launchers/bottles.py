"""Bottles library discovery and launcher commands."""

from __future__ import annotations

import json
import os
import shutil
import struct
from pathlib import Path
from typing import NamedTuple

from Utils.launchers.lutris import (
    _game_root_from_exe, _split_exe_rel_parts, _stored_exe_matches,
    parse_lutris_yaml,
)

BOTTLES_FLATPAK_ID = "com.usebottles.bottles"
BOTTLES_CPAK_ORIGIN = "github.com/bottlesdevs/bottles"
_HOME = Path.home()
_XDG_DATA = Path(os.environ.get("XDG_DATA_HOME", _HOME / ".local/share"))
_FLATPAK_APP = _HOME / ".var/app" / BOTTLES_FLATPAK_ID


class BottlesRoot(NamedTuple):
    data_dir: Path
    bottles_dir: Path
    is_flatpak: bool

    @property
    def package(self) -> str:
        if self.is_flatpak:
            return "flatpak"
        from Utils.ui.config import load_bottles_appimage_path
        appimage = _path(load_bottles_appimage_path())
        if appimage is not None and appimage.is_file():
            return "appimage"
        return "cpak" if _cpak_export().is_file() else "native"


class BottlesProgram(NamedTuple):
    root: BottlesRoot
    prefix: Path
    bottle: str
    name: str
    exe: Path
    program_id: str
    stored_id: str = ""


def _read_yaml(path: Path) -> dict:
    try:
        lines = []
        scalar_indent = None
        for line in path.read_text(encoding="utf-8").splitlines():
            value = line.strip()
            indent = len(line) - len(line.lstrip())
            if (scalar_indent is not None and indent > scalar_indent
                    and value and not value.startswith(("#", "- "))):
                if lines[-1].endswith("\\"):
                    lines[-1] = lines[-1][:-1] + value
                else:
                    lines[-1] += " " + value
                continue
            lines.append(line)
            _, sep, tail = value.partition(":")
            scalar_indent = indent if sep and tail.strip() else None
        return parse_lutris_yaml("\n".join(lines))
    except (OSError, UnicodeError):
        return {}


def _path(value) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return Path(value).expanduser()


def _resolved(path: Path) -> Path:
    try:
        return path.resolve()
    except (OSError, RuntimeError):
        return path


def find_bottles_roots() -> list[BottlesRoot]:
    roots = []
    for data in (_FLATPAK_APP / "data/bottles", _XDG_DATA / "bottles",
                 _HOME / ".local/share/bottles"):
        custom = _path(_read_yaml(data / "data.yml").get("custom_bottles_path"))
        folder = custom if custom is not None and custom.is_dir() else data / "bottles"
        roots.append(BottlesRoot(data, folder, data.is_relative_to(_FLATPAK_APP)))
    from Utils.ui.config import load_bottles_data_path
    custom = _path(load_bottles_data_path())
    if custom is not None:
        existing = next((r for r in roots if _resolved(custom) in
                         (_resolved(r.data_dir), _resolved(r.bottles_dir))), None)
        if existing is not None:
            roots.insert(0, existing)
        else:
            folder = custom / "bottles" if (custom / "bottles").is_dir() else custom
            data = (custom.parent if custom.name == "bottles"
                    and not (custom / "bottles").is_dir() else custom)
            roots.insert(0, BottlesRoot(data, folder, custom.is_relative_to(_FLATPAK_APP)))
    seen = set()
    found = []
    for root in roots:
        key = _resolved(root.bottles_dir)
        if key not in seen and root.bottles_dir.is_dir():
            seen.add(key)
            found.append(root)
    return found


def _iter_bottles():
    seen = set()
    for root in find_bottles_roots():
        try:
            folders = ([root.bottles_dir] if (root.bottles_dir / "bottle.yml").is_file()
                       else sorted(root.bottles_dir.iterdir()))
        except OSError:
            continue
        for folder in folders:
            target = _path(_read_yaml(folder / "placeholder.yml").get("Path"))
            prefix = target if target is not None else folder
            key = _resolved(prefix)
            if key in seen:
                continue
            config = _read_yaml(prefix / "bottle.yml")
            if not isinstance(config.get("Name"), str) or not config["Name"]:
                continue
            seen.add(key)
            yield root, prefix, config


def _program_exe(raw, prefix: Path, root: BottlesRoot) -> Path | None:
    if not isinstance(raw, str) or not raw:
        return None
    raw = raw.strip('"').replace("\\", "/")
    if len(raw) >= 3 and raw[1:3] == ":/":
        drive = raw[0].lower()
        base = prefix / "dosdevices" / f"{drive}:"
        if not base.exists():
            if drive == "c":
                base = prefix / "drive_c"
            elif drive == "z":
                base = Path("/")
            else:
                return None
        path = _resolved(base) / raw[3:]
    elif root.is_flatpak and raw.startswith("/var/data/"):
        path = root.data_dir.parent / raw[len("/var/data/"):]
    else:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = prefix / path
    if path.is_file():
        return path
    # Windows paths and Linux filenames can differ in case.
    current = Path(path.anchor)
    try:
        for part in path.parts[1:]:
            exact = current / part
            current = exact if exact.exists() else next(
                p for p in current.iterdir() if p.name.casefold() == part.casefold())
        return current if current.is_file() else None
    except (OSError, StopIteration):
        return None


def _shortcut_target(path: Path) -> str:
    try:
        data = path.read_bytes()
        if len(data) < 76 or data[:20] != bytes.fromhex(
                "4c0000000114020000000000c000000000000046"):
            return ""
        flags = struct.unpack_from("<I", data, 20)[0]
        pos = 76
        if flags & 1:
            pos += 2 + struct.unpack_from("<H", data, pos)[0]
        if not flags & 2:
            return ""
        size, header, info_flags = struct.unpack_from("<III", data, pos)
        if not info_flags & 1 or header < 28 or size < header or pos + size > len(data):
            return ""
        info = data[pos:pos + size]

        def string(offset, unicode=False):
            if not header <= offset < size:
                return ""
            if unicode:
                end = offset
                while end + 1 < size and info[end:end + 2] != b"\0\0":
                    end += 2
                return info[offset:end].decode("utf-16-le")
            return info[offset:].split(b"\0", 1)[0].decode("cp1252")

        base, suffix = "", ""
        if header >= 36:
            base_unicode, suffix_unicode = struct.unpack_from("<II", info, 28)
            base = string(base_unicode, True)
            suffix = string(suffix_unicode, True)
        if not base:
            base = string(struct.unpack_from("<I", info, 16)[0])
        if not suffix:
            suffix = string(struct.unpack_from("<I", info, 24)[0])
        if not base:
            return ""
        return base.rstrip("\\/") + ("\\" + suffix.lstrip("\\/") if suffix else "")
    except (OSError, ValueError, struct.error):
        return ""


def _iter_programs(bottles=None):
    for root, prefix, config in _iter_bottles() if bottles is None else bottles:
        programs = config.get("External_Programs", {})
        seen = set()
        if isinstance(programs, dict):
            for key, entry in programs.items():
                if not isinstance(entry, dict):
                    continue
                exe = _program_exe(entry.get("path"), prefix, root)
                name = entry.get("name")
                if exe is None or not isinstance(name, str) or not name:
                    continue
                seen.add(exe)
                identity = json.dumps([str(root.data_dir), str(prefix), str(key)], ensure_ascii=False)
                stored_id = str(entry.get("id") or key)
                yield BottlesProgram(root, prefix, config["Name"], name, exe, identity, stored_id)
        for pattern in ("drive_c/users/*/Desktop/*.lnk",
                        "drive_c/users/*/Start Menu/Programs/**/*.lnk",
                        "drive_c/users/*/AppData/Roaming/Microsoft/Windows/Start Menu/Programs/**/*.lnk",
                        "drive_c/ProgramData/Microsoft/Windows/Start Menu/Programs/**/*.lnk"):
            for shortcut in prefix.glob(pattern):
                target = _shortcut_target(shortcut)
                exe = _program_exe(target, prefix, root)
                if exe is None or exe in seen:
                    continue
                seen.add(exe)
                identity = json.dumps([str(root.data_dir), str(prefix), str(exe)], ensure_ascii=False)
                name = Path(target.replace("\\", "/")).stem
                yield BottlesProgram(root, prefix, config["Name"], name, exe, identity)


def build_installed_exe_index() -> list[list[str]]:
    return [_split_exe_rel_parts(str(p.exe)) for p in _iter_programs()]


def find_bottles_game_info_by_exe(exe_name: str, *, game_path=None, program_id=None,
                                 programs=None):
    parts = _split_exe_rel_parts(exe_name)
    if not parts:
        return None
    if program_id:
        program = find_bottles_launch_info([program_id])
        programs = [program] if program is not None else []
    for program in _iter_programs() if programs is None else programs:
        if program_id and program.program_id != program_id:
            continue
        if not _stored_exe_matches(str(program.exe), parts):
            continue
        install = _game_root_from_exe(program.exe, exe_name)
        if install is not None and (game_path is None or _resolved(install) == _resolved(Path(game_path))):
            return install, program.prefix, program.program_id
    return None


def find_bottles_game_info_by_exes(exe_names, *, game_path=None, program_id=None):
    if program_id:
        program = find_bottles_launch_info([program_id])
        programs = [program] if program is not None else []
    else:
        programs = list(_iter_programs())
    for name in exe_names:
        if name:
            info = find_bottles_game_info_by_exe(name, game_path=game_path, programs=programs)
            if info is not None:
                return *info, name
    return None


def find_bottles_programs_by_exes(exe_names, game_path=None) -> list[str]:
    prepared = [(name, _split_exe_rel_parts(name)) for name in exe_names if name]
    found = []
    for program in _iter_programs():
        for name, parts in prepared:
            if not _stored_exe_matches(str(program.exe), parts):
                continue
            install = _game_root_from_exe(program.exe, name)
            if install is not None and (game_path is None or _resolved(install) == _resolved(Path(game_path))):
                found.append(program.program_id)
                break
    return found


def find_bottles_launch_info(program_ids) -> BottlesProgram | None:
    roots = None
    for identity in program_ids:
        try:
            data, location, key = json.loads(identity)
            if not all(isinstance(value, str) and value for value in (data, location, key)):
                continue
            prefix = Path(location)
            if not Path(data).is_absolute() or not prefix.is_absolute():
                continue
        except (TypeError, ValueError):
            continue
        if roots is None:
            roots = find_bottles_roots()
        root = next((r for r in roots if _resolved(r.data_dir) == _resolved(Path(data))), None)
        if root is None:
            continue
        config = _read_yaml(prefix / "bottle.yml")
        if not isinstance(config.get("Name"), str) or not config["Name"]:
            continue
        programs = config.get("External_Programs", {})
        entry = programs.get(key) if isinstance(programs, dict) else None
        if isinstance(entry, dict):
            exe = _program_exe(entry.get("path"), prefix, root)
            name = entry.get("name")
            if exe is not None and isinstance(name, str) and name:
                stored_id = str(entry.get("id") or key)
                return BottlesProgram(root, prefix, config["Name"], name, exe, identity, stored_id)
        elif Path(key).is_absolute():
            exe = _program_exe(key, prefix, root)
            if exe is not None:
                return BottlesProgram(root, prefix, config["Name"], exe.stem, exe, identity)
    return None


def find_bottles_prefix(path: str | Path):
    prefix = _resolved(Path(path))
    roots = find_bottles_roots()
    for root in roots:
        if prefix.is_relative_to(_resolved(root.bottles_dir)):
            config = _read_yaml(prefix / "bottle.yml")
            if config.get("Name"):
                return root, prefix, config
    return next(((root, bottle, config) for root, bottle, config in _iter_bottles()
                 if _resolved(bottle) == prefix), None)


def is_bottles_prefix(path: str | Path) -> bool:
    return (Path(path) / "bottle.yml").is_file()


def find_bottles_runner_for_prefix(path: str | Path) -> Path | None:
    prefix = Path(path)
    config = _read_yaml(prefix / "bottle.yml")
    name = config.get("Runner", "")
    if not isinstance(name, str) or not name:
        return None
    candidates = []
    explicit = _path(config.get("RunnerPath"))
    if explicit is not None and explicit.is_absolute():
        candidates.append(explicit)
    if Path(name).is_absolute():
        candidates.append(Path(name))
    info = find_bottles_prefix(prefix)
    if info is not None:
        candidates.append(info[0].data_dir / "runners" / name)
    binaries = ("wine64", "wine") if config.get("Arch") == "win64" else ("wine",)
    for runner in candidates:
        for folder in ("bin", "files/bin", "dist/bin"):
            for binary in binaries:
                path = runner / folder / binary
                if path.is_file():
                    return path
    from Utils.launchers.steam import list_installed_proton
    for proton in list_installed_proton():
        if proton.parent.name == name:
            for folder in ("files/bin", "dist/bin"):
                for binary in binaries:
                    path = proton.parent / folder / binary
                    if path.is_file():
                        return path
    return None


def _cpak_export() -> Path:
    home = Path(os.environ.get("CPAK_INSTALLATION_PATH", _HOME / ".local/share/cpak"))
    exports = Path(os.environ.get("CPAK_EXPORTS_PATH", home / "exports"))
    return exports / BOTTLES_CPAK_ORIGIN / "bottles-cli"


def bottles_cli_commands(root: BottlesRoot, args) -> list[list[str]]:
    host = ["flatpak-spawn", "--host", "--directory=/"] if Path("/.flatpak-info").is_file() else []
    package = root.package
    if package == "flatpak":
        return [[*host, "flatpak", "run", "--command=bottles-cli", BOTTLES_FLATPAK_ID, *args]]
    from Utils.ui.config import load_bottles_appimage_path
    commands = []
    appimage = _path(load_bottles_appimage_path())
    if package == "appimage":
        return [[*host, str(appimage), *args]]
    if package == "cpak":
        return [[*host, str(_cpak_export()), *args]]
    cli = shutil.which("bottles-cli")
    if cli and not host:
        commands.append([cli, *args])
    else:
        local = _HOME / ".local/bin/bottles-cli"
        if local.is_file():
            commands.append([*host, str(local), *args])
        commands.append([*host, "bottles-cli", *args])
    return commands


def bottles_launch_commands(program: BottlesProgram) -> list[list[str]]:
    target = ["--program-id", program.stored_id] if program.stored_id else ["-e", str(program.exe)]
    return bottles_cli_commands(program.root, ["run", "-b", program.bottle, *target])
