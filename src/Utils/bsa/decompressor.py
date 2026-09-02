"""GUI-neutral support for the Fallout 3 / New Vegas BSA decompressors."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from Utils.bethesda.ttw import (
    extract_mpi_from_archive, find_extracted_mpi as _find_extracted_mpi,
    find_mpi_archive, packages_dir,
)

if TYPE_CHECKING:
    from Games.base_game import BaseGame

__all__ = [
    "BSADecompressorConfig", "FNV_CONFIG", "FO3_CONFIG", "get_config",
    "NEXUS_URL", "OUTPUT_NAME", "ARCHIVE_KEYWORDS", "packages_dir",
    "extract_mpi_from_archive", "find_decompressor_archive",
    "find_extracted_mpi", "normalize_output_files",
    "decompressor_mod_dir", "register_output",
]


@dataclass(frozen=True)
class BSADecompressorConfig:
    output_name: str
    archive_keywords: tuple[str, ...]
    nexus_url: str
    nexus_game_domain: str
    nexus_mod_id: int
    nexus_file_id: int
    game_arg: str
    required_data_files: tuple[str, ...] = ()
    output_file_renames: tuple[tuple[str, str], ...] = ()


FNV_CONFIG = BSADecompressorConfig(
    output_name="FNV BSA Decompressor",
    archive_keywords=("fnv", "bsa", "decompressor"),
    nexus_url="https://www.nexusmods.com/newvegas/mods/65854?tab=files",
    nexus_game_domain="newvegas",
    nexus_mod_id=65854,
    nexus_file_id=1000136741,
    game_arg="--fnv",
)

FO3_CONFIG = BSADecompressorConfig(
    output_name="FO3 BSA Decompressor",
    archive_keywords=("fo3", "bsa", "decompressor"),
    nexus_url="https://www.nexusmods.com/fallout3/mods/25720?tab=files",
    nexus_game_domain="fallout3",
    nexus_mod_id=25720,
    nexus_file_id=1000027599,
    game_arg="--fo3",
    required_data_files=(
        "Fallout - Meshes.bsa",
        "Fallout - Misc.bsa",
        "Fallout - Textures.bsa",
    ),
    output_file_renames=(
        ("New Fallout - Meshes.bsa", "Fallout - Meshes.bsa"),
        ("New Fallout - Misc.bsa", "Fallout - Misc.bsa"),
        ("New Fallout - Textures.bsa", "Fallout - Textures.bsa"),
    ),
)

# Compatibility aliases for callers that expect the New Vegas defaults.
NEXUS_URL = FNV_CONFIG.nexus_url
OUTPUT_NAME = FNV_CONFIG.output_name
ARCHIVE_KEYWORDS = list(FNV_CONFIG.archive_keywords)

# Pinned main file for the hands-free fetch (premium direct download /
# download-folder watch) - see Utils.downloads.mpi.
NEXUS_GAME_DOMAIN = FNV_CONFIG.nexus_game_domain
NEXUS_MOD_ID = FNV_CONFIG.nexus_mod_id
NEXUS_FILE_ID = FNV_CONFIG.nexus_file_id


def _noop(_msg: str) -> None:
    pass


def get_config(game: "BaseGame") -> BSADecompressorConfig:
    if game.game_id in {"Fallout3", "Fallout3GOTY"}:
        return FO3_CONFIG
    return FNV_CONFIG


def find_decompressor_archive(
        config: BSADecompressorConfig = FNV_CONFIG) -> "Path | None":
    """Newest archive matching the BSA-Decompressor keywords across all
    configured download locations, or None."""
    return find_mpi_archive(list(config.archive_keywords))


def find_extracted_mpi(
        game: "BaseGame",
        config: BSADecompressorConfig = FNV_CONFIG) -> "Path | None":
    """A previously-extracted decompressor .mpi in the packages dir, or None."""
    return _find_extracted_mpi(game, list(config.archive_keywords))


def normalize_output_files(
        game: "BaseGame", log_fn: Callable[[str], None] = _noop, *,
        config: BSADecompressorConfig = FNV_CONFIG,
        require_complete: bool = False) -> "Path | None":
    """Apply game-specific output names inside the generated mod."""
    try:
        staging = game.get_effective_mod_staging_path()
    except Exception:
        if require_complete:
            raise
        return None
    if staging is None:
        if require_complete:
            raise RuntimeError("Mod staging path is not configured.")
        return None

    mod_dir = staging / config.output_name
    for generated_name, deployed_name in config.output_file_renames:
        generated = mod_dir / generated_name
        deployed = mod_dir / deployed_name
        if generated.is_file():
            generated.replace(deployed)
            log_fn(f"renamed {generated_name} to {deployed_name}")

    if require_complete and config.output_file_renames:
        missing = [
            deployed_name
            for _, deployed_name in config.output_file_renames
            if not (mod_dir / deployed_name).is_file()
        ]
        if missing:
            raise RuntimeError(
                "Expected BSA output files were not created: "
                + ", ".join(missing))
    return mod_dir


def decompressor_mod_dir(
        game: "BaseGame",
        config: BSADecompressorConfig = FNV_CONFIG) -> "Path | None":
    """Path to a complete decompressor mod in staging, or None."""
    try:
        staging = game.get_effective_mod_staging_path()
    except Exception:
        staging = None
    if staging is None:
        return None
    mod_dir = staging / config.output_name
    try:
        if config.output_file_renames:
            if all((mod_dir / deployed_name).is_file()
                   for _, deployed_name in config.output_file_renames):
                return mod_dir
            return None
        if any(mod_dir.glob("*.bsa")):
            return mod_dir
    except OSError:
        pass
    return None


def register_output(game: "BaseGame",
                    log_fn: Callable[[str], None] = _noop, *,
                    config: BSADecompressorConfig = FNV_CONFIG) -> None:
    """Register the installer's Data/-rooted output as the decompressor mod
    (normal Data-relative mod, not rootFolder) and index it."""
    from Utils.mods.install_as_mod import index_installed_mod, register_as_mod_neutral
    normalize_output_files(
        game, log_fn=log_fn, config=config, require_complete=True)
    register_as_mod_neutral(
        game, config.output_name, archive=None, log_fn=log_fn,
        root_folder=False)
    index_installed_mod(game, config.output_name, log_fn=log_fn)
