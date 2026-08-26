"""
ror2mm_handler.py
ror2mm:// protocol handler - parses Thunderstore one-click install links and
registers the app as a handler on Linux via XDG and .desktop files.

Link format
-----------
    ror2mm://v1/install/<host>/<namespace>/<name>/<version>/

e.g. ror2mm://v1/install/thunderstore.io/Subnautica_Modding/BepInExPack/5.4.2305/

Unlike nxm://, the URI carries NO game/community identifier - Thunderstore
builds it as ``f"ror2mm://v1/install/{PRIMARY_HOST}/{owner}/{name}/{version}/"``
so there is nothing to parse out. Links therefore install into the currently
selected game; a package's community can only be recovered from the API
(``/api/experimental/package/{ns}/{name}/`` → ``community_listings``).

Downloads need no API key, no OAuth and no one-time token: the download URL is
derivable from the link alone and 302s straight to the CDN.

Usage
-----
    from Thunderstore.ror2mm_handler import Ror2mmHandler, Ror2mmLink

    link = Ror2mmLink.parse(
        "ror2mm://v1/install/thunderstore.io/Subnautica_Modding/BepInExPack/5.4.2305/")
    print(link.namespace, link.name, link.version, link.download_url)

    Ror2mmHandler.register()   # one-time setup
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from urllib.parse import unquote, urlparse

_SCHEME = "ror2mm"
_DEFAULT_HOST = "thunderstore.io"
_ALLOWED_HOSTS = frozenset({_DEFAULT_HOST})
_DESKTOP_FILE_NAME = "amethystmodmanager-ror2mm.desktop"


def _handler_log(message: str) -> None:
    """Log protocol-handler activity without importing Nexus for URL parsing."""
    from Nexus.nxm_handler import nxm_log
    nxm_log(message)


@dataclass
class Ror2mmLink:
    """
    Parsed components of a ``ror2mm://`` install URL.

    Attributes
    ----------
    namespace : str   team/owner, e.g. "Subnautica_Modding"
    name      : str   package name, e.g. "BepInExPack"
    version   : str   version number, e.g. "5.4.2305"
    host      : str   source host, e.g. "thunderstore.io"
    community : str   NOT carried by the URI - always "" from parse();
                      filled in by the caller once resolved.
    raw       : str   the original URL string
    """
    namespace: str
    name: str
    version: str
    host: str = _DEFAULT_HOST
    community: str = ""
    raw: str = ""

    def __post_init__(self) -> None:
        self.host = (self.host or "").strip().lower()
        if self.host not in _ALLOWED_HOSTS:
            raise ValueError(
                f"Unsupported Thunderstore host {self.host!r}; expected "
                f"{_DEFAULT_HOST!r}")

    # v1/install/thunderstore.io/Subnautica_Modding/BepInExPack/5.4.2305/
    #
    # urlparse puts "v1" in netloc (it is the authority slot), so the path is
    # matched from "/install/..." onward. Namespace/name allow letters, digits,
    # underscores and hyphens; the version is a dotted triple.
    _PATH_RE = re.compile(
        r"^/install/"
        r"(?P<host>[^/]+)/"
        r"(?P<namespace>[A-Za-z0-9_-]+)/"
        r"(?P<name>[A-Za-z0-9_-]+)/"
        r"(?P<version>[0-9]+\.[0-9]+\.[0-9]+)"
        r"/?$",
        re.IGNORECASE,
    )

    @property
    def full_name(self) -> str:
        """Canonical ``{namespace}-{name}-{version}`` identifier."""
        return f"{self.namespace}-{self.name}-{self.version}"

    @property
    def package_id(self) -> str:
        """Version-independent identity, ``{namespace}-{name}``."""
        return f"{self.namespace}-{self.name}"

    @property
    def download_url(self) -> str:
        """Package zip endpoint - 302s to the CDN, no auth or token needed."""
        return (f"https://{self.host}/package/download/"
                f"{self.namespace}/{self.name}/{self.version}/")

    @property
    def api_url(self) -> str:
        """Experimental package endpoint (carries ``community_listings``)."""
        return (f"https://{self.host}/api/experimental/package/"
                f"{self.namespace}/{self.name}/")

    @property
    def version_api_url(self) -> str:
        """Cyberstorm version-detail endpoint for this exact version."""
        return (f"https://{self.host}/api/cyberstorm/package/"
                f"{self.namespace}/{self.name}/v/{self.version}/")

    @classmethod
    def parse(cls, url: str) -> "Ror2mmLink":
        """
        Parse a ``ror2mm://`` URL into its components.

        Raises ValueError if the URL is malformed.
        """
        parsed = urlparse(url)

        if parsed.scheme.lower() != _SCHEME:
            raise ValueError(f"Not a ror2mm:// URL: {url!r}")

        # The authority slot holds the protocol version ("v1"), not a host.
        api_version = (parsed.netloc or "").lower()
        if api_version and api_version != "v1":
            raise ValueError(
                f"Unsupported ror2mm protocol version {api_version!r}: {url!r}")

        match = cls._PATH_RE.match(unquote(parsed.path or ""))
        if not match:
            raise ValueError(
                f"Cannot parse package from ror2mm URL path: {parsed.path!r}")

        return cls(
            namespace=match.group("namespace"),
            name=match.group("name"),
            version=match.group("version"),
            host=match.group("host").lower(),
            raw=url,
        )


def parse_ror2mm_url(url: str) -> Ror2mmLink:
    """Parse a ror2mm:// URL, raising ValueError if it is not an install link."""
    parsed = urlparse(url)
    if parsed.scheme.lower() != _SCHEME:
        raise ValueError(f"Not a ror2mm:// URL: {url!r}")
    return Ror2mmLink.parse(url)


def ror2mm_url_from_argv(argv: list[str] | None = None) -> str | None:
    """Pull the ror2mm:// URL out of argv, with or without the --ror2mm flag."""
    # Our .desktop file passes `--ror2mm %u`, but a browser's "choose an
    # application" picker execs the selected binary with the bare URL as its
    # only argument - accept both (see nxm_url_from_argv).
    if argv is None:
        argv = sys.argv[1:]
    if "--ror2mm" in argv:
        idx = argv.index("--ror2mm")
        if (idx + 1 < len(argv)
                and argv[idx + 1].lower().startswith(f"{_SCHEME}://")):
            return argv[idx + 1]
    for arg in argv:
        if arg.lower().startswith(f"{_SCHEME}://"):
            return arg
    return None


def strip_ror2mm_argv(argv: list[str]) -> list[str]:
    """Drop --ror2mm and any ror2mm:// URL from argv (for a clean re-exec)."""
    out = [a for a in argv if not a.lower().startswith(f"{_SCHEME}://")]
    return [a for a in out if a != "--ror2mm"]


class Ror2mmHandler:
    """
    Manages ``ror2mm://`` protocol registration on Linux.

    Thin wrapper over :class:`Nexus.nxm_handler.NxmHandler`: the XDG desktop
    entry, mimeapps.list patching and flatpak/AppImage Exec resolution are
    scheme-agnostic, so they are reused verbatim with the scheme, flag and
    .desktop filename swapped.
    """

    _SCHEME = _SCHEME
    _FLAG = "--ror2mm"
    _DESKTOP_FILE_NAME = _DESKTOP_FILE_NAME

    @classmethod
    def _desktop_paths(cls) -> list:
        """Every location our ror2mm .desktop file could live in."""
        from Nexus.nxm_handler import NxmHandler
        return [p.with_name(cls._DESKTOP_FILE_NAME)
                for p in NxmHandler._all_desktop_paths()]

    @classmethod
    def _exec_command(cls) -> str:
        """Exec= line, reusing the Nexus flatpak/AppImage/source resolution."""
        from Nexus.nxm_handler import NxmHandler
        return NxmHandler._get_exec_command().replace("--nxm", cls._FLAG)

    @classmethod
    def _desktop_contents(cls) -> str:
        return (
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=Amethyst Mod Manager (Thunderstore)\n"
            "Comment=Handle Thunderstore ror2mm:// one-click install links\n"
            f"Exec={cls._exec_command()}\n"
            "Icon=amethystmodmanager\n"
            "Terminal=false\n"
            "NoDisplay=true\n"
            f"MimeType=x-scheme-handler/{cls._SCHEME};\n"
            "Categories=Game;\n"
        )

    @classmethod
    def register(cls) -> bool:
        """
        Write the .desktop file and associate ``ror2mm://`` with it.

        Returns True when at least one desktop entry was written.
        """
        import os
        import subprocess

        written = False
        for path in cls._desktop_paths():
            try:
                if not path.parent.exists():
                    # Only create the canonical applications dir; never conjure
                    # a flatpak exports dir that isn't already there (#187).
                    if "flatpak" in str(path):
                        continue
                    path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(cls._desktop_contents(), encoding="utf-8")
                os.chmod(path, 0o755)
                written = True
                _handler_log(f"Wrote ror2mm .desktop: {path}")
            except OSError as exc:
                _handler_log(f"Could not write ror2mm .desktop {path}: {exc}")

        if not written:
            return False

        # Refresh the desktop database so xdg-open picks the entry up.
        for path in cls._desktop_paths():
            if path.exists():
                try:
                    subprocess.run(["update-desktop-database", str(path.parent)],
                                   check=False, capture_output=True, timeout=15)
                except (OSError, subprocess.SubprocessError):
                    pass
                break

        try:
            subprocess.run(
                ["xdg-mime", "default", cls._DESKTOP_FILE_NAME,
                 f"x-scheme-handler/{cls._SCHEME}"],
                check=False, capture_output=True, timeout=15)
        except (OSError, subprocess.SubprocessError) as exc:
            _handler_log(f"xdg-mime registration for ror2mm:// failed: {exc}")

        cls._write_mimeapps_association()
        _handler_log("Registered ror2mm:// handler")
        return True

    @classmethod
    def _write_mimeapps_association(cls) -> None:
        """Set ``x-scheme-handler/ror2mm`` in every relevant mimeapps.list.

        Reuses NxmHandler's line-by-line patcher, which preserves all other
        associations; only the key/value pair is swapped for our scheme.
        """
        from Nexus.nxm_handler import NxmHandler

        for path in NxmHandler._mimeapps_paths():
            try:
                # Never *create* a DE-specific mimeapps.list (#187).
                if path.name != "mimeapps.list" and not path.exists():
                    continue
                if not path.parent.exists():
                    continue

                existing = path.read_text(encoding="utf-8") if path.exists() else ""
                updated = cls._patch_mimeapps_content(existing)
                if updated != existing:
                    path.write_text(updated, encoding="utf-8")
                    _handler_log(f"Updated ror2mm:// association in {path}")
            except OSError as exc:
                _handler_log(f"Could not update {path}: {exc}")

    @classmethod
    def _patch_mimeapps_content(cls, content: str) -> str:
        """
        Set ``x-scheme-handler/ror2mm=amethystmodmanager-ror2mm.desktop`` under
        both ``[Default Applications]`` and ``[Added Associations]``. Creates
        the sections if missing, replaces the key if already present, and
        leaves every other line intact.

        Deliberately self-contained rather than delegating to NxmHandler's
        patcher: that one hardcodes the nxm key internally, so reuse would mean
        mutating its module globals and rewriting its output - which would
        silently corrupt a real nxm:// association if the two ever ran
        concurrently.
        """
        key = f"x-scheme-handler/{cls._SCHEME}"
        value = cls._DESKTOP_FILE_NAME
        target_sections = ("[Default Applications]", "[Added Associations]")

        lines = content.splitlines() if content else []

        section_present: dict[str, bool] = {s: False for s in target_sections}
        key_set: dict[str, bool] = {s: False for s in target_sections}

        current_section: str | None = None
        new_lines: list[str] = []
        for raw in lines:
            stripped = raw.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                current_section = stripped
                if current_section in section_present:
                    section_present[current_section] = True
                new_lines.append(raw)
                continue

            if (
                current_section in target_sections
                and "=" in stripped
                and stripped.split("=", 1)[0].strip() == key
            ):
                new_lines.append(f"{key}={value}")
                key_set[current_section] = True  # type: ignore[index]
                continue

            new_lines.append(raw)

        for section in target_sections:
            if not section_present[section]:
                if new_lines and new_lines[-1] != "":
                    new_lines.append("")
                new_lines.append(section)
                new_lines.append(f"{key}={value}")
            elif not key_set[section]:
                insert_at = len(new_lines)
                in_section = False
                for i, line in enumerate(new_lines):
                    s = line.strip()
                    if s == section:
                        in_section = True
                        continue
                    if in_section and s.startswith("[") and s.endswith("]"):
                        insert_at = i
                        break
                else:
                    insert_at = len(new_lines)
                new_lines.insert(insert_at, f"{key}={value}")

        return "\n".join(new_lines) + ("\n" if new_lines else "")

    @classmethod
    def registration_is_current(cls) -> bool:
        """Return whether ror2mm already resolves to this running install."""
        import subprocess

        from Nexus.nxm_handler import NxmHandler

        try:
            expected = cls._desktop_contents()
            checked_desktop = False
            for path in cls._desktop_paths():
                # register() skips only a missing Flatpak exports directory;
                # every other returned location is expected to contain the
                # current install's entry after a successful registration.
                if "flatpak" in str(path) and not path.parent.exists():
                    continue
                checked_desktop = True
                if (not path.is_file()
                        or path.read_text(encoding="utf-8") != expected):
                    return False
            if not checked_desktop:
                return False

            mimeapps_paths = NxmHandler._mimeapps_paths()
            canonical = mimeapps_paths[0]
            for path in mimeapps_paths:
                if path != canonical and not path.exists():
                    continue
                if not path.is_file():
                    return False
                content = path.read_text(encoding="utf-8")
                if cls._patch_mimeapps_content(content) != content:
                    return False
        except OSError:
            return False

        try:
            result = subprocess.run(
                ["xdg-mime", "query", "default",
                 f"x-scheme-handler/{cls._SCHEME}"],
                check=False,
                capture_output=True,
                timeout=10,
            )
            return (result.returncode == 0
                    and result.stdout.decode(errors="replace").strip()
                    == cls._DESKTOP_FILE_NAME)
        except (OSError, subprocess.SubprocessError):
            return False
