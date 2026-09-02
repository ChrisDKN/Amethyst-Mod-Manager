"""
GUI-neutral support for the FNV BSA Decompressor wizard.

The FNV BSA Decompressor (nexusmods.com/newvegas/mods/65854) ships as a .mpi
package that the same native Linux MPI installer used by the TTW wizard can
run (Fallout 3 is not needed). The generic MPI-package helpers (archive
auto-detect, .mpi extraction, packages dir) live in Utils.bethesda.ttw; this module
binds them to the decompressor's keywords and holds its registration.
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
    "extract_mpi_from_archive", "find_decompressor_archive",
    "find_extracted_mpi", "decompressor_mod_dir", "register_output",
]

NEXUS_URL = "https://www.nexusmods.com/newvegas/mods/65854?tab=files"
OUTPUT_NAME = "FNV BSA Decompressor"
ARCHIVE_KEYWORDS = ["bsa", "decompressor"]

# Pinned main file for the hands-free fetch (premium direct download /
# download-folder watch) - see Utils.downloads.mpi.
NEXUS_GAME_DOMAIN = "newvegas"
NEXUS_MOD_ID = 65854
NEXUS_FILE_ID = 1000136741


def _noop(_msg: str) -> None:
    pass


def find_decompressor_archive() -> "Path | None":
    """Newest archive matching the BSA-Decompressor keywords across all
    configured download locations, or None."""
    return find_mpi_archive(ARCHIVE_KEYWORDS)


def find_extracted_mpi(game: "BaseGame") -> "Path | None":
    """A previously-extracted decompressor .mpi in the packages dir, or None."""
    return _find_extracted_mpi(game, ARCHIVE_KEYWORDS)


def decompressor_mod_dir(game: "BaseGame") -> "Path | None":
    """Path to the already-built decompressor mod in staging, or None (only
    when it actually contains a .bsa, so a stray empty folder doesn't trip
    the already-installed page)."""
    try:
        staging = game.get_effective_mod_staging_path()
    except Exception:
        staging = None
    if staging is None:
        return None
    mod_dir = staging / OUTPUT_NAME
    try:
        if any(mod_dir.glob("*.bsa")):
            return mod_dir
    except OSError:
        pass
    return None


def register_output(game: "BaseGame",
                    log_fn: Callable[[str], None] = _noop) -> None:
    """Register the installer's Data/-rooted output as the decompressor mod
    (normal Data-relative mod, not rootFolder) and index it."""
    from Utils.mods.install_as_mod import index_installed_mod, register_as_mod_neutral
    register_as_mod_neutral(
        game, OUTPUT_NAME, archive=None, log_fn=log_fn, root_folder=False)
    index_installed_mod(game, OUTPUT_NAME, log_fn=log_fn)
