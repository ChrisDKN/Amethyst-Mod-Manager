from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

from Thunderstore.ror2mm_handler import Ror2mmHandler


@dataclass(frozen=True)
class NexusCollectionLink:
    slug: str
    revision: int | None = None

    @classmethod
    def parse(cls, url):
        parsed = urlsplit(url)
        if (parsed.scheme.lower() != "wabbajack"
                or parsed.netloc.lower() != "nexus-collection"
                or parsed.query or parsed.fragment
                or any(ord(char) < 32 or ord(char) == 127 for char in url)):
            raise ValueError("Expected wabbajack://nexus-collection/<slug>/revision/<number>")
        match = re.fullmatch(r"/([A-Za-z0-9_-]+)(?:/revision/([1-9][0-9]*))?/?",
                             unquote(parsed.path), re.IGNORECASE)
        if match is None:
            raise ValueError("Invalid Wabbajack Nexus collection link")
        slug, revision = match.groups()
        return cls(slug, int(revision) if revision else None)

    @property
    def url(self):
        suffix = f"/revision/{self.revision}" if self.revision is not None else ""
        return f"wabbajack://nexus-collection/{self.slug}{suffix}"


def wabbajack_url_from_argv(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    return next((arg for arg in argv if arg.lower().startswith("wabbajack://")), None)


def strip_wabbajack_argv(argv):
    return [arg for arg in argv
            if arg != "--wabbajack" and not arg.lower().startswith("wabbajack://")]


class WabbajackHandler(Ror2mmHandler):
    _SCHEME = "wabbajack"
    _FLAG = "--wabbajack"
    _DESKTOP_FILE_NAME = "amethystmodmanager-wabbajack.desktop"

    @classmethod
    def _desktop_contents(cls):
        return (
            "[Desktop Entry]\nType=Application\n"
            "Name=Amethyst Mod Manager (Wabbajack)\n"
            f"Exec={cls._exec_command()}\n"
            "Icon=amethystmodmanager\nTerminal=false\nNoDisplay=true\n"
            "MimeType=x-scheme-handler/wabbajack;\nCategories=Game;\n"
        )

    @classmethod
    def _patch_mimeapps_content(cls, content):
        from Nexus.nxm_handler import NxmHandler
        return NxmHandler._patch_mimeapps_content(
            content, scheme=cls._SCHEME, desktop_file=cls._DESKTOP_FILE_NAME)
