"""Workshop archive metadata and legacy cache migration."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import json
from pathlib import Path
import re
from urllib.parse import parse_qs, urlparse

from Utils.config_paths import get_application_cache_dir, get_config_dir


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


def state_root() -> Path:
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


def migrate_cache() -> Path | None:
    state = state_root()
    legacy = tuple(state / name for name in ("tools", "downloads"))
    if not any(path.exists() or path.is_symlink() for path in legacy):
        return None
    with _download_lock(state):
        root = get_application_cache_dir("workshop")
        for source in legacy:
            if source.exists() or source.is_symlink():
                get_application_cache_dir(
                    "workshop", source.name, legacy=source,
                )
        return root


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
