"""
profile_state.py
Centralised read/write for profile_state.json — a single file that
consolidates all small per-profile JSON/text state files:

  collapsed_seps              list[str]
  separator_locks             dict[str, bool]
  separator_colors            dict[str, str]
  separator_deploy_paths      dict[str, dict]
  root_folder_state           dict  ({"enabled": bool})
  mod_strip_prefixes          dict[str, list[str]]
  plugin_locks                dict[str, ...]
  disabled_plugins            dict[str, list[str]]  (mod_name -> [plugin, ...])
  excluded_mod_files          dict[str, list[str]]  (mod_name -> [rel_key_lower, ...])
  profile_settings            dict  (profile_specific_mods, collection_url, original_default, …)
  ignored_missing_requirements list[str]

Migration: when profile_state.json is missing but legacy per-key files exist,
read_profile_state() merges them into a new profile_state.json and deletes those
legacy files (cleanup). If profile_state.json is missing and no legacy files
exist, nothing is written (treated as a new / empty profile).

While profile_state.json exists, read helpers still fall back to legacy files
for any key missing from the JSON (for partially migrated folders). Saves go
to profile_state.json only.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

_FILENAME = "profile_state.json"

# Guards read-modify-write cycles in _update_key against concurrent threads.
# Keyed by resolved profile_dir to allow independent profiles to update in parallel.
_profile_locks: dict[Path, threading.Lock] = {}
_profile_locks_guard = threading.Lock()

# Legacy filenames used for migration fallback
_LEGACY = {
    "collapsed_seps":               "collapsed_seps.json",
    "separator_locks":              "separator_locks.json",
    "separator_colors":             "separator_colors.json",
    "separator_deploy_paths":       "separator_deploy_paths.json",
    "root_folder_state":            "root_folder_state.json",
    "mod_strip_prefixes":           "mod_strip_prefixes.json",
    "plugin_locks":                 "plugin_locks.json",
    "disabled_plugins":             "disabled_plugins.json",
    "excluded_mod_files":           "excluded_mod_files.json",
    "profile_settings":             "profile_settings.json",
    # ignored_missing_requirements has no legacy JSON file — it was a .txt
}

_LEGACY_IGNORED_TXT = "ignored_missing_requirements.txt"


def _state_path(profile_dir: Path) -> Path:
    return profile_dir / _FILENAME


def _load_legacy_json_file(path: Path):
    """Return parsed JSON or None if missing/unreadable/wrong type for generic dict merge."""
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _consolidate_legacy_profile_state(profile_dir: Path) -> None:
    """If profile_state.json is absent, merge legacy files into it and delete them.

    No-op when the profile directory does not exist, profile_state.json already
    exists, or no legacy sources are present (new profile).
    """
    if not profile_dir.is_dir():
        return
    dest = _state_path(profile_dir)
    if dest.is_file():
        return

    legacy_paths: list[Path] = [profile_dir / name for name in _LEGACY.values()]
    ignored_txt = profile_dir / _LEGACY_IGNORED_TXT
    if not any(p.is_file() for p in legacy_paths) and not ignored_txt.is_file():
        return

    state: dict = {}
    to_unlink: list[Path] = []

    list_keys = frozenset({"collapsed_seps"})
    for key, fname in _LEGACY.items():
        p = profile_dir / fname
        raw = _load_legacy_json_file(p)
        if raw is None:
            continue
        if key in list_keys:
            if isinstance(raw, list):
                seps = sorted({x for x in raw if isinstance(x, str)})
                state[key] = seps
                to_unlink.append(p)
        elif isinstance(raw, dict):
            state[key] = raw
            to_unlink.append(p)

    if ignored_txt.is_file():
        try:
            lines = sorted({
                line.strip()
                for line in ignored_txt.read_text(encoding="utf-8").splitlines()
                if line.strip()
            })
            if lines:
                state["ignored_missing_requirements"] = lines
            to_unlink.append(ignored_txt)
        except OSError:
            pass

    if not state:
        return

    try:
        write_profile_state(profile_dir, state)
    except OSError:
        return

    for p in to_unlink:
        try:
            if p.is_file():
                p.unlink()
        except OSError:
            pass


def read_profile_state(profile_dir: Path) -> dict:
    """Load profile_state.json. Returns {} if absent or corrupt."""
    path = _state_path(profile_dir)
    if not path.is_file():
        _consolidate_legacy_profile_state(profile_dir)
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


def write_profile_state(profile_dir: Path, state: dict) -> None:
    """Write profile_state.json atomically."""
    profile_dir.mkdir(parents=True, exist_ok=True)
    path = _state_path(profile_dir)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _read_key(profile_dir: Path, state: dict | None, key: str):
    """Return state[key] if present in *state* snapshot, else current disk, else legacy file.

    If *state* is a stale in-memory snapshot (e.g. loaded at profile open) and a key was
    written later only to profile_state.json, we must read from disk — otherwise callers
    see {} and the next write drops other mods' data (e.g. disabled_plugins).
    """
    if state is not None and key in state:
        return state[key]
    disk = read_profile_state(profile_dir)
    if key in disk:
        return disk[key]
    # Migration fallback: read old separate file
    legacy_name = _LEGACY.get(key)
    if legacy_name:
        legacy = profile_dir / legacy_name
        if legacy.is_file():
            try:
                return json.loads(legacy.read_text(encoding="utf-8"))
            except Exception:
                pass
    return None


# ---------------------------------------------------------------------------
# Per-key typed accessors (used by modlist_panel, plugin_panel, deploy, etc.)
# ---------------------------------------------------------------------------

def read_collapsed_seps(profile_dir: Path, state: dict | None = None) -> set[str]:
    raw = _read_key(profile_dir, state, "collapsed_seps")
    if isinstance(raw, list):
        return set(raw)
    return set()


def read_separator_locks(profile_dir: Path, state: dict | None = None) -> dict:
    raw = _read_key(profile_dir, state, "separator_locks")
    if isinstance(raw, dict):
        return raw
    return {}


def read_separator_colors(profile_dir: Path, state: dict | None = None) -> dict[str, str]:
    raw = _read_key(profile_dir, state, "separator_colors")
    if isinstance(raw, dict):
        return {k: v for k, v in raw.items()
                if isinstance(k, str) and isinstance(v, str)}
    return {}


def read_separator_deploy_paths(profile_dir: Path, state: dict | None = None) -> dict[str, dict]:
    raw = _read_key(profile_dir, state, "separator_deploy_paths")
    if not isinstance(raw, dict):
        return {}
    result = {}
    for k, v in raw.items():
        if not isinstance(k, str):
            continue
        if isinstance(v, str):
            result[k] = {"path": v, "raw": False}
        elif isinstance(v, dict):
            result[k] = {
                "path": v.get("path", "") if isinstance(v.get("path"), str) else "",
                "raw": bool(v.get("raw", False)),
            }
    return result


def read_root_folder_state(profile_dir: Path, state: dict | None = None) -> bool:
    raw = _read_key(profile_dir, state, "root_folder_state")
    if isinstance(raw, dict):
        return bool(raw.get("enabled", True))
    return True


def read_mod_strip_prefixes(profile_dir: Path, state: dict | None = None) -> dict[str, list[str]]:
    raw = _read_key(profile_dir, state, "mod_strip_prefixes")
    if isinstance(raw, dict):
        return {
            k: v if isinstance(v, list) else []
            for k, v in raw.items() if isinstance(k, str)
        }
    return {}


def read_plugin_locks(profile_dir: Path, state: dict | None = None) -> dict:
    raw = _read_key(profile_dir, state, "plugin_locks")
    if isinstance(raw, dict):
        return raw
    return {}


def _normalize_mod_child_str_list(v) -> list[str]:
    """Coerce JSON value to list[str] (single string or list; for per-mod string lists)."""
    if isinstance(v, str):
        return [v] if v else []
    if isinstance(v, list):
        return [x for x in v if isinstance(x, str)]
    return []


def read_disabled_plugins(profile_dir: Path, state: dict | None = None) -> dict[str, list[str]]:
    """Read disabled_plugins. Returns {} if absent or corrupt. Falls back to legacy file."""
    raw = _read_key(profile_dir, state, "disabled_plugins")
    if not isinstance(raw, dict):
        return {}
    out: dict[str, list[str]] = {}
    for k, v in raw.items():
        if not isinstance(k, str):
            continue
        norm = _normalize_mod_child_str_list(v)
        if norm:
            out[k] = norm
    return out


def read_excluded_mod_files(profile_dir: Path, state: dict | None = None) -> dict[str, list[str]]:
    """Read excluded_mod_files. Returns {} if absent or corrupt. Falls back to legacy file.

    Format: {mod_name: [rel_key_lower, ...]}
    """
    raw = _read_key(profile_dir, state, "excluded_mod_files")
    if not isinstance(raw, dict):
        return {}
    out: dict[str, list[str]] = {}
    for k, v in raw.items():
        if not isinstance(k, str):
            continue
        norm = _normalize_mod_child_str_list(v)
        if norm:
            out[k] = norm
    return out


def read_profile_settings(profile_dir: Path, state: dict | None = None) -> dict:
    """Read profile_settings (flags and metadata). Returns {} if absent or corrupt.

    Typical keys: profile_specific_mods, collection_url, original_default.
    """
    raw = _read_key(profile_dir, state, "profile_settings")
    if isinstance(raw, dict):
        return dict(raw)
    return {}


def read_ignored_missing_requirements(profile_dir: Path, state: dict | None = None) -> set[str]:
    """Read ignored_missing_requirements. Falls back to legacy .txt file."""
    if state is not None and "ignored_missing_requirements" in state:
        raw = state["ignored_missing_requirements"]
        if isinstance(raw, list):
            return {s for s in raw if isinstance(s, str)}
        return set()
    disk = read_profile_state(profile_dir)
    if "ignored_missing_requirements" in disk:
        raw = disk["ignored_missing_requirements"]
        if isinstance(raw, list):
            return {s for s in raw if isinstance(s, str)}
        return set()
    # Migration fallback: old .txt file
    legacy = profile_dir / "ignored_missing_requirements.txt"
    if legacy.is_file():
        try:
            return {
                line.strip() for line in legacy.read_text().splitlines()
                if line.strip()
            }
        except OSError:
            pass
    return set()


# ---------------------------------------------------------------------------
# Per-key writers — load current state, update one key, write back
# ---------------------------------------------------------------------------

def _lock_for(profile_dir: Path) -> threading.Lock:
    resolved = profile_dir.resolve()
    with _profile_locks_guard:
        lock = _profile_locks.get(resolved)
        if lock is None:
            lock = threading.Lock()
            _profile_locks[resolved] = lock
        return lock


def _update_key(profile_dir: Path, key: str, value) -> None:
    with _lock_for(profile_dir):
        state = read_profile_state(profile_dir)
        state[key] = value
        write_profile_state(profile_dir, state)


def write_collapsed_seps(profile_dir: Path, value: set[str]) -> None:
    _update_key(profile_dir, "collapsed_seps", sorted(value))


def write_separator_locks(profile_dir: Path, value: dict) -> None:
    _update_key(profile_dir, "separator_locks", value)


def write_separator_colors(profile_dir: Path, value: dict) -> None:
    _update_key(profile_dir, "separator_colors", value)


def write_separator_deploy_paths(profile_dir: Path, value: dict) -> None:
    _update_key(profile_dir, "separator_deploy_paths", value)


def write_root_folder_state(profile_dir: Path, enabled: bool) -> None:
    _update_key(profile_dir, "root_folder_state", {"enabled": enabled})


def write_mod_strip_prefixes(profile_dir: Path, value: dict[str, list[str]]) -> None:
    _update_key(profile_dir, "mod_strip_prefixes", value)


def write_plugin_locks(profile_dir: Path, value: dict) -> None:
    _update_key(profile_dir, "plugin_locks", value)


def write_disabled_plugins(profile_dir: Path, value: dict[str, list[str]]) -> None:
    """Persist disabled_plugins to profile_state.json. Values are stored sorted per mod."""
    normalized = {k: sorted(v) for k, v in value.items() if v}
    if normalized:
        _update_key(profile_dir, "disabled_plugins", normalized)
    else:
        state = read_profile_state(profile_dir)
        state.pop("disabled_plugins", None)
        write_profile_state(profile_dir, state)


def write_excluded_mod_files(profile_dir: Path, value: dict[str, list[str]]) -> None:
    """Persist excluded_mod_files to profile_state.json. Values are stored sorted per mod."""
    normalized = {k: sorted(v) for k, v in value.items() if v}
    if normalized:
        _update_key(profile_dir, "excluded_mod_files", normalized)
    else:
        state = read_profile_state(profile_dir)
        state.pop("excluded_mod_files", None)
        write_profile_state(profile_dir, state)


def write_profile_settings(profile_dir: Path, value: dict) -> None:
    """Replace the entire profile_settings object. Pass {} to remove the key."""
    if value:
        _update_key(profile_dir, "profile_settings", dict(value))
    else:
        state = read_profile_state(profile_dir)
        state.pop("profile_settings", None)
        write_profile_state(profile_dir, state)


def merge_profile_settings(profile_dir: Path, updates: dict) -> None:
    """Shallow-merge *updates* into profile_settings (read from disk, then write)."""
    cur = read_profile_settings(profile_dir, None)
    for k, v in updates.items():
        if v is None:
            cur.pop(k, None)
        else:
            cur[k] = v
    write_profile_settings(profile_dir, cur)


def write_ignored_missing_requirements(profile_dir: Path, value: set[str]) -> None:
    if value:
        _update_key(profile_dir, "ignored_missing_requirements", sorted(value))
    else:
        state = read_profile_state(profile_dir)
        state.pop("ignored_missing_requirements", None)
        write_profile_state(profile_dir, state)


def read_collection_optional_skipped(profile_dir: Path) -> set[int]:
    """Return the set of file_ids that were skipped (unchecked) in the optional mods panel
    when this profile's collection was last installed. Returns empty set if not saved."""
    raw = _read_key(profile_dir, None, "collection_optional_skipped_fids")
    if isinstance(raw, list):
        return {int(x) for x in raw if isinstance(x, (int, float))}
    return set()


def write_collection_optional_skipped(profile_dir: Path, skipped_fids: set[int]) -> None:
    """Persist the set of skipped optional mod file_ids to profile_state.json."""
    if skipped_fids:
        _update_key(profile_dir, "collection_optional_skipped_fids", sorted(skipped_fids))
    else:
        state = read_profile_state(profile_dir)
        state.pop("collection_optional_skipped_fids", None)
        write_profile_state(profile_dir, state)
