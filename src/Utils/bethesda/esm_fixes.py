"""
GUI-neutral core of the Ultimate Edition ESM Fixes wizard.

Ultimate Edition ESM Fixes Remastered (nexusmods.com/newvegas/mods/92289)
ships as a .mpi package that the same native Linux MPI installer used by the
TTW wizard can run: it xdelta-patches the six vanilla masters (FalloutNV.esm
+ DLC esms) with community bugfixes. The generic MPI-package helpers live in
Utils.bethesda.ttw; this module binds them to the ESM-Fixes keywords and holds its
registration.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Callable

from Utils.bethesda.ttw import (
    extract_mpi_from_archive, find_extracted_mpi as _find_extracted_mpi,
    find_mpi_archive, packages_dir,
)

if TYPE_CHECKING:
    from Games.base_game import BaseGame

__all__ = [
    "NEXUS_URL", "OUTPUT_NAME", "ARCHIVE_KEYWORDS", "packages_dir",
    "extract_mpi_from_archive", "find_esm_fixes_archive",
    "find_extracted_mpi", "esm_fixes_mod_dir", "register_output",
]

NEXUS_URL = "https://www.nexusmods.com/newvegas/mods/92289?tab=files"
OUTPUT_NAME = "Ultimate Edition ESM Fixes Remastered"
ARCHIVE_KEYWORDS = ["esm", "fixes"]

# Pinned main file for the hands-free fetch (premium direct download /
# download-folder watch) - see Utils.downloads.mpi.
NEXUS_GAME_DOMAIN = "newvegas"
NEXUS_MOD_ID = 92289
NEXUS_FILE_ID = 1000176515


def _noop(_msg: str) -> None:
    pass


def find_esm_fixes_archive() -> "Path | None":
    """Newest archive matching the ESM-Fixes keywords across all configured
    download locations, or None."""
    return find_mpi_archive(ARCHIVE_KEYWORDS)


def find_extracted_mpi(game: "BaseGame") -> "Path | None":
    """A previously-extracted ESM-Fixes .mpi in the packages dir, or None."""
    return _find_extracted_mpi(game, ARCHIVE_KEYWORDS)


def esm_fixes_mod_dir(game: "BaseGame") -> "Path | None":
    """Path to the already-built ESM-Fixes mod in staging, or None (only
    when it actually contains the patched FalloutNV.esm, so a stray empty
    folder doesn't trip the already-installed page)."""
    try:
        staging = game.get_effective_mod_staging_path()
    except Exception:
        staging = None
    if staging is None:
        return None
    mod_dir = staging / OUTPUT_NAME
    if (mod_dir / "FalloutNV.esm").is_file():
        return mod_dir
    return None


def register_output(game: "BaseGame",
                    log_fn: Callable[[str], None] = _noop) -> None:
    """Register the installer's Data/-rooted output as the ESM-Fixes mod
    (normal Data-relative mod, not rootFolder) and index it."""
    from Utils.mods.install_as_mod import index_installed_mod, register_as_mod_neutral
    register_as_mod_neutral(
        game, OUTPUT_NAME, archive=None, log_fn=log_fn, root_folder=False)
    index_installed_mod(game, OUTPUT_NAME, log_fn=log_fn)
