"""
wine/dll_config.py
Shared helpers for per-game Wine DLL override storage and deployment.

Storage format (~/.config/AmethystModManager/games/<game>/wine_dll_overrides.json):
{
  "overrides": {"winhttp": "native,builtin", ...}
}
"""

from __future__ import annotations

import json
from pathlib import Path

from Utils.app_log import safe_log as _safe_log
from Utils.atomic_write import write_atomic_text
from Utils.config_paths import get_game_config_dir
from Utils.wine.registry import HIVE_USER, normalize_pfx, read_values


# ---------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------

def _overrides_path(game_name: str) -> Path:
    return get_game_config_dir(game_name) / "wine_dll_overrides.json"


def _load_raw(game_name: str) -> dict:
    p = _overrides_path(game_name)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {}


def load_wine_dll_overrides(game_name: str) -> dict[str, str]:
    """Load stored Wine DLL overrides, returning {} on error."""
    data = _load_raw(game_name)
    # Support old flat format ({dll: mode}) and new nested format
    raw = data.get("overrides", data) if "overrides" in data else data
    if isinstance(raw, dict):
        return _merge_overrides({str(k): str(v) for k, v in raw.items()
                                 if k and not str(k).startswith("_")})
    return {}


def load_removed_wine_dll_overrides(game_name: str) -> set[str]:
    raw = _load_raw(game_name).get("removed", [])
    if not isinstance(raw, list):
        return set()
    return {dll.lower() for dll in raw if isinstance(dll, str)}


def save_wine_dll_overrides(
    game_name: str, overrides: dict[str, str], *, removed: set[str] | None = None,
) -> None:
    """Persist Wine DLL overrides to config."""
    p = _overrides_path(game_name)
    overrides = _merge_overrides(overrides)
    if removed is None:
        removed = load_removed_wine_dll_overrides(game_name)
    data = {"overrides": overrides,
            "removed": sorted({dll.lower() for dll in removed} - overrides.keys())}
    write_atomic_text(p, json.dumps(data, indent=2))


def read_prefix_wine_dll_overrides(prefix_path: Path | None) -> dict[str, str]:
    if prefix_path is None:
        return {}
    values = read_values(normalize_pfx(prefix_path), r"Software\Wine\DllOverrides",
                         hive=HIVE_USER) or {}
    return _merge_overrides({dll: mode for dll, mode in values.items() if dll != "@"})


# ---------------------------------------------------------------------------
# Deploy helper
# ---------------------------------------------------------------------------

def discover_adjacent_dll_overrides(
    executable_path: Path | None,
    log_fn=None,
) -> dict[str, str]:
    """Return native-first overrides for DLLs beside the game executable."""
    if executable_path is None or not executable_path.is_file():
        return {}

    _log = _safe_log(log_fn)
    try:
        entries = sorted(
            executable_path.parent.iterdir(),
            key=lambda path: (path.name.casefold(), path.name),
        )
    except OSError as exc:
        _log(f"Warning: could not scan for game DLLs beside "
             f"{executable_path}: {exc}")
        return {}

    overrides: dict[str, str] = {}
    for path in entries:
        try:
            is_dll = path.is_file() and path.suffix.casefold() == ".dll"
        except OSError:
            continue
        if is_dll and path.stem:
            overrides[path.stem.lower()] = "native,builtin"
    return overrides


def _merge_overrides(*sources: dict[str, str]) -> dict[str, str]:
    merged: dict[str, str] = {}
    for source in sources:
        for dll, mode in source.items():
            compact = mode.lower().replace(" ", "")
            normalized = {"disabled": "", "n": "native", "b": "builtin",
                          "n,b": "native,builtin", "b,n": "builtin,native"}.get(compact)
            if normalized is None:
                known = ("native", "builtin", "native,builtin", "builtin,native", "")
                normalized = compact if compact in known else mode
            merged[dll.lower()] = normalized
    return merged


def deploy_game_wine_dll_overrides(
    game_name: str,
    prefix_path: Path,
    handler_overrides: dict[str, str],
    log_fn=None,
    *,
    discovered_overrides: dict[str, str] | None = None,
) -> None:
    """Merge automatic, handler, and stored overrides and apply to the prefix.

    Called automatically by the shared deploy pipeline after all game-root
    files have landed.  It:
      1. Merges discovered and user-stored overrides with handler overrides.
      2. Excludes overrides removed in the editor.
      3. Applies the full merged set to the Proton prefix.
    """
    _log = _safe_log(log_fn)

    # Classic Lutris prefixes lack the steamuser account handler paths
    # assume; make sure the compat symlink exists before touching the prefix.
    try:
        from Utils.launchers.lutris import is_lutris_prefix, ensure_steamuser_compat
        if is_lutris_prefix(prefix_path):
            ensure_steamuser_compat(prefix_path)
    except Exception:
        pass

    stored = load_wine_dll_overrides(game_name)
    removed = load_removed_wine_dll_overrides(game_name) - stored.keys()
    # Explicit handler and user choices take precedence over discovery.
    to_apply = _merge_overrides(
        discovered_overrides or {}, handler_overrides, stored)
    to_apply = {dll: mode for dll, mode in to_apply.items() if dll not in removed}
    new_defaults = {dll: mode for dll, mode in _merge_overrides(handler_overrides).items()
                    if dll not in stored and dll not in removed}
    if new_defaults:
        save_wine_dll_overrides(game_name, {**stored, **new_defaults}, removed=removed)

    if not to_apply and not removed:
        return

    _log("Applying Wine DLL overrides to Proton prefix ...")
    from Utils.deployment import apply_wine_dll_overrides, remove_wine_dll_overrides
    if removed and not remove_wine_dll_overrides(prefix_path, removed, log_fn=_log):
        return
    apply_wine_dll_overrides(prefix_path, to_apply, log_fn=_log)
