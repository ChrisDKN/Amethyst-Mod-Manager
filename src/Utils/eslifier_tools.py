"""
GUI-neutral core of the ESLifier wizard.

Moved out of wizards/eslifier.py (which imports customtkinter) so the Qt
wizard view can share it: the settings.json writer (MO2 mode, Wine paths),
the hardlinked prefix-free staging mirror ESLifier scans (its os.walk +
relpath crashes on Wine-prefix ``\\.\\com1`` dosdevices symlinks left inside
tool-as-mod folders), and the filtered modlist copy.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from Games.base_game import BaseGame

GITHUB_API_URL = "https://api.github.com/repos/MaskPlague/ESLifier/releases/latest"
EXE_NAME = "ESLifier.exe"
APP_DIR = "ESLifier"
OUTPUT_NAME = "ESLifier Output"


def _noop(_msg: str) -> None:
    pass


def find_eslifier_exe(game: "BaseGame") -> Path | None:
    from Utils.xedit_tools import tool_exe_path
    return tool_exe_path(game, EXE_NAME, APP_DIR)


def write_settings(game: "BaseGame", exe: Path, pfx: Path, profile: str,
                   log_fn: Callable[[str], None] = _noop) -> "Path | None":
    """Write/merge ESLifier_Data/settings.json next to the exe and return the
    scan-mirror path (None when scanning staging directly).

    All paths are stored as Wine (Z:\\) paths because ESLifier walks and
    opens them from inside the Proton prefix. Existing user-tweaked keys are
    preserved; only the path/mode keys we manage are overwritten.
    """
    from Utils.wine_paths import to_wine_path

    game.set_active_profile_dir(
        game.get_profile_root() / "profiles" / profile
    )
    # Reload so this profile's game/prefix path overrides apply.
    game.load_paths()

    staging = game.get_effective_mod_staging_path()
    overwrite = game.get_effective_overwrite_path()
    profile_dir = game.get_profile_root() / "profiles" / profile
    plugins_txt = profile_dir / "plugins.txt"
    modlist_txt = profile_dir / "modlist.txt"

    overwrite.mkdir(parents=True, exist_ok=True)

    settings_dir = exe.parent / "ESLifier_Data"
    settings_file = settings_dir / "settings.json"
    settings_dir.mkdir(parents=True, exist_ok=True)

    # ESLifier walks every enabled mod's folder with os.walk + os.path.relpath.
    # Tool wizards that run a tool installed as a mod (Pandora, BodySlide, …)
    # leave an isolated Wine prefix (prefix_<ProtonName>/) inside that mod's
    # staging folder. Those prefixes contain dosdevices/com1..6 symlinks which
    # Wine reports as being on mount '\\.\com1' - relpath then crashes ESLifier
    # with "path is on mount '\\.\com1', start on mount 'Z:'".
    #
    # Build a hardlinked mirror of the staging folder that omits every
    # prefix_*/ directory, and point ESLifier's scan path at the mirror so it
    # can never descend into a Wine prefix. Output still goes to the real
    # staging folder so the "ESLifier Output" mod lands in the mod list.
    scan_root = build_mods_mirror(staging, profile, settings_dir, log_fn)
    scan_mirror = scan_root if scan_root != staging else None

    # Belt and braces: also hand ESLifier a modlist copy with prefix mods
    # removed (cheap, and keeps its enabled set in sync with the mirror).
    modlist_for_eslifier = write_filtered_modlist(
        modlist_txt, staging, settings_dir / "modlist.txt", log_fn
    )

    existing: dict = {}
    if settings_file.is_file():
        try:
            existing = json.loads(settings_file.read_text(encoding="utf-8"))
            if not isinstance(existing, dict):
                existing = {}
        except (OSError, ValueError):
            existing = {}

    existing.update({
        "mo2_mode": True,
        # In MO2 mode "skyrim_folder_path" is actually the MO2 mods folder.
        # Point it at the prefix-free mirror so ESLifier never walks into a
        # Wine prefix; output still goes to the real staging folder.
        "skyrim_folder_path":   to_wine_path(scan_root, pfx),
        "output_folder_path":   to_wine_path(staging, pfx),
        "output_folder_name":   existing.get("output_folder_name") or OUTPUT_NAME,
        "overwrite_path":       to_wine_path(overwrite, pfx),
        "plugins_txt_path":     to_wine_path(plugins_txt, pfx),
        "mo2_modlist_txt_path": to_wine_path(modlist_for_eslifier, pfx),
    })

    settings_file.write_text(
        json.dumps(existing, ensure_ascii=False, indent=4),
        encoding="utf-8",
    )
    log_fn(f"wrote settings → {settings_file}")
    log_fn(f"  scan folder:  {scan_root}")
    log_fn(f"  output:       {staging}")
    log_fn(f"  overwrite:    {overwrite}")
    log_fn(f"  plugins.txt:  {plugins_txt}")
    log_fn(f"  modlist.txt:  {modlist_for_eslifier}")
    if not plugins_txt.is_file():
        log_fn(f"  WARN: plugins.txt not found at {plugins_txt}")
    return scan_mirror


def build_mods_mirror(staging: Path, profile: str, settings_dir: Path,
                      log_fn: Callable[[str], None] = _noop) -> Path:
    """Build a hardlinked mirror of *staging* that omits every ``prefix_*``
    directory, and return the mirror root.

    Mirroring with hardlinks is cheap (no data copied). Falls back to
    returning *staging* unchanged if the mirror can't be built.

    The mirror lives inside the ESLifier app dir
    (``<app>/ESLifier_Data/scan_<profile>/``). Hardlinks need the mirror on
    the same filesystem as *staging*; the Applications folder is a sibling of
    ``mods/`` under the profile root, so in practice they always match, and
    :func:`mirror_tree` falls back to a symlink on failure. Rebuilt from
    scratch each run so it always reflects the current load order.
    """
    safe_profile = "".join(c if c.isalnum() or c in "-_" else "_" for c in profile)
    mirror = settings_dir / f"scan_{safe_profile}"

    try:
        if mirror.exists():
            shutil.rmtree(mirror)
        mirror.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log_fn(f"could not prepare mods mirror ({exc}); scanning staging directly.")
        return staging

    skipped: list[str] = []
    try:
        for entry in os.scandir(staging):
            if not entry.is_dir(follow_symlinks=False):
                continue
            if entry.name == mirror.name:
                continue
            src_mod = Path(entry.path)
            dst_mod = mirror / entry.name
            mirror_tree(src_mod, dst_mod, skipped)
    except OSError as exc:
        log_fn(f"error building mods mirror ({exc}); scanning staging directly.")
        shutil.rmtree(mirror, ignore_errors=True)
        return staging

    if skipped:
        log_fn(f"omitted {len(skipped)} Wine prefix folder(s) from the scan "
               "mirror: " + ", ".join(skipped))
    log_fn(f"built scan mirror at {mirror}")
    return mirror


def mirror_tree(src: Path, dst: Path, skipped: list[str]) -> None:
    """Recursively mirror files under *src* into *dst*, skipping any
    ``prefix_*`` directory (appending its path to *skipped*).

    Each file is hardlinked; on failure it falls back to a symlink. Both
    avoid copying data (the mods folder can be many GB). If neither works the
    ``OSError`` propagates so the caller can abandon the mirror and scan the
    real staging folder directly instead of copying."""
    dst.mkdir(parents=True, exist_ok=True)
    try:
        entries = list(os.scandir(src))
    except OSError:
        return
    for entry in entries:
        if entry.is_dir(follow_symlinks=False):
            if entry.name.startswith("prefix_"):
                skipped.append(str(Path(entry.path)))
                continue
            mirror_tree(Path(entry.path), dst / entry.name, skipped)
        elif entry.is_file(follow_symlinks=False):
            target = dst / entry.name
            # Prefer a hardlink (cheapest, shares inode). If that fails
            # (cross-device, link count, …), fall back to an absolute symlink
            # to the real file - still no data copied. A file symlink can't
            # lead Wine's os.walk into a prefix_* dir because those are
            # pruned at the directory level above, so this stays safe against
            # the com1 crash.
            try:
                os.link(entry.path, target)
            except OSError:
                os.symlink(entry.path, target)
        elif entry.is_symlink():
            # Preserve symlinks (e.g. deployed loose files) verbatim.
            try:
                os.symlink(os.readlink(entry.path), dst / entry.name)
            except OSError:
                pass


def cleanup_scan_mirror(mirror: "Path | None",
                        log_fn: Callable[[str], None] = _noop) -> None:
    """Remove the hardlinked scan mirror built for this run, if any."""
    if not mirror:
        return
    try:
        shutil.rmtree(mirror, ignore_errors=True)
        log_fn(f"removed scan mirror {mirror}")
    except OSError:
        pass


def write_filtered_modlist(modlist_txt: Path, staging: Path, dest: Path,
                           log_fn: Callable[[str], None] = _noop) -> Path:
    """Write a copy of *modlist.txt* with enabled mods that contain a Wine
    prefix (``prefix_*/``) removed, and return *dest*.

    Returns the original ``modlist_txt`` unchanged if it can't be read.
    Lines are otherwise preserved verbatim so ESLifier sees the same load
    order, minus the mods that would crash its os.walk.
    """
    try:
        lines = modlist_txt.read_text(encoding="utf-8").splitlines(keepends=True)
    except OSError as exc:
        log_fn(f"could not read modlist.txt ({exc}); using original.")
        return modlist_txt

    def _has_prefix_dir(mod_name: str) -> bool:
        mod_dir = staging / mod_name
        try:
            return any(
                e.is_dir() and e.name.startswith("prefix_")
                for e in mod_dir.iterdir()
            )
        except OSError:
            return False

    kept: list[str] = []
    removed: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(("+", "*")) and not stripped.endswith("_separator"):
            mod_name = stripped[1:].strip()
            if mod_name and _has_prefix_dir(mod_name):
                removed.append(mod_name)
                continue
        kept.append(line)

    try:
        dest.write_text("".join(kept), encoding="utf-8")
    except OSError as exc:
        log_fn(f"could not write filtered modlist ({exc}); using original.")
        return modlist_txt

    if removed:
        log_fn(f"excluded {len(removed)} mod(s) with a Wine prefix from the "
               "scan: " + ", ".join(removed))
    return dest
