from __future__ import annotations

import configparser
import hashlib
import importlib.util
import io
from Utils import json_limits as json
import re
import sys
import tempfile
import uuid
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .catalog import discover, load_data, load_settings, plugin_directory


SIDECAR = ".plugin.json"
_ID = r"[a-z0-9][a-z0-9_.-]{0,99}"


def source_uri(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 16384
            or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError("Supply a source URI of at most 16384 characters without whitespace.")
    parsed = urlsplit(value)
    if not re.fullmatch(r"[a-z][a-z0-9+.-]{0,31}", parsed.scheme) or parsed.username or parsed.password:
        raise ValueError("The source URI needs a supported scheme and no embedded credentials.")
    parsed.hostname
    if parsed.scheme in ("http", "https"):
        if not parsed.hostname:
            raise ValueError("HTTP source URIs require a hostname.")
        parsed.port
    return parsed


def matches(provider, source):
    parsed = source_uri(source)
    return parsed.scheme in provider.schemes and (not provider.hosts or (parsed.hostname or "").lower() in provider.hosts)


def providers(game_id, source=None):
    if source is not None:
        source_uri(source)
    plugins, errors = discover(game_id)
    return [{"plugin_id": plugin.id, "plugin_name": plugin.name, "provider_id": provider.id,
             "label": provider.label, "schemes": list(provider.schemes), "hosts": list(provider.hosts),
             "repository": provider.repository_entrypoint is not None,
             "protocols": list(provider.protocols),
             "transports": ["http", "plugin"] if provider.download_entrypoint else ["http"]}
            for plugin in plugins if plugin.download_providers and load_settings(plugin).get("enabled") is not False
            for provider in plugin.download_providers if source is None or matches(provider, source)], errors


def _metadata(value):
    if not isinstance(value, dict):
        raise ValueError("Provider metadata must be a JSON object.")
    raw = json.dumps(value, max_bytes=65536, ensure_ascii=True)
    for key in ("name", "version", "author", "description"):
        if key in value and (not isinstance(value[key], str) or len(value[key]) > 4000):
            raise ValueError(f"Provider metadata {key} must be a string of at most 4000 characters.")
    return json.loads(raw)


def _descriptor(value, provider):
    from Utils.downloads.core import is_archive
    from Utils.downloads.modl import _safe_filename
    if not isinstance(value, dict) or set(value) - {"url", "file_name", "size_bytes", "sha256", "identity", "metadata", "headers", "transport", "private"}:
        raise ValueError("The provider must return an archive descriptor with known fields.")
    transport, url = value.get("transport", "http"), value.get("url", "")
    if transport == "http":
        parsed = source_uri(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("Provider download URLs must use HTTP or HTTPS.")
        parsed.port
    elif transport != "plugin" or provider.download_entrypoint is None:
        raise ValueError("Plugin transport requires a declared download_entrypoint.")
    elif "url" in value or "headers" in value:
        raise ValueError("Plugin transports should pass transfer-specific details through private.")
    name = value.get("file_name")
    if (not isinstance(name, str) or not name or len(name.encode("utf-8")) > 240
            or ":" in name or _safe_filename(name) != name or not is_archive(name)):
        raise ValueError("Provider file_name must be one supported archive filename of at most 240 bytes.")
    size, identity, checksum = value.get("size_bytes"), value.get("identity"), value.get("sha256", "")
    if type(size) is not int or not 1 <= size <= 9223372036854775807:
        raise ValueError("Provider size_bytes must be a positive integer.")
    if (not isinstance(identity, str) or not identity.strip() or len(identity) > 1000
            or any(ord(char) < 32 or ord(char) == 127 for char in identity)):
        raise ValueError("Provider identity must be a stable nonempty artifact ID of at most 1000 characters.")
    if not isinstance(checksum, str) or checksum and not re.fullmatch(r"[0-9a-f]{64}", checksum):
        raise ValueError("Provider sha256 must be a lowercase SHA-256 checksum.")
    headers = value.get("headers", {})
    if not isinstance(headers, dict) or len(headers) > 32:
        raise ValueError("Provider headers must contain at most 32 HTTP headers.")
    for key, item in headers.items():
        if (not isinstance(key, str) or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", key)
                or key.lower() in {"host", "content-length", "transfer-encoding"}
                or not isinstance(item, str) or len(item) > 8192
                or any(ord(char) < 32 or ord(char) == 127 or ord(char) > 255 for char in item)):
            raise ValueError("Invalid provider HTTP header.")
    if len(json.dumps(headers, ensure_ascii=True)) > 65536:
        raise ValueError("Provider HTTP headers exceed 64 KiB.")
    private = value.get("private", {})
    if not isinstance(private, dict) or len(json.dumps(private, ensure_ascii=True, allow_nan=False)) > 1024 * 1024:
        raise ValueError("Provider private data must be a JSON object of at most 1 MiB.")
    return {"url": url, "file_name": name, "size_bytes": size, "identity": identity, "transport": transport,
            "sha256": checksum, "metadata": _metadata(value.get("metadata", {})), "headers": dict(headers),
            "private": json.loads(json.dumps(private, ensure_ascii=True, allow_nan=False))}


def _signature(plugin, provider):
    return [repr(plugin), hashlib.sha256(provider.entrypoint.read_bytes()).hexdigest(),
            load_settings(plugin), load_data(plugin),
            hashlib.sha256(provider.download_entrypoint.read_bytes()).hexdigest() if provider.download_entrypoint else None,
            hashlib.sha256(provider.repository_entrypoint.read_bytes()).hexdigest() if provider.repository_entrypoint else None]


def _check(control):
    from Utils.downloads.control import wait_if_paused
    if wait_if_paused(control):
        from Utils.operations import OperationCancelled
        raise OperationCancelled("Plugin download cancelled.")


@dataclass(frozen=True)
class DownloadPlan:
    plugin: object
    provider: object
    descriptor: dict
    revision: str
    source: str
    signature: str

    def public(self):
        return {"plugin_id": self.plugin.id, "provider_id": self.provider.id,
                "provider_label": self.provider.label, "revision": self.revision,
                **{key: self.descriptor[key] for key in ("file_name", "size_bytes", "identity", "sha256", "metadata", "transport")}}


def resolve(game, plugin_id, provider_id, source, log, control):
    source_uri(source)
    plugins, _errors = discover(game.game_id)
    plugin = next((plugin for plugin in plugins if plugin.id == plugin_id), None)
    provider = next((provider for provider in plugin.download_providers if provider.id == provider_id), None) if plugin else None
    if provider is None or load_settings(plugin).get("enabled") is False:
        raise ValueError("The plugin download provider is unavailable.")
    if not matches(provider, source):
        raise ValueError("The source URI does not match this provider's declaration.")
    _check(control)
    signature = _signature(plugin, provider)
    name = f"_amethyst_download_{uuid.uuid4().hex}"
    try:
        spec = importlib.util.spec_from_file_location(name, provider.entrypoint,
                                                     submodule_search_locations=[str(provider.entrypoint.parent)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        context = {"api_version": 1, "plugin_id": plugin.id, "provider_id": provider.id,
                   "plugin_dir": str(plugin_directory(plugin)), "settings": deepcopy(signature[2]), "data": deepcopy(signature[3]),
                   "game_id": game.game_id, "game": game.name, "source": source,
                   "check_cancelled": lambda: _check(control), "log": lambda message: log(f"{plugin.name}: {message}")}
        descriptor = _descriptor(module.resolve_download(context), provider)
        _check(control)
        if signature != _signature(plugin, provider) or plugin not in discover(game.game_id)[0]:
            raise ValueError("The provider changed during resolution; resolve the source again.")
        public = {key: item for key, item in descriptor.items() if key not in {"url", "headers", "private"}}
        revision = hashlib.sha256(json.dumps([game.game_id, signature,
            hashlib.sha256(source.encode()).hexdigest(), public], sort_keys=True).encode()).hexdigest()
        return DownloadPlan(plugin, provider, descriptor, revision, source, json.dumps(signature, sort_keys=True))
    except Exception as exc:
        from Utils.operations import OperationCancelled
        if isinstance(exc, OperationCancelled):
            raise
        message = re.sub(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^\s]+", "[source URL]", str(exc))
        raise ValueError(f"{plugin.name} provider {provider.label}: {message}") from exc
    finally:
        for key in list(sys.modules):
            if key == name or key.startswith(name + "."):
                sys.modules.pop(key, None)


def _archive(path):
    from Utils.archives.identify import ArchiveType, identify_archive, may_be_zip
    import zipfile
    kind = identify_archive(path)
    if kind == ArchiveType.UNKNOWN and not may_be_zip(path, kind) or kind == ArchiveType.ZIP and not zipfile.is_zipfile(path):
        raise ValueError("The provider response is not a supported archive.")


def _plugin_transfer(game, plan, destination, log, progress, control):
    from Utils.downloads.modl import ModlDownloadResult
    path = plan.provider.download_entrypoint
    name = f"_amethyst_transfer_{uuid.uuid4().hex}"
    def report(done, total=0):
        if (type(done) is not int or type(total) is not int or not 0 <= done <= 9223372036854775807
                or not 0 <= total <= 9223372036854775807 or total and done > total):
            raise ValueError("Download progress requires nonnegative byte counts.")
        _check(control)
        progress(done, total, plan.provider.label)
    def throttle(count):
        from Utils.downloads.bandwidth import throttle as limit
        if type(count) is not int or count < 0:
            raise ValueError("Download throttling requires a nonnegative byte count.")
        _check(control)
        limit(count, control)
    try:
        spec = importlib.util.spec_from_file_location(name, path, submodule_search_locations=[str(path.parent)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        context = {"api_version": 1, "plugin_id": plan.plugin.id, "provider_id": plan.provider.id,
                   "plugin_dir": str(plugin_directory(plan.plugin)), "game_id": game.game_id, "game": game.name,
                   "settings": load_settings(plan.plugin), "data": load_data(plan.plugin), "source": plan.source,
                   "artifact": deepcopy(plan.public()), "private": deepcopy(plan.descriptor["private"]),
                   "destination": str(destination), "check_cancelled": lambda: _check(control),
                   "report_progress": report, "throttle": throttle,
                   "log": lambda message: log(f"{plan.plugin.name}: {message}")}
        result = module.download_archive(context)
        if result is not None:
            raise ValueError("download_archive must write destination and return None.")
        _check(control)
        if destination.is_symlink() or not destination.is_file():
            raise ValueError("The provider did not produce a regular archive at destination.")
        if destination.stat().st_size != plan.descriptor["size_bytes"]:
            raise ValueError("The provider archive does not match the expected size.")
        digest = hashlib.sha256()
        with destination.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                _check(control)
                digest.update(chunk)
        checksum = digest.hexdigest()
        if plan.descriptor["sha256"] and checksum != plan.descriptor["sha256"]:
            raise ValueError("The provider archive does not match the expected checksum.")
        return ModlDownloadResult(success=True, file_path=destination, file_name=destination.name,
                                  bytes_downloaded=destination.stat().st_size, sha256=checksum)
    except Exception as exc:
        from Utils.operations import OperationCancelled
        if isinstance(exc, OperationCancelled):
            raise
        message = re.sub(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^\s]+", "[source URL]", str(exc))
        raise ValueError(f"{plan.plugin.name} transfer {plan.provider.label}: {message}") from exc
    finally:
        for key in list(sys.modules):
            if key == name or key.startswith(name + "."):
                sys.modules.pop(key, None)


def transfer(game, plan, log, progress, control):
    from Utils.atomic_write import write_atomic_text
    from Utils.config_paths import get_download_cache_dir_for_game
    from Utils.downloads.modl import ModlLink, download_modl_file, _split_archive_suffix
    from Utils.operations import acquire_resource_operation
    from Utils.profiles.export import _publish_new_archive
    cache = get_download_cache_dir_for_game(game.name)
    cache.mkdir(parents=True, exist_ok=True)
    descriptor = plan.descriptor
    def validate():
        if plan.plugin not in discover(game.game_id)[0] or plan.signature != json.dumps(_signature(plan.plugin, plan.provider), sort_keys=True):
            raise ValueError("The download provider changed; resolve and review the source again.")
    validate()
    with acquire_resource_operation(f"plugin-download-cache:{cache.resolve()}", "Plugin download", cancel=control):
        with tempfile.TemporaryDirectory(prefix=".plugin-download-", dir=cache) as temporary:
            temporary = Path(temporary)
            _check(control)
            if descriptor["transport"] == "plugin":
                result = _plugin_transfer(game, plan, temporary / descriptor["file_name"], log, progress, control)
            else:
                result = download_modl_file(ModlLink(game.game_id, descriptor["url"]), temporary,
                    progress_cb=lambda done, total, _name: progress(done, total, plan.provider.label), cancel=control,
                    file_name=descriptor["file_name"], headers=descriptor["headers"],
                    expected_size=descriptor["size_bytes"], expected_sha256=descriptor["sha256"], calculate_sha256=True)
            _check(control)
            if not result.success or not result.file_path:
                raise ValueError("The provider download failed: " + re.sub(r"https?://[^\s]+", "[download URL]", result.error))
            _archive(result.file_path)
            _check(control)
            validate()
            identity = {"version": 1, "plugin_id": plan.plugin.id, "provider_id": plan.provider.id,
                        "provider_label": plan.provider.label, "identity": descriptor["identity"],
                        "size_bytes": result.bytes_downloaded, "sha256": result.sha256, "metadata": descriptor["metadata"]}
            stem, suffix = _split_archive_suffix(descriptor["file_name"])
            counter = 0
            while True:
                _check(control)
                marker = f" ({counter})" if counter else ""
                base = stem
                while len((base + marker + suffix).encode("utf-8")) > 240:
                    base = base[:-1]
                final = cache / (base + marker + suffix)
                sidecar = final.with_suffix(final.suffix + SIDECAR)
                counter += 1
                part = final.with_name(final.name + ".part")
                if (final.exists() or final.is_symlink() or sidecar.exists() or sidecar.is_symlink()
                        or part.exists() or part.is_symlink()):
                    continue
                pending = temporary / "source.json"
                write_atomic_text(pending, json.dumps(identity, ensure_ascii=True))
                try:
                    _publish_new_archive(pending, sidecar)
                except FileExistsError:
                    continue
                try:
                    _check(control)
                    _publish_new_archive(result.file_path, final)
                except FileExistsError:
                    sidecar.unlink()
                    continue
                except BaseException:
                    sidecar.unlink(missing_ok=True)
                    raise
                break
    log(f"Downloaded {final.name} with {plan.provider.label}.")
    return {"archive": {"path": str(final), "name": final.name, "size_bytes": identity["size_bytes"]},
            "provider": identity}


def read_source(path, *, verify=False, cancel=None, strict=True):
    path = Path(path)
    sidecar = path.with_suffix(path.suffix + SIDECAR)
    if not sidecar.exists() and not sidecar.is_symlink():
        return None
    try:
        if sidecar.is_symlink() or not sidecar.is_file() or sidecar.stat().st_size > 131072:
            raise ValueError("Invalid provider download sidecar.")
        value = json.read_json(sidecar, max_bytes=131072)
        if (not isinstance(value, dict) or set(value) != {"version", "plugin_id", "provider_id", "provider_label", "identity", "size_bytes", "sha256", "metadata"}
                or type(value["version"]) is not int or value["version"] != 1
                or any(not isinstance(value[key], str) or not re.fullmatch(_ID, value[key]) for key in ("plugin_id", "provider_id"))
                or not isinstance(value["provider_label"], str) or not 1 <= len(value["provider_label"]) <= 200
                or not isinstance(value["identity"], str) or not 1 <= len(value["identity"]) <= 1000
                or not isinstance(value["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", value["sha256"])
                or type(value["size_bytes"]) is not int or value["size_bytes"] <= 0):
            raise ValueError("Invalid provider download identity.")
        value["metadata"] = _metadata(value["metadata"])
        if path.is_symlink() or not path.is_file() or path.stat().st_size != value["size_bytes"]:
            raise ValueError("The provider archive no longer matches its recorded size.")
        if verify:
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    _check(cancel)
                    digest.update(chunk)
            if digest.hexdigest() != value["sha256"]:
                raise ValueError("The provider archive no longer matches its recorded checksum.")
        return value
    except (OSError, ValueError, UnicodeError, RecursionError):
        if strict:
            raise
        return None


def write_source(meta_path, value):
    from Utils.atomic_write import write_atomic_text
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(meta_path, encoding="utf-8")
    if value is None or parser.get("ManagerPluginSource", "identity", fallback="") != value["identity"]:
        parser.remove_section("ManagerPluginUpdate")
    removed = parser.remove_section("ManagerPluginSource")
    if value is None and not removed:
        return
    if value is not None:
        parser["ManagerPluginSource"] = {key: json.dumps(item, ensure_ascii=True) if key == "metadata" else str(item)
                                         for key, item in value.items() if key != "version"}
    output = io.StringIO()
    parser.write(output)
    write_atomic_text(meta_path, output.getvalue())
