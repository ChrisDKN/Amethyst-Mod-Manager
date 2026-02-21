"""
loot_sorter.py
Sort a plugins.txt load order using libloot's masterlist-based sorting.

Usage:
    from LOOT.loot_sorter import sort_plugins, is_available

The sort_plugins() function takes the current plugin entries and game info,
feeds them through libloot's sorting algorithm, and returns the reordered
list with enabled/disabled state preserved.

Game support is driven by the game handler's properties:
  - loot_sort_enabled: bool — whether LOOT sorting applies to this game
  - loot_game_type: str     — libloot GameType attribute name (e.g. 'SkyrimSE')
  - loot_masterlist_url: str — URL to download the masterlist YAML
  - game_id: str            — used to derive the masterlist filename
                              (masterlist_<game_id>.yaml in ~/.config/AmethystModManager/LOOT/data/)
"""

from __future__ import annotations

import shutil
import urllib.request
from pathlib import Path
from dataclasses import dataclass

try:
    import LOOT.loot as loot
    _AVAILABLE = True
except ImportError:
    loot = None
    _AVAILABLE = False

# Bundled masterlists shipped with the application (read-only in AppImage)
_BUNDLED_DATA_DIR = Path(__file__).parent / "data"

# User-writable masterlist directory in ~/.config/AmethystModManager/LOOT/data/
from Utils.config_paths import get_loot_data_dir  # noqa: E402
_DATA_DIR = get_loot_data_dir()

PRELUDE_FILE = "masterlist_prelude.yaml"
PRELUDE_URL = "https://raw.githubusercontent.com/loot/prelude/v0.21/prelude.yaml"


def _ensure_masterlist(
    filename: str,
    download_url: str = "",
    log_fn=None,
) -> None:
    """Ensure a masterlist exists in the config dir.

    Resolution order:
      1. Already present in config dir — nothing to do.
      2. Copy from the bundled LOOT/data/ directory.
      3. Download from the provided URL.
    """
    _log = log_fn or (lambda _: None)
    dest = _DATA_DIR / filename
    if dest.is_file():
        return
    # Try bundled copy first
    src = _BUNDLED_DATA_DIR / filename
    if src.is_file():
        shutil.copy2(src, dest)
        _log(f"Copied bundled {filename} to config dir.")
        return
    # Download if a URL was provided
    if download_url:
        _log(f"Downloading {filename}...")
        try:
            urllib.request.urlretrieve(download_url, dest)
            _log(f"Downloaded {filename} successfully.")
        except Exception as exc:
            _log(f"Failed to download {filename}: {exc}")


def _masterlist_filename(game_type_attr: str) -> str:
    """Derive the masterlist filename from the libloot game type attribute name.
    e.g. 'SkyrimSE' → 'masterlist_skyrimse.yaml'
    """
    return f"masterlist_{game_type_attr.lower()}.yaml"


def is_available() -> bool:
    """Return True if libloot is importable and ready to use."""
    return _AVAILABLE


@dataclass
class SortResult:
    """Result of a LOOT sort operation."""
    sorted_names: list[str]
    moved_count: int
    warnings: list[str]


def _find_plugin_paths(
    plugin_names: list[str],
    game_data_dir: Path,
    staging_root: Path | None,
) -> tuple[list[str], list[str]]:
    """
    Locate plugin files on disk, searching the game's Data directory first,
    then falling back to the mod staging folders, then the overwrite folder.

    Returns:
        (found_paths, missing_names)
    """
    found: dict[str, str] = {}  # plugin name → absolute path
    names_lower = {n.lower(): n for n in plugin_names}

    # 1. Check the game's Data directory
    if game_data_dir.is_dir():
        for name in plugin_names:
            full = game_data_dir / name
            if full.is_file():
                found[name] = str(full)

    # 2. For anything still missing, search staging mod folders
    if staging_root and staging_root.is_dir():
        still_missing = [n for n in plugin_names if n not in found]
        if still_missing:
            missing_lower = {n.lower() for n in still_missing}
            for mod_dir in staging_root.iterdir():
                if not mod_dir.is_dir():
                    continue
                for f in mod_dir.iterdir():
                    if f.is_file() and f.name.lower() in missing_lower:
                        # Map back to the original-cased name
                        orig = names_lower.get(f.name.lower())
                        if orig and orig not in found:
                            found[orig] = str(f)

    # 3. For anything still missing, check the overwrite folder
    #    (staging_root's sibling: Profiles/<game>/overwrite/)
    if staging_root:
        overwrite_dir = staging_root.parent / "overwrite"
        if overwrite_dir.is_dir():
            still_missing = [n for n in plugin_names if n not in found]
            if still_missing:
                missing_lower = {n.lower() for n in still_missing}
                for f in overwrite_dir.iterdir():
                    if f.is_file() and f.name.lower() in missing_lower:
                        orig = names_lower.get(f.name.lower())
                        if orig and orig not in found:
                            found[orig] = str(f)

    found_paths = [found[n] for n in plugin_names if n in found]
    missing_names = [n for n in plugin_names if n not in found]
    return found_paths, missing_names


def sort_plugins(
    plugin_names: list[str],
    enabled_set: set[str],
    game_name: str,
    game_path: Path,
    staging_root: Path | None = None,
    log_fn=None,
    game_type_attr: str = "",
    game_id: str = "",
    masterlist_url: str = "",
) -> SortResult:
    """
    Sort plugins using libloot's masterlist rules.

    Args:
        plugin_names: Current plugin names in load order.
        enabled_set: Set of plugin names (case-sensitive) that are enabled.
        game_name: Display name of the game (for log messages).
        game_path: Root install directory of the game.
        staging_root: Path to the mod staging directory (Profiles/<game>/mods/).
        log_fn: Optional callback for status messages.
        game_type_attr: libloot GameType attribute name (e.g. 'SkyrimSE').
                        Obtained from game.loot_game_type.
        game_id: Game ID used to locate the masterlist file
                 (~/.config/AmethystModManager/LOOT/data/masterlist_<game_id>.yaml).
                 Obtained from game.game_id.

    Returns:
        SortResult with the new order, count of moved plugins, and any warnings.

    Raises:
        RuntimeError: If libloot is not available or no masterlist is found.
    """
    _log = log_fn or (lambda _: None)

    if not _AVAILABLE:
        raise RuntimeError(
            "libloot is not available. "
            "Build it with: cd LOOT/libloot/python && maturin develop"
        )

    if not game_type_attr:
        raise RuntimeError(
            f"No LOOT game type configured for '{game_name}'. "
            "Set loot_game_type in the game's Python handler."
        )

    game_type = getattr(loot.GameType, game_type_attr, None)
    if game_type is None:
        raise RuntimeError(
            f"Unknown libloot GameType '{game_type_attr}' for '{game_name}'."
        )

    ml_filename = _masterlist_filename(game_type_attr)
    _ensure_masterlist(ml_filename, download_url=masterlist_url, log_fn=_log)
    _ensure_masterlist(PRELUDE_FILE, download_url=PRELUDE_URL, log_fn=_log)

    masterlist_path = _DATA_DIR / ml_filename
    prelude_path = _DATA_DIR / PRELUDE_FILE

    if not masterlist_path.is_file():
        url_hint = (f"\nDownload from: {masterlist_url}" if masterlist_url
                    else "\nDownload it from the LOOT GitHub repository.")
        raise RuntimeError(
            f"Masterlist not found: {masterlist_path}{url_hint}"
        )

    warnings: list[str] = []

    # Create libloot Game instance
    local_data = str(_DATA_DIR)
    _log("Initializing LOOT...")
    game = loot.Game(game_type, str(game_path), local_data)
    game.load_current_load_order_state()

    # Load masterlist
    db = game.database()
    if prelude_path.is_file():
        db.load_masterlist_with_prelude(str(masterlist_path), str(prelude_path))
        _log("Loaded masterlist with prelude.")
    else:
        db.load_masterlist(str(masterlist_path))
        _log("Loaded masterlist (no prelude found).")
        warnings.append("Prelude file not found — sorting may be less accurate.")

    # Find plugin files on disk — check game Data dir AND staging mods
    data_dir = game_path / "Data"
    plugin_paths, missing = _find_plugin_paths(
        plugin_names, data_dir, staging_root,
    )

    if missing:
        warnings.append(
            f"{len(missing)} plugin(s) not found on disk: "
            f"{', '.join(missing[:5])}"
            + (f" ... and {len(missing)-5} more" if len(missing) > 5 else "")
        )

    if plugin_paths:
        _log(f"Loading {len(plugin_paths)} plugin headers...")
        game.load_plugin_headers(plugin_paths)

    # Only sort plugins that exist on disk — libloot can't sort unknown plugins
    missing_set = set(missing)
    sortable = [n for n in plugin_names if n not in missing_set]
    unsortable = [n for n in plugin_names if n in missing_set]

    _log(f"Sorting {len(sortable)} plugins...")

    try:
        sorted_names = game.sort_plugins(sortable)
    except loot.CyclicInteractionError as e:
        raise RuntimeError(f"LOOT found a cyclic dependency: {e}") from e

    # Append any unsortable plugins (missing from disk) at the end
    if unsortable:
        sorted_names.extend(unsortable)

    # Count how many actually moved
    moved = sum(
        1 for i, name in enumerate(sorted_names)
        if i >= len(plugin_names) or plugin_names[i] != name
    )

    _log(f"Sort complete. {moved} plugin(s) changed position.")
    return SortResult(
        sorted_names=sorted_names,
        moved_count=moved,
        warnings=warnings,
    )
