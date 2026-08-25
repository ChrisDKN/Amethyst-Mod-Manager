"""Toolkit-neutral modding-framework detection for the Plugins tab banner.

Each game class declares the frameworks it cares about via its ``frameworks``
property → ``{display_name: relative_exe_path}`` (e.g. Skyrim SE →
``{"Script Extender": "skse64_loader.exe"}``). A value may also be a
tuple/list of alternative paths where ANY present file satisfies the
framework (e.g. BepInEx → ``("winhttp.dll", "run_bepinex.sh")`` - the native
Linux build ships the shell script instead of the proxy dll). This module
decides, for each, one of four states by checking where the exe lives:

  installed     - present in the deployed game root            (green)
  not_deployed  - staged in the modlist but not deployed yet   (orange)
  not_enabled   - present only in a disabled mod / RF-off       (blue)
  missing       - not found anywhere                            (red)

Ported from the Tk ``gui/plugin_panel.py`` framework-banner logic so both
front-ends behave identically. GUI-free (no tkinter / PySide6).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

STATE_INSTALLED = "installed"
STATE_NOT_DEPLOYED = "not_deployed"
STATE_NOT_ENABLED = "not_enabled"
STATE_MISSING = "missing"


@dataclass
class FrameworkStatus:
    label: str
    state: str        # one of the STATE_* values
    message: str      # ready-to-show banner text (with ✔/●/✘ prefix)


def framework_exe_candidates(value) -> "tuple[str, ...]":
    """Normalise a ``frameworks`` dict value to a tuple of candidate paths -
    a plain string is one candidate, a tuple/list is taken as alternatives
    where any present file satisfies the framework."""
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (tuple, list)):
        return tuple(v for v in value if isinstance(v, str) and v)
    return ()


def resolve_file_ci(base: Path, rel: Path) -> "Path | None":
    """Case-insensitive file resolution - walk each component of *rel* under
    *base*, matching names case-insensitively (framework files may live in
    differently-cased folders on a case-sensitive filesystem). Returns the
    actual on-disk Path, or None when any component is missing."""
    current = base
    for part in rel.parts:
        try:
            entries = {e.name.lower(): e for e in current.iterdir()}
        except OSError:
            return None
        match = entries.get(part.lower())
        if match is None:
            return None
        current = match
    return current if current.is_file() else None


def file_exists_ci(base: Path, rel: Path) -> bool:
    """Case-insensitive file existence check. Port of the Tk
    ``_file_exists_ci``."""
    return resolve_file_ci(base, rel) is not None


def exe_in_staged(exe: str, staged_keys: set[str], mods_dir: str) -> bool:
    """True if *exe* matches a key in the filemap *staged_keys* (lowercased,
    deploy-relative). Handles the ``mods_dir`` prefix and a basename fallback
    for loose framework files relocated by a routing rule. Port of the Tk
    ``_framework_exe_in_staged``."""
    key = exe.replace("\\", "/").lower().lstrip("/")
    if key in staged_keys:
        return True
    mods_dir = (mods_dir or "").strip("/\\").lower()
    if mods_dir:
        prefix = mods_dir + "/"
        if key.startswith(prefix) and key[len(prefix):] in staged_keys:
            return True
    basename = key.rsplit("/", 1)[-1]
    if basename and any(k.rsplit("/", 1)[-1] == basename for k in staged_keys):
        return True
    return False


def detect_frameworks_snapshot(game, snapshot, modlist_path,
                               rf_toggle_enabled: bool = True
                               ) -> list[FrameworkStatus]:
    """Filegraph-backed framework detection with no legacy map/index reads."""
    if game is None:
        return []
    try:
        frameworks = game.frameworks or {}
    except Exception:
        frameworks = {}
    if not frameworks:
        return []

    try:
        game_root = game.get_game_path()
    except Exception:
        game_root = None
    try:
        root_folder = game.get_effective_root_folder_path()
    except Exception:
        root_folder = None
    rf_allowed = bool(getattr(game, "root_folder_deploy_enabled", True))
    mods_dir = getattr(game, "mods_dir", "") or ""

    staged_keys: set[str] = set()
    for winner in snapshot.framework_winners():
        if winner.legacy_rel:
            staged_keys.add(winner.legacy_rel.replace("\\", "/").lower())
        if winner.destination_display:
            staged_keys.add(
                winner.destination_display.replace("\\", "/").lower())

    disabled_mods: set[str] = set()
    if modlist_path:
        try:
            from Utils.modlist import read_modlist
            disabled_mods = {
                entry.name for entry in read_modlist(Path(modlist_path))
                if not entry.is_separator and not entry.enabled
            }
        except Exception:
            disabled_mods = set()
    disabled = snapshot.framework_basenames(disabled_mods)

    def state_for_exe(exe: str) -> str:
        exe_path = Path(exe)
        if game_root is not None and file_exists_ci(game_root, exe_path):
            return STATE_INSTALLED
        virtual_exists = getattr(game, "vfs_file_exists", None)
        if callable(virtual_exists):
            try:
                if virtual_exists(exe):
                    return STATE_INSTALLED
            except Exception:
                pass
        in_root_staging = (
            rf_allowed and root_folder is not None
            and file_exists_ci(root_folder, exe_path)
        )
        if in_root_staging and not rf_toggle_enabled:
            return STATE_NOT_ENABLED
        if in_root_staging or exe_in_staged(exe, staged_keys, mods_dir):
            return STATE_NOT_DEPLOYED
        basename = exe.replace("\\", "/").rsplit("/", 1)[-1].lower()
        if basename and basename in disabled:
            return STATE_NOT_ENABLED
        return STATE_MISSING

    rank = {
        STATE_INSTALLED: 3,
        STATE_NOT_DEPLOYED: 2,
        STATE_NOT_ENABLED: 1,
        STATE_MISSING: 0,
    }
    result: list[FrameworkStatus] = []
    for label, exes in frameworks.items():
        state = STATE_MISSING
        for exe in framework_exe_candidates(exes):
            candidate_state = state_for_exe(exe)
            if rank[candidate_state] > rank[state]:
                state = candidate_state
            if state == STATE_INSTALLED:
                break
        if state == STATE_INSTALLED:
            message = f"✔  {label} Installed"
        elif state == STATE_NOT_DEPLOYED:
            message = f"●  {label} present in modlist but not deployed"
        elif state == STATE_NOT_ENABLED:
            message = f"●  {label} present in modlist but not enabled"
        else:
            message = f"✘  {label} Not Present"
        result.append(FrameworkStatus(label, state, message))
    return result
