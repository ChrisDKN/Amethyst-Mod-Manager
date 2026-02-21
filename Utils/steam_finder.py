"""
steam_finder.py
Utilities for locating Steam game installations across all configured library paths.
No UI, no game-specific knowledge.
"""

from __future__ import annotations

import re
from pathlib import Path

# ---------------------------------------------------------------------------
# Known Steam base directories for different install methods
# ---------------------------------------------------------------------------
_HOME = Path.home()

_STEAM_CANDIDATES: list[Path] = [
    _HOME / ".local" / "share" / "Steam",                                          # Standard
    _HOME / ".var" / "app" / "com.valvesoftware.Steam" / ".local" / "share" / "Steam",  # Flatpak
    _HOME / "snap" / "steam" / "common" / ".local" / "share" / "Steam",            # Snap
    _HOME / ".steam" / "steam",                                                     # Symlink fallback
]

_VDF_FILENAME = "libraryfolders.vdf"
_COMMON_SUBDIR = Path("steamapps") / "common"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def find_steam_libraries() -> list[Path]:
    """
    Parse libraryfolders.vdf from all known Steam install locations.
    Returns a deduplicated list of existing steamapps/common/ directories.
    """
    seen: set[Path] = set()
    libraries: list[Path] = []

    for steam_root in _STEAM_CANDIDATES:
        vdf_path = steam_root / "steamapps" / _VDF_FILENAME
        if vdf_path.is_file():
            for common in parse_vdf_libraries(vdf_path):
                resolved = common.resolve()
                if resolved not in seen:
                    seen.add(resolved)
                    libraries.append(common)

    return libraries


def parse_vdf_libraries(vdf_path: Path) -> list[Path]:
    """
    Parse a libraryfolders.vdf file and return all steamapps/common paths
    that currently exist on disk.

    The VDF format contains lines like:
        "path"    "/home/deck/.local/share/Steam"
    We extract every "path" value and append steamapps/common to each.
    The Steam root containing the VDF is always included as the first entry.
    """
    libraries: list[Path] = []
    pattern = re.compile(r'"path"\s+"([^"]+)"')

    try:
        text = vdf_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return libraries

    for match in pattern.finditer(text):
        raw = match.group(1)
        common = Path(raw) / "steamapps" / "common"
        if common.is_dir():
            libraries.append(common)

    return libraries


def find_prefix(steam_id: str) -> Path | None:
    """
    Locate the Steam compatibility prefix directory for a given App ID.

    Steam stores per-game Proton prefixes under:
        <steam_root>/steamapps/compatdata/<steam_id>/pfx/

    Searches every known Steam root candidate and returns the first pfx/
    directory that exists on disk, or None if not found.

    Args:
        steam_id: The Steam App ID as a string, e.g. '377160' for Fallout 4.
    """
    if not steam_id:
        return None

    for steam_root in _STEAM_CANDIDATES:
        pfx = steam_root / "steamapps" / "compatdata" / steam_id / "pfx"
        if pfx.is_dir():
            return pfx

    return None


def find_proton_for_game(steam_id: str) -> Path | None:
    """
    Find the Proton launcher script assigned to a Steam game.

    Reads CompatToolMapping from Steam's config files to determine which Proton
    version the game uses, then locates the 'proton' script in steamapps/common/.

    Steam rewrites config.vdf atomically, so the live file may temporarily lack
    CompatToolMapping — we also check .bak and .tmp variants of the file.

    Returns the path to the 'proton' script, or None if the game's assigned
    Proton cannot be found (never falls back to an arbitrary version).

    The returned path can be used to run a Windows exe via:
        STEAM_COMPAT_DATA_PATH=<pfx_parent>
        STEAM_COMPAT_CLIENT_INSTALL_PATH=<steam_root>
        python3 <proton_script> run <exe_path>
    """
    import re as _re
    import glob as _glob

    _COMPAT_TOOL_NAMES: dict[str, str] = {
        "proton_experimental": "Proton - Experimental",
        "proton_hotfix":       "Proton Hotfix",
        "proton_10":           "Proton 10.0",
        "proton_9":            "Proton 9.0 (Beta)",
        "proton_8":            "Proton 8.0",
        "proton_7":            "Proton 7.0",
    }

    _ID_PATTERN = _re.compile(
        r'"' + _re.escape(steam_id) + r'"\s*\{[^}]*?"name"\s*"([^"]+)"',
        _re.DOTALL,
    )

    if not steam_id:
        return None

    for steam_root in _STEAM_CANDIDATES:
        config_dir = steam_root / "config"
        if not config_dir.is_dir():
            continue

        # Collect all config.vdf variants: live, .bak, and any .tmp files.
        # Steam writes atomically so the live file may be mid-swap.
        candidates_vdf: list[Path] = []
        for pattern in ("config.vdf", "config.vdf.bak", "config.vdf.*.tmp"):
            candidates_vdf.extend(
                Path(p) for p in _glob.glob(str(config_dir / pattern))
            )

        tool_name: str | None = None
        for vdf_path in candidates_vdf:
            try:
                text = vdf_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            # Only search inside the CompatToolMapping block
            compat_idx = text.find("CompatToolMapping")
            if compat_idx < 0:
                continue
            m = _ID_PATTERN.search(text, compat_idx)
            if m:
                tool_name = m.group(1)
                break

        if tool_name is None:
            continue

        # Map internal short names to steamapps/common directory names
        dir_name = _COMPAT_TOOL_NAMES.get(tool_name, tool_name)

        # Search steamapps/common/ and compatibilitytools.d/ (GE-Proton, etc.)
        search_dirs = [
            steam_root / "steamapps" / "common",
            steam_root / "compatibilitytools.d",
        ]

        for search_dir in search_dirs:
            # Exact match first
            candidate = search_dir / dir_name / "proton"
            if candidate.is_file():
                return candidate

            # Case-insensitive match (handles minor name variations)
            if search_dir.is_dir():
                dir_lower = dir_name.lower()
                for entry in search_dir.iterdir():
                    if entry.name.lower() == dir_lower:
                        p = entry / "proton"
                        if p.is_file():
                            return p

    # --- Fallback: read compatdata/<steam_id>/config_info ----------------
    # When Steam uses the default Proton for a game it may not write an
    # entry to CompatToolMapping.  The compatdata directory, however,
    # stores a config_info file whose lines include the Proton path used
    # to create the prefix (e.g. ".../common/Proton 10.0/files/...").
    # We extract the Proton directory name from that path.
    for steam_root in _STEAM_CANDIDATES:
        config_info = (steam_root / "steamapps" / "compatdata"
                       / steam_id / "config_info")
        if not config_info.is_file():
            continue
        try:
            lines = config_info.read_text(encoding="utf-8",
                                          errors="replace").splitlines()
        except OSError:
            continue
        # Check for Proton paths in both steamapps/common/ and
        # compatibilitytools.d/ (GE-Proton, custom builds, etc.)
        _PROTON_PATH_MARKERS = [
            "/steamapps/common/",
            "/compatibilitytools.d/",
        ]
        for line in lines:
            for marker in _PROTON_PATH_MARKERS:
                if marker not in line:
                    continue
                idx = line.find(marker)
                after = line[idx + len(marker):]
                proton_dir_name = after.split("/")[0]
                if not proton_dir_name.lower().startswith(("proton", "ge-proton")):
                    continue
                # Reconstruct the parent directory from the marker
                parent_dir = Path(line[:idx + len(marker)].rstrip("/"))
                candidate = parent_dir / proton_dir_name / "proton"
                if candidate.is_file():
                    return candidate

    return None


def find_game_in_libraries(libraries: list[Path], exe_name: str) -> Path | None:
    """
    Search each library's steamapps/common/* subfolder for exe_name.
    Checks one level deep: <library>/<GameFolder>/<exe_name>
    Returns the game root directory (the <GameFolder>) or None if not found.

    The search is case-insensitive on the exe name to handle Linux/Proton layouts.
    """
    exe_lower = exe_name.lower()

    for common in libraries:
        try:
            for game_dir in common.iterdir():
                if not game_dir.is_dir():
                    continue
                for entry in game_dir.iterdir():
                    if entry.name.lower() == exe_lower and entry.is_file():
                        return game_dir
        except PermissionError:
            continue

    return None
