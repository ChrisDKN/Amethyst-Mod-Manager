"""Path helpers for the ACMOS Road Generator wizard."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from Games.base_game import BaseGame


EXE_NAME = "ACMOS Road Generator.exe"
APP_DIR = "ACMOS Road Generator"
OUTPUT_DIR = "ACMOS_Output"


def applications_root(game: "BaseGame") -> Path:
    return Path(game.get_mod_staging_path()).parent / "Applications"


def find_acmos_exe(game: "BaseGame") -> Path | None:
    root = applications_root(game)
    canonical = root / APP_DIR / EXE_NAME
    if canonical.is_file():
        return canonical
    try:
        candidates = [
            entry / EXE_NAME
            for entry in root.iterdir()
            if entry.is_dir()
            and entry.name.casefold().startswith(APP_DIR.casefold())
            and (entry / EXE_NAME).is_file()
        ]
    except OSError:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime,
               default=None)


def profile_mod_names(game: "BaseGame", profile_name: str) -> list[str]:
    active = getattr(game, "_active_profile_dir", None)
    if (active is not None
            and (not profile_name or Path(active).name == profile_name)):
        profile_dir = Path(active)
    else:
        profile_dir = (Path(game.get_profile_root()) / "profiles"
                       / (profile_name or "default"))

    modlist_path = profile_dir / "modlist.txt"
    if not modlist_path.is_file():
        return []

    from Utils.modlist import read_modlist
    return [entry.name for entry in read_modlist(modlist_path)
            if not entry.is_separator]


def contains_terrain_lod(mod_path: Path) -> bool:
    current = Path(mod_path)
    for component in ("textures", "terrain"):
        try:
            current = next(
                child for child in current.iterdir()
                if child.is_dir() and child.name.casefold() == component)
        except (OSError, StopIteration):
            return False
    return True


def cli_path_args(lod_path: Path, output_path: Path,
                  compat_data: Path) -> list[str]:
    from Utils.wine_paths import to_wine_path

    prefix = Path(compat_data) / "pfx"
    if not (prefix / "dosdevices").exists():
        prefix = Path(compat_data)
    return [
        f"-l:{to_wine_path(lod_path, prefix)}",
        f"-o:{to_wine_path(output_path, prefix)}",
    ]
