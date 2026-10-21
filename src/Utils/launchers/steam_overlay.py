"""Steam overlay loading for games launched outside the Steam client."""

from __future__ import annotations

import re
from pathlib import Path


PRELOAD_MARKER = "_AMM_STEAM_OVERLAY_PRELOAD"


def _overlay_libraries(env: dict, proton_script: Path | None) -> list[str]:
    from Utils.launchers.steam import (
        _STEAM_CANDIDATES, find_steam_root_for_proton_script,
    )

    selected = env.get("STEAM_COMPAT_CLIENT_INSTALL_PATH")
    if not selected and proton_script is not None:
        try:
            selected = find_steam_root_for_proton_script(proton_script)
        except (OSError, RuntimeError, ValueError):
            return []
    roots = [Path(selected)] if selected else list(_STEAM_CANDIDATES)
    for root in roots:
        libraries = []
        for directory in ("ubuntu12_32", "ubuntu12_64", "bin32", "bin64"):
            path = root / directory / "gameoverlayrenderer.so"
            try:
                if not path.is_file():
                    continue
                resolved = path.resolve()
            except (OSError, RuntimeError, ValueError):
                continue
            value = str(resolved)
            if value not in libraries and not re.search(r"[\s:]", value):
                libraries.append(value)
        if libraries:
            return libraries
    return []


def apply_steam_overlay(
        env: dict, *, steam_game: bool, proton_script: Path | None = None,
        explicit_preload: bool = False, log_fn=None) -> None:
    """Add discovered overlay libraries without changing the game's identity."""
    log = log_fn or (lambda _message: None)
    mode = str(env.get("AMM_STEAM_OVERLAY", "auto")).strip().lower()
    if mode in ("0", "false", "off", "no"):
        return
    if not steam_game and mode not in ("1", "true", "on", "yes"):
        return
    if env.get("DISABLE_VK_LAYER_VALVE_steam_overlay_1") == "1":
        return
    if env.get("ENABLE_VK_LAYER_VALVE_steam_overlay_1") == "0":
        return
    configured = env.get("_AMM_ENV_KEYS", "").split(",")
    if (explicit_preload or "LD_PRELOAD" in configured) \
            and not env.get("LD_PRELOAD"):
        return

    existing = env.get("LD_PRELOAD", "")
    preloads = re.split(r"[\s:]+", existing.strip()) if existing.strip() else []
    if not any(Path(value).name == "gameoverlayrenderer.so" for value in preloads):
        libraries = _overlay_libraries(env, proton_script)
        if not libraries:
            log("Steam overlay: no usable overlay libraries found; "
                "continuing without automatic loading.")
            return
        preloads.extend(libraries)
        env["LD_PRELOAD"] = ":".join(preloads)
    env[PRELOAD_MARKER] = env["LD_PRELOAD"]
    env.setdefault("ENABLE_VK_LAYER_VALVE_steam_overlay_1", "1")
    log("Steam overlay: loading " + env["LD_PRELOAD"]
        + " (set AMM_STEAM_OVERLAY=0 to disable).")
