"""GUI-neutral core of the Fallout 3 / New Vegas ESM fixes wizards."""

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
    "ESMFixesConfig", "FNV_CONFIG", "FO3_CONFIG", "get_config",
    "NEXUS_URL", "OUTPUT_NAME", "ARCHIVE_KEYWORDS", "packages_dir",
    "extract_mpi_from_archive", "find_esm_fixes_archive",
    "find_extracted_mpi", "esm_fixes_mod_dir", "register_output",
]


@dataclass(frozen=True)
class ESMFixesConfig:
    output_name: str
    archive_keywords: tuple[str, ...]
    nexus_url: str
    nexus_game_domain: str
    nexus_mod_id: int
    nexus_file_id: int
    game_arg: str
    primary_master: str
    warn_4gb_patch: bool = False


FNV_CONFIG = ESMFixesConfig(
    output_name="Ultimate Edition ESM Fixes Remastered",
    archive_keywords=("esm", "fixes"),
    nexus_url="https://www.nexusmods.com/newvegas/mods/92289?tab=files",
    nexus_game_domain="newvegas",
    nexus_mod_id=92289,
    nexus_file_id=1000176515,
    game_arg="--fnv",
    primary_master="FalloutNV.esm",
    warn_4gb_patch=True,
)

FO3_CONFIG = ESMFixesConfig(
    output_name="Unofficial Fallout 3 ESM Patcher",
    archive_keywords=("unofficial", "fallout", "3", "esm", "patcher"),
    nexus_url="https://www.nexusmods.com/fallout3/mods/25717?tab=files",
    nexus_game_domain="fallout3",
    nexus_mod_id=25717,
    nexus_file_id=1000030170,
    game_arg="--fo3",
    primary_master="Fallout3.esm",
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


def get_config(game: "BaseGame") -> ESMFixesConfig:
    if game.game_id in {"Fallout3", "Fallout3GOTY"}:
        return FO3_CONFIG
    return FNV_CONFIG


def find_esm_fixes_archive(
        config: ESMFixesConfig = FNV_CONFIG) -> "Path | None":
    """Newest archive matching the ESM-Fixes keywords across all configured
    download locations, or None."""
    return find_mpi_archive(list(config.archive_keywords))


def find_extracted_mpi(
        game: "BaseGame",
        config: ESMFixesConfig = FNV_CONFIG) -> "Path | None":
    """A previously-extracted ESM-Fixes .mpi in the packages dir, or None."""
    return _find_extracted_mpi(game, list(config.archive_keywords))


def esm_fixes_mod_dir(
        game: "BaseGame",
        config: ESMFixesConfig = FNV_CONFIG) -> "Path | None":
    """Path to the already-built ESM-Fixes mod in staging, or None (only
    when it actually contains the key patched master, so a stray empty
    folder doesn't trip the already-installed page)."""
    try:
        staging = game.get_effective_mod_staging_path()
    except Exception:
        staging = None
    if staging is None:
        return None
    mod_dir = staging / config.output_name
    if (mod_dir / config.primary_master).is_file():
        return mod_dir
    return None


def register_output(game: "BaseGame",
                    log_fn: Callable[[str], None] = _noop, *,
                    config: ESMFixesConfig = FNV_CONFIG) -> None:
    """Register the installer's Data/-rooted output as the ESM-Fixes mod
    (normal Data-relative mod, not rootFolder) and index it."""
    from Utils.mods.install_as_mod import index_installed_mod, register_as_mod_neutral
    register_as_mod_neutral(
        game, config.output_name, archive=None, log_fn=log_fn,
        root_folder=False)
    index_installed_mod(game, config.output_name, log_fn=log_fn)
