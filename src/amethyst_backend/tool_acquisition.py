from __future__ import annotations

import hashlib
import json
import os
import tarfile
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from urllib.parse import unquote, urlsplit

from Utils.operations import OperationCancelled

from .jobs import JobCancelled, JobError


def _checkpoint(control):
    if control.wait_if_paused():
        raise JobCancelled("Tool download cancelled.")


def _filename(url):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise JobError("tool_source_invalid", "The declared tool download must use HTTPS.")
    name = unquote(parsed.path.rsplit("/", 1)[-1])
    from Utils.wizards.archives import is_archive
    if (not name or name.startswith(".") or name.strip() != name
            or len(os.fsencode(name)) > 220
            or any(char in name for char in "/\\")
            or any(ord(char) < 32 or ord(char) == 127 for char in name)
            or not is_archive(name)):
        raise JobError("tool_source_invalid", "The declared tool URL has no safe archive filename.")
    return name


def _verify_archive(path, name):
    name = name.lower()
    if name.endswith(".zip"):
        valid = zipfile.is_zipfile(path)
    elif name.endswith(".7z"):
        with path.open("rb") as stream:
            valid = stream.read(6) == b"7z\xbc\xaf\x27\x1c"
    elif name.endswith(".rar"):
        with path.open("rb") as stream:
            valid = stream.read(7) in (b"Rar!\x1a\x07\x00", b"Rar!\x1a\x07\x01")
    else:
        valid = tarfile.is_tarfile(path)
    if not valid:
        raise JobError("tool_archive_invalid", "The downloaded file is not a valid archive.")


def _publish(cache, name, temporary, identity):
    from Utils.wizards.archives import ARCHIVE_EXTS

    suffix = next(ext for ext in sorted(ARCHIVE_EXTS, key=len, reverse=True)
                  if name.lower().endswith(ext))
    stem = name[:-len(suffix)]
    for index in range(1000):
        candidate = cache / (name if index == 0 else f"{stem} ({index}){suffix}")
        sidecar = candidate.with_suffix(candidate.suffix + ".tool.json")
        if sidecar.exists() or sidecar.is_symlink():
            continue
        try:
            os.link(temporary, candidate, follow_symlinks=False)
        except FileExistsError:
            continue
        except OSError as exc:
            raise JobError("tool_download_failed", "Could not publish the downloaded archive.",
                           {"reason": str(exc)}) from exc
        sidecar_tmp = None
        try:
            fd, raw = tempfile.mkstemp(prefix=".amethyst-tool-meta-", dir=cache)
            sidecar_tmp = Path(raw)
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                output.write(json.dumps(identity, sort_keys=True))
            os.link(sidecar_tmp, sidecar, follow_symlinks=False)
        except FileExistsError:
            candidate.unlink(missing_ok=True)
            continue
        except OSError as exc:
            candidate.unlink(missing_ok=True)
            raise JobError("tool_download_failed", "Could not record the tool archive identity.",
                           {"reason": str(exc)}) from exc
        finally:
            if sidecar_tmp is not None:
                sidecar_tmp.unlink(missing_ok=True)
        return candidate
    raise JobError("tool_download_failed", "Too many archives already have the declared filename.")


def _download(game, tool_id, version_index, version, url, tag, control, progress):
    from Utils.ca_bundle import get_ssl_context
    from Utils.config_paths import get_download_cache_dir_for_game
    from Utils.operations import acquire_resource_operation

    name = _filename(url)
    keywords = version["archive_keywords"]
    if (version["source"]["kind"] == "github_release" and keywords
            and not all(keyword in name.casefold() for keyword in keywords)):
        raise JobError("tool_source_mismatch", "The release asset name does not match the tool choice.",
                       {"name": name, "keywords": keywords})
    game_name = str(game.name)
    if (not game_name or game_name in (".", "..") or "/" in game_name
            or "\\" in game_name or any(char in game_name for char in "\x00\r\n")):
        raise JobError("tool_download_unsafe_path", "The game's cache folder name is unsafe.")
    cache = Path(get_download_cache_dir_for_game(game_name))
    _checkpoint(control)
    with acquire_resource_operation(f"tool-download:{cache.resolve()}",
                                    "Tool download", cancel=control):
        if cache.is_symlink() or (cache.exists() and not cache.is_dir()):
            raise JobError("tool_download_unsafe_path", "The game's cache folder is unsafe.")
        cache.mkdir(parents=True, exist_ok=True)
        fd, raw = tempfile.mkstemp(prefix=".amethyst-tool-", suffix=".part", dir=cache)
        temporary = Path(raw)
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "ModManager/1.0"})
            _checkpoint(control)
            with urllib.request.urlopen(request, timeout=30, context=get_ssl_context()) as response, \
                    os.fdopen(fd, "wb") as output:
                fd = -1
                if urlsplit(response.url).scheme != "https":
                    raise JobError("tool_source_invalid", "The tool download redirected away from HTTPS.")
                raw_size = response.headers.get("Content-Length")
                try:
                    total = int(raw_size) if raw_size is not None else 0
                except (TypeError, ValueError) as exc:
                    raise JobError("tool_download_failed", "The server returned an invalid archive size.") from exc
                if total < 0:
                    raise JobError("tool_download_failed", "The server returned an invalid archive size.")
                digest = hashlib.sha256()
                size = 0
                progress(0, total, "download")
                while True:
                    _checkpoint(control)
                    chunk = response.read(65536)
                    if not chunk:
                        break
                    output.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                    progress(size, total, "download")
            _checkpoint(control)
            if not size or (total and size != total):
                raise JobError("tool_archive_incomplete", "The tool archive download is incomplete.",
                               {"received": size, "expected": total or None})
            _verify_archive(temporary, name)
            _checkpoint(control)
            identity = {"version": 1, "tool_id": version.get("source_tool_id", tool_id), "version_index": version_index,
                        "source_kind": version["source"]["kind"], "source_url": url,
                        "release_tag": tag, "sha256": digest.hexdigest(), "size_bytes": size}
            path = _publish(cache, name, temporary, identity)
            progress(size, size, "download")
            return {"ok": True, "source": {"kind": version["source"]["kind"],
                                              "release_tag": tag},
                    "archive": {"name": path.name, "path": str(path),
                                "size_bytes": size, "sha256": digest.hexdigest(),
                                "location_kind": "cache"}}
        finally:
            if fd >= 0:
                os.close(fd)
            temporary.unlink(missing_ok=True)


def acquire_runner(session, game, tool_id, version_index, version, control, nxm_link=None):
    def run(log, progress):
        source = version["source"]
        kind = source["kind"]
        _checkpoint(control)
        if kind == "manual":
            return {"ok": True, "source": source, "archive_keywords": version["archive_keywords"],
                    "manual_download_required": True}
        if kind == "nexus":
            from .nexus import download_runner
            result = download_runner(session, str(game.name), source["domain"], source["mod_id"],
                                     source["file_id"], nxm_link=nxm_link, cancel=control)(log, progress)
            return {**result, "tool_id": tool_id, "version_index": version_index}
        if kind == "github_release":
            from Utils.wizards.archives import fetch_latest_github_asset
            declared = urlsplit(source["url"])
            if declared.scheme != "https" or declared.hostname != "api.github.com":
                raise JobError("tool_source_invalid", "The declared GitHub release API URL is invalid.")
            _checkpoint(control)
            try:
                tag, url = fetch_latest_github_asset(source["url"], version["archive_keywords"])
            except (OSError, ValueError, RuntimeError, KeyError, TypeError,
                    urllib.error.URLError) as exc:
                raise JobError("tool_release_unavailable", "Could not resolve the declared GitHub release asset.",
                               {"reason": str(exc)}) from exc
            _checkpoint(control)
            if urlsplit(url).hostname != "github.com":
                raise JobError("tool_source_invalid", "The declared GitHub release asset is not hosted on GitHub.")
        elif kind == "direct":
            tag, url = None, source["url"]
        else:
            raise JobError("tool_source_invalid", "The declared tool source is unsupported.")
        try:
            result = _download(game, tool_id, version_index, version, url, tag, control, progress)
        except OperationCancelled as exc:
            raise JobCancelled("Tool download cancelled.") from exc
        except (OSError, urllib.error.URLError) as exc:
            raise JobError("tool_download_failed", "Could not download the declared tool archive.",
                           {"reason": str(exc)}) from exc
        return {**result, "tool_id": tool_id, "version_index": version_index}

    return run
