"""
Fabricate a minimal ("stub") Mod Organizer 2 instance folder for Windows
modding tools that can read a real MO2 install (PGPatcher, ESLifier, ...).

A stub instance is a directory containing:

  * ``ModOrganizer.ini`` - ``[General]`` with the selected profile (plus the
    game identity keys when the tool needs them) and ``[Settings]`` with
    ``base_directory`` / ``mod_directory`` / ``profiles_directory`` /
    ``overwrite_directory``, all as Wine ``Z:`` paths.
  * ``profiles/<profile>/modlist.txt`` - our modlist is already MO2's exact
    on-disk format (top line = highest priority, ``+``/``-``/``*``/``#``/
    ``*_separator`` markers), so it is copied through, optionally reshaped by
    per-tool :data:`ModlistTransform` functions.
  * ``profiles/<profile>/plugins.txt`` - optional copy of the real one.

Each tool keeps its own stub directory and decides what ``mod_directory``
resolves to (real staging for PGPatcher, the prefix-free scan mirror for
ESLifier); this module only owns the common fabrication.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Callable, Iterable, NamedTuple

from Utils.wine_paths import to_wine_path

# Takes (modlist lines, log_fn) and returns the reshaped lines.
ModlistTransform = Callable[[list[str], Callable[[str], None]], list[str]]


def _noop(_msg: str) -> None:
    pass


class Mo2GameInfo(NamedTuple):
    """Game identity keys for tools (PGPatcher) that read them from the ini."""
    game_name: str
    game_edition: str
    game_path: Path | None


def disable_wine_incompatible_names() -> ModlistTransform:
    """Transform that disables (``+`` -> ``-``) enabled mods whose folder name
    ends in ``.`` or `` `` - Windows path normalisation strips trailing dots
    and spaces, so no Wine tool can address those folders (PGPatcher aborts
    its whole run on the failed ``filesystem::exists`` check).
    """
    def _transform(lines: list[str], log_fn: Callable[[str], None]) -> list[str]:
        out: list[str] = []
        skipped: list[str] = []
        for line in lines:
            if line.startswith("+"):
                name = line[1:]
                if name.endswith(".") or name.endswith(" "):
                    out.append("-" + name)
                    skipped.append(name)
                    continue
            out.append(line)
        if skipped:
            log_fn(
                "MO2 stub: disabled "
                + str(len(skipped))
                + " mod(s) with Wine-incompatible names (trailing dot/space): "
                + ", ".join(repr(s) for s in skipped)
            )
        return out
    return _transform


def drop_prefix_mods(staging: Path) -> ModlistTransform:
    """Transform that removes enabled mods whose *staging* folder contains a
    top-level ``prefix_*`` Wine prefix (tool-as-mod wizards leave one next to
    the tool exe) - tools that ``os.walk`` mod folders under Wine crash on
    the prefix's ``dosdevices/com1..6`` symlinks.
    """
    def _has_prefix_dir(mod_name: str) -> bool:
        try:
            return any(
                e.is_dir() and e.name.startswith("prefix_")
                for e in (staging / mod_name).iterdir()
            )
        except OSError:
            return False

    def _transform(lines: list[str], log_fn: Callable[[str], None]) -> list[str]:
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
        if removed:
            log_fn(
                "MO2 stub: excluded "
                + str(len(removed))
                + " mod(s) with a Wine prefix from the modlist: "
                + ", ".join(removed)
            )
        return kept
    return _transform


def write_mo2_stub(
    instance_dir: Path,
    *,
    prefix: Path | None,
    mod_directory: Path,
    profile_name: str,
    modlist_src: Path,
    overwrite_dir: Path | None = None,
    plugins_txt: Path | None = None,
    game_info: Mo2GameInfo | None = None,
    modlist_transforms: Iterable[ModlistTransform] = (),
    log_fn: Callable[[str], None] = _noop,
) -> Path:
    """Generate (or refresh) an MO2 stub instance at *instance_dir*.

    *prefix* is the Wine prefix whose ``Z:`` drive the written paths resolve
    through (the tool's prefix).  *modlist_src* is the profile's real
    modlist.txt; it is run through *modlist_transforms* in order and written
    into ``profiles/<profile_name>/``.  *plugins_txt* (when given and present)
    is copied alongside it.  *game_info* adds the ``[General]`` game identity
    keys PGPatcher-style consumers parse; tools that only read the
    ``[Settings]`` paths ignore them.

    Raises ``RuntimeError`` if *modlist_src* does not exist.  Returns
    *instance_dir*.
    """
    profile_dir = instance_dir / "profiles" / profile_name
    profile_dir.mkdir(parents=True, exist_ok=True)

    if not modlist_src.is_file():
        raise RuntimeError(f"modlist.txt not found: {modlist_src}")

    # --- profiles/<name>/modlist.txt ---
    lines = modlist_src.read_text(encoding="utf-8").splitlines()
    for transform in modlist_transforms:
        lines = transform(lines, log_fn)
    (profile_dir / "modlist.txt").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )

    # --- profiles/<name>/plugins.txt ---
    if plugins_txt is not None:
        if plugins_txt.is_file():
            try:
                shutil.copyfile(plugins_txt, profile_dir / "plugins.txt")
            except OSError as exc:
                log_fn(f"MO2 stub: could not copy plugins.txt ({exc})")
        else:
            log_fn(f"MO2 stub: WARN: plugins.txt not found at {plugins_txt}")

    # --- ModOrganizer.ini ---
    # Tools resolve the instance through Wine, whose path lookup is
    # case-insensitive, but keep exactly one casing on disk: drop any
    # differently-cased leftover (earlier builds wrote "modorganizer.ini").
    ini_path = instance_dir / "ModOrganizer.ini"
    try:
        for entry in instance_dir.iterdir():
            if entry.name.lower() == "modorganizer.ini" and entry.name != ini_path.name:
                entry.unlink()
    except OSError:
        pass

    general = f"selected_profile=@ByteArray({profile_name})\n"
    if game_info is not None:
        game_wine = (
            to_wine_path(game_info.game_path, prefix) if game_info.game_path else ""
        )
        general = (
            f"gameName={game_info.game_name}\n"
            + general
            + f"gamePath=@ByteArray({game_wine})\n"
            + f"game_edition={game_info.game_edition}\n"
        )

    settings = (
        f"base_directory={to_wine_path(instance_dir, prefix)}\n"
        f"mod_directory={to_wine_path(mod_directory, prefix)}\n"
        f"profiles_directory={to_wine_path(instance_dir / 'profiles', prefix)}\n"
    )
    if overwrite_dir is not None:
        settings += f"overwrite_directory={to_wine_path(overwrite_dir, prefix)}\n"

    ini_path.write_text(
        "[General]\n" + general + "\n[Settings]\n" + settings,
        encoding="utf-8",
    )

    log_fn(f"MO2 stub: built instance at {instance_dir} (mods -> {mod_directory})")
    return instance_dir
