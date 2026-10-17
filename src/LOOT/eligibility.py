from __future__ import annotations

import atexit
import json
import os
import threading
from collections import OrderedDict
from pathlib import Path

from LOOT.loot_sorter import _libloot_version_str, is_available, loot
from LOOT.worker import LootWorker
from Utils.atomic_write import write_atomic_text
from Utils.mods.metadata import meta_file_signature


_worker = None
_lock = threading.Lock()
_CACHE_VERSION = 3
_CACHE_LIMIT = 8192
_CACHE_BYTES = 8 * 1024 * 1024
_cache = OrderedDict()
_cache_context = None
_cache_dirty = False


def _load_cache():
    global _cache_context, _cache_dirty
    root = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    path = root / "AmethystModManager" / "esl-eligibility.json"
    context = path, _libloot_version_str()
    if _cache_context == context:
        return
    _cache.clear()
    _cache_context = context
    _cache_dirty = False
    try:
        with path.open("rb") as stream:
            raw = stream.read(_CACHE_BYTES + 1)
        if len(raw) > _CACHE_BYTES:
            return
        data = json.loads(raw)
        if (not isinstance(data, dict) or data.get("version") != _CACHE_VERSION
                or data.get("libloot") != context[1]):
            return
        entries = data.get("entries")
        if not isinstance(entries, list):
            return
        for entry in entries[-_CACHE_LIMIT:]:
            if not isinstance(entry, list) or len(entry) != 4:
                continue
            name, game_type, signature, eligible = entry
            if (not isinstance(name, str) or not isinstance(game_type, str)
                    or not isinstance(signature, list) or len(signature) != 5
                    or not all(type(value) is int for value in signature)
                    or type(eligible) is not bool):
                continue
            _cache[name, game_type] = tuple(signature), eligible
    except (OSError, ValueError):
        pass


def flush_eligibility_cache(*, blocking=True):
    global _cache_dirty
    if not _lock.acquire(blocking=blocking):
        return
    try:
        if not _cache_dirty or _cache_context is None:
            return
        path, version = _cache_context
        data = {
            "version": _CACHE_VERSION, "libloot": version,
            "entries": [[name, game, signature, eligible]
                        for (name, game), (signature, eligible) in _cache.items()],
        }
        payload = json.dumps(data, separators=(",", ":"))
        while len(payload.encode("utf-8")) > _CACHE_BYTES and data["entries"]:
            data["entries"] = data["entries"][len(data["entries"]) // 2 + 1:]
            payload = json.dumps(data, separators=(",", ":"))
        try:
            write_atomic_text(path, payload)
        except OSError:
            return
        _cache_dirty = False
    finally:
        _lock.release()


atexit.register(flush_eligibility_cache, blocking=False)


def check_esl_eligible(plugin_path: Path, game_type_attr: str,
                       cancelled=None) -> bool | None:
    global _worker, _cache_dirty
    unknown = None if cancelled is not None else False
    if (not is_available() or not plugin_path.is_file()
            or getattr(loot.GameType, game_type_attr, None) is None):
        return unknown
    cancelled = cancelled or (lambda: False)
    with _lock:
        if cancelled():
            return None
        plugin_path = plugin_path.absolute()
        signature = meta_file_signature(plugin_path)
        if signature is None:
            return unknown
        _load_cache()
        key = str(plugin_path), game_type_attr
        cached = _cache.get(key)
        if cached is not None and cached[0] == signature:
            _cache.move_to_end(key)
            return cached[1]
        worker = None
        try:
            if (_worker is None or _worker.process is None
                    or _worker.process.poll() is not None):
                if _worker is not None and _worker.process is not None:
                    _worker.close()
                _worker = None
                _worker = LootWorker()
            worker = _worker
            worker.cancelled = cancelled
            eligible = worker.call("eligibility", path=str(plugin_path),
                                   game_type=game_type_attr, timeout=30.0)
            if type(eligible) is not bool:
                raise RuntimeError("LOOT returned an invalid eligibility result.")
            if meta_file_signature(plugin_path) != signature:
                return unknown
            _cache[key] = signature, eligible
            _cache.move_to_end(key)
            while len(_cache) > _CACHE_LIMIT:
                _cache.popitem(last=False)
            _cache_dirty = True
            return eligible
        except Exception as exc:
            _worker = None
            if worker is not None and worker.process is not None:
                worker.close()
            if cancelled():
                return None
            from Utils.app_log import app_log
            app_log(f"[loot] ESL eligibility failed for {plugin_path.name}: {exc}")
            return unknown
        finally:
            if worker is not None:
                worker.cancelled = lambda: False
