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
from Utils.wine.registry import (
    HIVE_USER, normalize_pfx, read_values, values_from_text, wine_state_file_edit,
)


# Other DLLs require an explicit handler/user override, even when beside the exe.
_AUTOMATIC_DLL_OVERRIDES = frozenset({
    "d3d8", "d3d9", "d3d11", "dinput8", "dsound", "dwmapi", "dwrite",
    "dxgi", "version", "winhttp", "wininet", "winmm",
})


def _dll_name(dll: str) -> str:
    return dll.lower().lstrip("*").removesuffix(".dll")


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
    nested = "overrides" in data
    raw = data.get("overrides", data) if nested else data
    if isinstance(raw, dict):
        return _merge_overrides({str(k): str(v) for k, v in raw.items()
                                 if k and (nested or (not str(k).startswith("_")
                                                     and k not in ("removed", "reset")))})
    return {}


def load_removed_wine_dll_overrides(game_name: str) -> set[str]:
    raw = _load_raw(game_name).get("removed", [])
    if not isinstance(raw, list):
        return set()
    return {dll.lower() for dll in raw if isinstance(dll, str)}


def load_reset_wine_dll_overrides(game_name: str) -> set[str]:
    raw = _load_raw(game_name).get("reset", [])
    return {dll.lower() for dll in raw if isinstance(dll, str)} if isinstance(raw, list) else set()


def save_wine_dll_overrides(
    game_name: str, overrides: dict[str, str], *, removed: set[str] | None = None,
    reset: set[str] | None = None,
) -> None:
    """Persist Wine DLL overrides to config."""
    p = _overrides_path(game_name)
    with wine_state_file_edit(p):
        overrides = _merge_overrides(overrides)
        if removed is None:
            removed = load_removed_wine_dll_overrides(game_name)
        if reset is None:
            reset = load_reset_wine_dll_overrides(game_name)
        removed = {dll.lower() for dll in removed} - overrides.keys()
        reset = {dll.lower() for dll in reset} - overrides.keys() - removed
        existing = _load_raw(game_name)
        data = dict(existing) if "overrides" in existing else {
            key: value for key, value in existing.items() if key.startswith("_")}
        data.update(overrides=overrides,
                    removed=sorted(removed))
        if reset:
            data["reset"] = sorted(reset)
        else:
            data.pop("reset", None)
        write_atomic_text(p, json.dumps(data, indent=2))


def read_prefix_wine_dll_overrides(
    prefix_path: Path | None, *, registry_text: str | None = None,
) -> dict[str, str]:
    if prefix_path is None:
        return {}
    values = (values_from_text(registry_text, r"Software\Wine\DllOverrides")
              if registry_text is not None else
              read_values(normalize_pfx(prefix_path), r"Software\Wine\DllOverrides",
                          hive=HIVE_USER)) or {}
    return _merge_overrides({dll: mode for dll, mode in values.items() if dll != "@"})


# ---------------------------------------------------------------------------
# Deploy helper
# ---------------------------------------------------------------------------

def discover_adjacent_dll_overrides(
    executable_path: Path | None,
    log_fn=None,
) -> dict[str, str]:
    """Return native-first overrides for common adjacent mod-loader DLLs."""
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
    skipped: list[str] = []
    for path in entries:
        try:
            is_dll = path.is_file() and path.suffix.casefold() == ".dll"
        except OSError:
            continue
        if is_dll and path.stem:
            dll = path.stem.lower()
            if dll in _AUTOMATIC_DLL_OVERRIDES:
                overrides[dll] = "native,builtin"
            else:
                skipped.append(path.name)
    if skipped:
        names = ", ".join(skipped[:12])
        if len(skipped) > 12:
            names += f", and {len(skipped) - 12} more"
        _log(f"Wine DLL overrides: skipped {len(skipped)} DLL(s) outside "
             f"the automatic mod-loader list: {names}. "
             "Game defaults and saved overrides still apply.")
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
    """Apply explicit overrides and fill missing entries for known mod proxies."""
    _log = _safe_log(log_fn)

    # Classic Lutris prefixes lack the steamuser account handler paths
    # assume; make sure the compat symlink exists before touching the prefix.
    try:
        from Utils.launchers.lutris import is_lutris_prefix, ensure_steamuser_compat
        from Utils.launchers.bottles import is_bottles_prefix
        if is_lutris_prefix(prefix_path) or is_bottles_prefix(prefix_path):
            ensure_steamuser_compat(prefix_path)
    except Exception:
        pass

    with (wine_state_file_edit(_overrides_path(game_name)),
          wine_state_file_edit(normalize_pfx(prefix_path) / HIVE_USER)):
        stored = load_wine_dll_overrides(game_name)
        removed = load_removed_wine_dll_overrides(game_name) - stored.keys()
        reset = load_reset_wine_dll_overrides(game_name) - stored.keys() - removed
        prefix_overrides = read_prefix_wine_dll_overrides(prefix_path)
        existing_names = {_dll_name(dll) for dll in prefix_overrides}
        reset_names = {_dll_name(dll) for dll in reset}
        explicit_names = {
            _dll_name(dll) for dll in (*handler_overrides, *stored, *removed)
        }
        automatic = {
            dll: mode for dll, mode in _merge_overrides(discovered_overrides or {}).items()
            if dll in _AUTOMATIC_DLL_OVERRIDES
            and (dll not in existing_names or dll in reset_names)
            and dll not in explicit_names
        }
        # Older discovery did not record ownership; report these without deleting them.
        review = sorted(
            dll for dll, mode in prefix_overrides.items()
            if (_dll_name(dll).startswith(("xinput", "gameinput"))
                or _dll_name(dll) in {"dinput", "hid", "windows.gaming.input"})
            and _dll_name(dll) not in explicit_names | reset_names
            and mode in ("native", "native,builtin")
        )
        if review:
            _log(f"Wine DLL overrides: existing native input overrides: {', '.join(review)}. "
                 "These may come from older deployments or other tools. If controllers fail, "
                 "review them under Proton > Wine DLL overrides. Removing an entry "
                 "there also prevents deployment from adding it again.")

        to_apply = _merge_overrides(
            automatic, handler_overrides, stored)
        to_apply = {dll: mode for dll, mode in to_apply.items() if dll not in removed}
        new_defaults = {dll: mode for dll, mode in _merge_overrides(handler_overrides).items()
                        if dll not in stored and dll not in removed and dll not in reset}
        if new_defaults:
            save_wine_dll_overrides(game_name, {**stored, **new_defaults}, removed=removed)

        if not to_apply and not removed and not reset:
            return

        _log("Applying Wine DLL overrides to Proton prefix ...")
        from Utils.deployment import apply_wine_dll_overrides, remove_wine_dll_overrides
        if (removed or reset) and not remove_wine_dll_overrides(prefix_path, removed | reset, log_fn=_log):
            return
        apply_wine_dll_overrides(prefix_path, to_apply, log_fn=_log)
