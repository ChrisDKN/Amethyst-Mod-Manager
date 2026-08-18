"""On-demand install and launch of the ``me3`` mod loader.

me3 is the FROMSOFTWARE mod loader (https://github.com/garyttierney/me3).  We
never bundle it - same policy as umu-run - because it is a host-side tool that
launches Proton itself and updates on its own release cadence.

Installing it is not "drop one binary somewhere".  The release tarball carries
both halves of the loader::

    bin/me3                     -> ~/.local/bin/me3            (Linux CLI)
    bin/win64/me3-launcher.exe  -> ~/.local/share/me3/windows-bin/
    bin/win64/me3_mod_host.dll  -> ~/.local/share/me3/windows-bin/

The two Windows files are the injection payload; me3 looks for them under
``$XDG_DATA_HOME/me3/windows-bin`` at launch.  Extracting only the Linux binary
produces a CLI that runs and then fails to inject, so :func:`install_me3`
reproduces the upstream install-user.sh layout for all three.  We fetch and
extract the tarball ourselves rather than piping installer.sh into sh.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import tarfile
import threading
import time
import urllib.request
from pathlib import Path
from typing import Callable

LogFn = Callable[[str], None]

_LATEST_API = "https://api.github.com/repos/garyttierney/me3/releases/latest"
_TARBALL_NAME = "me3-linux-amd64.tar.gz"

# Members we install, mapped to their destination directory kind.
_LINUX_BIN = "bin/me3"
_WINDOWS_BIN = ("bin/win64/me3-launcher.exe", "bin/win64/me3_mod_host.dll")
# Bare names of the two payload files, for verifying an existing install.
_WINDOWS_PAYLOAD_NAMES = ("me3-launcher.exe", "me3_mod_host.dll")

_UPDATE_INTERVAL = 7 * 24 * 3600

# One download attempt per session - an offline or rate-limited box must not
# re-try on every Play press.
_attempted = False
_attempt_lock = threading.Lock()


def _noop(_msg: str) -> None:
    pass


def _log_fn(log_fn: "LogFn | None") -> LogFn:
    if log_fn is not None:
        return log_fn
    try:
        from Utils.app_log import app_log
        return lambda m: app_log(f"me3: {m}")
    except Exception:
        return _noop


def user_bin_dir() -> Path:
    """Where upstream's installer puts the me3 CLI."""
    return Path.home() / ".local" / "bin"


def _data_dir_candidates() -> "list[Path]":
    """Data roots me3's payload could live under, host locations included.

    Inside our sandbox ``XDG_DATA_HOME`` points at the app's private data dir,
    not the host's ``~/.local/share`` where me3 actually installs - so probe the
    host paths (and Flatpak's HOST_XDG_* hint) separately, the same way
    :func:`Utils.lutris_finder.find_umu_run` does.
    """
    seen: list[Path] = []
    for raw in (os.environ.get("HOST_XDG_DATA_HOME", ""),
                os.environ.get("XDG_DATA_HOME", ""),
                str(Path.home() / ".local" / "share")):
        if not raw:
            continue
        path = Path(raw)
        if path not in seen:
            seen.append(path)
    return seen


def windows_bin_dir() -> Path:
    """Where me3's Windows injection payload should be installed."""
    # Install target: always the host convention, never the sandbox data dir
    # (install_me3 refuses to run inside Flatpak for exactly that reason).
    return Path.home() / ".local" / "share" / "me3" / "windows-bin"


def find_windows_payload() -> "Path | None":
    """Return the dir holding me3's Windows payload, or None if incomplete."""
    for base in _data_dir_candidates():
        candidate = base / "me3" / "windows-bin"
        if all((candidate / n).is_file() for n in _WINDOWS_PAYLOAD_NAMES):
            return candidate
    return None


def find_me3() -> "Path | None":
    """Return the me3 CLI, or None when it is not installed."""
    if _in_flatpak():
        # PATH inside the sandbox is the sandbox's own; me3 lives on the host.
        # Home is normally shared through, so look there directly.
        host = user_bin_dir() / "me3"
        return host if host.is_file() else None
    found = shutil.which("me3")
    if found:
        return Path(found)
    # A desktop-launched AppImage often has no ~/.local/bin on PATH.
    candidate = user_bin_dir() / "me3"
    return candidate if candidate.is_file() else None


def _in_flatpak() -> bool:
    return Path("/.flatpak-info").exists()


def me3_available() -> bool:
    """Whether a me3 we can actually invoke exists (host or via flatpak-spawn)."""
    if _in_flatpak():
        return shutil.which("flatpak-spawn") is not None and \
            find_me3() is not None
    return find_me3() is not None


def me3_version(log_fn: "LogFn | None" = None) -> str:
    """Return me3's reported version, or '' when it cannot be determined."""
    import re
    import subprocess

    cmd = _binary_argv()
    if cmd is None:
        return ""
    try:
        proc = subprocess.run([*cmd, "--version"], capture_output=True,
                              text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        _log_fn(log_fn)(f"could not run me3 --version: {exc}")
        return ""
    match = re.search(r"(\d+\.\d+\.\d+)", (proc.stdout or "") + (proc.stderr or ""))
    return match.group(1) if match else ""


def _binary_argv() -> "list[str] | None":
    """Return the argv prefix that invokes me3, or None when unavailable."""
    found = find_me3()
    if _in_flatpak() and shutil.which("flatpak-spawn"):
        # me3 lives on the host and needs the host's Steam/Proton, so run it
        # there. Pass the absolute path we resolved rather than a bare "me3":
        # the host shell that flatpak-spawn starts is non-interactive and may
        # not have ~/.local/bin on PATH.
        exe = str(found) if found is not None else "me3"
        return ["flatpak-spawn", "--host", "--directory=/", exe]
    return [str(found)] if found is not None else None


def launch_command(cli_id: str, profile_path: Path) -> "list[str] | None":
    """Return the argv that launches cli_id with profile_path, or None."""
    base = _binary_argv()
    if base is None:
        return None
    return [*base, "launch", "-g", cli_id, "-p", str(profile_path)]


def _state_path() -> Path:
    from Utils.config_paths import get_tools_dir
    return get_tools_dir() / "me3.json"


def _read_state() -> dict:
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_state(tag: str, checked: float) -> None:
    try:
        path = _state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"tag": tag, "checked": checked}, indent=2),
                        encoding="utf-8")
    except OSError:
        pass


def installed_tag() -> str:
    """Release tag of the copy we installed ('' when we did not install one)."""
    return _read_state().get("tag", "") if find_me3() is not None else ""


def _fetch_latest(log: LogFn) -> "tuple[str, str] | None":
    """Return (tag, tarball_url) for the newest release, or None."""
    from Utils.ca_bundle import get_ssl_context
    try:
        req = urllib.request.Request(
            _LATEST_API, headers={"User-Agent": "Amethyst-Mod-Manager"})
        with urllib.request.urlopen(req, timeout=30,
                                    context=get_ssl_context()) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        log(f"could not reach the me3 release feed: {exc}")
        return None

    tag = data.get("tag_name") or ""
    for asset in data.get("assets") or []:
        if asset.get("name") == _TARBALL_NAME:
            url = asset.get("browser_download_url") or ""
            if tag and url:
                return tag, url
    log(f"no {_TARBALL_NAME} in the latest me3 release.")
    return None


def _install_member(tar: tarfile.TarFile, member_name: str, dest_dir: Path,
                    *, executable: bool, log: LogFn) -> bool:
    """Extract one tar member into dest_dir, atomically."""
    member = None
    for candidate in (member_name, f"./{member_name}"):
        try:
            member = tar.getmember(candidate)
            break
        except KeyError:
            continue
    if member is None:
        log(f"{member_name} missing from the me3 tarball.")
        return False

    src = tar.extractfile(member)
    if src is None:
        log(f"{member_name} could not be read from the me3 tarball.")
        return False

    dest = dest_dir / Path(member_name).name
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        with src, open(tmp, "wb") as out:
            shutil.copyfileobj(src, out)
        mode = tmp.stat().st_mode | stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH
        if executable:
            mode |= stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
        tmp.chmod(mode)
        os.replace(tmp, dest)
    except OSError as exc:
        log(f"could not write {dest}: {exc}")
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    return True


def install_me3(log_fn: "LogFn | None" = None) -> bool:
    """Download me3 and install the CLI plus its Windows payload.

    Blocking (~10 MB); call from a worker thread.  Returns True when a usable
    me3 is in place afterwards.
    """
    log = _log_fn(log_fn)

    if _in_flatpak():
        # A copy installed from inside the sandbox lands in the sandbox and the
        # host Steam/Proton could never see it.
        log("running inside Flatpak - install me3 on the host instead: "
            "curl --proto '=https' --tlsv1.2 -sSfL "
            "https://github.com/garyttierney/me3/releases/latest/download/"
            "installer.sh | sh")
        return False

    latest = _fetch_latest(log)
    if latest is None:
        return False
    tag, url = latest

    log(f"downloading me3 {tag} ...")
    from Utils.ca_bundle import get_ssl_context
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "Amethyst-Mod-Manager"})
        with urllib.request.urlopen(req, timeout=180,
                                    context=get_ssl_context()) as resp:
            payload = resp.read()
    except Exception as exc:
        log(f"me3 download failed: {exc}")
        return False

    import io
    try:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as tar:
            if not _install_member(tar, _LINUX_BIN, user_bin_dir(),
                                   executable=True, log=log):
                return False
            # Without these the CLI runs but cannot inject into the game.
            for name in _WINDOWS_BIN:
                if not _install_member(tar, name, windows_bin_dir(),
                                       executable=False, log=log):
                    return False
    except (tarfile.TarError, OSError, ValueError) as exc:
        log(f"could not unpack the me3 tarball: {exc}")
        return False

    _write_state(tag, time.time())
    log(f"me3 {tag} installed to {user_bin_dir() / 'me3'}.")
    if find_me3() is None:
        log(f"note: add {user_bin_dir()} to PATH so 'me3' is found.")
    return True


def ensure_me3(log_fn: "LogFn | None" = None) -> "Path | None":
    """Return a fully usable me3, fetching one if this session has not tried yet.

    "Fully usable" means the CLI *and* its Windows payload: a CLI on its own
    runs and then fails to inject, so a half-install is repaired rather than
    reported as fine. Blocking (~10 MB) - call from a worker thread.
    """
    global _attempted

    found = find_me3()
    complete = found is not None and find_windows_payload() is not None
    if complete:
        return found
    if _in_flatpak():
        # A copy installed in here would be invisible to the host Steam.
        return None

    with _attempt_lock:
        if _attempted:
            return None
        _attempted = True

    log = _log_fn(log_fn)
    if found is None:
        log("not installed - fetching it now.")
    else:
        log("the Windows injection files are missing - reinstalling to repair.")

    if not install_me3(log_fn):
        return None
    return find_me3() if find_windows_payload() is not None else None


def install_hint() -> str:
    """One-line instruction for installing me3 by hand."""
    return ("Install the me3 mod loader: curl --proto '=https' --tlsv1.2 -sSfL "
            "https://github.com/garyttierney/me3/releases/latest/download/"
            "installer.sh | sh")
