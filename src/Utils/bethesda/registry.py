"""Register Bethesda game install paths in a Proton prefix.

Bethesda tools locate the game by reading ``Bethesda Softworks\\<Game>\\
Installed Path``. 32-bit tools (xEdit, LOOT, Synthesis, Wrye Bash) read it
under ``HKLM\\Software\\Wow6432Node\\...``; 64-bit tools (BodySlide x64,
Outfit Studio x64) read the plain ``HKLM\\Software\\...`` view. Wine does
not mirror writes between the two views, so we write both.

Steam itself writes the 64-bit key when the user launches the game through
Steam - but users who install the game and immediately use a mod-manager
wizard never trigger that, leaving the key absent. Per-exe prefixes
(xEdit/Synthesis launched in their own compat folder) never have it at
all. Registering ourselves covers both cases.
"""

from __future__ import annotations

import subprocess
from Utils.launchers.steam import proton_run_command
from pathlib import Path
from typing import Callable


def _posix_to_wine_path(p: Path) -> str:
    s = str(p).replace("/", "\\")
    if not s.endswith("\\"):
        s += "\\"
    return "Z:" + s


def _marker_path(prefix_dir: Path, registry_game_name: str) -> Path:
    # Marker suffix is bumped (.v2) when dual-view writing was added; old v1
    # markers indicated a Wow6432Node-only write and would otherwise suppress
    # the now-needed 64-bit key write that BodySlide / Outfit Studio rely on.
    safe = registry_game_name.replace(" ", "_").replace("\\", "_").replace("/", "_")
    return prefix_dir / ".bethesda_registry" / f"{safe}.v2.done"


def register_bethesda_game_path(
    prefix_dir: Path,
    proton_script: Path,
    env: dict[str, str],
    game_path: Path,
    registry_game_name: str,
    log_fn: Callable[[str], None] | None = None,
) -> bool:
    """Write the game's install path to the Bethesda Softworks registry key.

    *prefix_dir* is the STEAM_COMPAT_DATA_PATH directory (the parent of pfx/).
    *proton_script* is the Proton entrypoint (run via ``python3 <script> run``).
    *env* must already contain STEAM_COMPAT_DATA_PATH / STEAM_COMPAT_CLIENT_INSTALL_PATH.

    Idempotent - a marker file under the prefix records the registered path
    and skips the write while it is unchanged. A different game path (e.g. a
    profile pinned to another install) re-registers, since the tool prefix is
    shared across profiles. Returns True on success or if already done.
    """
    def _log(msg: str) -> None:
        if log_fn is not None:
            try:
                log_fn(msg)
            except Exception:
                pass

    if not game_path or not Path(game_path).is_dir():
        _log(f"Bethesda registry: game path not available, skipping ({registry_game_name}).")
        return False

    wine_value = _posix_to_wine_path(Path(game_path))

    marker = _marker_path(prefix_dir, registry_game_name)
    try:
        if marker.is_file() and marker.read_text(encoding="utf-8").strip() == wine_value:
            return True
    except OSError:
        pass
    keys = [
        r"HKLM\Software\Bethesda Softworks" + "\\" + registry_game_name,
        r"HKLM\Software\Wow6432Node\Bethesda Softworks" + "\\" + registry_game_name,
    ]
    _log(f"Bethesda registry: registering {registry_game_name} → {wine_value}")
    # runinprefix: no steam.exe shim, so the write doesn't flash the game as
    # "Running" in Steam. Only safe once the prefix has been initialised -
    # runinprefix skips Proton's prefix setup, and the Run-EXE override path
    # calls us right after mkdir on a brand-new prefix. There, fall back to
    # "run" (its env carries no SteamAppId, so nothing shows in Steam anyway).
    verb = ("runinprefix"
            if (Path(prefix_dir) / "pfx" / "user.reg").is_file() else "run")
    all_ok = True
    for key in keys:
        cmd = proton_run_command(
            proton_script, verb,
            "reg", "add", key,
            "/v", "Installed Path",
            "/t", "REG_SZ",
            "/d", wine_value,
            "/f",
            env=env,
        )
        try:
            result = subprocess.run(
                cmd, env=env,
                capture_output=True, text=True, timeout=60,
            )
        except Exception as exc:
            _log(f"Bethesda registry: reg add failed for {key}: {exc}")
            all_ok = False
            continue
        if result.returncode != 0:
            stderr = (result.stderr or result.stdout or "").strip()[:200]
            _log(f"Bethesda registry: reg add exited {result.returncode} for {key}: {stderr}")
            all_ok = False

    if not all_ok:
        return False

    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(wine_value + "\n", encoding="utf-8")
    except OSError:
        pass
    return True


def maybe_register_for_game(
    prefix_dir: Path,
    proton_script: Path,
    env: dict[str, str],
    game,
    log_fn: Callable[[str], None] | None = None,
) -> bool:
    """Register the game's install path if *game* is a Bethesda title.

    No-op (returns True) for games that don't expose ``synthesis_registry_name``.
    """
    registry_name = getattr(game, "synthesis_registry_name", None)
    if not registry_name:
        return True
    game_path = game.get_game_path() if hasattr(game, "get_game_path") else None
    if game_path is None:
        return False
    return register_bethesda_game_path(
        prefix_dir=prefix_dir,
        proton_script=proton_script,
        env=env,
        game_path=Path(game_path),
        registry_game_name=registry_name,
        log_fn=log_fn,
    )
