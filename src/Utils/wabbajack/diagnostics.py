from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import ssl
import sys
import threading
import time
import traceback
import uuid
import zlib
from collections import Counter
from importlib.metadata import PackageNotFoundError, version
from itertools import count
from pathlib import Path
from urllib.parse import urlparse


_URL = re.compile(r"(?:https?|nxm|ftp)://[^\s\"'<>]+", re.IGNORECASE)
_SECRET = re.compile(
    r'''(?ix)(?P<prefix>["']?(?:authorization|proxy-authorization|cookie|set-cookie|'''
    r'''x-api-key|api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|'''
    r'''password|passwd|secret)["']?\s*[:=]\s*)(?P<value>"(?:\\.|[^"\\])*"|'''
    r'''\'(?:\\.|[^\'\\])*\'|[^\r\n,}\]]+)'''
)
_AUTH = re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+")
_SESSION = uuid.uuid4().hex[:12]
_SEQUENCE = count(1)


def redact(value) -> str:
    text = str(value)

    def origin(match):
        try:
            parsed = urlparse(match.group(0))
            host = parsed.hostname or "redacted"
            port = parsed.port
        except ValueError:
            scheme = match.group(0).partition(":")[0].lower()
            return f"{scheme}://<redacted>"
        if port:
            host += f":{port}"
        return f"{parsed.scheme}://{host}/<redacted>"

    text = _URL.sub(origin, text)
    text = _SECRET.sub(lambda match: match.group("prefix") + "<redacted>", text)
    return _AUTH.sub(lambda match: match.group(1) + " <redacted>", text)


def _value(value, key=""):
    if key.casefold().replace("-", "_") in {
        "authorization", "proxy_authorization", "cookie", "set_cookie",
        "x_api_key", "api_key", "access_token", "refresh_token",
        "client_secret", "password", "passwd", "secret",
    }:
        return "<redacted>"
    if isinstance(value, Path):
        return redact(value)
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    if isinstance(value, dict):
        return {str(k): _value(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        rows = sorted(value, key=str) if isinstance(value, (set, frozenset)) else value
        return [_value(item) for item in rows]
    if isinstance(value, str):
        return redact(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact(value)


def emit(log, event: str, **fields) -> None:
    if log is None:
        return
    try:
        record = {"schema": 1, "session": _SESSION, "sequence": next(_SEQUENCE),
                  "monotonic_seconds": round(time.monotonic(), 6),
                  "event": event, "pid": os.getpid(),
                  "thread": threading.current_thread().name}
        diagnostic_id = getattr(log, "diagnostic_id", "")
        if diagnostic_id:
            record["diagnostic_id"] = diagnostic_id
        record.update({key: _value(value, key) for key, value in fields.items()})
        log("diag " + json.dumps(record, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
    except Exception:
        pass


def emit_exception(log, event: str, exc: BaseException, **fields) -> None:
    if log is None:
        return
    try:
        detail = str(exc)
    except Exception as stringify_error:
        detail = f"<could not format exception: {type(stringify_error).__name__}>"
    try:
        trace = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    except Exception as traceback_error:
        trace = f"<could not format traceback: {type(traceback_error).__name__}>"
    fields.update(
        exception_type=type(exc).__name__,
        exception=detail,
        traceback=trace,
    )
    emit(log, event, **fields)


def bind(log, diagnostic_id):
    if log is None or not diagnostic_id:
        return log
    if getattr(log, "diagnostic_id", "") == diagnostic_id:
        return log

    def bound(message):
        log(message)

    bound.diagnostic_id = diagnostic_id
    return bound


def url_host(url) -> str:
    try:
        parsed = urlparse(str(url))
        return parsed.hostname or parsed.scheme or "unknown"
    except Exception:
        return "unknown"


def _mount(path: Path) -> dict:
    try:
        device = path.stat().st_dev
        wanted = f"{os.major(device)}:{os.minor(device)}"
        matches = []
        for line in Path("/proc/self/mountinfo").read_text(errors="replace").splitlines():
            left, separator, right = line.partition(" - ")
            fields = left.split()
            extra = right.split()
            if separator and len(fields) >= 6 and len(extra) >= 3 and fields[2] == wanted:
                matches.append((fields[4], fields[5], extra[0], extra[2]))
        if matches:
            point, options, filesystem, super_options = max(matches, key=lambda row: len(row[0]))
            return {
                "mount": point.replace("\\040", " "),
                "filesystem": filesystem,
                "options": options,
                "super_options": super_options,
            }
    except (OSError, ValueError):
        pass
    return {}


def path_facts(path) -> dict:
    try:
        requested = Path(path).expanduser().absolute()
        parent = requested
        while not parent.exists() and parent != parent.parent:
            parent = parent.parent
        result = {
            "path": requested,
            "exists": requested.exists(),
            "symlink": requested.is_symlink(),
            "existing_parent": parent,
            "writable": os.access(parent, os.W_OK),
            "executable": os.access(parent, os.X_OK),
        }
        stat = parent.stat()
        usage = shutil.disk_usage(parent)
        result.update(
            device=stat.st_dev,
            mode=oct(stat.st_mode & 0o7777),
            uid=stat.st_uid,
            gid=stat.st_gid,
            free_bytes=usage.free,
            total_bytes=usage.total,
        )
    except Exception as exc:
        return {"path": str(path), "probe_error": f"{type(exc).__name__}: {exc}"}
    result.update(_mount(parent))
    return result


def file_facts(path) -> dict:
    if not path:
        return {"path": None, "exists": False}
    try:
        requested = Path(path).expanduser().absolute()
        result = {
            "path": requested,
            "exists": requested.exists(),
            "file": requested.is_file(),
            "symlink": requested.is_symlink(),
            "resolved": requested.resolve(strict=False),
            "executable": os.access(requested, os.X_OK),
        }
        if requested.exists():
            stat = requested.stat()
            result.update(size=stat.st_size, mode=oct(stat.st_mode & 0o7777),
                          uid=stat.st_uid, gid=stat.st_gid,
                          mtime_ns=stat.st_mtime_ns)
    except Exception as exc:
        return {"path": str(path), "probe_error": f"{type(exc).__name__}: {exc}"}
    return result


def _library_versions() -> dict:
    result = {}
    for name in ("requests", "urllib3", "certifi", "cryptography", "lz4", "xxhash"):
        try:
            result[name] = version(name)
        except PackageNotFoundError:
            result[name] = "not installed"
        except Exception as exc:
            result[name] = f"<{type(exc).__name__}: {exc}>"
    result["sqlite"] = sqlite3.sqlite_version
    result["zlib"] = zlib.ZLIB_VERSION
    return result


def _game_value(game, name, default=""):
    try:
        value = getattr(game, name, default)
        return value() if callable(value) else value
    except Exception as exc:
        return f"<{type(exc).__name__}: {exc}>"


def _log_request(log, request, event) -> None:
    package = request.package
    from .hosts import source_url

    archives = Counter(archive.kind or "unknown" for archive in package.archives.values())
    directives = Counter(directive.kind or "unknown" for directive in package.directives)
    hosts = Counter(url_host(source_url(archive)) for archive in package.archives.values()
                    if source_url(archive))
    game = request.game
    prefix = _game_value(game, "get_prefix_path", None)
    try:
        from Utils.diagnostics.system import collect
        system = dict(collect())
    except Exception as exc:
        system = {"probe_error": f"{type(exc).__name__}: {exc}"}
    environment = {name: os.environ.get(name) for name in (
        "FLATPAK_ID", "APPIMAGE", "APPDIR", "XDG_SESSION_TYPE",
        "XDG_CURRENT_DESKTOP", "XDG_CONFIG_HOME", "XDG_CACHE_HOME",
        "XDG_DATA_HOME", "WAYLAND_DISPLAY", "DISPLAY", "LANG", "LC_ALL",
        "PATH", "LD_LIBRARY_PATH", "LD_PRELOAD", "WINEDEBUG", "WINEARCH",
        "WINEPREFIX", "WINEDLLOVERRIDES", "STEAM_COMPAT_DATA_PATH",
        "STEAM_COMPAT_CLIENT_INSTALL_PATH", "STEAM_RUNTIME",
        "PRESSURE_VESSEL_RUNTIME", "PROTON_LOG", "PROTON_USE_WINED3D",
        "PROTON_USE_NTSYNC", "PROTON_USE_FSYNC", "PROTON_USE_ESYNC",
        "DXVK_CONFIG_FILE", "DXVK_STATE_CACHE_PATH", "VK_ICD_FILENAMES",
        "MESA_LOADER_DRIVER_OVERRIDE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
        "SSL_CERT_FILE", "SSL_CERT_DIR", "HTTP_PROXY", "HTTPS_PROXY",
        "ALL_PROXY", "NO_PROXY",
    ) if os.environ.get(name)}
    emit(log, event + ".environment", system=system, cwd=Path.cwd(),
         executable=sys.executable, uid=os.getuid(), gid=os.getgid(),
         effective_uid=os.geteuid(), effective_gid=os.getegid(),
         environment=environment)
    try:
        package_bytes = package.path.stat().st_size if package.path.is_file() else None
    except OSError:
        package_bytes = None
    seven_zip = next((shutil.which(name) for name in ("7zzs", "7zz", "7z", "7za")
                      if shutil.which(name)), None)
    emit(
        log,
        event,
        diagnostic_id=getattr(request, "diagnostic_id", ""),
        operation=request.mode,
        package_name=package.name,
        package_version=package.version,
        package_game=package.game,
        package_identity=package.identity,
        package_path=package.path,
        package_bytes=package_bytes,
        archives=len(package.archives),
        archive_bytes=sum(archive.size for archive in package.archives.values()),
        archive_kinds=dict(archives),
        source_hosts=dict(hosts),
        directives=len(package.directives),
        output_bytes=sum(directive.size for directive in package.directives),
        directive_kinds=dict(directives),
        authored_profiles=package.profiles,
        selected_profile=package.selected_profile,
        selected_profiles=request.profiles,
        premium=request.premium,
        clear_archives=getattr(request, "clear_archives", False),
        nexus_api=bool(request.api),
        fixes=request.fixes,
        setup_options=request.setup_options,
        gallery_id=request.gallery_id,
    )
    emit(
        log,
        event + ".game",
        name=_game_value(game, "name"),
        game_id=_game_value(game, "game_id"),
        handler=f"{type(game).__module__}.{type(game).__name__}",
        steam_id=_game_value(game, "effective_steam_id") or _game_value(game, "steam_id"),
        runtime_mode=_game_value(game, "get_runtime_mode", "automatic"),
        game_path=_game_value(game, "get_game_path", None),
        profile_root=_game_value(game, "get_profile_root", None),
        prefix_path=prefix,
        deployment_active=_game_value(game, "get_deploy_active", False),
        root_deployment=bool(getattr(game, "root_folder_deploy_enabled", True)),
    )
    paths = {
        "installation": request.directory,
        "downloads": request.downloads,
        "profile_root": _game_value(game, "get_profile_root", None),
        "prefix": prefix,
        **{f"source:{name}": path for name, path in request.game_roots.items()},
    }
    for role, path in paths.items():
        if path:
            emit(log, event + ".filesystem", role=role, **path_facts(path))
    drive_mappings = {}
    if prefix:
        devices = Path(prefix) / "dosdevices"
        if devices.is_dir():
            for path in sorted(devices.iterdir(), key=lambda item: item.name):
                if path.is_symlink() and len(path.name) == 2 and path.name.endswith(":"):
                    try:
                        drive_mappings[path.name] = str(path.resolve())
                    except OSError as exc:
                        drive_mappings[path.name] = f"<{type(exc).__name__}: {exc}>"
        compat_data = Path(prefix)
        try:
            from Utils.wine.prefix import read_prefix_runner, resolve_compat_data
            compat_data = resolve_compat_data(Path(prefix))
            runner = read_prefix_runner(compat_data)
        except Exception as exc:
            runner = f"<{type(exc).__name__}: {exc}>"
        try:
            config_info = (compat_data / "config_info").read_text(
                encoding="utf-8", errors="replace")[:8192].splitlines()[:16]
        except OSError:
            config_info = []
        emit(
            log,
            event + ".prefix",
            path=prefix,
            compat_data=compat_data,
            runner=runner,
            config_info=config_info,
            drive_mappings=drive_mappings,
            user_registry=(Path(prefix) / "user.reg").is_file(),
            system_registry=(Path(prefix) / "system.reg").is_file(),
        )
    try:
        import requests
        requests_version = requests.__version__
    except Exception:
        requests_version = "unknown"
    try:
        from Utils.ui.config import load_custom_proton_path
        custom_proton = load_custom_proton_path()
        custom_proton_error = ""
    except Exception as exc:
        custom_proton = ""
        custom_proton_error = f"{type(exc).__name__}: {exc}"
    try:
        from Utils.ca_bundle import resolve_ca_bundle
        ca_bundle = resolve_ca_bundle() or "system default"
    except Exception as exc:
        ca_bundle = f"<{type(exc).__name__}: {exc}>"
    emit(
        log,
        event + ".tools",
        cpu_count=os.cpu_count(),
        requests=requests_version,
        libraries=_library_versions(),
        openssl=ssl.OPENSSL_VERSION,
        ca_bundle=ca_bundle,
        seven_zip=file_facts(seven_zip),
        ffmpeg=file_facts(shutil.which("ffmpeg")),
        bubblewrap=file_facts(shutil.which("bwrap")),
        protontricks=file_facts(shutil.which("protontricks")),
        texconv=file_facts(getattr(request, "texconv", None)),
        texture_proton=file_facts(getattr(request, "proton", None)),
        custom_proton=path_facts(custom_proton) if custom_proton else None,
        custom_proton_error=custom_proton_error,
    )


def log_request(log, request, event="request") -> None:
    try:
        _log_request(log, request, event)
    except Exception as exc:
        emit_exception(log, event + ".diagnostics_failed", exc)
