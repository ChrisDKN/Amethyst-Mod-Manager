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


def disabled_basenames(modlist_path, index_path) -> set[str]:
    """Lowercased basenames of every file belonging to a DISABLED mod - lets us
    flag a framework that's installed but toggled off. Port of the Tk
    ``_framework_disabled_basenames``."""
    if not modlist_path or not index_path:
        return set()
    modlist_path = Path(modlist_path)
    index_path = Path(index_path)
    if not modlist_path.is_file() or not index_path.is_file():
        return set()
    try:
        from Utils.modlist import read_modlist
        from Utils.filemap import read_mod_index
        disabled = {e.name for e in read_modlist(modlist_path)
                    if not e.is_separator and not e.enabled}
        if not disabled:
            return set()
        index = read_mod_index(index_path) or {}
    except Exception:
        return set()
    names: set[str] = set()
    for mod_name in disabled:
        entry = index.get(mod_name)
        if not entry:
            continue
        normal, root = entry
        for k in (*normal.keys(), *root.keys()):
            names.add(k.rsplit("/", 1)[-1].lower())
    return names


def _load_staged_keys(filemap_path) -> set[str]:
    """Lowercased deploy-relative paths from filemap.txt + filemap_root.txt."""
    keys: set[str] = set()
    if not filemap_path:
        return keys
    fm_path = Path(filemap_path)
    for fm in (fm_path, fm_path.parent / "filemap_root.txt"):
        if not fm.is_file():
            continue
        try:
            with fm.open(encoding="utf-8") as f:
                for line in f:
                    if "\t" not in line:
                        continue
                    rel = line.split("\t", 1)[0].replace("\\", "/")
                    keys.add(rel.lower())
        except OSError:
            pass
    return keys


def detect_frameworks(game, filemap_path, modlist_path,
                      rf_toggle_enabled: bool = True) -> list[FrameworkStatus]:
    """Return one FrameworkStatus per framework the *game* declares, in order.

    Empty list if *game* is None or declares no frameworks. *rf_toggle_enabled*
    is the modlist's Root_Folder toggle (Root_Folder staging only reaches the
    game root on deploy AND only while that toggle is on)."""
    if game is None:
        return []
    try:
        frameworks = game.frameworks or {}
    except Exception:
        frameworks = {}
    if not frameworks:
        return []

    game_root = None
    try:
        game_root = game.get_game_path() if hasattr(game, "get_game_path") else None
    except Exception:
        game_root = None

    root_folder = None
    try:
        root_folder = game.get_effective_root_folder_path()
    except Exception:
        root_folder = None

    rf_allowed = bool(getattr(game, "root_folder_deploy_enabled", True))
    mods_dir = getattr(game, "mods_dir", "") or ""

    staged_keys = _load_staged_keys(filemap_path)
    index_path = (Path(filemap_path).parent / "modindex.bin") if filemap_path else None
    disabled = disabled_basenames(modlist_path, index_path)

    def state_for_exe(exe: str) -> str:
        exe_path = Path(exe)
        if game_root is not None and file_exists_ci(game_root, exe_path):
            return STATE_INSTALLED

        in_root_staging = (rf_allowed and root_folder is not None
                           and file_exists_ci(root_folder, exe_path))

        if in_root_staging and not rf_toggle_enabled:
            return STATE_NOT_ENABLED
        if in_root_staging or exe_in_staged(exe, staged_keys, mods_dir):
            return STATE_NOT_DEPLOYED
        exe_basename = exe.replace("\\", "/").rsplit("/", 1)[-1].lower()
        if exe_basename and exe_basename in disabled:
            return STATE_NOT_ENABLED
        return STATE_MISSING

    # Any one candidate satisfies the framework - report the best state.
    rank = {STATE_INSTALLED: 3, STATE_NOT_DEPLOYED: 2,
            STATE_NOT_ENABLED: 1, STATE_MISSING: 0}
    out: list[FrameworkStatus] = []
    for label, exes in frameworks.items():
        candidates = framework_exe_candidates(exes)
        state = STATE_MISSING
        for exe in candidates:
            s = state_for_exe(exe)
            if rank[s] > rank[state]:
                state = s
            if state == STATE_INSTALLED:
                break

        if state == STATE_INSTALLED:
            out.append(FrameworkStatus(label, state,
                                       f"✔  {label} Installed"))
        elif state == STATE_NOT_DEPLOYED:
            out.append(FrameworkStatus(
                label, state,
                f"●  {label} present in modlist but not deployed"))
        elif state == STATE_NOT_ENABLED:
            out.append(FrameworkStatus(
                label, state,
                f"●  {label} present in modlist but not enabled"))
        else:
            out.append(FrameworkStatus(label, state,
                                       f"✘  {label} Not Present"))
    return out
