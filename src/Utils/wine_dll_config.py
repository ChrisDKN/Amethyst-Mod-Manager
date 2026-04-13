"""
wine_dll_config.py
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
from Utils.config_paths import get_game_config_dir


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
        return {str(k): str(v) for k, v in raw.items() if k and not k.startswith("_")}
    return {}


def save_wine_dll_overrides(game_name: str, overrides: dict[str, str]) -> None:
    """Persist Wine DLL overrides to config."""
    p = _overrides_path(game_name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"overrides": overrides}, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Deploy helper
# ---------------------------------------------------------------------------

def deploy_game_wine_dll_overrides(
    game_name: str,
    prefix_path: Path,
    handler_overrides: dict[str, str],
    log_fn=None,
) -> None:
    """Merge handler overrides with stored config and apply to the prefix.

    Called automatically after every game.deploy() by the deploy orchestration
    in top_bar, cli, and plugin_panel.  It:
      1. Merges user-stored overrides with handler overrides.
      2. Persists any new handler DLLs back to storage so the panel
         reflects the current state.
      3. Applies the full merged set to the Proton prefix.
    """
    _log = _safe_log(log_fn)

    stored = load_wine_dll_overrides(game_name)
    # Handler overrides are always present; user overrides sit on top
    to_apply: dict[str, str] = {**handler_overrides, **stored}
    # Handler DLLs not in stored yet should be persisted
    if handler_overrides:
        for dll, mode in handler_overrides.items():
            stored.setdefault(dll, mode)
        save_wine_dll_overrides(game_name, stored)

    if not to_apply:
        return

    _log("Applying Wine DLL overrides to Proton prefix ...")
    from Utils.deploy import apply_wine_dll_overrides
    apply_wine_dll_overrides(prefix_path, to_apply, log_fn=_log)
