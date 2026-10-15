"""GUI-free SMAPI download and native installation helpers."""

from __future__ import annotations

import json as _json
import shutil
import stat
import tempfile
import urllib.request
import zipfile
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from Games.base_game import BaseGame

LogFn = Callable[[str], None]

_GITHUB_API_URL = "https://api.github.com/repos/Pathoschild/SMAPI/releases/latest"
LINUX_MOD_NAME = "SMAPI (Linux)"

#: Payload zip inside the installer archive, in preference order.  SMAPI has
#: shipped this under both ``unix`` (older) and ``linux`` (current) folders.
_PAYLOAD_CANDIDATES = ("internal/linux/install.dat", "internal/unix/install.dat")

#: Vanilla launcher (no extension) that SMAPI displaces.
_GAME_LAUNCHER = "StardewValley"
_GAME_LAUNCHER_BACKUP = "StardewValley-original"
_SMAPI_LAUNCHER = "StardewModdingAPI"
_UNIX_LAUNCHER = "unix-launcher.sh"

#: deps.json copy - SMAPI reuses the game's dependency manifest.
_GAME_DEPS = "Stardew Valley.deps.json"
_SMAPI_DEPS = "StardewModdingAPI.deps.json"


def _noop(_msg: str) -> None:
    pass


# ---------------------------------------------------------------------------
# Fetch + download
# ---------------------------------------------------------------------------

def fetch_latest_smapi_asset() -> tuple[str, str]:
    """Return (version_tag, download_url) for the latest SMAPI installer zip."""
    req = urllib.request.Request(
        _GITHUB_API_URL,
        headers={"Accept": "application/vnd.github+json",
                 "User-Agent": "ModManager/1.0"},
    )
    from Utils.ca_bundle import get_ssl_context
    with urllib.request.urlopen(req, timeout=15, context=get_ssl_context()) as resp:
        data = _json.loads(resp.read().decode())
    tag = data.get("tag_name", "unknown")
    assets = data.get("assets", [])
    for asset in assets:
        nl = asset.get("name", "").lower()
        if (nl.endswith(".zip") and "smapi" in nl and "installer" in nl
                and "double" not in nl):
            return tag, asset["browser_download_url"]
    for asset in assets:
        nl = asset.get("name", "").lower()
        if nl.endswith(".zip") and "smapi" in nl and "double" not in nl:
            return tag, asset["browser_download_url"]
    raise RuntimeError("No SMAPI installer zip found in the latest GitHub release.")


def download_smapi(url: str, dest: Path, reporthook=None) -> None:
    """Download *url* to *dest* over HTTPS using the app's resolved CA bundle."""
    from Utils.ca_bundle import download_file
    download_file(url, dest, reporthook=reporthook)


# ---------------------------------------------------------------------------
# Payload extraction
# ---------------------------------------------------------------------------

def _chmod_exec(path: Path) -> None:
    try:
        path.chmod(path.stat().st_mode
                   | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        pass


def find_payload(installer_root: Path) -> Path:
    """Return the Linux ``install.dat`` payload zip inside an unpacked installer.

    The release zip wraps everything in a top-level ``SMAPI x.y.z installer/``
    folder, so match on the path *suffix* rather than an exact relative path.
    The archive also ships ``internal/windows`` and ``internal/macOS``
    payloads - picking either of those would install the wrong platform's
    binaries, so only the documented Linux/unix locations are accepted.
    """
    matches = sorted(installer_root.rglob("install.dat"))
    for rel in _PAYLOAD_CANDIDATES:
        for cand in matches:
            if cand.is_file() and cand.as_posix().endswith("/" + rel):
                return cand
    # No wrapper dir at all (payload sitting at the root of the search path).
    for rel in _PAYLOAD_CANDIDATES:
        cand = installer_root / rel
        if cand.is_file():
            return cand
    raise RuntimeError(
        "Could not find 'internal/linux/install.dat' inside the SMAPI "
        "archive - the archive may be corrupt or not a SMAPI installer.")


MAX_PAYLOAD_BYTES = 256 * 1024 * 1024
MAX_TREE_BYTES = 512 * 1024 * 1024
MAX_FILE_BYTES = 128 * 1024 * 1024


def _safe_entry(entry):
    from Utils.wizards.payload import PayloadInstallError

    path = Path(entry.filename)
    mode = (entry.external_attr >> 16) & 0o170000
    if (not path.parts or path.is_absolute() or ".." in path.parts
            or "\\" in entry.filename or ":" in entry.filename
            or any(ord(char) < 32 for char in entry.filename)
            or entry.flag_bits & 1 or mode not in (0, stat.S_IFREG, stat.S_IFDIR)):
        raise PayloadInstallError("unsafe_smapi_archive", "The SMAPI archive contains an unsafe entry.",
                                  {"entry": entry.filename})
    return path


def _copy_entry(archive, entry, target, limit, checkpoint):
    from Utils.wizards.payload import PayloadInstallError

    if entry.file_size > limit:
        raise PayloadInstallError("unsafe_smapi_archive", "An SMAPI archive entry is too large.")
    target.parent.mkdir(parents=True, exist_ok=True)
    with archive.open(entry) as source, target.open("xb") as output:
        written = 0
        while chunk := source.read(1024 * 1024):
            checkpoint()
            written += len(chunk)
            if written > min(entry.file_size, limit):
                raise PayloadInstallError("unsafe_smapi_archive", "An SMAPI entry exceeded its declared size.")
            output.write(chunk)
    if written != entry.file_size:
        raise PayloadInstallError("unsafe_smapi_archive", "An SMAPI entry is incomplete.")


def extract_smapi_payload(archive: Path, dest: Path, log_fn: LogFn = _noop,
                          checkpoint=lambda: None, *, work_dir: Path | None = None) -> int:
    from Utils.wizards.payload import PayloadInstallError

    if work_dir is not None:
        Path(work_dir).mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="amethyst-smapi-archive-", dir=work_dir) as raw:
        nested = Path(raw) / "install.zip"
        checkpoint()
        with zipfile.ZipFile(archive) as outer:
            entries = outer.infolist()
            if len(entries) > 50000:
                raise PayloadInstallError("unsafe_smapi_archive", "The installer has too many entries.")
            chosen = None
            for suffix in _PAYLOAD_CANDIDATES:
                matches = [entry for entry in entries if not entry.is_dir()
                           and ("/" + entry.filename).endswith("/" + suffix)]
                if len(matches) > 1:
                    raise PayloadInstallError("ambiguous_smapi_payload", "The installer has multiple Linux payloads.")
                if matches:
                    chosen = matches[0]
                    break
            if chosen is None:
                raise PayloadInstallError("smapi_payload_missing", "The installer has no Linux/unix payload.")
            _safe_entry(chosen)
            _copy_entry(outer, chosen, nested, MAX_PAYLOAD_BYTES, checkpoint)
        log_fn(f"SMAPI Wizard: preparing {archive.name}")
        with zipfile.ZipFile(nested) as payload:
            entries = payload.infolist()
            if len(entries) > 50000 or sum(entry.file_size for entry in entries) > MAX_TREE_BYTES:
                raise PayloadInstallError("unsafe_smapi_archive", "The SMAPI payload exceeds install limits.")
            seen = set()
            count = 0
            for entry in entries:
                checkpoint()
                relative = _safe_entry(entry)
                key = relative.as_posix().casefold()
                if key in seen:
                    raise PayloadInstallError("unsafe_smapi_archive", "The SMAPI payload contains duplicate paths.")
                seen.add(key)
                if entry.is_dir():
                    continue
                if relative.as_posix().casefold() in {
                        name.casefold() for name in (_GAME_LAUNCHER, _GAME_LAUNCHER_BACKUP, _GAME_DEPS)}:
                    raise PayloadInstallError("unsafe_smapi_archive", "The payload contains a reserved vanilla game file.")
                _copy_entry(payload, entry, dest / relative, MAX_FILE_BYTES, checkpoint)
                count += 1
        for name in (_SMAPI_LAUNCHER, _UNIX_LAUNCHER):
            if (dest / name).is_file():
                _chmod_exec(dest / name)
        _find_launcher(dest)
        if not (dest / "StardewModdingAPI.dll").is_file():
            raise PayloadInstallError("smapi_payload_missing", "The SMAPI assembly is absent from the payload.")
        return count


def extract_payload(payload: Path, dest: Path) -> int:
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(payload, "r") as zf:
        zf.extractall(dest)
        count = sum(1 for i in zf.infolist() if not i.is_dir())
    for name in (_SMAPI_LAUNCHER, _UNIX_LAUNCHER):
        path = dest / name
        if path.is_file():
            _chmod_exec(path)
    return count


# ---------------------------------------------------------------------------
# Launcher wiring
# ---------------------------------------------------------------------------

def _find_launcher(dest: Path) -> Path:
    for name in (_UNIX_LAUNCHER, _SMAPI_LAUNCHER):
        if not (dest / name).is_file():
            raise RuntimeError(
                f"'{name}' is missing after extraction - "
                "the SMAPI payload did not unpack correctly.")
    return dest / _UNIX_LAUNCHER


def wire_game_folder(game_dir: Path, log_fn: LogFn = _noop) -> None:
    """Apply the in-place launcher swap inside a real *game_dir*.

    Copies the game's deps.json for SMAPI, then renames the vanilla launcher
    aside and puts SMAPI's in its place. Idempotent: re-running after SMAPI is
    already installed will not clobber ``StardewValley-original`` with the
    SMAPI launcher.
    """
    launcher = _find_launcher(game_dir)
    deps_src = game_dir / _GAME_DEPS
    deps_dst = game_dir / _SMAPI_DEPS
    if deps_src.is_file():
        shutil.copy2(deps_src, deps_dst)
        log_fn(f"SMAPI Wizard: copied {_GAME_DEPS} → {_SMAPI_DEPS}")
    else:
        log_fn(f"SMAPI Wizard: warning - {_GAME_DEPS} not found in the game "
               "folder; SMAPI may fail to start.")

    vanilla = game_dir / _GAME_LAUNCHER
    backup = game_dir / _GAME_LAUNCHER_BACKUP
    if backup.is_file():
        # Already installed once: `vanilla` is a previous SMAPI launcher, so
        # overwrite it and leave the real backup untouched.
        log_fn(f"SMAPI Wizard: {_GAME_LAUNCHER_BACKUP} already present - "
               "reusing the existing vanilla backup.")
    elif vanilla.is_file():
        shutil.move(str(vanilla), str(backup))
        log_fn(f"SMAPI Wizard: renamed {_GAME_LAUNCHER} → {_GAME_LAUNCHER_BACKUP}")
    else:
        log_fn(f"SMAPI Wizard: warning - no {_GAME_LAUNCHER} launcher found "
               "to back up.")

    launcher.replace(vanilla)
    _chmod_exec(vanilla)
    log_fn(f"SMAPI Wizard: installed {_UNIX_LAUNCHER} as {_GAME_LAUNCHER}")


def _stage_launcher(dest: Path, log_fn: LogFn = _noop) -> None:
    """Stage SMAPI's official launcher; deployment backs up the vanilla one."""
    launcher = _find_launcher(dest)
    target = dest / _GAME_LAUNCHER
    launcher.replace(target)
    _chmod_exec(target)
    log_fn(f"SMAPI Wizard: staged {_UNIX_LAUNCHER} as {_GAME_LAUNCHER}.")


def _stage_deps_json(game_dir: Path | None, dest: Path, log_fn: LogFn = _noop) -> None:
    """Copy ``StardewModdingAPI.deps.json`` into a staged payload.

    Sourced from the game folder's ``Stardew Valley.deps.json`` (vanilla file,
    always present in a real install).  Staged rather than generated so the
    deployed SMAPI sees the same manifest the official installer would create.
    """
    if game_dir is None:
        log_fn("SMAPI Wizard: warning - game path not configured, cannot stage "
               f"{_SMAPI_DEPS}.")
        return
    src = Path(game_dir) / _GAME_DEPS
    if not src.is_file():
        log_fn(f"SMAPI Wizard: warning - {_GAME_DEPS} not found in the game "
               f"folder; {_SMAPI_DEPS} was not staged.")
        return
    shutil.copy2(src, dest / _SMAPI_DEPS)
    log_fn(f"SMAPI Wizard: staged {_SMAPI_DEPS} from the game folder.")


def stage_smapi_payload(game_dir: Path | None, dest: Path,
                        log_fn: LogFn = _noop) -> None:
    _stage_deps_json(game_dir, dest, log_fn=log_fn)
    _stage_launcher(dest, log_fn=log_fn)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def game_sources(game, mode):
    from Utils.wizards.payload import PayloadInstallError, archive_token

    root = game.get_game_path()
    if root is None or not root.is_dir():
        raise PayloadInstallError("smapi_game_files_missing", "The native Stardew Valley folder is unavailable.")
    root = Path(root).resolve()
    source = {"deps": root / _GAME_DEPS}
    if mode == "game":
        backup = root / _GAME_LAUNCHER_BACKUP
        if backup.is_symlink() or (backup.exists() and not backup.is_file()):
            raise PayloadInstallError("unsafe_smapi_backup", "The vanilla launcher backup is unsafe.")
        source["launcher"] = backup if backup.is_file() else root / _GAME_LAUNCHER
    result = {}
    for key, path in source.items():
        try:
            result[key] = {"path": str(path), "identity": archive_token(path)}
        except (OSError, ValueError) as exc:
            raise PayloadInstallError("smapi_game_files_missing", "A native game launcher or dependency manifest is unavailable.",
                                      {"path": str(path)}) from exc
    return result


def _stage_game_sources(game, payload_root, mode, checkpoint, *, allow_links):
    from Utils.wizards.payload import PayloadInstallError

    sources = game_sources(game, mode)
    for key, record in sources.items():
        checkpoint()
        source = Path(record["path"])
        if source.is_symlink() and not allow_links:
            raise PayloadInstallError("unsafe_smapi_game_file", "Restore left a linked game launcher or manifest.")
        target = payload_root / (_SMAPI_DEPS if key == "deps" else _GAME_LAUNCHER_BACKUP)
        shutil.copy2(source, target)
    checkpoint()
    if sources != game_sources(game, mode):
        raise PayloadInstallError("smapi_game_files_changed", "Game files changed while preparing SMAPI.")
    return sources


@contextmanager
def prepare_smapi_payload(game, archive, mode, *, log_fn=_noop, checkpoint=lambda: None):
    from Utils.wizards.payload import PreparedPayload, archive_token

    archive = Path(archive)
    identity = archive_token(archive)
    with tempfile.TemporaryDirectory(prefix="amethyst-smapi-payload-") as raw:
        root = Path(raw)
        extract_smapi_payload(archive, root, log_fn, checkpoint)
        _stage_launcher(root, log_fn)
        sources = _stage_game_sources(game, root, mode, checkpoint, allow_links=game.get_deploy_active())
        files = tuple(sorted(path.relative_to(root) for path in root.rglob("*") if path.is_file()))
        payload = PreparedPayload(archive, root, files, identity,
                                  keep_existing=(Path(_GAME_LAUNCHER_BACKUP),) if mode == "game" else (),
                                  remove_files=(Path(_UNIX_LAUNCHER),))
        payload.check_archive()

        def refresh_sources():
            if game.get_deploy_active():
                from Utils.wizards.payload import PayloadInstallError
                raise PayloadInstallError("smapi_game_still_deployed", "Restore left the game deployed.")
            sources.clear()
            sources.update(_stage_game_sources(game, root, mode, checkpoint, allow_links=False))

        yield payload, sources, refresh_sources


def install_smapi(game: "BaseGame", archive: Path, mode: str = "game", *,
                  mod_name: str = LINUX_MOD_NAME, modlist_path: "Path | None" = None,
                  restore_first: bool = True, delete_archive: bool = True,
                  log_fn: LogFn = _noop) -> tuple[str, int, "str | None"]:
    from Utils.wizards.payload import PayloadInstallError, install_prepared_payload

    with prepare_smapi_payload(game, archive, mode, log_fn=log_fn) as (payload, _sources, refresh):
        def before_write():
            if mode == "game" and not restore_first:
                if game.get_deploy_active():
                    raise PayloadInstallError("smapi_game_still_deployed", "Restore the game before installing SMAPI.")
                refresh()

        return install_prepared_payload(
            game, payload, mode, mod_fallback_name="SMAPI", mod_name_override=mod_name,
            modlist_path=modlist_path, restore_first=restore_first, delete_archive=delete_archive,
            log_fn=log_fn, before_write=before_write, after_restore=refresh)
