from __future__ import annotations

import sys
from Utils import json_limits as json
import os
import subprocess

from .catalog import discover, load_settings
from .downloads import matches, source_uri


FLAG = "--plugin-source"


def candidates(source, game_id=None):
    scheme = source_uri(source).scheme
    plugins, _ = discover(game_id)
    return [(plugin, provider) for plugin in plugins if load_settings(plugin).get("enabled") is not False
            for provider in plugin.download_providers if scheme in provider.protocols and matches(provider, source)]


def from_argv(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if FLAG in argv:
        index = argv.index(FLAG)
        if index + 1 < len(argv):
            try:
                source_uri(argv[index + 1])
                return argv[index + 1]
            except ValueError:
                return None
    for value in argv:
        if "://" in value:
            try:
                if candidates(value):
                    return value
            except ValueError:
                pass
    return None


def strip_argv(argv):
    result, skip = [], False
    for value in argv:
        if skip:
            skip = False
        elif value == FLAG:
            skip = True
        else:
            try:
                handled = "://" in value and bool(candidates(value))
            except ValueError:
                handled = False
            if not handled:
                result.append(value)
    return result


def handler(scheme):
    from Thunderstore.ror2mm_handler import Ror2mmHandler
    class PluginProtocolHandler(Ror2mmHandler):
        @classmethod
        def _desktop_contents(cls):
            return ("[Desktop Entry]\nType=Application\nName=Amethyst Mod Manager (Plugin source)\n"
                    f"Exec={cls._exec_command()}\nIcon=amethystmodmanager\nTerminal=false\nNoDisplay=true\n"
                    f"MimeType=x-scheme-handler/{cls._SCHEME};\nCategories=Game;\n")

        @classmethod
        def _patch_mimeapps_content(cls, content):
            key = f"x-scheme-handler/{cls._SCHEME}"
            previous, section = {}, ""
            for line in content.splitlines():
                if line.strip().startswith("["):
                    section = line.strip()
                elif line.strip().startswith(key + "="):
                    previous[section] = [value for value in line.split("=", 1)[1].split(";") if value and value != cls._DESKTOP_FILE_NAME]
            patched, section = [], ""
            for line in super()._patch_mimeapps_content(content).splitlines():
                if line.strip().startswith("["):
                    section = line.strip()
                elif line.startswith(key + "=") and previous.get(section):
                    line += ";" + ";".join(dict.fromkeys(previous[section]))
                patched.append(line)
            return "\n".join(patched) + "\n"

        @classmethod
        def register(cls):
            from Nexus.nxm_handler import NxmHandler
            from Utils.environment.xdg import host_env
            written = False
            for path in cls._desktop_paths():
                if "flatpak" in str(path) and not path.parent.exists():
                    continue
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(cls._desktop_contents(), encoding="utf-8")
                written = True
            if written:
                cls._write_mimeapps_association()
                flatpak = os.path.exists("/.flatpak-info")
                for tool, arguments in (("xdg-mime", ["default", cls._DESKTOP_FILE_NAME, f"x-scheme-handler/{cls._SCHEME}"]),
                                        ("update-desktop-database", [str(cls._desktop_paths()[0].parent)])):
                    command = NxmHandler._host_cmd(flatpak, tool)
                    if command:
                        subprocess.run([*command, *arguments], env=host_env(), capture_output=True, timeout=15, check=False)
            return written

        @classmethod
        def unregister(cls):
            from Nexus.nxm_handler import NxmHandler
            for path in NxmHandler._mimeapps_paths():
                if not path.is_file():
                    continue
                lines = []
                for line in path.read_text(encoding="utf-8").splitlines():
                    if line.strip().startswith(f"x-scheme-handler/{cls._SCHEME}="):
                        values = [value for value in line.split("=", 1)[1].split(";") if value and value != cls._DESKTOP_FILE_NAME]
                        if not values:
                            continue
                        line = f"x-scheme-handler/{cls._SCHEME}=" + ";".join(values)
                    lines.append(line)
                path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            for path in cls._desktop_paths():
                path.unlink(missing_ok=True)
    return type("DeclaredPluginProtocolHandler", (PluginProtocolHandler,), {
        "_SCHEME": scheme, "_FLAG": FLAG,
        "_DESKTOP_FILE_NAME": f"amethystmodmanager-plugin-{scheme}.desktop"})


def register_all(log):
    plugins, errors = discover(None)
    for error in errors:
        log(error)
    schemes = {scheme for plugin in plugins if load_settings(plugin).get("enabled") is not False
               for provider in plugin.download_providers for scheme in provider.protocols}
    from Utils.config_paths import get_config_dir
    from Utils.atomic_write import write_atomic_text
    path = get_config_dir() / "plugin_protocols.json"
    try:
        previous = json.read_json(path)
        if not isinstance(previous, list) or any(not isinstance(value, str) for value in previous):
            previous = []
    except (OSError, ValueError):
        previous = []
    import re
    for scheme in set(previous) - schemes:
        if re.fullmatch(r"[a-z][a-z0-9+.-]{0,31}", scheme):
            try:
                handler(scheme).unregister()
            except OSError as exc:
                log(f"Could not remove plugin protocol {scheme}: {exc}")
    for scheme in sorted(schemes):
        try:
            cls = handler(scheme)
            if not cls.registration_is_current():
                cls.register()
        except Exception as exc:
            log(f"Could not register plugin protocol {scheme}: {exc}")
    write_atomic_text(path, json.dumps(sorted(schemes)))
