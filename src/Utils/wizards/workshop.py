"""Steam Workshop downloads through an isolated DepotDownloader process."""

from __future__ import annotations

import codecs
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import platform
import queue
import re
import selectors
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
from urllib.parse import parse_qs, urlparse
import zipfile

import requests

from Utils.config_paths import get_config_dir
from Utils.mods.names import sanitize_mod_folder_name

RELEASE = "DepotDownloader_3.4.0"
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_PROMPT = re.compile(
    r"(?:Enter account password[^\r\n]*|(?:STEAM GUARD! )?Please enter[^\r\n]*):\s*$",
    re.IGNORECASE)


class DownloadCancelled(Exception):
    pass


def _check_cancel(cancel):
    if cancel.is_set():
        raise DownloadCancelled()


def parse_item_id(value: str) -> str:
    value = value.strip()
    if not re.fullmatch(r"[0-9]+", value):
        url = urlparse(value)
        if (url.scheme not in {"http", "https"}
                or url.hostname not in {"steamcommunity.com", "www.steamcommunity.com"}
                or url.path.rstrip("/") not in {
                    "/sharedfiles/filedetails", "/workshop/filedetails"}):
            raise ValueError("Enter a Workshop item ID or a Steam Workshop item URL.")
        ids = parse_qs(url.query).get("id", [])
        value = ids[0] if len(ids) == 1 else ""
    if not re.fullmatch(r"[0-9]{1,20}", value) or not 0 < int(value) < 2**64:
        raise ValueError("The Workshop item ID must be a positive 64-bit number.")
    return str(int(value))


@dataclass(frozen=True)
class WorkshopItem:
    app_id: str
    item_id: str
    title: str
    size: int = 0
    updated: int = 0

    @property
    def url(self) -> str:
        return f"https://steamcommunity.com/sharedfiles/filedetails/?id={self.item_id}"

    def mod_meta(self, archive=None):
        from Nexus.nexus_meta import NexusModMeta
        return NexusModMeta(
            workshop_app_id=self.app_id, workshop_item_id=self.item_id,
            workshop_title=self.title, workshop_updated=str(self.updated),
            workshop_archive=str(archive) if archive else "")


def fetch_item(app_id: str, item_id: str) -> WorkshopItem:
    if not re.fullmatch(r"[0-9]{1,10}", app_id) or not 0 < int(app_id) < 2**32:
        raise ValueError("The selected game does not have a valid Steam App ID.")
    item_id = parse_item_id(item_id)
    response = requests.post(
        "https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/",
        data={"itemcount": "1", "publishedfileids[0]": item_id}, timeout=(15, 30))
    response.raise_for_status()
    items = response.json().get("response", {}).get("publishedfiledetails", [])
    if len(items) != 1 or items[0].get("result") != 1:
        raise ValueError("Steam could not find this public Workshop item. Check its ID and visibility.")
    item = items[0]
    if str(item.get("publishedfileid")) != item_id:
        raise ValueError("Steam returned a different Workshop item.")
    if str(item.get("consumer_app_id")) != str(int(app_id)):
        raise ValueError("This Workshop item belongs to a different game.")
    if item.get("banned"):
        raise ValueError("Steam has marked this Workshop item as unavailable.")
    if not item.get("file_url") and not int(item.get("hcontent_file") or 0):
        raise ValueError("This item has no downloadable files. Enter an individual mod, not a collection.")
    return WorkshopItem(str(int(app_id)), item_id,
                        str(item.get("title") or f"Workshop {item_id}"),
                        int(item.get("file_size") or 0), int(item.get("time_updated") or 0))


def cache_root() -> Path:
    root = get_config_dir() / "workshop"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


@contextmanager
def _download_lock(root: Path):
    with (root / "download.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another Workshop download is running. Wait for it to finish.") from None
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def saved_account() -> str:
    try:
        return str(json.loads((cache_root() / "account" / "user.json").read_text())["username"])
    except (OSError, ValueError, KeyError, TypeError):
        return ""


def forget_account():
    root = cache_root()
    with _download_lock(root):
        account = root / "account"
        if account.is_symlink():
            raise RuntimeError("The Workshop account folder is a symbolic link.")
        if account.exists():
            shutil.rmtree(account)


def ensure_downloader(root: Path, cancel, emit) -> Path:
    arch = {"x86_64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine())
    if platform.system() != "Linux" or arch is None:
        raise RuntimeError("Automatic Workshop downloads currently require Linux x64 or ARM64.")
    folder = root / "tools" / RELEASE / arch
    exe = folder / "DepotDownloader"
    if exe.is_file():
        return exe
    folder.parent.mkdir(parents=True, exist_ok=True)
    emit("status", "Downloading DepotDownloader…")
    url = (f"https://github.com/SteamRE/DepotDownloader/releases/download/{RELEASE}/"
           f"DepotDownloader-linux-{arch}.zip")
    with tempfile.TemporaryDirectory(prefix="download-", dir=folder.parent) as tmp:
        tmp = Path(tmp)
        archive = tmp / "tool.zip"
        with requests.get(url, stream=True, timeout=(15, 30)) as response:
            response.raise_for_status()
            total = int(response.headers.get("Content-Length") or 0)
            done = 0
            with archive.open("wb") as out:
                for chunk in response.iter_content(256 * 1024):
                    _check_cancel(cancel)
                    out.write(chunk)
                    done += len(chunk)
                    emit("progress", (done, total))
        extracted = tmp / "tool"
        extracted.mkdir()
        with zipfile.ZipFile(archive) as bundle:
            for name in ("DepotDownloader", "LICENSE"):
                info = bundle.getinfo(name)
                if stat.S_ISLNK(info.external_attr >> 16):
                    raise ValueError("Unexpected symbolic link in the downloader release.")
                with bundle.open(info) as source, (extracted / name).open("wb") as target:
                    shutil.copyfileobj(source, target)
        (extracted / "DepotDownloader").chmod(0o700)
        _check_cancel(cancel)
        extracted.rename(folder)
    return exe


class DownloaderProcess:
    def __init__(self, cancel, emit):
        self.cancel = cancel
        self.emit = emit
        self.inputs = queue.Queue()
        self._secrets = []
        self._qr_rows = None
        self._tail = []
        self._process = None
        self._process_lock = threading.Lock()

    def abort(self):
        self.cancel.set()
        with self._process_lock:
            if self._process is not None and self._process.poll() is None:
                try:
                    os.killpg(self._process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass

    def submit(self, value: str):
        if not value or any(c in value for c in "\r\n\0"):
            raise ValueError("Enter a single non-empty response.")
        self.inputs.put(value)

    def _line(self, line: str):
        line = _ANSI.sub("", line)
        if "Use the Steam Mobile App to sign in with this QR code:" in line:
            self._qr_rows = []
            self.emit("status", "Scan the QR code with the Steam mobile app to sign in.")
            return
        if self._qr_rows is not None:
            if line and all(c in " █▀▄" for c in line):
                self._qr_rows.append(line)
                self.emit("qr", "\n".join(self._qr_rows))
                return
            if not line.strip():
                return
            self._qr_rows = None
        for secret in self._secrets:
            line = line.replace(secret, "[redacted]")
        if not line.strip():
            return
        if "Got " in line and " licenses for account" in line:
            self.emit("authenticated", None)
        if "STEAM GUARD! Use the Steam Mobile App" in line:
            self.emit("status", "Approve the sign-in request in the Steam mobile app.")
        self._tail = (self._tail + [line])[-8:]
        self.emit("output", line)
        progress = re.match(r"\s*(\d+(?:\.\d+)?)%\s", line)
        if progress:
            self.emit("progress", (float(progress[1]), 100))

    def run(self, command, auth_dir: Path, cwd: Path):
        auth_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        auth_dir.chmod(0o700)
        env = os.environ.copy()
        env["XDG_DATA_HOME"] = str(auth_dir)
        env["DOTNET_CLI_TELEMETRY_OPTOUT"] = "1"
        for key in ("LD_PRELOAD", "LD_LIBRARY_PATH", "DOTNET_ROOT", "DOTNET_ROOT_X64"):
            env.pop(key, None)
        with self._process_lock:
            _check_cancel(self.cancel)
            proc = subprocess.Popen(
                command, cwd=cwd, env=env, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0,
                start_new_session=True, umask=0o077)
            self._process = proc
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        pending = ""
        waiting = False
        last_output = time.monotonic()
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(proc.stdout, selectors.EVENT_READ)
                while selector.get_map():
                    _check_cancel(self.cancel)
                    if time.monotonic() - last_output > 900:
                        raise RuntimeError("Steam did not respond for 15 minutes. Cancelled; please try again.")
                    if waiting:
                        try:
                            response = self.inputs.get_nowait()
                        except queue.Empty:
                            pass
                        else:
                            self._secrets.append(response)
                            proc.stdin.write((response + "\n").encode())
                            proc.stdin.flush()
                            waiting = False
                            last_output = time.monotonic()
                    for key, _ in selector.select(0.2):
                        chunk = os.read(key.fd, 16384)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        last_output = time.monotonic()
                        pending += decoder.decode(chunk)
                        while "\n" in pending:
                            line, pending = pending.split("\n", 1)
                            self._line(line.rstrip("\r"))
                        prompt = _PROMPT.search(_ANSI.sub("", pending))
                        if prompt and not waiting:
                            kind = "password" if "password" in prompt[0].lower() else "guard"
                            pending = ""
                            waiting = True
                            self.emit("prompt", kind)
                        if len(pending) > 65536:
                            self._line(pending[:65536])
                            pending = pending[65536:]
            if pending.strip():
                self._line(pending)
            _check_cancel(self.cancel)
            result = proc.wait(timeout=10)
            if result:
                details = "\n".join(self._tail[-4:])
                raise RuntimeError(f"Workshop download failed (exit {result}).\n{details}")
        finally:
            with self._process_lock:
                self._process = None
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
            proc.stdin.close()
            proc.stdout.close()
            self._secrets.clear()
            while not self.inputs.empty():
                self.inputs.get_nowait()
            self.emit("prompt", "")


def package_item(item: WorkshopItem, payload: Path, output: Path, cancel) -> Path:
    files = []
    for directory, dirs, names in os.walk(payload, followlinks=False):
        dirs[:] = [name for name in dirs if name != ".DepotDownloader"]
        for name in dirs + names:
            path = Path(directory) / name
            if path.is_symlink():
                raise ValueError("The Workshop download contains a symbolic link.")
        for name in names:
            path = Path(directory) / name
            if not path.is_file():
                raise ValueError("The Workshop download contains a non-regular file.")
            files.append((path, path.relative_to(payload)))
        _check_cancel(cancel)
    if not files:
        raise RuntimeError("Steam returned no mod files. Nothing was installed.")
    if any("\\" in str(relative) or any(part in {".", ".."} for part in relative.parts)
           for _, relative in files):
        raise ValueError("The Workshop download contains an unsupported file path.")
    name = sanitize_mod_folder_name(item.title)[:120]
    if len(files) == 1 and files[0][0].suffix.lower() in {".zip", ".7z", ".rar"}:
        archive = output / f"{name} - Workshop {item.item_id}{files[0][0].suffix.lower()}"
        temporary = archive.with_suffix(archive.suffix + ".part")
        try:
            with files[0][0].open("rb") as source, temporary.open("wb") as target:
                while chunk := source.read(1024 * 1024):
                    _check_cancel(cancel)
                    target.write(chunk)
            temporary.replace(archive)
        finally:
            temporary.unlink(missing_ok=True)
        archive.with_suffix(".workshop.json").write_text(
            json.dumps(item.__dict__, ensure_ascii=False), encoding="utf-8")
        return archive
    prefix = Path()
    if item.app_id == "2868840":
        # STS2 loads a directory per mod; Workshop downloads may have flat files.
        manifests = [rel for _, rel in files if len(rel.parts) == 1
                     and rel.suffix.lower() == ".json"
                     and (payload / rel.with_suffix(".pck")).is_file()]
        if len(manifests) == 1:
            prefix = Path(manifests[0].stem)
    archive = output / f"{name} - Workshop {item.item_id}.zip"
    temporary = archive.with_suffix(".zip.part")
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_STORED) as bundle:
            for path, relative in files:
                _check_cancel(cancel)
                with path.open("rb") as source, bundle.open(
                        (prefix / relative).as_posix(), "w", force_zip64=True) as target:
                    while chunk := source.read(1024 * 1024):
                        _check_cancel(cancel)
                        target.write(chunk)
        temporary.replace(archive)
    finally:
        temporary.unlink(missing_ok=True)
    sidecar = archive.with_suffix(".workshop.json")
    sidecar.write_text(json.dumps(item.__dict__, ensure_ascii=False), encoding="utf-8")
    return archive


def archive_item(archive: Path) -> WorkshopItem | None:
    sidecar = archive.with_suffix(".workshop.json")
    try:
        if not sidecar.is_file() or sidecar.stat().st_size > 16384:
            return None
        data = json.loads(sidecar.read_text(encoding="utf-8"))
        item = WorkshopItem(**data)
        if not all(isinstance(value, str) for value in (item.item_id, item.app_id, item.title)):
            return None
        if parse_item_id(item.item_id) != item.item_id:
            return None
        if not re.fullmatch(r"[0-9]{1,10}", item.app_id) or not 0 < int(item.app_id) < 2**32:
            return None
        return item
    except (OSError, ValueError, TypeError):
        return None


def download_item(item: WorkshopItem, process: DownloaderProcess, *, mode: str,
                  username: str = "", remember: bool = False) -> Path:
    root = cache_root()
    with _download_lock(root):
        cancel, emit = process.cancel, process.emit
        if mode not in {"qr", "password", "saved", "anonymous"}:
            raise ValueError("Unknown Steam sign-in method.")
        username = username.strip()
        if (mode in {"password", "saved"} or remember) and not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.@-]{0,63}", username):
            raise ValueError("Enter your Steam account name (not your profile display name).")
        exe = ensure_downloader(root, cancel, emit)
        downloads = root / "downloads" / item.app_id / item.item_id
        downloads.mkdir(parents=True, exist_ok=True)
        run = Path(tempfile.mkdtemp(prefix="item-", dir=downloads))
        payload = run / "content"
        payload.mkdir()
        persistent = remember or mode == "saved"
        auth = root / "account" if persistent else run / "login"
        command = [str(exe), "-app", item.app_id, "-pubfile", item.item_id,
                   "-dir", str(payload), "-validate", "-loginid", str(os.getpid())]
        if mode == "qr":
            command.append("-qr")
        elif mode in {"password", "saved"}:
            command.extend(["-username", username])
        if persistent:
            command.append("-remember-password")
        emit("progress", (0, 0))
        emit("status", "Connecting to Steam…")
        try:
            process.run(command, auth, run)
            _check_cancel(cancel)
            emit("status", "Preparing the downloaded mod for installation…")
            archive = package_item(item, payload, run, cancel)
            if persistent:
                (auth / "user.json").write_text(json.dumps({"username": username}), encoding="utf-8")
            shutil.rmtree(payload)
            return archive
        finally:
            if not persistent and auth.exists():
                shutil.rmtree(auth)
