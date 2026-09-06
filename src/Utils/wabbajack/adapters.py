from __future__ import annotations

from dataclasses import dataclass

from .manifest import stock_folder
from .paths import WabbajackError

ROOT_FOLDERS = {"root", "game root", "game folder files", "root files"}
APPLICATION_FILES = {"modorganizer.exe", "modorganizer.ini", "nxmhandler.exe",
                     "usvfs_proxy_x86.exe", "usvfs_proxy_x64.exe"}


@dataclass(frozen=True)
class GameAdapter:
    mo2: bool
    stock: str
    launch_files: frozenset[str]

    def root_destination(self, path):
        parts = path.split("/")
        first = parts[0].casefold()
        if first in APPLICATION_FILES or first.startswith(("qt5", "qt6", "usvfs_")):
            return None
        if self.stock and path.casefold().startswith(self.stock.casefold() + "/"):
            return None
        if not self.mo2:
            return path
        if first in ROOT_FOLDERS and len(parts) > 1:
            return "/".join(parts[1:])
        if len(parts) == 1 and first in self.launch_files:
            return path
        return None


def adapter_for(package, game):
    mo2 = bool(package.profiles)
    if not mo2 and any(d.path.casefold() in {"modorganizer.exe", "modorganizer.ini"} for d in package.directives):
        raise WabbajackError("This package includes a mod-organizer layout but has no authored profile modlists")
    launch_files = {str(getattr(game, "exe_name", "")).casefold(),
                    str(getattr(game, "_script_extender_exe", "")).casefold(),
                    *(str(p).casefold() for p in getattr(game, "exe_name_alts", []))}
    launch_files.discard("")
    return GameAdapter(mo2, stock_folder(package), frozenset(launch_files))
