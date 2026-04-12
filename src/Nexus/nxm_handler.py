"""
nxm_handler.py
NXM protocol handler — parses ``nxm://`` links and registers the app
as a handler on Linux via XDG and .desktop files.

NXM link formats
----------------
    Mod download:
    nxm://<game_domain>/mods/<mod_id>/files/<file_id>?key=<key>&expires=<expires>

    Collection:
    nxm://<game_domain>/collections/<slug>
    nxm://<game_domain>/collections/<slug>/revisions/<revision_id>

Free users must click "Download with Manager" on the Nexus website;
the browser fires an ``nxm://`` URL containing a one-time key + expiry.
Premium users can generate download links directly via the API.

Usage
-----
    from Nexus.nxm_handler import NxmHandler, NxmLink

    link = NxmLink.parse("nxm://skyrimspecialedition/mods/2014/files/1234?key=abc&expires=999")
    print(link.game_domain, link.mod_id, link.file_id)

    NxmHandler.register()   # one-time setup
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import parse_qs, urlparse

from Utils.app_log import app_log

# Path for the Unix domain socket used for single-instance IPC
_SOCKET_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp"))
_SOCKET_PATH = _SOCKET_DIR / "amethyst-mod-manager.sock"

# XDG .desktop file name used to register the handler
_DESKTOP_FILE_NAME = "amethystmodmanager-nxm.desktop"


# ---------------------------------------------------------------------------
# Parsed NXM link
# ---------------------------------------------------------------------------

@dataclass
class NxmLink:
    """
    Parsed components of an ``nxm://`` URL.

    Attributes
    ----------
    game_domain : str   e.g. "skyrimspecialedition"
    mod_id      : int   e.g. 2014
    file_id     : int   e.g. 1234
    key         : str   one-time download key (empty for premium direct calls)
    expires     : int   Unix timestamp when the key expires (0 if absent)
    raw         : str   the original URL string
    """
    game_domain: str
    mod_id: int
    file_id: int
    key: str = ""
    expires: int = 0
    raw: str = ""

    # nxm://skyrimspecialedition/mods/2014/files/1234?key=abc&expires=999
    _PATH_RE = re.compile(
        r"^/mods/(?P<mod_id>\d+)/files/(?P<file_id>\d+)",
        re.IGNORECASE,
    )

    @classmethod
    def parse(cls, url: str) -> NxmLink:
        """
        Parse an ``nxm://`` URL into its components.

        Raises ValueError if the URL is malformed.
        """
        parsed = urlparse(url)

        if parsed.scheme.lower() != "nxm":
            raise ValueError(f"Not an nxm:// URL: {url!r}")

        game_domain = parsed.netloc or parsed.hostname or ""
        if not game_domain:
            raise ValueError(f"Missing game domain in NXM URL: {url!r}")

        match = cls._PATH_RE.match(parsed.path)
        if not match:
            raise ValueError(
                f"Cannot parse mod/file IDs from NXM URL path: {parsed.path!r}"
            )

        qs = parse_qs(parsed.query)
        key = qs.get("key", [""])[0]
        expires_str = qs.get("expires", ["0"])[0]
        try:
            expires = int(expires_str)
        except ValueError:
            expires = 0

        return cls(
            game_domain=game_domain.lower(),
            mod_id=int(match.group("mod_id")),
            file_id=int(match.group("file_id")),
            key=key,
            expires=expires,
            raw=url,
        )


@dataclass
class NxmCollectionLink:
    """
    Parsed components of an nxm:// collection URL.

    Attributes
    ----------
    game_domain : str   e.g. "stardewvalley"
    slug        : str   e.g. "tckf0m"
    revision_id : int   revision number (0 if absent)
    raw         : str   the original URL string
    """
    game_domain: str
    slug: str
    revision_id: int = 0
    raw: str = ""

    # nxm://stardewvalley/collections/tckf0m
    # nxm://stardewvalley/collections/tckf0m/revisions/104
    _PATH_RE = re.compile(
        r"^/collections/(?P<slug>[A-Za-z0-9_-]+)(?:/revisions/(?P<revision_id>\d+))?$",
        re.IGNORECASE,
    )

    @classmethod
    def parse(cls, url: str) -> NxmCollectionLink:
        """
        Parse an nxm:// collection URL into its components.

        Raises ValueError if the URL is malformed.
        """
        parsed = urlparse(url)

        if parsed.scheme.lower() != "nxm":
            raise ValueError(f"Not an nxm:// URL: {url!r}")

        game_domain = parsed.netloc or parsed.hostname or ""
        if not game_domain:
            raise ValueError(f"Missing game domain in NXM URL: {url!r}")

        match = cls._PATH_RE.match(parsed.path)
        if not match:
            raise ValueError(
                f"Cannot parse collection slug from NXM URL path: {parsed.path!r}"
            )

        slug = match.group("slug")
        rev_str = match.group("revision_id") or "0"
        try:
            revision_id = int(rev_str)
        except ValueError:
            revision_id = 0

        return cls(
            game_domain=game_domain.lower(),
            slug=slug,
            revision_id=revision_id,
            raw=url,
        )


def parse_nxm_url(url: str) -> tuple[NxmLink | None, NxmCollectionLink | None]:
    """
    Parse an nxm:// URL as either a mod download link or a collection link.

    Returns (NxmLink, None) for mod links, (None, NxmCollectionLink) for
    collection links, or raises ValueError if neither matches.
    """
    parsed = urlparse(url)
    if parsed.scheme.lower() != "nxm":
        raise ValueError(f"Not an nxm:// URL: {url!r}")

    path = parsed.path or ""
    if "/collections/" in path.lower():
        return None, NxmCollectionLink.parse(url)
    if NxmLink._PATH_RE.match(path):
        return NxmLink.parse(url), None
    raise ValueError(f"Unknown nxm:// URL format: {url!r}")


# ---------------------------------------------------------------------------
# Protocol registration (Linux / XDG)
# ---------------------------------------------------------------------------

class NxmHandler:
    """
    Manages ``nxm://`` protocol registration on Linux.

    Calling ``NxmHandler.register()`` creates (or updates) a .desktop file
    in ``~/.local/share/applications/`` that associates ``nxm://`` URLs
    with the running AmethystModManager executable, then registers it
    with ``xdg-mime``.
    """

    @staticmethod
    def _desktop_path() -> Path:
        # Inside a Flatpak XDG_DATA_HOME is redirected to ~/.var/app/<id>/data,
        # which the host xdg-mime doesn't search.  Always write to the real
        # host location so the registration actually takes effect.
        if Path("/.flatpak-info").exists():
            return Path.home() / ".local" / "share" / "applications" / _DESKTOP_FILE_NAME
        xdg = os.environ.get("XDG_DATA_HOME")
        base = Path(xdg) if xdg else Path.home() / ".local" / "share"
        return base / "applications" / _DESKTOP_FILE_NAME

    @staticmethod
    def _flatpak_desktop_path() -> Path:
        """Flatpak exports dir — visible to Flatpak-sandboxed browsers."""
        return (
            Path.home()
            / ".local" / "share" / "flatpak" / "exports" / "share"
            / "applications" / _DESKTOP_FILE_NAME
        )

    @classmethod
    def _all_desktop_paths(cls) -> list[Path]:
        """
        Every location an NXM .desktop file could live in, across flatpak
        and non-flatpak installs. Used to scrub stale registrations from
        *other* instances before re-registering this one.
        """
        paths: list[Path] = []

        # Host ~/.local/share/applications (non-flatpak + flatpak host write)
        paths.append(Path.home() / ".local" / "share" / "applications" / _DESKTOP_FILE_NAME)

        # XDG_DATA_HOME override (only meaningful outside flatpak — inside
        # flatpak this is redirected into the sandbox)
        xdg = os.environ.get("XDG_DATA_HOME")
        if xdg:
            paths.append(Path(xdg) / "applications" / _DESKTOP_FILE_NAME)

        # Flatpak exports dir (visible to flatpak-sandboxed browsers)
        paths.append(cls._flatpak_desktop_path())

        # Deduplicate while preserving order
        seen: set[Path] = set()
        unique: list[Path] = []
        for p in paths:
            if p not in seen:
                seen.add(p)
                unique.append(p)
        return unique

    @classmethod
    def _scrub_all(cls) -> None:
        """
        Remove every NXM .desktop file we might have written previously,
        from both flatpak and non-flatpak locations, and clear the xdg-mime
        default association. Safe to call before register() so a freshly
        launched instance always takes over the handler cleanly — otherwise
        a stale .desktop from another install can hijack nxm:// links into
        a different (possibly not-running) instance of the manager.
        """
        for path in cls._all_desktop_paths():
            try:
                if path.exists():
                    path.unlink()
                    app_log(f"Scrubbed stale NXM .desktop file: {path}")
            except OSError as exc:
                app_log(f"Could not scrub NXM .desktop {path}: {exc}")

        # Best-effort: tell xdg-mime to forget the old default. We try both
        # host and in-sandbox invocations so this works whichever variant
        # we're running as.
        cmds: list[list[str]] = []
        in_flatpak = Path("/.flatpak-info").exists()
        if in_flatpak and shutil.which("flatpak-spawn"):
            cmds.append(["flatpak-spawn", "--host", "--directory=/", "xdg-mime"])
        if shutil.which("xdg-mime"):
            cmds.append(["xdg-mime"])

        for base in cmds:
            try:
                subprocess.run(
                    [*base, "default", "", "x-scheme-handler/nxm"],
                    check=False,
                    capture_output=True,
                )
            except OSError as exc:
                app_log(f"xdg-mime default clear failed: {exc}")

        # Also strip the association from any mimeapps.list files — this is
        # the canonical source-of-truth used by xdg-open on minimal setups.
        cls._remove_mimeapps_association()

    @staticmethod
    def _get_exec_command() -> str:
        """
        Build the Exec= line for the .desktop file.

        The command must be resolvable on the *host* system (where the browser
        runs), not inside the sandbox.
        """
        # Flatpak: the host can't see /app/..., so use `flatpak run <app-id>`
        flatpak_app_id = os.environ.get("FLATPAK_ID")
        if flatpak_app_id:
            return f"flatpak run {flatpak_app_id} --nxm %u"

        appimage = os.environ.get("APPIMAGE")
        if appimage:
            return f'"{appimage}" --nxm %u'

        # Running from source — use python + gui.py
        script = Path(sys.argv[0]).resolve()
        return f'"{sys.executable}" "{script}" --nxm %u'

    @classmethod
    def _mimeapps_paths(cls) -> list[Path]:
        """
        Candidate mimeapps.list locations per the XDG MIME Applications spec.
        We write to ~/.config/mimeapps.list (the user's canonical one) and,
        if already present, also update the legacy ~/.local/share/applications
        one so both are in sync.
        """
        paths: list[Path] = []
        xdg_cfg = os.environ.get("XDG_CONFIG_HOME")
        cfg_base = Path(xdg_cfg) if xdg_cfg else Path.home() / ".config"
        paths.append(cfg_base / "mimeapps.list")
        paths.append(Path.home() / ".local" / "share" / "applications" / "mimeapps.list")
        return paths

    @classmethod
    def _write_mimeapps_association(cls) -> None:
        """
        Ensure ``x-scheme-handler/nxm=amethystmodmanager-nxm.desktop`` is set
        under ``[Default Applications]`` and ``[Added Associations]`` in
        mimeapps.list, so xdg-open / gio / portals resolve nxm:// correctly
        even on systems where xdg-mime isn't consulted.

        We edit the file line-by-line to preserve every other association.
        """
        for path in cls._mimeapps_paths():
            try:
                if not path.parent.exists():
                    # Only touch mimeapps.list in dirs that already exist —
                    # we don't want to create ~/.local/share/applications
                    # just to drop a mimeapps.list into it.
                    if path == cls._mimeapps_paths()[0]:
                        path.parent.mkdir(parents=True, exist_ok=True)
                    else:
                        continue

                existing = path.read_text() if path.exists() else ""
                updated = cls._patch_mimeapps_content(existing)
                if updated != existing:
                    path.write_text(updated)
                    app_log(f"Updated nxm:// association in {path}")
            except OSError as exc:
                app_log(f"Could not update {path}: {exc}")

    @staticmethod
    def _patch_mimeapps_content(content: str) -> str:
        """
        Set ``x-scheme-handler/nxm=amethystmodmanager-nxm.desktop`` under both
        ``[Default Applications]`` and ``[Added Associations]`` sections of a
        mimeapps.list-style file. Creates the sections if missing, replaces
        the key if already present, and leaves every other line intact.
        """
        key = "x-scheme-handler/nxm"
        value = _DESKTOP_FILE_NAME
        target_sections = ("[Default Applications]", "[Added Associations]")

        lines = content.splitlines() if content else []

        # Track which sections exist, and whether the key is already set in each
        section_present: dict[str, bool] = {s: False for s in target_sections}
        key_set: dict[str, bool] = {s: False for s in target_sections}

        current_section: Optional[str] = None
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
                # Replace existing assignment
                new_lines.append(f"{key}={value}")
                key_set[current_section] = True  # type: ignore[index]
                continue

            new_lines.append(raw)

        # Append missing sections / keys
        for section in target_sections:
            if not section_present[section]:
                if new_lines and new_lines[-1] != "":
                    new_lines.append("")
                new_lines.append(section)
                new_lines.append(f"{key}={value}")
            elif not key_set[section]:
                # Section exists but key missing — insert the key at the end
                # of that section.
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
    def _gio_register(cls, in_flatpak: bool) -> None:
        """
        Register the handler via ``gio mime`` as well. Many GTK/GNOME tools
        and some browsers (incl. Brave on certain Arch setups) consult gio
        rather than xdg-mime directly. Best-effort — silent on failure.
        """
        if in_flatpak and shutil.which("flatpak-spawn"):
            base = ["flatpak-spawn", "--host", "--directory=/", "gio"]
        elif shutil.which("gio"):
            base = ["gio"]
        else:
            return
        try:
            subprocess.run(
                [*base, "mime", "x-scheme-handler/nxm", _DESKTOP_FILE_NAME],
                check=False,
                capture_output=True,
            )
            app_log("Registered nxm:// handler via gio mime")
        except OSError as exc:
            app_log(f"gio mime registration failed: {exc}")

    @classmethod
    def _remove_mimeapps_association(cls) -> None:
        """Remove our nxm:// key from every mimeapps.list we can find."""
        key = "x-scheme-handler/nxm"
        for path in cls._mimeapps_paths():
            try:
                if not path.exists():
                    continue
                lines = path.read_text().splitlines()
                filtered = [
                    l for l in lines
                    if not (
                        "=" in l
                        and l.split("=", 1)[0].strip() == key
                        and _DESKTOP_FILE_NAME in l
                    )
                ]
                if filtered != lines:
                    path.write_text("\n".join(filtered) + "\n")
                    app_log(f"Removed nxm:// association from {path}")
            except OSError as exc:
                app_log(f"Could not clean {path}: {exc}")

    @classmethod
    def register(cls) -> bool:
        """
        Register AmethystModManager as the handler for nxm:// links.

        Returns True on success, False if it could not be registered
        (e.g. xdg-mime not available).
        """
        # Always scrub first. This removes any leftover .desktop from a
        # different install variant (e.g. flatpak vs native) so the handler
        # doesn't get routed to an old/other instance of the manager.
        cls._scrub_all()

        desktop_path = cls._desktop_path()
        desktop_path.parent.mkdir(parents=True, exist_ok=True)

        exec_cmd = cls._get_exec_command()

        desktop_content = (
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=Amethyst Mod Manager (NXM Handler)\n"
            "Comment=Handle nxm:// download links from Nexus Mods\n"
            f"Exec={exec_cmd}\n"
            "Terminal=false\n"
            "NoDisplay=true\n"
            "MimeType=x-scheme-handler/nxm;\n"
            "Categories=Game;\n"
        )

        desktop_path.write_text(desktop_content)
        app_log(f"Wrote NXM .desktop file: {desktop_path}")

        # Also write to Flatpak exports dir so Flatpak-sandboxed browsers can
        # see the handler.  The dir may not exist if Flatpak isn't installed,
        # so we only write if the parent already exists.
        flatpak_path = cls._flatpak_desktop_path()
        if flatpak_path.parent.exists():
            try:
                flatpak_path.write_text(desktop_content)
                app_log(f"Wrote NXM .desktop file to Flatpak exports: {flatpak_path}")
            except OSError as exc:
                app_log(f"Could not write Flatpak .desktop file: {exc}")

        # Register as default handler.
        # Inside a Flatpak sandbox xdg-mime is not available directly; use
        # flatpak-spawn --host to run it on the host system instead.
        in_flatpak = Path("/.flatpak-info").exists()
        if in_flatpak and shutil.which("flatpak-spawn"):
            xdg_mime_cmd = ["flatpak-spawn", "--host", "--directory=/", "xdg-mime"]
        elif shutil.which("xdg-mime"):
            xdg_mime_cmd = ["xdg-mime"]
        else:
            app_log("xdg-mime not found — nxm:// handler not registered")
            return False

        try:
            subprocess.run(
                [*xdg_mime_cmd, "default", _DESKTOP_FILE_NAME,
                 "x-scheme-handler/nxm"],
                check=True,
                capture_output=True,
            )
            app_log("Registered nxm:// protocol handler via xdg-mime")
        except subprocess.CalledProcessError as exc:
            app_log(f"xdg-mime default failed: {exc.stderr}")
            return False

        # On some distros (e.g. CachyOS / minimal Arch setups without a full
        # desktop environment) xdg-open runs in "generic" mode and ignores
        # xdg-mime, cycling through a hardcoded browser list instead — which
        # produces "xdg-open: no method available for opening 'nxm://...'"
        # when the user's browser (Brave, etc.) isn't in that list. To cover
        # that case we *also* write the association directly to
        # ~/.config/mimeapps.list (the canonical source of truth per the
        # XDG spec) and register via `gio mime`, which many modern tools use.
        cls._write_mimeapps_association()
        cls._gio_register(in_flatpak)

        # Refresh the desktop database so Flatpak apps pick up the new entry.
        if in_flatpak and shutil.which("flatpak-spawn"):
            udd_cmd = ["flatpak-spawn", "--host", "--directory=/", "update-desktop-database"]
            udd_available = True
        else:
            udd_cmd = ["update-desktop-database"]
            udd_available = bool(shutil.which("update-desktop-database"))
        if udd_available:
            for db_dir in {desktop_path.parent, flatpak_path.parent}:
                if db_dir.exists():
                    try:
                        subprocess.run(
                            [*udd_cmd, str(db_dir)],
                            check=True,
                            capture_output=True,
                        )
                        app_log(f"Updated desktop database: {db_dir}")
                    except subprocess.CalledProcessError as exc:
                        app_log(f"update-desktop-database failed for {db_dir}: {exc.stderr}")

        return True

    @classmethod
    def unregister(cls) -> None:
        """
        Remove the .desktop file(s) from *every* install variant
        (flatpak + non-flatpak) and clear the xdg-mime default, best-effort.
        """
        cls._scrub_all()

    @classmethod
    def is_registered(cls) -> bool:
        """Check whether our .desktop file exists."""
        return cls._desktop_path().is_file()


# ---------------------------------------------------------------------------
# Single-instance IPC via Unix domain socket
# ---------------------------------------------------------------------------

class NxmIPC:
    """
    Ensures only one instance of the app runs at a time.

    The first instance calls ``start_server(callback)`` which listens on
    a Unix domain socket.  Subsequent instances call ``send_to_running()``
    which sends the ``nxm://`` URL to the existing instance and returns True,
    signalling the caller to exit immediately.

    Usage (entry point)::

        def on_nxm(url: str):
            app.after(0, lambda: app._process_nxm_link(url))

        if NxmIPC.send_to_running(nxm_url):
            sys.exit(0)          # handed off to running instance

        app = App()
        NxmIPC.start_server(on_nxm)
        app.mainloop()
    """

    _server_socket: Optional[socket.socket] = None
    _thread: Optional[threading.Thread] = None

    @classmethod
    def send_to_running(cls, nxm_url: str) -> bool:
        """
        Try to send *nxm_url* to an already-running instance.

        Returns True if delivered, False if no instance was listening.
        """
        if not _SOCKET_PATH.exists():
            return False

        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(2)
            sock.connect(str(_SOCKET_PATH))
            payload = json.dumps({"nxm_url": nxm_url}).encode("utf-8")
            sock.sendall(payload)
            sock.close()
            app_log("Sent NXM link to running instance")
            return True
        except (ConnectionRefusedError, FileNotFoundError, OSError) as exc:
            app_log(f"No running instance to hand off to: {exc}")
            # Stale socket — clean up
            _SOCKET_PATH.unlink(missing_ok=True)
            return False

    @classmethod
    def start_server(cls, callback: Callable[[str], None]) -> None:
        """
        Start listening for NXM links from new instances.

        *callback* is called (from a background thread) with the nxm:// URL
        string whenever another instance sends one.  The callback should use
        ``app.after()`` to schedule work on the main thread.
        """
        # Clean stale socket
        _SOCKET_PATH.unlink(missing_ok=True)

        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(_SOCKET_PATH))
        srv.listen(4)
        cls._server_socket = srv

        def _accept_loop():
            while True:
                try:
                    conn, _ = srv.accept()
                except OSError:
                    break  # socket closed → shutting down
                try:
                    data = conn.recv(4096)
                    if data:
                        msg = json.loads(data.decode("utf-8"))
                        url = msg.get("nxm_url", "")
                        if url:
                            app_log(f"Received NXM link from new instance: {url}")
                            callback(url)
                except Exception as exc:
                    app_log(f"Error handling IPC message: {exc}")
                finally:
                    conn.close()

        t = threading.Thread(target=_accept_loop, daemon=True, name="nxm-ipc")
        t.start()
        cls._thread = t
        app_log(f"NXM IPC server listening on {_SOCKET_PATH}")

    @classmethod
    def shutdown(cls) -> None:
        """Close the IPC socket and clean up."""
        if cls._server_socket:
            try:
                cls._server_socket.close()
            except OSError:
                pass
            cls._server_socket = None
        _SOCKET_PATH.unlink(missing_ok=True)
