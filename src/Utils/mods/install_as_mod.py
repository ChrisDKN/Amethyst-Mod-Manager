"""Shared helpers for wizards that can install their payload as a managed mod
(staging folder + modlist entry + rootFolder=true flag).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from Games.base_game import BaseGame


def derive_mod_name(archive: Path, fallback: str) -> str:
    """Derive a mod folder name from the archive filename, stripping the
    extension (including double extensions like ``.tar.gz``).  Falls back to
    *fallback* when the resulting stem is empty.
    """
    stem = archive.name
    for ext in (".tar.gz", ".tar.bz2", ".tar.xz"):
        if stem.lower().endswith(ext):
            stem = stem[: -len(ext)]
            break
    else:
        stem = Path(stem).stem
    return stem.strip() or fallback


def register_as_mod_neutral(
    game: "BaseGame",
    mod_name: str,
    archive: "Path | None" = None,
    *,
    modlist_path: "Path | None" = None,
    log_fn: Callable[[str], None],
    root_folder: bool = True,
) -> Path:
    """GUI-neutral core of :func:`register_as_mod`.

    Writes meta.ini with rootFolder=*root_folder* and prepends the mod to
    *modlist_path* (defaults to the ACTIVE profile's ``modlist.txt``).
    Does NO UI refresh - callers that have a panel/app refresh it themselves.

    *archive* is optional - pass ``None`` for payloads built directly in the
    staging folder, leaving ``installation_file`` blank. Returns the staging
    mod directory. Call from a worker thread.
    """
    from Nexus.nexus_meta import NexusModMeta, write_meta
    from Utils.mods.modlist import prepend_mod

    staging = game.get_effective_mod_staging_path()
    if staging is None:
        raise RuntimeError("Mod staging path is not configured.")

    mod_dir = staging / mod_name
    mod_dir.mkdir(parents=True, exist_ok=True)

    meta = NexusModMeta(
        mod_name=mod_name,
        installation_file=archive.name if archive is not None else "",
        root_folder=root_folder,
    )
    write_meta(mod_dir / "meta.ini", meta)

    if modlist_path is None:
        # Register into the ACTIVE profile's modlist. Hardcoding profiles/default
        # here put wizard installs in the WRONG modlist whenever another profile
        # was active: the mod then reached the active modlist only via the
        # refresh's sync_modlist_with_mods_folder - as a DISABLED entry - and a
        # disabled root-flagged mod is skipped by collect_root_flagged_mods, so
        # the index rescan stripped its Data/ prefix (SKSE wizard → Scripts/
        # deployed to the game root once the user enabled it).
        prof_dir = getattr(game, "_active_profile_dir", None)
        if prof_dir is not None:
            modlist_path = Path(prof_dir) / "modlist.txt"
        else:
            modlist_path = game.get_profile_root() / "profiles" / "default" / "modlist.txt"

    prepend_mod(modlist_path, mod_name, enabled=True)
    log_fn(f"Wizard: added '{mod_name}' to modlist with rootFolder={str(root_folder).lower()}.")

    return mod_dir


def register_as_mod(
    game: "BaseGame",
    mod_name: str,
    archive: "Path | None" = None,
    *,
    parent_widget,
    log_fn: Callable[[str], None],
    root_folder: bool = True,
) -> Path:
    """Tk variant: :func:`register_as_mod_neutral` plus a modlist-panel refresh
    reached through *parent_widget*.

    Returns the staging mod directory so callers can drop files into it.
    Must be called from the worker thread; UI refresh is scheduled via .after().
    """
    mod_panel = None
    try:
        toplevel = parent_widget.winfo_toplevel()
        mod_panel = getattr(toplevel, "_mod_panel", None)
    except Exception:
        mod_panel = None

    modlist_path: Path | None = None
    if mod_panel is not None:
        modlist_path = getattr(mod_panel, "_modlist_path", None)

    mod_dir = register_as_mod_neutral(
        game, mod_name, archive,
        modlist_path=modlist_path, log_fn=log_fn, root_folder=root_folder,
    )

    if mod_panel is not None:
        try:
            mod_panel.after(0, mod_panel.reload_after_install)
        except Exception as exc:
            log_fn(f"Wizard: could not trigger mod panel refresh: {exc}")

    return mod_dir


def index_installed_mod(
    game: "BaseGame",
    mod_name: str,
    *,
    log_fn: Callable[[str], None],
) -> None:
    """Publish one newly installed mod's complete manifest to filegraph."""
    try:
        from Utils.filegraph.service import FileGraphService
        profile_dir = getattr(game, "_active_profile_dir", None)
        if profile_dir is None:
            profile_dir = game.get_profile_root() / "profiles" / "default"
        library = FileGraphService.open_library(
            game, profile_dir, log_fn=log_fn)
        session = library.open_profile(profile_dir)
        library.replace_mod_manifest(session.adapter.build_manifest(mod_name))
        log_fn(f"Wizard: catalogued '{mod_name}' for deployment.")
    except Exception as exc:
        log_fn(f"Wizard: could not catalogue '{mod_name}': {exc}")
