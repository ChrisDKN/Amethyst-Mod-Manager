"""Anonymous Workshop metadata through the local SteamKit helper."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import select
import subprocess
import threading
import time

from .download import DownloadCancelled, WorkshopItem, parse_item_id

PAGE_SIZE = 24


@dataclass(frozen=True)
class Entry:
    item: WorkshopItem
    description: str
    preview: str
    tags: tuple[str, ...]
    children: tuple[str, ...]
    created: int
    subscriptions: int
    votes: int
    file_type: int
    creator: str = ""
    previews: tuple[str, ...] = ()
    mature: bool = False
    views: int = 0
    favorites: int = 0

    @property
    def package_id(self):
        return self.item.item_id

    @property
    def full_name(self):
        return self.item.title

    @property
    def file_size(self):
        return self.item.size


def parse_entry(raw, app_id):
    item_id = parse_item_id(str(raw.get("id", "")))
    if (raw.get("result") != 1 or str(raw.get("appid")) != str(app_id)
            or raw.get("banned")):
        raise ValueError(f"Workshop item {item_id} is unavailable or belongs to another game.")
    return Entry(
        WorkshopItem(str(app_id), item_id, str(raw.get("title") or item_id),
                     int(raw.get("file_size") or 0), int(raw.get("time_updated") or 0)),
        str(raw.get("description") or ""), str(raw.get("preview_url") or ""),
        tuple(str(x) for x in raw.get("tags", ())),
        tuple(parse_item_id(str(x)) for x in raw.get("children", ())),
        int(raw.get("time_created") or 0), int(raw.get("subscriptions") or 0),
        int(raw.get("votes") or 0), int(raw.get("file_type") or 0),
        str(raw.get("creator") or ""), tuple(str(x) for x in raw.get("previews", ()) if x),
        bool(raw.get("maybe_inappropriate_sex") or raw.get("maybe_inappropriate_violence")),
        int(raw.get("views") or 0), int(raw.get("favorited") or 0))


class WorkshopClient:
    def __init__(self):
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._process = None

    def close(self):
        self._closed.set()
        self._stop()

    def _stop(self):
        process, self._process = self._process, None
        if process is not None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            process.wait()
            process.stdin.close()
            process.stdout.close()

    def _check(self, cancel):
        if self._closed.is_set() or (cancel is not None and cancel.is_set()):
            raise DownloadCancelled()

    def request(self, request, cancel=None):
        while not self._lock.acquire(timeout=0.1):
            self._check(cancel)
        try:
            self._check(cancel)
            process = self._process
            if process is None or process.poll() is not None:
                self._stop()
                from .helper import helper_path
                helper = helper_path()
                if not helper.is_file():
                    raise RuntimeError("The plugin Workshop helper is missing. See this plugin’s README for build instructions.")
                env = os.environ.copy()
                for key in ("LD_PRELOAD", "LD_LIBRARY_PATH", "DOTNET_ROOT", "DOTNET_ROOT_X64"):
                    env.pop(key, None)
                process = subprocess.Popen([str(helper)], env=env, stdin=subprocess.PIPE,
                                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                           bufsize=0, start_new_session=True)
                self._process = process
            process.stdin.write((json.dumps(request) + "\n").encode())
            deadline = time.monotonic() + 75
            output = bytearray()
            while b"\n" not in output:
                self._check(cancel)
                if time.monotonic() > deadline:
                    raise TimeoutError("Steam Workshop did not respond. Please retry.")
                ready, _, _ = select.select([process.stdout], [], [], 0.1)
                if not ready:
                    continue
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    raise RuntimeError("Steam Workshop disconnected. Please retry.")
                output.extend(chunk)
                if len(output) > 8 * 1024 * 1024:
                    raise ValueError("Steam returned an oversized Workshop response.")
            result = json.loads(output)
            if not result.get("ok"):
                raise RuntimeError(result.get("error") or "Steam Workshop request failed.")
            return result["data"]
        except Exception:
            self._stop()
            raise
        finally:
            self._lock.release()

    def listing(self, app_id, *, cursor="*", search="", sort=9, tag="", cancel=None,
                page=None, page_size=PAGE_SIZE, tags=(), excluded_tags=(), match_all=True,
                since=0, author=""):
        request = dict(op="author" if author else "query", appid=int(app_id), cursor=cursor,
                       search=search, sort=sort, tag=tag, page_size=page_size, tags=list(tags),
                       excluded_tags=list(excluded_tags), match_all=match_all, since=since,
                       author=author)
        if page is not None:
            request["page"] = page
        data = self.request(request, cancel)
        entries = [parse_entry(raw, app_id) for raw in data["items"]
                   if raw.get("result") == 1 and not raw.get("banned")]
        return entries, int(data["total"]), str(data.get("cursor") or "")

    def resolve(self, app_id, item_id, cancel=None):
        return self.resolve_many(app_id, [item_id], cancel)

    def resolve_many(self, app_id, item_ids, cancel=None):
        roots = list(dict.fromkeys(parse_item_id(item) for item in item_ids))
        entries = {}
        pending = list(roots)
        while pending:
            self._check(cancel)
            if len(entries) + len(pending) > 200:
                raise ValueError("This Workshop dependency graph exceeds 200 items.")
            batch, pending = pending[:100], pending[100:]
            data = self.request(dict(op="details", appid=int(app_id), ids=batch), cancel)
            found = {}
            for raw in data["items"]:
                entry = parse_entry(raw, app_id)
                if entry.file_type != 0:
                    raise ValueError(f"{entry.full_name} is not an installable mod. Collections are not supported.")
                found[entry.package_id] = entry
            if set(found) != set(batch):
                raise ValueError("Steam did not return all required Workshop items.")
            entries.update(found)
            for entry in found.values():
                for child in entry.children:
                    if child not in entries and child not in pending:
                        pending.append(child)
        ordered, visited, active, cycles = [], set(), set(), set()

        def visit(item):
            if item in active:
                cycles.add(item)
                return
            if item in visited:
                return
            visited.add(item)
            active.add(item)
            for child in entries[item].children:
                visit(child)
            active.remove(item)
            ordered.append(entries[item])

        for root in roots:
            visit(root)
        return ordered, cycles


def installed_items(staging, app_id):
    from Nexus.nexus_meta import read_meta
    result = {}
    if not staging:
        return result
    root = Path(staging)
    if root.is_dir():
        for folder in root.iterdir():
            if not folder.is_dir():
                continue
            meta = read_meta(folder / "meta.ini")
            if meta and meta.workshop_app_id == str(app_id) and meta.workshop_item_id:
                result.setdefault(meta.workshop_item_id, []).append(folder.name)
    return result
