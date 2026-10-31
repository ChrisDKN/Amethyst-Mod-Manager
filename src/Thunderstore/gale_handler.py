from __future__ import annotations

import re
import sys
from urllib.parse import unquote, urlsplit

from Thunderstore.repositories import RepositoryLink
from Thunderstore.ror2mm_handler import Ror2mmHandler, Ror2mmLink


def parse_gale_url(url: str) -> RepositoryLink | Ror2mmLink:
    parsed = urlsplit(url)
    if (parsed.scheme.lower() != "gale" or parsed.netloc.lower() != "install"
            or parsed.query or parsed.fragment or "?" in url or "#" in url
            or any(ord(char) < 32 or ord(char) == 127 for char in url)):
        raise ValueError("Expected gale://install/<source>/<namespace>/<name>/<version>")
    match = re.fullmatch(
        r"/(hexium|thunderstore)/([A-Za-z0-9_-]+)/([A-Za-z0-9_-]+)/([0-9][A-Za-z0-9.+-]*)/?",
        unquote(parsed.path),
    )
    if match is None:
        raise ValueError("Invalid Gale install link or unsupported repository")
    source, namespace, name, version = match.groups()
    if source == "hexium":
        return RepositoryLink(namespace, name, version, raw=url)
    link = Ror2mmLink.parse(
        f"ror2mm://v1/install/thunderstore.io/{namespace}/{name}/{version}/")
    link.raw = url
    return link


def gale_url_from_argv(argv: list[str] | None = None) -> str | None:
    argv = sys.argv[1:] if argv is None else argv
    return next((arg for arg in argv if arg.lower().startswith("gale://")), None)


def strip_gale_argv(argv: list[str]) -> list[str]:
    return [arg for arg in argv
            if arg != "--gale" and not arg.lower().startswith("gale://")]


class GaleHandler(Ror2mmHandler):
    _SCHEME = "gale"
    _FLAG = "--gale"
    _DESKTOP_FILE_NAME = "amethystmodmanager-gale.desktop"

    @classmethod
    def _desktop_contents(cls) -> str:
        return (
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=Amethyst Mod Manager (Gale Install Links)\n"
            "Comment=Handle Hexium and Thunderstore gale:// install links\n"
            f"Exec={cls._exec_command()}\n"
            "Icon=amethystmodmanager\n"
            "Terminal=false\n"
            "NoDisplay=true\n"
            f"MimeType=x-scheme-handler/{cls._SCHEME};\n"
            "Categories=Game;\n"
        )
