"""Parse, register, and download generic ``modl://`` protocol links."""

from __future__ import annotations

import os
import re
import sys
import threading
from dataclasses import dataclass
from email.message import Message
from email.utils import collapse_rfc2231_value
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, unquote, urlparse

import requests

from Thunderstore.ror2mm_handler import Ror2mmHandler
from Utils.downloads import bandwidth
from Utils.app_log import app_log
from Utils.ca_bundle import resolve_ca_bundle

_SCHEME = "modl"
_FLAG = "--modl"
_DESKTOP_FILE_NAME = "amethystmodmanager-modl.desktop"
_GAME_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_CHUNK_SIZE = 256 * 1024
_ARCHIVE_SUFFIXES = (
    ".tar.bz2", ".tar.gz", ".tar.xz", ".override", ".fomod", ".dazip",
    ".zip", ".7z", ".rar", ".tar",
)
_CONTENT_TYPE_SUFFIXES = {
    "application/zip": ".zip",
    "application/x-7z-compressed": ".7z",
    "application/vnd.rar": ".rar",
    "application/x-rar-compressed": ".rar",
    "application/x-tar": ".tar",
    "application/gzip": ".tar.gz",
    "application/x-gzip": ".tar.gz",
    "application/x-bzip2": ".tar.bz2",
    "application/x-xz": ".tar.xz",
}


@dataclass(frozen=True)
class ModlLink:
    game_id: str
    download_url: str
    raw: str = ""

    @property
    def download_host(self) -> str:
        return (urlparse(self.download_url).hostname or "").lower()

    @classmethod
    def parse(cls, url: str) -> "ModlLink":
        parsed = urlparse(url)
        if parsed.scheme.lower() != _SCHEME:
            raise ValueError(f"Not a modl:// URL: {url!r}")

        game_id = unquote(parsed.netloc or "").strip()
        if not game_id or not _GAME_ID_RE.fullmatch(game_id):
            raise ValueError("Missing or invalid MODL game ID")
        if parsed.path not in ("", "/"):
            raise ValueError(f"Unexpected MODL path: {parsed.path!r}")

        try:
            values = parse_qs(
                parsed.query, keep_blank_values=True, max_num_fields=32
            ).get("url", [])
        except ValueError as exc:
            raise ValueError("Invalid MODL query") from exc
        if len(values) != 1 or not values[0].strip():
            raise ValueError("MODL link must contain exactly one url parameter")

        download_url = values[0].strip()
        target = urlparse(download_url)
        if target.scheme.lower() not in ("http", "https") or not target.netloc:
            raise ValueError("MODL download URL must use HTTP or HTTPS")
        if any(ord(char) < 32 or ord(char) == 127 for char in download_url):
            raise ValueError("MODL download URL contains control characters")

        return cls(
            game_id=game_id.lower(), download_url=download_url, raw=url)


def parse_modl_url(url: str) -> ModlLink:
    return ModlLink.parse(url)


def modl_url_from_argv(argv: list[str] | None = None) -> str | None:
    if argv is None:
        argv = sys.argv[1:]
    if _FLAG in argv:
        idx = argv.index(_FLAG)
        if (idx + 1 < len(argv)
                and argv[idx + 1].lower().startswith(f"{_SCHEME}://")):
            return argv[idx + 1]
    for arg in argv:
        if arg.lower().startswith(f"{_SCHEME}://"):
            return arg
    return None


def strip_modl_argv(argv: list[str]) -> list[str]:
    out = [arg for arg in argv
           if not arg.lower().startswith(f"{_SCHEME}://")]
    return [arg for arg in out if arg != _FLAG]


class ModlHandler(Ror2mmHandler):
    _SCHEME = _SCHEME
    _FLAG = _FLAG
    _DESKTOP_FILE_NAME = _DESKTOP_FILE_NAME

    @classmethod
    def _desktop_contents(cls) -> str:
        return (
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=Amethyst Mod Manager (MODL Handler)\n"
            "Comment=Handle generic modl:// download links\n"
            f"Exec={cls._exec_command()}\n"
            "Icon=amethystmodmanager\n"
            "Terminal=false\n"
            "NoDisplay=true\n"
            f"MimeType=x-scheme-handler/{cls._SCHEME};\n"
            "Categories=Game;\n"
        )


@dataclass
class ModlDownloadResult:
    success: bool = False
    file_path: Path | None = None
    file_name: str = ""
    error: str = ""
    bytes_downloaded: int = 0
    cancelled: bool = False


def _safe_filename(value: str) -> str:
    name = unquote(value or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join("_" if ord(char) < 32 or ord(char) == 127 else char
                   for char in name).strip()
    if not name or name in (".", ".."):
        return ""

    suffix = next(
        (ext for ext in _ARCHIVE_SUFFIXES if name.lower().endswith(ext)),
        Path(name).suffix,
    )
    stem = (name[:-len(suffix)] if suffix else name)[:240]
    while stem and len((stem + suffix).encode("utf-8", "replace")) > 240:
        stem = stem[:-1]
    return stem + suffix if stem else ""


def _content_disposition_filename(value: str) -> str:
    if not value:
        return ""
    message = Message()
    message["content-disposition"] = value
    candidate = message.get_param("filename", header="content-disposition")
    if isinstance(candidate, tuple):
        candidate = collapse_rfc2231_value(candidate)
    return _safe_filename(str(candidate or ""))


def _response_filename(response: requests.Response, original_url: str) -> str:
    name = _content_disposition_filename(
        response.headers.get("Content-Disposition", ""))
    if not name:
        for value in (response.url, original_url):
            name = _safe_filename(urlparse(value).path)
            if name:
                break

    content_type = response.headers.get("Content-Type", "").split(";", 1)[0]
    suffix = _CONTENT_TYPE_SUFFIXES.get(content_type.strip().lower(), "")
    if not name:
        return f"modl-download{suffix}" if suffix else ""
    if suffix and not Path(name).suffix:
        name += suffix
    return name


def _split_archive_suffix(name: str) -> tuple[str, str]:
    lower = name.lower()
    for suffix in _ARCHIVE_SUFFIXES:
        if lower.endswith(suffix):
            return name[:-len(suffix)], name[-len(suffix):]
    return os.path.splitext(name)


def _unique_destination(dest_dir: Path, file_name: str) -> tuple[Path, Path]:
    stem, suffix = _split_archive_suffix(file_name)
    counter = 0
    while True:
        numbered = file_name if counter == 0 else f"{stem} ({counter}){suffix}"
        dest = dest_dir / numbered
        part = dest.with_name(dest.name + ".part")
        if not dest.exists() and not part.exists():
            return dest, part
        counter += 1


ProgressCallback = Callable[[int, int, str], None]


def download_modl_file(
    link: ModlLink,
    dest_dir: Path,
    progress_cb: ProgressCallback | None = None,
    cancel: threading.Event | None = None,
) -> ModlDownloadResult:
    result = ModlDownloadResult()
    if cancel is not None and cancel.is_set():
        result.error = "Download cancelled"
        result.cancelled = True
        return result

    dest_dir = Path(dest_dir)
    part_path: Path | None = None
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        with requests.Session() as session:
            session.headers["User-Agent"] = "Amethyst-Mod-Manager"
            session.verify = resolve_ca_bundle() or True
            with session.get(
                link.download_url, stream=True, timeout=(15, 60),
                allow_redirects=True, verify=session.verify,
            ) as response:
                response.raise_for_status()
                file_name = _response_filename(response, link.download_url)
                if not file_name:
                    result.error = "Server did not provide a usable file name"
                    return result

                while True:
                    dest, candidate_part = _unique_destination(
                        dest_dir, file_name)
                    try:
                        candidate_part.touch(exist_ok=False)
                    except FileExistsError:
                        continue
                    part_path = candidate_part
                    break
                encoded = response.headers.get("Content-Encoding", "").lower()
                try:
                    total = max(
                        0, int(response.headers.get("Content-Length") or 0))
                except (TypeError, ValueError):
                    total = 0
                if encoded not in ("", "identity"):
                    total = 0

                downloaded = 0
                if progress_cb:
                    progress_cb(0, total, dest.name)
                with open(part_path, "wb") as output:
                    for chunk in response.iter_content(_CHUNK_SIZE):
                        if cancel is not None and cancel.is_set():
                            result.error = "Download cancelled"
                            result.cancelled = True
                            return result
                        if not chunk:
                            continue
                        output.write(chunk)
                        downloaded += len(chunk)
                        bandwidth.throttle(len(chunk), cancel)
                        if progress_cb:
                            progress_cb(downloaded, total, dest.name)

        if downloaded == 0:
            result.error = "Server returned an empty file"
            return result
        if total and downloaded != total:
            result.error = (
                f"Incomplete download: got {downloaded} of {total} bytes")
            return result

        part_path.replace(dest)
        part_path = None
        result.success = True
        result.file_path = dest
        result.file_name = dest.name
        result.bytes_downloaded = downloaded
        app_log(f"modl: downloaded {dest.name} ({downloaded} bytes)")
        return result
    except (requests.RequestException, OSError) as exc:
        result.error = str(exc)
        return result
    finally:
        if part_path is not None:
            try:
                part_path.unlink(missing_ok=True)
            except OSError:
                pass
