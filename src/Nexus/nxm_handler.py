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

# Max size of logs/nxm.log before it is rotated to nxm.log.old.
_NXM_LOG_MAX_BYTES = 512_000


def nxm_log(message: str) -> None:
    """Log an NXM-flow message to the GUI panel AND to logs/nxm.log.

    The browser-spawned ``--nxm`` handoff process has no GUI, so plain
    app_log() calls made there vanish — the file is the only trace of why
    a "Download with Manager" click did or didn't reach the running
    instance. Timestamp + pid let the sender and receiver sides of one
    click be correlated across the two processes.
    """
    app_log(message)
    try:
        from datetime import datetime

        from Utils.config_paths import get_logs_dir

        path = get_logs_dir() / "nxm.log"
        try:
            if path.stat().st_size > _NXM_LOG_MAX_BYTES:
                path.replace(path.with_suffix(".log.old"))
        except OSError:
            pass
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"[{stamp}] [pid {os.getpid()}] {message}\n")
    except Exception:
        pass


# Path for the Unix domain socket used for single-instance IPC.
#
# Resolved via _resolve_socket_path() so that every launch of the same app
# (including browser-triggered `flatpak run ... --nxm %u` invocations) picks
# the same path, regardless of whether XDG_RUNTIME_DIR is set in the env
# inherited from the caller. Under Flatpak, /run/user/<uid>/app/<FLATPAK_ID>/
# is auto-created per-user per-app and is stable across invocations; outside
# Flatpak, XDG_RUNTIME_DIR is used when set, otherwise a uid-scoped path
# under /tmp as a last resort.
def _resolve_socket_path() -> Path:
    uid = os.getuid()
    flatpak_id = os.environ.get("FLATPAK_ID")
    if flatpak_id:
        app_run = Path(f"/run/user/{uid}/app/{flatpak_id}")
        if app_run.is_dir():
            return app_run / "amethyst-mod-manager.sock"
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return Path(xdg) / "amethyst-mod-manager.sock"
    return Path(f"/tmp/amethyst-mod-manager-{uid}.sock")


# Our Flatpak app id — hard-coded so a *native/AppImage* sender can still try
# the Flatpak per-app runtime dir (the host sees it at the same path) even
# though FLATPAK_ID isn't in its environment.
_FLATPAK_APP_ID = "io.github.Amethyst.ModManager"


def _home_socket_path() -> Path:
    """Env-independent socket path in the real home directory.

    Under Flatpak both /tmp and XDG_RUNTIME_DIR are private to the sandbox, so
    a Flatpak instance and a native/AppImage instance can never meet on either
    — a click routed to the wrong install variant then opens a second window
    instead of handing the link off. The real home IS shared (the sandbox has
    --filesystem=home and HOME is not redirected), so a socket here is the one
    path every install variant can reach.
    """
    return (Path.home() / ".local" / "share" / "AmethystModManager"
            / "amethyst-mod-manager.sock")


# Every socket path the app *might* use across launch contexts.
#
# The single-instance handoff fails when the browser-spawned `--nxm` process
# resolves a different socket path than the long-running instance — which
# happens whenever the two launches see different environments (e.g. a Flatpak
# browser, or a browser launched without XDG_RUNTIME_DIR) or the two processes
# are different install variants (Flatpak vs AppImage/native). To make handoff
# robust, the sender tries *all* of these and the server listens on the
# env-independent fallbacks in addition to its primary path, so the two
# processes meet on at least one common path regardless of env.
def _candidate_socket_paths() -> list[Path]:
    uid = os.getuid()
    paths: list[Path] = []

    flatpak_id = os.environ.get("FLATPAK_ID")
    if flatpak_id:
        app_run = Path(f"/run/user/{uid}/app/{flatpak_id}")
        paths.append(app_run / "amethyst-mod-manager.sock")

    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        paths.append(Path(xdg) / "amethyst-mod-manager.sock")

    # Cross-variant meeting point in the real home (see _home_socket_path).
    paths.append(_home_socket_path())

    # A native/AppImage sender reaching a *Flatpak* instance: the Flatpak's
    # primary socket lives in its per-app runtime dir, which the host sees at
    # this same path.
    paths.append(
        Path(f"/run/user/{uid}/app/{_FLATPAK_APP_ID}") / "amethyst-mod-manager.sock")

    # Env-independent /tmp fallback. NOTE: under Flatpak /tmp is sandbox-
    # private, so this only ever connects same-variant instances.
    paths.append(Path(f"/tmp/amethyst-mod-manager-{uid}.sock"))

    # Deduplicate while preserving order.
    seen: set[Path] = set()
    unique: list[Path] = []
    for p in paths:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    return unique


_SOCKET_PATH = _resolve_socket_path()
# The env-independent /tmp fallback — always bound by the server in addition to
# _SOCKET_PATH so that a sender which lost XDG_RUNTIME_DIR can still reach us.
_FALLBACK_SOCKET_PATH = Path(f"/tmp/amethyst-mod-manager-{os.getuid()}.sock")

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


def nxm_url_from_argv(argv: list[str] | None = None) -> str | None:
    """Pull the nxm:// URL out of argv, with or without the --nxm flag."""
    # Our .desktop file passes `--nxm %u`, but a browser's "choose an
    # application" picker execs the selected binary with the bare URL as its
    # only argument. Accepting both means picking the AppImage directly in that
    # dialog works instead of opening the app with the link silently ignored.
    if argv is None:
        argv = sys.argv[1:]
    if "--nxm" in argv:
        idx = argv.index("--nxm")
        # The flag may be present with no URL after it (or a stray flag);
        # fall through to the bare-URL scan in that case.
        if idx + 1 < len(argv) and argv[idx + 1].lower().startswith("nxm://"):
            return argv[idx + 1]
    for arg in argv:
        if arg.lower().startswith("nxm://"):
            return arg
    return None


def strip_nxm_argv(argv: list[str]) -> list[str]:
    """Drop --nxm and any nxm:// URL from argv (for a clean self re-exec)."""
    out = [a for a in argv if not a.lower().startswith("nxm://")]
    return [a for a in out if a != "--nxm"]


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
                    nxm_log(f"Scrubbed stale NXM .desktop file: {path}")
            except OSError as exc:
                nxm_log(f"Could not scrub NXM .desktop {path}: {exc}")

        # Strip the association from all mimeapps.list files (including
        # DE-specific ones like kde-mimeapps.list).  We remove *any* handler
        # for nxm://, not just ours — this clears entries set by Firefox, the
        # desktop environment, etc. that would otherwise shadow our registration.
        cls._remove_mimeapps_association(ours_only=False)

    @staticmethod
    def _quote_if_needed(path: str) -> str:
        """Quote a path for a .desktop Exec line only if it contains spaces.

        Some xdg-open implementations (notably the 'generic' fallback on
        minimal Arch/CachyOS setups without a full DE) mishandle quoted
        arguments in Exec lines, so we only quote when strictly necessary.
        """
        return f'"{path}"' if " " in path else path

    @classmethod
    def _get_exec_command(cls) -> str:
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
            return f'{cls._quote_if_needed(appimage)} --nxm %u'

        # Running from source — use python + gui.py
        script = str(Path(sys.argv[0]).resolve())
        exe = sys.executable
        return f'{cls._quote_if_needed(exe)} {cls._quote_if_needed(script)} --nxm %u'

    @classmethod
    def _mimeapps_paths(cls) -> list[Path]:
        """
        Candidate mimeapps.list locations per the XDG MIME Applications spec.
        We write to ~/.config/mimeapps.list (the user's canonical one) and,
        if already present, also update the legacy ~/.local/share/applications
        one so both are in sync.  We also include DE-specific variants
        (e.g. kde-mimeapps.list) since xdg-open checks those first — a
        handler registered there by Firefox/the DE will shadow ours.

        Every $XDG_CURRENT_DESKTOP token is included so the cleanup path
        can strip stale entries wherever they live; the write path never
        *creates* a DE-specific file (#187).
        """
        paths: list[Path] = []
        xdg_cfg = os.environ.get("XDG_CONFIG_HOME")
        cfg_base = Path(xdg_cfg) if xdg_cfg else Path.home() / ".config"
        paths.append(cfg_base / "mimeapps.list")

        # DE-specific mimeapps.list — xdg-open checks every
        # $XDG_CURRENT_DESKTOP variant before the generic one, so a handler
        # registered there shadows ~/.config/mimeapps.list.
        desktop = os.environ.get("XDG_CURRENT_DESKTOP", "")
        for de_name in desktop.split(":"):
            de_name = de_name.strip().lower()
            if de_name:
                paths.append(cfg_base / f"{de_name}-mimeapps.list")

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
        canonical = cls._mimeapps_paths()[0]
        for path in cls._mimeapps_paths():
            try:
                # Never *create* a DE-specific mimeapps.list — most desktops
                # (Hyprland, XFCE, sway, …) never ship one and conjuring it
                # surprises users (#187). Patch it only where the DE already
                # maintains it (e.g. kde-mimeapps.list on KDE).
                if path.name != "mimeapps.list" and not path.exists():
                    continue

                if not path.parent.exists():
                    # Only touch mimeapps.list in dirs that already exist —
                    # we don't want to create ~/.local/share/applications
                    # just to drop a mimeapps.list into it.
                    if path == canonical:
                        path.parent.mkdir(parents=True, exist_ok=True)
                    else:
                        continue

                existing = path.read_text(encoding="utf-8") if path.exists() else ""
                updated = cls._patch_mimeapps_content(existing)
                if updated != existing:
                    path.write_text(updated, encoding="utf-8")
                    nxm_log(f"Updated nxm:// association in {path}")
            except OSError as exc:
                nxm_log(f"Could not update {path}: {exc}")

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
    def _host_cmd(cls, in_flatpak: bool, tool: str) -> list[str] | None:
        """Build a command prefix to run *tool* on the host.

        Outside Flatpak: returns ``[tool]`` if found in PATH, else None.
        Inside Flatpak: returns ``["flatpak-spawn", "--host", ...]`` if
        ``flatpak-spawn`` is available. Inside Flatpak we cannot check
        whether *tool* itself exists on the host (no PATH visibility), so
        caller must treat rc=127 as "tool missing on host".

        Returns None when the tool cannot be invoked; caller should log
        and skip.
        """
        if in_flatpak:
            if shutil.which("flatpak-spawn"):
                return ["flatpak-spawn", "--host", "--directory=/", tool]
            nxm_log(f"{tool}: flatpak-spawn unavailable — cannot reach host tool from sandbox")
            return None
        if shutil.which(tool):
            return [tool]
        return None

    @staticmethod
    def _is_host_tool_missing(rc: int) -> bool:
        """flatpak-spawn returns 127 when the host binary isn't installed."""
        return rc == 127

    @classmethod
    def _gio_register(cls, in_flatpak: bool) -> None:
        """
        Register the handler via ``gio mime`` as well. Many GTK/GNOME tools
        and some browsers (incl. Brave on certain Arch setups) consult gio
        rather than xdg-mime directly. Best-effort — silent on failure.
        """
        base = cls._host_cmd(in_flatpak, "gio")
        if base is None:
            return
        try:
            result = subprocess.run(
                [*base, "mime", "x-scheme-handler/nxm", _DESKTOP_FILE_NAME],
                check=False,
                capture_output=True,
            )
            if in_flatpak and cls._is_host_tool_missing(result.returncode):
                nxm_log("gio not installed on host — skipping gio mime registration")
                return
            nxm_log("Registered nxm:// handler via gio mime")
        except OSError as exc:
            nxm_log(f"gio mime registration failed: {exc}")

    @classmethod
    def _xdg_settings_register(cls, in_flatpak: bool) -> None:
        """
        Register via ``xdg-settings set default-url-scheme-handler nxm``.
        This is the XDG-recommended way to register URL scheme handlers and
        is more reliable than xdg-mime on some desktop environments (e.g. KDE
        on Arch/CachyOS) where xdg-open checks xdg-settings first.
        """
        base = cls._host_cmd(in_flatpak, "xdg-settings")
        if base is None:
            return
        try:
            result = subprocess.run(
                [*base, "set", "default-url-scheme-handler", "nxm",
                 _DESKTOP_FILE_NAME],
                check=False,
                capture_output=True,
            )
            if in_flatpak and cls._is_host_tool_missing(result.returncode):
                nxm_log("xdg-settings not installed on host — skipping registration")
                return
            nxm_log("Registered nxm:// handler via xdg-settings")
        except OSError as exc:
            nxm_log(f"xdg-settings registration failed: {exc}")

    @classmethod
    def _remove_mimeapps_association(cls, ours_only: bool = False) -> None:
        """Remove nxm:// handler entries from every mimeapps.list we can find.

        If *ours_only* is True, only remove lines pointing to our .desktop file.
        If False (the default, used by _scrub_all), remove **any** handler for
        x-scheme-handler/nxm — including entries set by Firefox, the DE, etc. —
        so that the subsequent register() has a clean slate.
        """
        key = "x-scheme-handler/nxm"
        for path in cls._mimeapps_paths():
            try:
                if not path.exists():
                    continue
                lines = path.read_text(encoding="utf-8").splitlines()
                filtered = [
                    l for l in lines
                    if not (
                        "=" in l
                        and l.split("=", 1)[0].strip() == key
                        and (not ours_only or _DESKTOP_FILE_NAME in l)
                    )
                ]
                if filtered == lines:
                    continue
                # A DE-specific file that we (or an old Amethyst) created can
                # end up holding nothing but empty section headers once the
                # nxm entry is gone — delete it outright rather than leave a
                # husk the desktop never shipped (#187).
                meaningless = all(
                    not s or (s.startswith("[") and s.endswith("]"))
                    for s in (l.strip() for l in filtered)
                )
                if path.name != "mimeapps.list" and meaningless:
                    path.unlink()
                    nxm_log(f"Removed now-empty {path}")
                else:
                    path.write_text("\n".join(filtered) + "\n", encoding="utf-8")
                    nxm_log(f"Removed nxm:// association from {path}")
            except OSError as exc:
                nxm_log(f"Could not clean {path}: {exc}")

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

        desktop_path.write_text(desktop_content, encoding="utf-8")
        nxm_log(f"Wrote NXM .desktop file: {desktop_path} (Exec={exec_cmd})")

        # Also write to Flatpak exports dir so Flatpak-sandboxed browsers can
        # see the handler.  The dir may not exist if Flatpak isn't installed,
        # so we only write if the parent already exists.
        flatpak_path = cls._flatpak_desktop_path()
        if flatpak_path.parent.exists():
            try:
                flatpak_path.write_text(desktop_content, encoding="utf-8")
                nxm_log(f"Wrote NXM .desktop file to Flatpak exports: {flatpak_path}")
            except OSError as exc:
                nxm_log(f"Could not write Flatpak .desktop file: {exc}")

        # Register as default handler.
        # Inside a Flatpak sandbox xdg-mime is not available directly; use
        # flatpak-spawn --host to run it on the host system instead.
        in_flatpak = Path("/.flatpak-info").exists()
        xdg_mime_cmd = cls._host_cmd(in_flatpak, "xdg-mime")
        if xdg_mime_cmd is None:
            nxm_log("xdg-mime not available — nxm:// handler not registered")
            return False

        try:
            result = subprocess.run(
                [*xdg_mime_cmd, "default", _DESKTOP_FILE_NAME,
                 "x-scheme-handler/nxm"],
                check=False,
                capture_output=True,
            )
            if in_flatpak and cls._is_host_tool_missing(result.returncode):
                nxm_log("xdg-mime not installed on host — nxm:// handler not registered")
                return False
            if result.returncode != 0:
                nxm_log(f"xdg-mime default failed: {result.stderr.decode(errors='replace').strip()}")
                return False
            nxm_log("Registered nxm:// protocol handler via xdg-mime")
        except OSError as exc:
            nxm_log(f"xdg-mime default failed: {exc}")
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
        cls._xdg_settings_register(in_flatpak)

        # Refresh the desktop database so Flatpak apps pick up the new entry.
        udd_cmd = cls._host_cmd(in_flatpak, "update-desktop-database")
        if udd_cmd is not None:
            host_missing_logged = False
            for db_dir in {desktop_path.parent, flatpak_path.parent}:
                if not db_dir.exists():
                    continue
                try:
                    result = subprocess.run(
                        [*udd_cmd, str(db_dir)],
                        check=False,
                        capture_output=True,
                    )
                    if in_flatpak and cls._is_host_tool_missing(result.returncode):
                        if not host_missing_logged:
                            nxm_log("update-desktop-database not installed on host — desktop database not refreshed")
                            host_missing_logged = True
                        continue
                    if result.returncode != 0:
                        nxm_log(f"update-desktop-database failed for {db_dir}: {result.stderr.decode(errors='replace').strip()}")
                        continue
                    nxm_log(f"Updated desktop database: {db_dir}")
                except OSError as exc:
                    nxm_log(f"update-desktop-database failed for {db_dir}: {exc}")

        cls._log_effective_handler(in_flatpak)
        return True

    @classmethod
    def _log_effective_handler(cls, in_flatpak: bool) -> None:
        """Query what the system NOW resolves for nxm:// and log it.

        Registration can 'succeed' while a DE-specific mimeapps.list or a
        stale desktop cache still routes nxm:// elsewhere — this readback is
        the ground truth for diagnosing 'button does nothing' reports.
        """
        base = cls._host_cmd(in_flatpak, "xdg-mime")
        if base is None:
            return
        try:
            result = subprocess.run(
                [*base, "query", "default", "x-scheme-handler/nxm"],
                check=False,
                capture_output=True,
                timeout=10,
            )
            effective = result.stdout.decode(errors="replace").strip()
            if in_flatpak and cls._is_host_tool_missing(result.returncode):
                return
            if effective == _DESKTOP_FILE_NAME:
                nxm_log(f"nxm:// handler verified: system resolves to {effective}")
            else:
                nxm_log(
                    "nxm:// handler MISMATCH after registration: system "
                    f"resolves to {effective!r}, expected {_DESKTOP_FILE_NAME!r}"
                )
        except (OSError, subprocess.TimeoutExpired) as exc:
            nxm_log(f"xdg-mime query failed: {exc}")

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

    _servers: dict[Path, socket.socket] = {}
    _threads: list[threading.Thread] = []
    _callback: Optional[Callable[[str], None]] = None
    # Serializes ensure_bound() against itself and shutdown() — ensure_bound
    # runs on a worker thread while shutdown comes from the UI thread.
    _lock = threading.Lock()

    @classmethod
    def send_to_running(cls, nxm_url: str) -> bool:
        """
        Try to send *nxm_url* to an already-running instance.

        Tries every candidate socket path (env-derived + the home and /tmp
        fallbacks) so the handoff still works when the browser-spawned process
        resolves a different path than the long-running instance — including a
        *different install variant* (Flatpak vs AppImage/native). Returns True
        as soon as one delivery succeeds, False if no instance was reachable
        on any path.
        """
        candidates = _candidate_socket_paths()
        payload = json.dumps({"nxm_url": nxm_url}).encode("utf-8")
        tried: list[str] = []

        for path in candidates:
            if not path.exists():
                tried.append(f"{path} (absent)")
                continue
            try:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.settimeout(2)
                sock.connect(str(path))
                sock.sendall(payload)
                sock.close()
                nxm_log(f"Sent NXM link to running instance via {path}")
                return True
            except socket.timeout as exc:
                # An instance is listening but didn't answer in time (busy /
                # backlog full). Do NOT delete the socket — it is live, and
                # unlinking it would permanently orphan the running instance:
                # every later click would open a new window.
                tried.append(f"{path} (timeout: {exc})")
            except ConnectionRefusedError as exc:
                # Nobody is listening on this inode — genuinely stale leftover
                # from a crashed instance. Safe to clean up.
                tried.append(f"{path} ({exc})")
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            except OSError as exc:
                # Permission problems, vanished mid-connect, etc. — leave the
                # file alone; we can't tell whether it belongs to a live
                # instance.
                tried.append(f"{path} ({exc})")

        nxm_log(
            "NXM handoff: no running instance reachable "
            f"(FLATPAK_ID={os.environ.get('FLATPAK_ID', '')!r}, "
            f"XDG_RUNTIME_DIR={os.environ.get('XDG_RUNTIME_DIR', '')!r}) "
            f"— tried {tried} — opening new window"
        )
        return False

    @classmethod
    def _bind_targets(cls) -> set[Path]:
        """Paths this server should hold: primary + env-independent fallbacks."""
        return {_SOCKET_PATH, _FALLBACK_SOCKET_PATH, _home_socket_path()}

    @staticmethod
    def _is_live(path: Path) -> bool:
        """True if a live listener currently answers on *path*.

        The probe connect is harmless to the listener: its accept loop reads
        zero bytes and just closes the connection.
        """
        try:
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            probe.settimeout(0.5)
            probe.connect(str(path))
            probe.close()
            return True
        except OSError:
            return False

    @classmethod
    def _accept_loop(cls, srv: socket.socket) -> None:
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
                    cb = cls._callback
                    if url and cb is not None:
                        nxm_log(f"Received NXM link from new instance: {url}")
                        cb(url)
            except Exception as exc:
                nxm_log(f"Error handling IPC message: {exc}")
            finally:
                conn.close()

    @classmethod
    def _bind_path(cls, path: Path) -> bool:
        """Bind *path* and start an accept thread on it.

        Never steals a live socket: if another running instance answers on
        the path, that instance keeps it. (Blindly unlinking here is what used
        to orphan the first instance whenever a second full instance launched
        — after which no click could reach either.)
        """
        try:
            if path.exists():
                if cls._is_live(path):
                    nxm_log(
                        f"NXM IPC: {path} is held by another live instance — leaving it")
                    return False
                path.unlink(missing_ok=True)  # dead leftover — safe to replace
            path.parent.mkdir(parents=True, exist_ok=True)
            srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            srv.bind(str(path))
            srv.listen(4)
            cls._servers[path] = srv
            t = threading.Thread(
                target=cls._accept_loop, args=(srv,), daemon=True, name="nxm-ipc"
            )
            t.start()
            cls._threads.append(t)
            return True
        except OSError as exc:
            nxm_log(f"NXM IPC: could not bind {path}: {exc}")
            return False

    @classmethod
    def start_server(cls, callback: Callable[[str], None]) -> None:
        """
        Start listening for NXM links from new instances.

        *callback* is called (from a background thread) with the nxm:// URL
        string whenever another instance sends one; it must marshal any UI
        work onto the main thread itself.

        Binds the primary env-derived path *and* the env-independent home +
        /tmp fallbacks so a browser-spawned sender that lost XDG_RUNTIME_DIR,
        runs under a different sandbox, or is a different install variant can
        still reach us on a common path.
        """
        # Tear down any previous server state so a re-start (without an
        # intervening shutdown) doesn't leak the old sockets/threads.
        cls.shutdown()

        cls._callback = callback
        bound = [str(p) for p in sorted(cls._bind_targets()) if cls._bind_path(p)]
        nxm_log(
            f"NXM IPC server listening on {bound} "
            f"(FLATPAK_ID={os.environ.get('FLATPAK_ID', '')!r}, "
            f"XDG_RUNTIME_DIR={os.environ.get('XDG_RUNTIME_DIR', '')!r})"
        )

    @classmethod
    def ensure_bound(cls) -> None:
        """Self-heal the IPC sockets (called periodically by the running app).

        Another instance (an older build blindly unlinks our paths on its own
        startup/shutdown) or a /tmp cleaner can remove our socket files while
        we run — after that, no sender can reach us and every 'Download with
        Mod Manager' click opens a new window. Re-bind any of our paths whose
        socket file has vanished, and pick up paths that a now-exited instance
        used to hold. Safe to call from a worker thread; no-op if the server
        was never started.
        """
        if cls._callback is None:
            return
        if not cls._lock.acquire(blocking=False):
            return  # a previous ensure_bound is still running
        try:
            if cls._callback is None:
                return  # shut down while we waited
            cls._threads = [t for t in cls._threads if t.is_alive()]
            for path in cls._bind_targets():
                srv = cls._servers.get(path)
                if srv is not None and path.exists():
                    continue  # bound and still on disk — healthy
                if srv is None and path.exists() and cls._is_live(path):
                    continue  # another live instance legitimately holds it
                if srv is not None:
                    # Our socket file vanished — the fd is orphaned (clients
                    # connect by path, not inode). Drop it and bind fresh.
                    # shutdown() first: close() alone doesn't release the
                    # listener while the accept thread is blocked in accept().
                    try:
                        srv.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    try:
                        srv.close()
                    except OSError:
                        pass
                    cls._servers.pop(path, None)
                if cls._bind_path(path):
                    nxm_log(f"NXM IPC: (re-)bound {path}")
        finally:
            cls._lock.release()

    @classmethod
    def shutdown(cls) -> None:
        """Close every IPC socket we bound and remove only OUR socket files.

        Never unlink a path another instance holds — that would orphan a
        still-running instance and route every future click to a new window.
        """
        with cls._lock:
            cls._callback = None
            servers = dict(cls._servers)
            cls._servers = {}
            for srv in servers.values():
                # shutdown() wakes the accept thread; close() alone leaves the
                # listener alive (the blocked accept() holds the kernel-side
                # open file description) and the liveness probe below would
                # then mistake our own dying socket for another instance's.
                try:
                    srv.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    srv.close()
                except OSError:
                    pass
            for t in cls._threads:
                t.join(timeout=1)
            cls._threads = []
            for path in servers:
                # With our server gone, a live answer on the path means
                # another instance has re-bound it — leave their file alone.
                if not cls._is_live(path):
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        pass
