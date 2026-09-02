"""
GUI-neutral tool-prefix discovery + deletion safety (moved out of
gui/prefix_manager_overlay.py so the Qt prefix manager can share it).

Wizard tools (Pandora, BodySlide, xEdit, PGPatcher, ESLifier, Wrye Bash, …)
each run in their own ``prefix_<ProtonName>`` directory created next to the
tool exe (see Utils.executables.launch.get_tool_prefix_env). Those exes live either
under a game's mod-staging folders (Pandora ships as a mod) or in the per-game
``Applications/`` folder. VRAMr/Bendr/ParallaxR instead use shared
``wine_prefixes/<tool>`` directories, and "Use shared prefix" wizard runs use
``wine_prefixes/shared_<Proton>``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from Utils.config_paths import get_profiles_dir, get_wine_prefixes_dir

# Shared wine_prefixes/<tool> dirs are plain WINEPREFIX folders (not prefix_*).
WINE_PREFIX_TOOLS = {
    "vramr": "VRAMr",
    "bendr": "Bendr",
    "parallaxr": "ParallaxR",
}


def fmt_size(n_bytes: int) -> str:
    if n_bytes <= 0:
        return "-"
    for unit, threshold in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if n_bytes >= threshold:
            return f"{n_bytes / threshold:.1f} {unit}"
    return f"{n_bytes} B"


def get_dir_size(path: Path) -> int:
    # os.walk(followlinks=False) so the Wine prefix's dosdevices symlinks
    # (e.g. z: -> /) never send us crawling the whole host filesystem.
    if not path.is_dir():
        return 0
    total = 0
    try:
        for dirpath, _dirnames, files in os.walk(path):
            for f in files:
                fp = os.path.join(dirpath, f)
                try:
                    st = os.lstat(fp)
                    if not os.path.islink(fp):
                        total += st.st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


@dataclass
class PrefixEntry:
    """One discovered tool prefix."""

    key: str          # unique id (the absolute path string)
    path: Path        # the prefix_* / wine_prefixes/<tool> directory itself
    tool: str         # tool/application name (folder owning the prefix)
    game: str         # owning game (or "Shared" for wine_prefixes tools)
    location: str     # short context: "Applications", "Staging", profile name, …
    proton: str       # Proton/Wine version label ("" when unknown)


def classify_location(rel_parts: tuple[str, ...]) -> str:
    """Human label for where a prefix lives, given path parts below the game dir.

    rel_parts excludes the leading game-name segment and the trailing
    ``prefix_*`` + tool-folder segments.
    """
    if not rel_parts:
        return "Staging"
    head = rel_parts[0]
    if head == "Applications":
        return "Applications"
    if head == "mods":
        return "Staging (mods)"
    if head == "overwrite":
        return "Overwrite"
    if head == "profiles" and len(rel_parts) >= 2:
        return f"Profile: {rel_parts[1]}"
    return head


def scan_root_for_prefixes(root: Path, game: str) -> list[PrefixEntry]:
    """Find every ``prefix_*`` directory under *root* (a single game's tree)."""
    out: list[PrefixEntry] = []
    if not root.is_dir():
        return out
    # os.walk so we can prune: never descend into a discovered prefix dir
    # (they hold a full Wine prefix tree - searching inside is pointless).
    for dirpath, dirnames, _files in os.walk(root):
        found = [d for d in dirnames if d.startswith("prefix_")]
        for d in found:
            p = Path(dirpath) / d
            tool_dir = p.parent
            proton = d[len("prefix_"):]
            try:
                rel = tool_dir.relative_to(root).parts  # e.g. ("Applications", "SSEEdit")
            except ValueError:
                rel = (tool_dir.name,)
            tool = rel[-1] if rel else tool_dir.name
            location = classify_location(rel[:-1])
            out.append(PrefixEntry(
                key=str(p),
                path=p,
                tool=tool,
                game=game,
                location=location,
                proton=proton,
            ))
        # Prune found prefix dirs so os.walk doesn't recurse into them.
        if found:
            dirnames[:] = [d for d in dirnames if not d.startswith("prefix_")]
    return out


def enumerate_prefixes(games_by_name=None) -> list[PrefixEntry]:
    """Discover every tool prefix across all games, profiles and shared dirs.

    *games_by_name* - optional ``{name: BaseGame}`` mapping used to scan
    custom staging roots living outside Profiles/ (each GUI passes its own
    game registry).
    """
    seen: set[str] = set()
    out: list[PrefixEntry] = []

    def _add(entry: PrefixEntry) -> None:
        if entry.key in seen:
            return
        seen.add(entry.key)
        out.append(entry)

    # 1) Every game folder under Profiles/ (covers staging, Applications and
    #    per-profile mods for all games and profiles, default layout).
    profiles_root = get_profiles_dir()
    if profiles_root.is_dir():
        try:
            game_dirs = [d for d in profiles_root.iterdir() if d.is_dir()]
        except OSError:
            game_dirs = []
        for game_dir in game_dirs:
            for entry in scan_root_for_prefixes(game_dir, game_dir.name):
                _add(entry)

    # 2) Custom staging paths (live outside Profiles/).
    for name, game in list((games_by_name or {}).items()):
        try:
            root = game.get_profile_root()
        except Exception:
            continue
        if root is None or not root.is_dir():
            continue
        # Skip if already covered by the Profiles scan above.
        try:
            if profiles_root in root.parents or root == profiles_root:
                continue
        except Exception:
            pass
        for entry in scan_root_for_prefixes(root, name):
            _add(entry)

    # 3) Shared wine_prefixes/<tool> dirs (VRAMr / Bendr / ParallaxR) and the
    #    shared_<Proton> wizard-tool prefixes (one per Proton version, reused by
    #    every wizard tool that opts into "Use shared prefix").
    wine_root = get_wine_prefixes_dir()
    if wine_root.is_dir():
        for sub, label in WINE_PREFIX_TOOLS.items():
            d = wine_root / sub
            if d.is_dir():
                _add(PrefixEntry(
                    key=str(d),
                    path=d,
                    tool=label,
                    game="Shared",
                    location="wine_prefixes",
                    proton="",
                ))
        try:
            shared_dirs = [
                d for d in wine_root.iterdir()
                if d.is_dir() and d.name.startswith("shared_")
            ]
        except OSError:
            shared_dirs = []
        for d in shared_dirs:
            _add(PrefixEntry(
                key=str(d),
                path=d,
                tool="Wizard Tools (shared)",
                game="Shared",
                location="wine_prefixes",
                proton=d.name[len("shared_"):],
            ))

        # Isolated wizard prefixes relocated into wine_prefixes/ because their
        # exe lives somewhere a prefix shouldn't go (e.g. CreationKit.exe in the
        # game root → creationkit_<Proton>/).
        try:
            isolated_dirs = [
                d for d in wine_root.iterdir()
                if d.is_dir() and d.name.startswith("creationkit_")
            ]
        except OSError:
            isolated_dirs = []
        for d in isolated_dirs:
            _add(PrefixEntry(
                key=str(d),
                path=d,
                tool="Creation Kit",
                game="Skyrim Special Edition",
                location="wine_prefixes",
                proton=d.name[len("creationkit_"):],
            ))

    out.sort(key=lambda e: (e.game.lower(), e.tool.lower(), e.proton.lower()))
    return out


def is_deletable_prefix(path: Path) -> bool:
    """True for prefix_* dirs, the shared_<Proton> / creationkit_<Proton> wizard
    prefixes, or a known shared wine_prefixes/<tool> dir."""
    if path.name.startswith("prefix_"):
        return True
    try:
        return (
            path.parent == get_wine_prefixes_dir()
            and (
                path.name in WINE_PREFIX_TOOLS
                or path.name.startswith("shared_")
                or path.name.startswith("creationkit_")
            )
        )
    except Exception:
        return False
