"""
GUI-neutral core of the Tale of Two Wastelands installer wizard.

Moved out of wizards/ttw.py (which imports customtkinter) so the Qt wizard
view can share it: installer/mod discovery, vanilla-esm validation, the
restore-to-vanilla step, output registration and requirement seeding.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from Games.base_game import BaseGame

GITHUB_API_URL = (
    "https://api.github.com/repos/SulfurNitride/TTW_Linux_Installer/releases/latest"
)
GITHUB_REPO_URL = "https://github.com/SulfurNitride/TTW_Linux_Installer"
MODPUB_URL = "https://mod.pub/ttw/133/files"
EXE_NAME = "mpi_installer"
APP_DIR = "TTW"
OUTPUT_NAME = "Tale of Two Wastelands"

# Nexus (newvegas) mod ids the TTW setup recommends/requires. Seeded into the
# TTW mod's meta.ini missing_requirements so they surface through the standard
# "missing requirements" flag (red marker → install panel).
TTW_REQUIRED_MOD_IDS = [
    57174, 68714, 82540, 70801, 65906, 77415, 58277, 66927,
    72541, 66537, 66347, 80993, 71973, 84823, 80666, 71336,
]

# Fallout 3 Steam app ids (vanilla + GOTY) used to auto-locate the FO3 install.
_FO3_STEAM_IDS = ("22300", "22370")
_FO3_EXE_NAME = "Fallout3.exe"

# Vanilla master + DLC plugins TTW xdelta-patches (FO3 list is fixed; the
# wizard runs under FNV so there's no FO3 game object to query).
FO3_REQUIRED_ESMS = [
    "Fallout3.esm",
    "Anchorage.esm", "ThePitt.esm", "BrokenSteel.esm",
    "PointLookout.esm", "Zeta.esm",
]


def _noop(_msg: str) -> None:
    pass


def applications_dir(game: "BaseGame") -> Path:
    return game.get_mod_staging_path().parent / "Applications" / APP_DIR


def find_ttw_installer(game: "BaseGame") -> Path | None:
    p = applications_dir(game) / EXE_NAME
    return p if p.is_file() else None


def download_installer(game: "BaseGame",
                       status_fn: Callable[[str], None] = _noop,
                       log_fn: Callable[[str], None] = _noop) -> Path:
    """Download the latest MPI-installer release from GitHub into
    Applications/TTW and return the executable path. Raises on failure.
    Shared by the TTW and BSA-Decompressor wizards (same binary)."""
    import json
    import os
    import shutil
    import tempfile
    import urllib.request
    from Utils.ca_bundle import download_file, get_ssl_context
    from Utils.wizards.archives import extract_archive

    req = urllib.request.Request(
        GITHUB_API_URL,
        headers={"Accept": "application/vnd.github+json",
                 "User-Agent": "ModManager/1.0"})
    with urllib.request.urlopen(req, timeout=15,
                                context=get_ssl_context()) as resp:
        data = json.loads(resp.read().decode())
    tag = data.get("tag_name", "unknown")
    url = None
    for asset in data.get("assets", []):
        name = asset.get("name", "").lower()
        if "linux" in name and name.endswith((".zip", ".tar.gz")):
            url = asset["browser_download_url"]
            break
    if not url:
        raise RuntimeError(
            f"No Linux installer asset found in the latest TTW release ({tag}).")

    log_fn(f"downloading TTW installer {tag} from {url}")
    status_fn(f"Downloading TTW installer {tag}…")
    tmp_dir = Path(tempfile.mkdtemp())
    archive = tmp_dir / Path(url).name
    try:
        download_file(url, archive)
        dest = applications_dir(game)
        dest.mkdir(parents=True, exist_ok=True)
        status_fn("Extracting installer…")
        log_fn(f"extracting {archive.name} → {dest}")
        paths = extract_archive(archive, dest)
        log_fn(f"extracted {len([p for p in paths if p.is_file()])} file(s).")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    exe = dest / EXE_NAME
    if not exe.is_file():
        raise RuntimeError(f"{EXE_NAME} not found after extraction at {dest}.")
    try:
        os.chmod(exe, 0o755)
    except OSError:
        pass
    return exe


def find_fo3_install() -> Path | None:
    """Locate the Fallout 3 install folder via Steam libraries, or None."""
    try:
        from Utils.launchers.steam import find_game_by_steam_id, find_steam_libraries
        libs = find_steam_libraries()
        for sid in _FO3_STEAM_IDS:
            hit = find_game_by_steam_id(libs, sid, _FO3_EXE_NAME)
            if hit is not None:
                return hit
    except Exception:
        pass
    return None


def packages_dir(game: "BaseGame") -> Path:
    """Where extracted .mpi packages are kept (next to the installer)."""
    return applications_dir(game) / "packages"


def find_mpi_archive(keywords: "list[str]") -> "Path | None":
    """Newest archive matching all *keywords* across the configured download
    locations, or None."""
    from Utils.downloads.locations import get_effective_download_locations
    from Utils.wizards.archives import find_archive

    best: "Path | None" = None
    best_mtime = -1.0
    for directory in get_effective_download_locations():
        hit = find_archive(directory, keywords)
        if hit is None:
            continue
        try:
            mtime = hit.stat().st_mtime
        except OSError:
            continue
        if mtime > best_mtime:
            best, best_mtime = hit, mtime
    return best


def extract_mpi_from_archive(archive: Path, dest_dir: Path,
                             log_fn: Callable[[str], None] = _noop) -> Path:
    """Extract *archive* to a temp dir, move the .mpi inside it into
    *dest_dir* and return its path. An already-extracted .mpi of the same
    name is reused. Raises when the archive holds no .mpi."""
    import shutil
    import tempfile
    from Utils.wizards.archives import extract_to_dir

    tmp = Path(tempfile.mkdtemp())
    try:
        log_fn(f"extracting {archive.name}…")
        extract_to_dir(archive, tmp)
        mpis = sorted(tmp.rglob("*.mpi"))
        if not mpis:
            raise RuntimeError(f"No .mpi package found inside {archive.name}.")
        src = mpis[0]
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / src.name
        if dest.is_file() and dest.stat().st_size == src.stat().st_size:
            log_fn(f"reusing already-extracted {dest.name}")
            return dest
        shutil.move(str(src), str(dest))
        log_fn(f"extracted {dest.name} → {dest_dir}")
        return dest
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def find_extracted_mpi(game: "BaseGame",
                       keywords: "list[str]") -> "Path | None":
    """A previously-extracted .mpi matching all *keywords* in the packages
    dir, or None."""
    try:
        hits = sorted(packages_dir(game).glob("*.mpi"))
    except OSError:
        return None
    for p in hits:
        low = p.name.lower()
        if all(kw in low for kw in keywords):
            return p
    return None


def missing_vanilla_esms(game_root: Path, esms: "list[str]") -> list[str]:
    """Return the *esms* not present in ``<game_root>/Data`` (case-insensitive)."""
    data = game_root / "Data"
    try:
        present = {p.name.lower() for p in data.iterdir() if p.is_file()}
    except OSError:
        return list(esms)
    return [e for e in esms if e.lower() not in present]


def fnv_required_esms(game: "BaseGame") -> list[str]:
    """Vanilla master + DLC .esm files TTW patches (from the game's plugin lists)."""
    plugins = list(getattr(game, "vanilla_plugins", []) or []) + \
        list(getattr(game, "vanilla_dlc_plugins", []) or [])
    return [p for p in plugins if p.lower().endswith(".esm")]


def ttw_mod_dir(game: "BaseGame") -> "Path | None":
    """Path to the already-installed TTW mod in staging, or None (only when the
    key merged plugin is present, so a stray empty folder doesn't trip skip)."""
    try:
        staging = game.get_effective_mod_staging_path()
    except Exception:
        staging = None
    if staging is None:
        return None
    mod_dir = staging / OUTPUT_NAME
    if (mod_dir / "TaleOfTwoWastelands.esm").is_file():
        return mod_dir
    return None


def sync_active_profile(game: "BaseGame", profile: str) -> None:
    """Point the game's active profile dir at *profile* so staging/modlist/INI
    paths resolve there (staging can be per-profile)."""
    try:
        game.set_active_profile_dir(
            game.get_profile_root() / "profiles" / profile)
        game.load_paths()
    except Exception:
        pass


def restore_to_vanilla(game: "BaseGame", current_profile: str,
                       log_fn: Callable[[str], None] = _noop) -> "tuple[bool, Path | None]":
    """Restore the game to vanilla, mirroring the top-bar Restore button.

    Returns (success, fnv_game_root).  The game root is re-resolved from the
    last-deployed profile (per-profile paths) and returned so the caller keeps
    the post-restore esm check + installer on the same root.  Always restores
    the active profile to *current_profile* in the finally block.
    """
    if not hasattr(game, "restore"):
        return False, None

    fnv_root: "Path | None" = None
    success = True
    try:
        from Utils.deployment.pipeline import check_paths_mounted
        mount_err = check_paths_mounted(game)
        if mount_err:
            log_fn(f"Restore aborted: {mount_err}")
            return False, None

        last_deployed = game.get_last_deployed_profile()
        if last_deployed:
            game.set_active_profile_dir(
                game.get_profile_root() / "profiles" / last_deployed)
            game.load_paths()
        game_root = game.get_game_path()
        if game_root is not None:
            fnv_root = game_root

        game.restore(log_fn=log_fn)

        from Utils.deployment import restore_root_folder_for_game
        root_folder_dir = game.get_effective_root_folder_path()
        if root_folder_dir.is_dir() and game_root:
            restore_root_folder_for_game(
                game, root_folder_dir=root_folder_dir,
                game_root=game_root, log_fn=log_fn,
            )
    except Exception as exc:
        success = False
        log_fn(f"restore error: {exc}")
    finally:
        try:
            game.set_active_profile_dir(
                game.get_profile_root() / "profiles" / current_profile)
            game.load_paths()
        except Exception:
            pass
        if success:
            try:
                game.clear_deploy_active()
            except Exception:
                pass
    return success, fnv_root


def register_output(game: "BaseGame", dest: Path,
                    log_fn: Callable[[str], None] = _noop) -> None:
    """Register the installer's Data/-rooted output as the TTW mod (normal
    Data-relative mod, not rootFolder) and index it."""
    from Utils.mods.install_as_mod import index_installed_mod, register_as_mod_neutral
    register_as_mod_neutral(
        game, OUTPUT_NAME, archive=None, log_fn=log_fn, root_folder=False)
    index_installed_mod(game, OUTPUT_NAME, log_fn=log_fn)


def seed_required_mods(game: "BaseGame",
                       log_fn: Callable[[str], None] = _noop) -> None:
    """Write the recommended-mod id list into the TTW mod's meta.ini
    ``missing_requirements`` (filtered live against installed mods)."""
    from Nexus.nexus_meta import read_meta, write_meta

    mod_dir = ttw_mod_dir(game)
    if mod_dir is None:
        return
    meta_path = mod_dir / "meta.ini"
    if not meta_path.is_file():
        log_fn("TTW meta.ini not found - skipping requirement seeding.")
        return
    meta = read_meta(meta_path)
    meta.missing_requirements = ";".join(f"{mid}:" for mid in TTW_REQUIRED_MOD_IDS)
    write_meta(meta_path, meta)
    log_fn(f"seeded {len(TTW_REQUIRED_MOD_IDS)} recommended mod(s) into the "
           "TTW requirements list.")
