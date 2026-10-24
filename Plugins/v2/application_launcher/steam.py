from __future__ import annotations

import fcntl
import os
from pathlib import Path
import re
import shlex
import struct
import sys
from typing import NamedTuple
import zlib

from Utils.atomic_write import write_atomic, write_atomic_text
from Utils.launchers.steam import _STEAM_CANDIDATES, steam_client_running
from .i18n import tr


class Account(NamedTuple):
    root: Path
    user_id: str
    name: str
    recent: bool
    flatpak: bool

    @property
    def label(self):
        install = "Flatpak" if self.flatpak else tr('Native')
        return f"{self.name} ({self.user_id}) · {install}"


def _login_users(path):
    tokens = iter(re.findall(r'"(?:\\.|[^"\\])*"|[{}]|//[^\n]*', path.read_text(encoding="utf-8")))
    def parse(nested=False, depth=0):
        if depth > 8:
            raise ValueError(tr('Steam account data is nested too deeply.'))
        result = {}
        for key in tokens:
            if key.startswith("//"):
                continue
            if key == "}" and nested:
                return result
            if not key.startswith('"'):
                raise ValueError(tr('Invalid Steam account data.'))
            value = next(tokens)
            while value.startswith("//"):
                value = next(tokens)
            decode = lambda text: re.sub(r'\\(["\\nt])', lambda m: {"n": "\n", "t": "\t"}.get(m[1], m[1]), text[1:-1])
            result[decode(key).lower()] = parse(True, depth + 1) if value == "{" else decode(value)
        if nested:
            raise ValueError(tr('Incomplete Steam account data.'))
        return result
    return parse().get("users", {})


def accounts():
    result, seen = [], set()
    roots = [*_STEAM_CANDIDATES, Path.home() / ".var/app/com.valvesoftware.Steam/.steam/steam"]
    for candidate in roots:
        try:
            root = candidate.resolve()
            if root in seen:
                continue
            seen.add(root)
            users = sorted((root / "userdata").iterdir())
        except OSError:
            continue
        try:
            names = _login_users(root / "config/loginusers.vdf")
            names = {str(int(key) & 0xFFFFFFFF): value for key, value in names.items()
                     if key.isdigit() and isinstance(value, dict)}
        except (OSError, UnicodeError, ValueError, StopIteration, AttributeError):
            names = {}
        flatpak = "com.valvesoftware.Steam" in root.parts
        for user in users:
            if not user.name.isdigit() or not 0 < int(user.name) <= 0xFFFFFFFF or not user.is_dir():
                continue
            info = names.get(user.name, {})
            name = info.get("personaname") or info.get("accountname") or user.name
            result.append(Account(root, user.name, name, info.get("mostrecent") == "1", flatpak))
    return sorted(result, key=lambda account: (not account.recent, account.name.casefold(), str(account.root)))


class Node(NamedTuple):
    key: str
    value: object
    raw: bytes


def _parse(data, pos=0, depth=0):
    if depth > 8:
        raise ValueError(tr('Steam shortcuts are nested too deeply.'))
    nodes = []
    while pos < len(data):
        start = pos
        kind = data[pos]
        pos += 1
        if kind == 8:
            return nodes, pos
        end = data.index(b"\0", pos)
        key = data[pos:end].decode("utf-8", "surrogateescape")
        pos = end + 1
        if kind == 0:
            value, pos = _parse(data, pos, depth + 1)
        elif kind == 1:
            end = data.index(b"\0", pos)
            value = data[pos:end].decode("utf-8", "surrogateescape")
            pos = end + 1
        elif kind in (2, 3, 4, 6, 7, 10):
            size = 8 if kind in (7, 10) else 4
            if pos + size > len(data):
                raise ValueError(tr('Incomplete Steam shortcut value.'))
            value = struct.unpack_from("<i", data, pos)[0] if kind == 2 else data[pos:pos + size]
            pos += size
        elif kind == 5:
            end = pos
            while end + 2 <= len(data) and data[end:end + 2] != b"\0\0":
                end += 2
            if end + 2 > len(data):
                raise ValueError(tr('Incomplete Steam shortcut string.'))
            value = data[pos:end]
            pos = end + 2
        else:
            raise ValueError(tr('Unsupported Steam shortcut value type: {kind}.', kind=kind))
        nodes.append(Node(key, value, data[start:pos]))
    raise ValueError(tr('Incomplete Steam shortcuts file.'))


def _get(nodes, key, default=None):
    return next((node.value for node in nodes if node.key.lower() == key.lower()), default)


def _encode(key, value):
    key = key.encode("utf-8") + b"\0"
    if isinstance(value, dict):
        return b"\0" + key + b"".join(_encode(k, v) for k, v in value.items()) + b"\x08"
    if isinstance(value, int):
        return b"\x02" + key + struct.pack("<I", value & 0xFFFFFFFF)
    if "\0" in value:
        raise ValueError(tr('Steam shortcut text cannot contain NUL characters.'))
    return b"\x01" + key + value.encode("utf-8") + b"\0"


def _object(key, nodes):
    return b"\0" + key.encode("utf-8", "surrogateescape") + b"\0" + b"".join(node.raw for node in nodes) + b"\x08"


def _runner(context, entry_id):
    from Utils.config_paths import cli_invocation
    root = Path(context["plugin_dir"])
    runner = str(root / "steam_runner.py")
    invocation = cli_invocation()
    app_id = os.environ.get("FLATPAK_ID")
    if app_id:
        command = ["/usr/bin/flatpak", "run", "--command=python3", app_id, runner, entry_id,
                   "--", *context["headless_command"]]
    else:
        python = "/usr/bin/python3" if invocation == [os.environ.get("APPIMAGE")] else sys.executable
        if not Path(python).is_file():
            raise ValueError(tr('Steam shortcuts need Python 3 on the host system.'))
        command = [python, runner, entry_id, "--", *invocation, "headless", "--stdio"]
    config_home = str(root.parent.parent.parent)
    command = ["/usr/bin/env", "-u", "LD_LIBRARY_PATH", "-u", "LD_PRELOAD", "-u", "LD_AUDIT",
               f"XDG_CONFIG_HOME={config_home}", *command]
    return "#!/bin/sh\nexec " + shlex.join(command) + "\n"


def add_shortcut(context, entry, account):
    if not entry["installed"]:
        raise ValueError(tr('Choose the installed application EXE before adding a Steam shortcut.'))
    if account not in accounts():
        raise ValueError(tr('This Steam account is no longer available. Refresh and try again.'))
    if steam_client_running():
        raise ValueError(tr('Exit Steam completely, then add the shortcut again.'))
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", entry["id"]):
        raise ValueError(tr('Invalid application ID.'))
    root = Path(context["plugin_dir"])
    wrapper = root / "shortcuts" / (entry["id"] + ".sh")
    script = _runner(context, entry["id"])
    config = account.root / "userdata" / account.user_id / "config"
    path = config / "shortcuts.vdf"
    config.mkdir(parents=True, exist_ok=True)
    with (config / ".amethyst-shortcuts.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            original = path.read_bytes()
        except FileNotFoundError:
            original = None
        try:
            nodes, pos = _parse(original) if original is not None else ([], 0)
            if original is not None and pos != len(original):
                raise ValueError(tr('Unexpected data after Steam shortcuts.'))
            shortcuts = _get(nodes, "shortcuts", [])
            if not isinstance(shortcuts, list) or any(not isinstance(n.value, list) or not n.key.isdigit() for n in shortcuts):
                raise ValueError(tr('Invalid Steam shortcuts list.'))
            if len({n.key for n in shortcuts}) != len(shortcuts):
                raise ValueError(tr('Duplicate Steam shortcut indices.'))
            if len([n for n in nodes if n.key.lower() == "shortcuts"]) > 1:
                raise ValueError(tr('Duplicate Steam shortcuts lists.'))
        except (ValueError, IndexError, struct.error) as exc:
            raise ValueError(tr('Cannot safely update {path}: {exc}', path=path, exc=exc)) from exc
        existing = next((n for n in shortcuts if _get(n.value, "ShortcutPath") == str(wrapper)
                         or _get(n.value, "Exe", "").strip('"') == str(wrapper)), None)
        if account.flatpak:
            exe = '"flatpak-spawn"'
            options = shlex.join(["--host", "/bin/sh", str(wrapper)])
        else:
            exe, options = '"' + str(wrapper) + '"', ""
        fields = {"AppName": entry["name"], "Exe": exe, "StartDir": '"/"',
                  "LaunchOptions": options, "ShortcutPath": str(wrapper)}
        appid = _get(existing.value, "appid") if existing else None
        if not isinstance(appid, int):
            appid = zlib.crc32((exe + entry["name"]).encode("utf-8")) | 0x80000000
            used = {_get(n.value, "appid", 0) & 0xFFFFFFFF for n in shortcuts}
            while appid in used:
                appid = ((appid + 1) & 0xFFFFFFFF) | 0x80000000
            fields["appid"] = appid
        appid &= 0xFFFFFFFF
        icon_path = config / "grid" / f"{appid}_icon.png"
        fields["icon"] = str(icon_path)
        if existing:
            lower = {key.lower() for key in fields}
            values = [n for n in existing.value if n.key.lower() not in lower]
            values += [Node(key, value, _encode(key, value)) for key, value in fields.items()]
            updated = Node(existing.key, values, _object(existing.key, values))
            shortcuts = [updated if n is existing else n for n in shortcuts]
        else:
            index = str(max((int(n.key) for n in shortcuts), default=-1) + 1)
            fields.update(IsHidden=0, AllowDesktopConfig=1, AllowOverlay=1, OpenVR=0, Devkit=0,
                          DevkitGameID="", LastPlayTime=0, FlatpakAppID="", tags={"0": "Amethyst"})
            shortcuts = [*shortcuts, Node(index, None, _encode(index, fields))]
        shortcut_node = Node("shortcuts", shortcuts, _object("shortcuts", shortcuts))
        updated_root = [shortcut_node if n.key.lower() == "shortcuts" else n for n in nodes]
        if not any(n.key.lower() == "shortcuts" for n in nodes):
            updated_root.append(shortcut_node)
        payload = b"".join(n.raw for n in updated_root) + b"\x08"
        write_atomic_text(wrapper, script)
        wrapper.chmod(0o755)
        _save_icon(entry, root, icon_path)
        if steam_client_running():
            raise ValueError(tr('Steam started while preparing the shortcut. Exit it and try again.'))
        if (path.read_bytes() if path.exists() else None) != original:
            raise ValueError(tr('Steam shortcuts changed. Try again after Steam has exited.'))
        if original is not None:
            write_atomic(path.with_name("shortcuts.vdf.amethyst.bak"), original)
        write_atomic(path, payload)
    return {"updated": existing is not None, "account": account.label, "path": str(path)}


def _save_icon(entry, root, destination):
    from PySide6.QtGui import QImage
    from Utils.executables.icon import extract_exe_icon
    try:
        image = QImage.fromData(extract_exe_icon(Path(entry["executable"])) or b"", "ICO")
    except Exception:
        image = QImage()
    if image.isNull():
        image = QImage(str(root / "icons/amethyst.png"))
    destination.parent.mkdir(parents=True, exist_ok=True)
    if image.isNull() or not image.save(str(destination), "PNG"):
        raise ValueError(tr('Could not save the Steam shortcut icon.'))
