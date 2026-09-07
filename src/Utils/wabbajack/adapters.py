from __future__ import annotations

import configparser
import zipfile
from dataclasses import dataclass

from .games import nexus_domain
from .manifest import qvalue, stock_folder
from .paths import WabbajackError

ROOT_FOLDERS = {"root", "game root", "game folder files", "root files"}
APPLICATION_FILES = {"modorganizer.exe", "modorganizer.ini", "nxmhandler.exe",
                     "nxmhandler.ini", "helper.exe", "uibase.dll", "portable.txt",
                     "categories.dat", "nexuscatmap.dat", "splash.png", "dump_running_process.bat",
                     "libcrypto-3-x64.dll", "libssl-3-x64.dll"}
APPLICATION_FOLDERS = {"dlls", "licenses", "loot", "platforms", "plugins", "qml",
                       "resources", "styles", "stylesheets", "translations", "tutorials"}
MANUAL_ROOT_FOLDERS = {"files requiring manual install", "root mods"}
STORE_ROOT_FOLDERS = {"gog or steam only files requiring manual install": "steam-gog",
                      "epic only files requiring manual install": "epic"}
ROOT_MOD_NAME = "Wabbajack Root Files"
_BETHESDA = {"morrowind", "oblivion", "skyrim", "skyrimspecialedition", "skyrimvr",
             "fallout3", "newvegas", "fallout4", "fallout76", "enderal", "enderalspecialedition", "starfield"}


@dataclass(frozen=True)
class GameAdapter:
    mo2: bool
    stock: str
    launch_files: frozenset[str]
    bethesda: bool = False
    keep: frozenset[str] = frozenset()
    store: str = ""

    def application_file(self, path):
        parts = path.casefold().split("/")
        first = parts[0]
        required_store = STORE_ROOT_FOLDERS.get(first.strip("_ "))
        if self.bethesda and required_store and required_store != self.store:
            return True
        if not self.mo2 or first in self.keep:
            return False
        return (first in APPLICATION_FILES or first in APPLICATION_FOLDERS
                or first.startswith(("qt5", "qt6", "qtwebengine", "usvfs_"))
                or (len(parts) == 1 and (first.endswith(".compiler_settings")
                    or first.endswith((" wj image.webp", " wj image.png", " wj image.jpg")))))

    def root_destination(self, path):
        parts = path.split("/")
        first = parts[0].casefold()
        if self.application_file(path):
            return None
        if self.stock and path.casefold().startswith(self.stock.casefold() + "/"):
            return None
        if first.strip("_ ") in MANUAL_ROOT_FOLDERS | STORE_ROOT_FOLDERS.keys():
            return "/".join(parts[1:]) if self.bethesda and len(parts) > 1 else None
        if not self.mo2:
            return path
        if first in ROOT_FOLDERS and len(parts) > 1:
            return "/".join(parts[1:])
        if len(parts) == 1 and first in self.launch_files:
            return path
        return None

    def root_mod_destination(self, path):
        return self.root_destination(path) if self.mo2 and self.bethesda else None

    def installed_path(self, path):
        if self.stock and path.casefold().startswith(self.stock.casefold() + "/"):
            return path
        dest = self.root_mod_destination(path)
        if dest:
            return f"mods/{ROOT_MOD_NAME}/{dest}"
        first = path.split("/")[0].casefold()
        if self.application_file(path) or (self.mo2 and first in {"profiles", "overwrite"} and first not in self.keep):
            return None
        if self.mo2 and self.root_destination(path):
            return None
        return path


def _declared_tool_roots(package):
    keep = set()
    def referenced(value):
        text = "/" + qvalue(value).replace("\\", "/").casefold() + "/"
        keep.update(folder for folder in APPLICATION_FOLDERS | {"profiles", "overwrite"} if f"/{folder}/" in text)
        return text
    directive = next((d for d in package.directives if d.path.casefold() == "modorganizer.ini"), None)
    if directive and directive.data.get("SourceDataID"):
        try:
            with zipfile.ZipFile(package.path) as archive:
                content = archive.read(directive.data["SourceDataID"]).decode("utf-8-sig")
            cp = configparser.ConfigParser(interpolation=None, strict=False)
            cp.read_string(content)
            if cp.has_section("customExecutables"):
                for key, value in cp["customExecutables"].items():
                    text = referenced(value)
                    if key.endswith("\\binary"):
                        name = text.rstrip("/").rsplit("/", 1)[-1]
                        if name not in {"modorganizer.exe", "nxmhandler.exe", "qtwebengineprocess.exe"}:
                            keep.add(name)
        except (OSError, ValueError, KeyError, configparser.Error, zipfile.BadZipFile):
            pass
    configs = [d for d in package.directives if d.kind == "RemappedInlineFile"
               and d.path.split("/")[0].casefold() not in APPLICATION_FILES | APPLICATION_FOLDERS | {"profiles", "mods"}]
    if configs:
        try:
            with zipfile.ZipFile(package.path) as archive:
                for directive in configs:
                    member = archive.getinfo(directive.data["SourceDataID"])
                    if member.file_size <= 8 * 1024 * 1024:
                        referenced(archive.read(member).decode("utf-8-sig", errors="replace"))
        except (OSError, ValueError, KeyError, zipfile.BadZipFile):
            pass
    return frozenset(keep)


def adapter_for(package, game, *, store=""):
    mo2 = bool(package.profiles)
    if not mo2 and any(d.path.casefold() in {"modorganizer.exe", "modorganizer.ini"} for d in package.directives):
        raise WabbajackError("This package includes a mod-organizer layout but has no authored profile modlists")
    launch_files = {str(getattr(game, "exe_name", "")).casefold(),
                    str(getattr(game, "_script_extender_exe", "")).casefold(),
                    *(str(p).casefold() for p in getattr(game, "exe_name_alts", []))}
    launch_files.discard("")
    if not store:
        from pathlib import Path
        root = Path(getattr(game, "get_game_path", lambda: None)() or "/")
        if (root / ".egstore").is_dir():
            store = "epic"
        elif (root.parent.name.casefold() == "common" and root.parent.parent.name.casefold() == "steamapps") or any(root.glob("goggame-*.info")):
            store = "steam-gog"
    return GameAdapter(mo2, stock_folder(package), frozenset(launch_files),
                       nexus_domain(package.game) in _BETHESDA, _declared_tool_roots(package), store)
