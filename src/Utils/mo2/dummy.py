"""
Build a synthetic ("dummy") Mod Organizer 2 instance so PGPatcher's
"Conflict Resolution Mod Manager = Mod Organizer 2" mode can attribute each
conflicting loose file back to its owning mod - giving MO2-parity per-mod
conflict sorting without a real MO2 / USVFS install.

How PGPatcher reads an MO2 instance (from PGModManager.cpp):
  * ``ModOrganizer.ini`` in the instance dir is parsed for ``gameName=``,
    ``selected_profile=``, ``gamePath=`` and the ``base_directory`` /
    ``mod_directory`` / ``profiles_directory`` keys.
  * ``profiles/<selected_profile>/modlist.txt`` is walked TOP -> BOTTOM.  Our
    manager already uses MO2's exact on-disk format and ordering, so it is
    copied through (minus Wine-unaddressable trailing-dot/space mod names,
    which would abort PGPatcher's whole run).
  * Each enabled mod's folder must exist under ``mod_directory``.  Rather than
    building a symlink tree (which adds a Wine symlink-traversal failure mode),
    ``mod_directory`` points straight at the real staging mods folder - the
    modlist names are exactly the staging subfolder names.

The instance fabrication itself lives in ``Utils.mo2.stub``; this module adds
the PGPatcher-specific parts (game identity / Steam-vs-GOG detection and the
settings.json ``game.type`` int).

PGPatcher still reads the files it actually patches from the deployed game Data
folder; the dummy only supplies the file -> mod map, so a normal deploy is still
required.  Launch PGPatcher with ``--ignore-mo2vfscheck`` since no USVFS layer
is present.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from Games.base_game import BaseGame

from Utils.mo2.stub import (
    Mo2GameInfo,
    disable_wine_incompatible_names,
    write_mo2_stub,
)

DUMMY_DIRNAME = "amm_mo2_dummy"
_PROFILE_NAME = "Default"

# gameName / game_edition strings PGPatcher recognises (PGModManager.cpp
# getGameTypeFromInstanceDir) keyed by our game_id, with the matching
# settings.json game.type int (BethesdaGame::GameType enum: SE=0, GOG=1, VR=2,
# Enderal=3).  Steam vs GOG for Skyrim SE is resolved at runtime from which
# AppData folder exists in the prefix (see _detect_skyrim_edition).
_GAME_INFO_BY_ID: dict[str, tuple[str, str, int]] = {
    # game_id        : (gameName, game_edition, settings.json game.type)
    "skyrim_se": ("Skyrim Special Edition", "Steam", 0),
    "skyrimvr":  ("Skyrim VR", "Steam", 2),
    "enderal_se": ("Enderal Special Edition", "Steam", 3),
}
# GOG variant of Skyrim SE (same game_id, distinguished by prefix AppData).
_SKYRIM_GOG_INFO = ("Skyrim Special Edition", "GOG", 1)


def get_dummy_instance_dir(applications_dir: Path) -> Path:
    """Location of the dummy MO2 instance, next to PGPatcher.exe."""
    return applications_dir / DUMMY_DIRNAME


def _detect_skyrim_edition(game: "BaseGame", prefix: "Path | None") -> tuple[str, str, int] | None:
    """For Skyrim SE, decide Steam vs GOG from which AppData folder exists in
    the tool prefix.  Returns (gameName, game_edition, game.type) or None if not
    Skyrim SE / can't tell (caller then uses the Steam default).

    Mirrors Bethesda._plugins_txt_targets: GOG and Steam builds use separate
    AppData\\Local folders.  A GOG-via-Heroic user will have the "… GOG" folder.
    If only GOG exists we report GOG; otherwise Steam (the safe default, also
    used when both or neither exist).
    """
    if getattr(game, "game_id", "") != "skyrim_se" or prefix is None:
        return None

    appdata_steam = getattr(game, "_APPDATA_SUBPATH", None)
    appdata_gog = getattr(game, "_APPDATA_SUBPATH_GOG", None)
    if appdata_gog is None:
        return None

    steam_dir = prefix / appdata_steam if appdata_steam else None
    gog_dir = prefix / appdata_gog

    gog_exists = gog_dir.is_dir()
    steam_exists = bool(steam_dir and steam_dir.is_dir())

    if gog_exists and not steam_exists:
        return _SKYRIM_GOG_INFO
    return None  # Steam default (incl. both-exist: PGPatcher can't do both)


def build_mo2_dummy_instance(
    game: "BaseGame",
    applications_dir: Path,
    profile: str,
    *,
    prefix: Path | None = None,
    game_prefix: Path | None = None,
    log_fn: Callable[[str], None] = lambda _msg: None,
) -> tuple[Path, int]:
    """Generate (or refresh) the dummy MO2 instance for *game* / *profile*.

    Returns ``(instance_dir, game_type)`` where ``instance_dir`` is the path to
    pass as ``mo2instancedir`` and ``game_type`` is the settings.json
    ``params.game.type`` int (Skyrim SE=0, GOG=1, VR=2, Enderal=3) - the caller
    must write this so PGPatcher reads the correct game (GOG users on Heroic
    need type 1, not 0).

    ``prefix`` is the prefix whose Z: drive the written paths resolve through
    (the tool prefix).  ``game_prefix`` is the *game's* prefix, inspected for
    Steam-vs-GOG detection (its AppData folders reflect what the user actually
    plays); defaults to ``prefix`` when not given.
    """
    instance_dir = get_dummy_instance_dir(applications_dir)

    # Self-heal: earlier versions built a mods/ symlink tree here; we now point
    # mod_directory straight at staging, so drop the stale symlink dir.
    legacy_mods = instance_dir / "mods"
    if legacy_mods.exists():
        import shutil
        shutil.rmtree(legacy_mods, ignore_errors=True)

    game_id = getattr(game, "game_id", "") or ""
    game_name, game_edition, game_type = _GAME_INFO_BY_ID.get(
        game_id, ("Skyrim Special Edition", "Steam", 0)
    )
    # Skyrim SE: prefer GOG if that's the edition installed in the prefix.
    gog_info = _detect_skyrim_edition(game, game_prefix or prefix)
    if gog_info is not None:
        game_name, game_edition, game_type = gog_info
        log_fn("MO2 dummy: detected GOG edition (game.type=1)")

    write_mo2_stub(
        instance_dir,
        prefix=prefix,
        mod_directory=game.get_effective_mod_staging_path(),
        profile_name=_PROFILE_NAME,
        modlist_src=game.get_profile_root() / "profiles" / profile / "modlist.txt",
        game_info=Mo2GameInfo(game_name, game_edition, game.get_game_path()),
        modlist_transforms=[disable_wine_incompatible_names()],
        log_fn=log_fn,
    )
    return instance_dir, game_type
