"""
github/sync.py
Background sync of custom game handlers from configured GitHub repositories
and wizard plugins from the Amethyst-Mod-Manager ``Resources`` branch.

Tkinter-free port of the old ``gui.py`` ``_sync_custom_handlers`` /
``_sync_plugins`` startup helpers, with two Qt-era differences:

* Custom handlers are all fetched from the flat ``Custom Handlers/`` root
  folder - the versioned ``X.Y/`` subfolder walking (``1.3`` etc.) is gone
  because those subfolders were removed from the repo.
* Plugins are fetched from ``Plugins/v2`` (Qt-compatible) instead of the
  ``Plugins`` root, which still holds the Tkinter-only versions.

Both functions run their network + disk work on a daemon thread and swallow
all errors so a startup sync never blocks or crashes the UI. An optional
``on_changed`` callback fires (on the worker thread - marshal to the GUI
thread yourself) when at least one file was written.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from typing import Callable, Optional

from Utils.config_paths import (
    get_plugins_dir, get_languages_dir,
    get_ludusavi_manifest_path,
)
from Utils.github.cache import fetch, fetch_text
from Utils.ui.config import load_dev_mode

# Qt-compatible plugins live under Plugins/v2; the Plugins root holds the
# Tkinter versions that will not work with the Qt UI.
_PLUGINS_API_URL = (
    "https://api.github.com/repos/ChrisDKN/Amethyst-Mod-Manager/contents/"
    "Plugins/v2?ref=Resources"
)

# Compiled UI translations (amethyst_<code>.qm) live under Localisation/.
_LANGUAGES_API_URL = (
    "https://api.github.com/repos/ChrisDKN/Amethyst-Mod-Manager/contents/"
    "Localisation?ref=Resources"
)

# Ludusavi save-path data. A single known file name, so this one is fetched
# straight from raw.githubusercontent (ETag-conditional) instead of listing a
# folder through the rate-limited contents API.
_LUDUSAVI_RAW_URL = (
    "https://raw.githubusercontent.com/ChrisDKN/Amethyst-Mod-Manager/"
    "Resources/Ludusavi/ludusavi_saves.json"
)


def _log_sync(what: str, detail) -> None:
    """Log a background-sync problem to stderr (sync must never crash the UI)."""
    try:
        print(f"[gh_sync] {what}: {detail}", file=sys.stderr, flush=True)
    except Exception:
        pass


def _safe_filename(name: str) -> "str | None":
    """Validate a repo-listed file name as a bare path component, or None.

    The name comes from the GitHub API response and is joined onto a local
    directory (and, for plugins, imported) - reject anything that could
    escape the destination folder.
    """
    if not name or "/" in name or "\\" in name or ".." in name:
        return None
    base = os.path.basename(name)
    return base or None


def _write_if_changed(dest, raw: str) -> bool:
    """Write *raw* to *dest* only if it differs from the current contents.

    Returns True if the file was written.
    """
    try:
        if dest.is_file() and dest.read_text(encoding="utf-8") == raw:
            return False
    except Exception:
        pass
    dest.write_text(raw, encoding="utf-8")
    return True


def _write_bytes_if_changed(dest, data: bytes) -> bool:
    """Write binary *data* to *dest* only if it differs. Returns True if written.

    Used for .qm translation files (binary, unlike the text handlers/plugins).
    """
    try:
        if dest.is_file() and dest.read_bytes() == data:
            return False
    except Exception:
        pass
    dest.write_bytes(data)
    return True


def _handler_entries(source, *, force=False):
    from Utils.github.handler_sources import listing_url, safe_filename
    listing = fetch_text(listing_url(source), timeout=15,
                         min_interval=0 if force else 3600, force=force, allow_stale=not force)
    if listing is None:
        raise OSError("Could not load handler repository " + source["repository"])
    data = json.loads(listing)
    if not isinstance(data, list):
        raise ValueError("Expected a folder of handler JSON files in " + source["repository"])
    entries = {e["name"]: e["download_url"] for e in data
               if isinstance(e, dict) and e.get("type") == "file"
               and safe_filename(e.get("name")) and e.get("download_url")}
    if not entries:
        raise ValueError("No handler JSON files found in " + source["repository"])
    return entries


def sync_custom_handlers(on_changed: Optional[Callable[[], None]] = None,
                         *, force=False, on_done=None) -> None:
    """Sync enabled handler repositories in the background; skip dev mode unless forced."""
    if load_dev_mode() and not force:
        return

    def _do():
        from Utils.github.handler_sources import HandlerSources
        changed, errors, conflicts = False, [], []

        def failed(detail):
            errors.append(str(detail))
            _log_sync("sync_custom_handlers", detail)

        try:
            store = HandlerSources()
            for source in store.sources():
                if not source["enabled"]:
                    continue
                try:
                    entries = _handler_entries(source, force=force)
                except Exception as exc:
                    failed(exc)
                    continue
                for filename, download_url in sorted(entries.items()):
                    try:
                        raw = fetch_text(download_url, accept="*/*", timeout=10,
                                         min_interval=0 if force else 3600,
                                         force=force, allow_stale=not force)
                        if raw is None:
                            raise OSError("Could not download " + filename)
                        changed = store.write_handler(source, filename, raw, filenames=entries) or changed
                    except Exception as exc:
                        failed(f"{source['repository']}: {filename}: {exc}")
            if on_done is not None:
                conflicts = [game["game_id"] for game in store.games() if len(game["options"]) > 1]
        except Exception as exc:
            failed(exc)
        if changed and on_changed is not None:
            on_changed()
        if on_done is not None:
            on_done({"changed": changed, "errors": errors, "conflicts": conflicts})

    threading.Thread(target=_do, daemon=True).start()


def force_update_handler(candidates,
                         on_done: Optional[Callable[[str], None]] = None,
                         *, game_id=None) -> None:
    """Force-update the selected repository's handler for a game ID."""
    def _do():
        from Utils.github.handler_sources import HandlerSources, _definition
        status = "missing"
        try:
            store = HandlerSources()
            chosen = store.selected_handler(game_id) if game_id else None
            if game_id is None:
                matches = [store.selected_handler(game["game_id"]) for game in store.games()
                           if any(option["filename"] in candidates for option in game["options"])]
                chosen = matches[0] if len(matches) == 1 else None
            if chosen and chosen["source_id"]:
                source = next((s for s in store.sources()
                               if s["id"] == chosen["source_id"] and s["enabled"]), None)
                if source:
                    entries = _handler_entries(source, force=True)
                    filename = chosen["filename"]
                    if filename in entries:
                        raw = fetch_text(entries[filename], accept="*/*", timeout=15,
                                         min_interval=0, force=True, allow_stale=False)
                        if raw is None:
                            raise OSError("Could not download " + filename)
                        actual_gid, _ = _definition(raw)
                        expected_gid, _ = _definition(json.dumps(chosen["definition"]))
                        if actual_gid != expected_gid:
                            raise ValueError("The repository handler changed its game ID.")
                        current = store.selected_handler(actual_gid)
                        if current and current["key"] == chosen["key"]:
                            status = ("updated" if store.write_handler(source, filename, raw)
                                      else "unchanged")
        except Exception as exc:
            _log_sync("force_update_handler", f"update failed: {exc}")
            status = "failed"
        if on_done is not None:
            try:
                on_done(status)
            except Exception:
                pass

    threading.Thread(target=_do, daemon=True).start()


def sync_plugins(on_changed: Optional[Callable[[], None]] = None) -> None:
    """Background-download every Qt wizard plugin, overwriting stale copies.

    Pulls from ``Plugins/v2`` (Qt-compatible). Skips in dev mode.
    """
    if load_dev_mode():
        return

    def _do():
        try:
            listing = fetch_text(_PLUGINS_API_URL, timeout=15, min_interval=3600)
            if listing is None:
                return
            data = json.loads(listing)
            entries = [
                e for e in data
                if isinstance(e, dict)
                and e.get("type") == "file"
                and e.get("name", "").endswith(".py")
            ]
            changed = False
            dest_dir = get_plugins_dir()
            for e in entries:
                filename = _safe_filename(e.get("name", ""))
                if filename is None:
                    _log_sync("sync_plugins",
                              f"skipped unsafe file name {e.get('name', '')!r}")
                    continue
                download_url = e.get("download_url")
                if not download_url:
                    continue
                try:
                    raw = fetch_text(
                        download_url, accept="*/*", timeout=10, min_interval=3600,
                    )
                    if raw is None:
                        continue
                    if _write_if_changed(dest_dir / filename, raw):
                        changed = True
                except Exception as exc:
                    _log_sync("sync_plugins",
                              f"could not update {filename}: {exc}")
            if changed:
                try:
                    from Utils.wizards.plugins import discover_plugins
                    discover_plugins(force=True)
                except Exception as exc:
                    _log_sync("sync_plugins", f"plugin rediscovery failed: {exc}")
                if on_changed is not None:
                    on_changed()
        except Exception as exc:
            _log_sync("sync_plugins", f"sync failed: {exc}")

    threading.Thread(target=_do, daemon=True).start()


def sync_languages(on_changed: Optional[Callable[[], None]] = None,
                   *, force: bool = False) -> None:
    """Background-download every UI translation (amethyst_<code>.qm) from the
    Resources branch ``Localisation/`` folder into the config languages/ dir.

    Mirrors sync_custom_handlers/sync_plugins: runs on a daemon thread, swallows
    all errors, only fires ``on_changed`` (on the worker thread - marshal it
    yourself) when at least one .qm was written.

    ``force=True`` (a manual "Sync language files" press) bypasses the dev-mode
    skip and the fetch cache interval so it always hits the network. The
    automatic startup sync leaves force=False (skips in dev, throttled by cache).

    NB: .qm files are binary, so they are fetched with fetch() (raw bytes) and
    written with _write_bytes_if_changed, unlike the text handlers/plugins.
    """
    if load_dev_mode() and not force:
        return

    def _do():
        try:
            listing = fetch_text(
                _LANGUAGES_API_URL, timeout=15,
                min_interval=(0 if force else 3600), force=force,
            )
            if listing is None:
                return
            data = json.loads(listing)
            entries = [
                e for e in data
                if isinstance(e, dict)
                and e.get("type") == "file"
                and e.get("name", "").startswith("amethyst_")
                and e.get("name", "").endswith(".qm")
            ]
            changed = False
            dest_dir = get_languages_dir()
            for e in entries:
                filename = _safe_filename(e.get("name", ""))
                if filename is None:
                    _log_sync("sync_languages",
                              f"skipped unsafe file name {e.get('name', '')!r}")
                    continue
                download_url = e.get("download_url")
                if not download_url:
                    continue
                try:
                    raw = fetch(
                        download_url, accept="*/*", timeout=15,
                        min_interval=(0 if force else 3600), force=force,
                    )
                    if raw is None:
                        continue
                    if _write_bytes_if_changed(dest_dir / filename, raw):
                        changed = True
                except Exception as exc:
                    _log_sync("sync_languages",
                              f"could not update {filename}: {exc}")
            if changed and on_changed is not None:
                on_changed()
        except Exception as exc:
            _log_sync("sync_languages", f"sync failed: {exc}")

    threading.Thread(target=_do, daemon=True).start()


def sync_ludusavi_manifest(on_changed: Optional[Callable[[], None]] = None,
                           *, force: bool = False) -> None:
    """Background-download the Ludusavi save-path data from the Resources branch.

    Lets save locations for new games ship without an app release: the file
    lands in the config folder and Utils.saves.ludusavi prefers whichever
    copy (this one or the bundled one) has the later ``generated`` date.

    The download is rejected unless it parses as JSON, declares a schema
    version this build understands, and is not older than what we already use
    - a corrupt or rolled-back publish must never replace working data.

    Checked daily (``force=True`` for a manual press bypasses the throttle and
    the dev-mode skip). ``on_changed`` fires on the worker thread.
    """
    if load_dev_mode() and not force:
        return

    def _do():
        try:
            from Utils.saves.ludusavi import (
                SCHEMA_VERSION, data_info, peek_header, reload,
            )
            raw = fetch(
                _LUDUSAVI_RAW_URL, accept="*/*", timeout=30,
                min_interval=(0 if force else 86400), force=force,
            )
            if raw is None:
                return
            try:
                blob = json.loads(raw.decode("utf-8"))
            except Exception as exc:
                _log_sync("sync_ludusavi_manifest", f"not valid JSON: {exc}")
                return
            version = blob.get("v")
            if version != SCHEMA_VERSION:
                _log_sync("sync_ludusavi_manifest",
                          f"skipped: schema v{version} != v{SCHEMA_VERSION}")
                return
            if not isinstance(blob.get("games"), dict) or not blob["games"]:
                _log_sync("sync_ludusavi_manifest", "skipped: no games in payload")
                return
            generated = str(blob.get("generated", ""))
            if generated < data_info()[1]:
                _log_sync("sync_ludusavi_manifest",
                          f"skipped: {generated} is older than the copy in use")
                return
            dest = get_ludusavi_manifest_path()
            before = peek_header(dest)
            if not _write_bytes_if_changed(dest, raw):
                return
            _log_sync("sync_ludusavi_manifest",
                      f"updated {before[1] if before else 'none'} -> {generated}")
            reload()
            if on_changed is not None:
                on_changed()
        except Exception as exc:
            _log_sync("sync_ludusavi_manifest", f"sync failed: {exc}")

    threading.Thread(target=_do, daemon=True).start()
