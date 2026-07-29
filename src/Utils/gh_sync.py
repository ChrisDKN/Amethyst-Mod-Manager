"""
gh_sync.py
Background sync of custom game handlers and wizard plugins from the
Amethyst-Mod-Manager ``Resources`` branch on GitHub.

Tkinter-free port of the old ``gui.py`` ``_sync_custom_handlers`` /
``_sync_plugins`` startup helpers, with two Qt-era differences:

* Custom handlers are all fetched from the flat ``Custom Handlers/`` root
  folder — the versioned ``X.Y/`` subfolder walking (``1.3`` etc.) is gone
  because those subfolders were removed from the repo.
* Plugins are fetched from ``Plugins/v2`` (Qt-compatible) instead of the
  ``Plugins`` root, which still holds the Tkinter-only versions.

Both functions run their network + disk work on a daemon thread and swallow
all errors so a startup sync never blocks or crashes the UI. An optional
``on_changed`` callback fires (on the worker thread — marshal to the GUI
thread yourself) when at least one file was written.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from typing import Callable, Optional

from Utils.config_paths import (
    get_custom_games_dir, get_plugins_dir, get_languages_dir,
)
from Utils.gh_cache import fetch, fetch_text
from Utils.ui_config import load_dev_mode

_CUSTOM_HANDLERS_API_URL = (
    "https://api.github.com/repos/ChrisDKN/Amethyst-Mod-Manager/contents/"
    "Custom%20Handlers?ref=Resources"
)

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


def _log_sync(what: str, detail) -> None:
    """Log a background-sync problem to stderr (sync must never crash the UI)."""
    try:
        print(f"[gh_sync] {what}: {detail}", file=sys.stderr, flush=True)
    except Exception:
        pass


def _safe_filename(name: str) -> "str | None":
    """Validate a repo-listed file name as a bare path component, or None.

    The name comes from the GitHub API response and is joined onto a local
    directory (and, for plugins, imported) — reject anything that could
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


def sync_custom_handlers(on_changed: Optional[Callable[[], None]] = None) -> None:
    """Background-download every custom handler .json, overwriting stale copies.

    Skips entirely in dev mode so a developer's in-place edits are never
    clobbered by the repo copy.
    """
    if load_dev_mode():
        return

    def _do():
        try:
            listing = fetch_text(_CUSTOM_HANDLERS_API_URL, timeout=15, min_interval=3600)
            if listing is None:
                return
            data = json.loads(listing)
            entries = [
                e for e in data
                if isinstance(e, dict)
                and e.get("type") == "file"
                and e.get("name", "").endswith(".json")
            ]
            changed = False
            dest_dir = get_custom_games_dir()
            for e in entries:
                filename = _safe_filename(e.get("name", ""))
                if filename is None:
                    _log_sync("sync_custom_handlers",
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
                    json.loads(raw)  # validate
                    if _write_if_changed(dest_dir / filename, raw):
                        changed = True
                except Exception as exc:
                    _log_sync("sync_custom_handlers",
                              f"could not update {filename}: {exc}")
            if changed and on_changed is not None:
                on_changed()
        except Exception as exc:
            _log_sync("sync_custom_handlers", f"sync failed: {exc}")

    threading.Thread(target=_do, daemon=True).start()


def force_update_handler(candidates,
                         on_done: Optional[Callable[[str], None]] = None) -> None:
    """Force re-download a single custom handler .json from the Resources
    branch, bypassing the fetch-cache throttle (a manual "Force update
    handler" press — so it also ignores dev mode, like sync_languages
    force=True).

    *candidates* is a sequence of possible file names inside the repo's
    ``Custom Handlers/`` folder, tried in order against a fresh listing
    (normally ``[basename of _source_file, "<game_id>.json"]`` — repo
    handlers are named ``<game_id>.json``, but a local edit re-saves under
    the game_id so the two can differ).

    Runs on a daemon thread. ``on_done`` fires on the worker thread —
    marshal to the GUI thread yourself — with one of:

    * ``"updated"``   — a newer definition was downloaded and written
    * ``"unchanged"`` — the repo copy already matches the local file
    * ``"missing"``   — no candidate exists on the Resources branch
    * ``"failed"``    — network error or the repo copy isn't valid JSON
    """

    def _do():
        status = "failed"
        try:
            listing = fetch_text(
                _CUSTOM_HANDLERS_API_URL, timeout=15, min_interval=0, force=True,
            )
            if listing is not None:
                by_name = {
                    e.get("name"): e.get("download_url")
                    for e in json.loads(listing)
                    if isinstance(e, dict) and e.get("type") == "file"
                }
                download_url = next(
                    (by_name[c] for c in candidates if by_name.get(c)), None)
                if download_url is None:
                    status = "missing"
                else:
                    raw = fetch_text(
                        download_url, accept="*/*", timeout=15,
                        min_interval=0, force=True,
                    )
                    if raw is not None:
                        json.loads(raw)  # validate
                        # Write back under the repo name so future startup
                        # syncs keep updating the same file.
                        matched = next(c for c in candidates if by_name.get(c))
                        dest = get_custom_games_dir() / matched
                        status = ("updated" if _write_if_changed(dest, raw)
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
                    from Utils.plugin_loader import discover_plugins
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
    all errors, only fires ``on_changed`` (on the worker thread — marshal it
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
