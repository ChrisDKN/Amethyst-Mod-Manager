"""
GUI-neutral code for the external LOOT wizard.

Runs the official Windows LOOT build under Proton. The tool prefix gets the
game's Installed Path in its registry, a copy of the deployed profile's
plugins.txt in its AppData and the game prefix's My Games folder (see
prepare_xedit_prefix), so LOOT finds the game and the load order exactly as
the game would. LOOT's settings.toml is pointed at those folders
explicitly, and AMM's userlist is shared with it. LOOT sorts the plugins.txt
copy in place; read_loot_order() reads it back after LOOT closes and
merge_loot_order() turns it into a full profile order the plugin panel can
apply.
"""

from __future__ import annotations

import json
import shutil
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from Utils.atomic_write import write_atomic_text

if TYPE_CHECKING:
    from Games.base_game import BaseGame


APP_DIR = "LOOT"
# Kept outside APP_DIR so a tool update can wipe the install without losing
# LOOT's settings, masterlists and userlists.
DATA_DIR = "LOOT Data"
EXE_NAME = "LOOT.exe"
GITHUB_API_URL = "https://api.github.com/repos/loot/loot/releases/latest"
ARCHIVE_KEYWORDS = ["win64"]


def _noop(_msg: str) -> None:
    pass


def loot_data_dir(game: "BaseGame") -> Path:
    from Utils.bethesda.xedit import applications_dir
    return applications_dir(game, DATA_DIR)


def remove_loot_install(game: "BaseGame", log_fn: Callable[[str], None] = _noop) -> None:
    """Delete the LOOT program folder before an update.

    The release archive wraps everything in a versioned folder, so extracting
    a new release over an old install would leave the old LOOT.exe on top.
    """
    from Utils.bethesda.xedit import applications_dir
    app = applications_dir(game, APP_DIR)
    if app.is_dir() and not app.is_symlink():
        shutil.rmtree(app)
        log_fn(f"removed old install at {app}")


def loot_game_root(game: "BaseGame") -> Path | None:
    """Install folder LOOT should read: the VFS shadow view when the profile
    is deployed through the VFS, otherwise the real game folder."""
    from Utils.vfs import effective_shadow_root, manifest_path
    if manifest_path(game).is_file():
        return effective_shadow_root(game)
    return game.get_game_path()


def _toml_str(value: str) -> str:
    # JSON string escapes are valid TOML basic strings.
    return json.dumps(value, ensure_ascii=False)


def write_loot_game_settings(game: "BaseGame", pfx: Path, loot_game: str,
                             log_fn: Callable[[str], None] = _noop) -> None:
    """Point LOOT's settings.toml entry for *loot_game* at AMM's paths.

    Sets ``path`` (install folder, or the VFS view) and ``local_path`` (the
    AppData folder holding the plugins.txt copy) so LOOT never has to detect
    the game itself - its Steam/GOG/registry detection misses Heroic and
    other non-Steam installs, and --game-path alone isn't applied on a first
    run. Other settings in the file are left as LOOT wrote them.
    """
    from Utils.wine.paths import to_wine_path
    data = loot_data_dir(game)
    data.mkdir(parents=True, exist_ok=True)
    settings = data / "settings.toml"
    keys = {"local_path": to_wine_path(pfx / _local_subpath(game), pfx)}
    root = loot_game_root(game)
    if root is not None:
        keys["path"] = to_wine_path(root, pfx)
    new_keys = [f"{k} = {_toml_str(v)}" for k, v in keys.items()]

    text = settings.read_text(encoding="utf-8") if settings.is_file() else ""
    games = tomllib.loads(text).get("games", []) if text else []
    index = next((i for i, g in enumerate(games) if g.get("folder") == loot_game), None)
    lines = text.splitlines()
    if index is None:
        lines += ["", "[[games]]", f"folder = {_toml_str(loot_game)}",
                  f"gameId = {_toml_str(loot_game)}", *new_keys]
    else:
        headers = [i for i, line in enumerate(lines) if line.strip() == "[[games]]"]
        start = headers[index]
        end = next((i for i in range(start + 1, len(lines))
                    if lines[i].lstrip().startswith("[")), len(lines))
        drop = ("path", "local_path", "local_folder")
        body = [line for line in lines[start + 1:end]
                if line.split("=", 1)[0].strip() not in drop]
        lines[start + 1:end] = new_keys + body
    out = "\n".join(lines) + "\n"
    tomllib.loads(out)   # never hand LOOT a file it can't parse
    write_atomic_text(settings, out)
    log_fn(f"LOOT settings: {loot_game} path={keys.get('path', '-')} "
           f"local_path={keys['local_path']}")


def _amm_userlist(game: "BaseGame", profile: str) -> Path:
    """Userlist AMM's own sort uses: the profile's, else the game-wide one."""
    from Utils.config_paths import get_loot_game_dir
    profile_ul = game.get_profile_root() / "profiles" / profile / "userlist.yaml"
    if profile_ul.is_file():
        return profile_ul
    global_ul = get_loot_game_dir(game.game_id) / "userlist.yaml"
    return global_ul if global_ul.is_file() else profile_ul


def push_userlist(game: "BaseGame", profile: str, loot_game: str,
                  log_fn: Callable[[str], None] = _noop) -> bytes | None:
    """Give LOOT the same userlist AMM sorts with.

    Returns the bytes LOOT started from, for pull_userlist(); None when LOOT
    has no userlist to start from.
    """
    source = _amm_userlist(game, profile)
    target = loot_data_dir(game) / "games" / loot_game / "userlist.yaml"
    if source.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        log_fn(f"userlist: {source} -> LOOT")
    return target.read_bytes() if target.is_file() else None


def pull_userlist(game: "BaseGame", profile: str, loot_game: str,
                  baseline: bytes | None,
                  log_fn: Callable[[str], None] = _noop) -> bool:
    """Copy rules edited in LOOT back to AMM's userlist. True if copied."""
    source = loot_data_dir(game) / "games" / loot_game / "userlist.yaml"
    if not source.is_file() or source.read_bytes() == baseline:
        return False
    target = _amm_userlist(game, profile)
    target.parent.mkdir(parents=True, exist_ok=True)
    write_atomic_text(target, source.read_text(encoding="utf-8"))
    log_fn(f"userlist: LOOT -> {target}")
    return True


def loot_launch_args(game: "BaseGame", pfx: Path, loot_game: str) -> list[str]:
    """--game / --game-path / --loot-data-path for LOOT.exe.

    *loot_game* is LOOT's game folder name ("Skyrim Special Edition", ...);
    LOOT ignores --game-path unless --game names one of its games.
    """
    from Utils.wine.paths import to_wine_path
    data = loot_data_dir(game)
    data.mkdir(parents=True, exist_ok=True)
    args = [f"--game={loot_game}", f"--loot-data-path={to_wine_path(data, pfx)}"]
    root = loot_game_root(game)
    if root is not None:
        args.append(f"--game-path={to_wine_path(root, pfx)}")
    return args


def _local_subpath(game: "BaseGame") -> Path:
    """AppData/Local folder LOOT uses for *game*.

    Like LOOT, treat an install with Galaxy64.dll as the GOG build, which
    keeps plugins.txt under "<Game> GOG".
    """
    gog = getattr(game, "_APPDATA_SUBPATH_GOG", None)
    root = game.get_game_path()
    if gog is not None and root is not None and (root / "Galaxy64.dll").is_file():
        return gog
    return game._APPDATA_SUBPATH


def prepare_loot_local_dir(game: "BaseGame", pfx: Path) -> None:
    """Create LOOT's AppData folder in *pfx* before plugins.txt is copied in.

    The copy only goes to folders that already exist, and a fresh tool prefix
    has none for the GOG build.
    """
    (pfx / _local_subpath(game)).mkdir(parents=True, exist_ok=True)


def loot_plugins_txt(game: "BaseGame", pfx: Path) -> Path | None:
    """The plugins.txt copy LOOT reads and rewrites inside *pfx*."""
    target = pfx / _local_subpath(game) / game.plugins_txt_filename
    return target if target.is_file() else None


_PLUGIN_EXTS = (".esm", ".esp", ".esl")


def _uses_timestamps(game: "BaseGame") -> bool:
    """Oblivion / Fallout 3 / New Vegas: plugin file dates are the load order."""
    return bool(getattr(game, "_orders_plugins_by_mtime", lambda: False)())


def _uses_loadorder_txt(game: "BaseGame") -> bool:
    """Skyrim LE / Enderal: loadorder.txt next to an active-only plugins.txt."""
    return (not getattr(game, "plugins_use_star_prefix", True)
            and not _uses_timestamps(game))


def seed_loadorder_txt(game: "BaseGame", pfx: Path, profile: str,
                       log_fn: Callable[[str], None] = _noop) -> None:
    """Give LOOT the profile's full order for loadorder.txt games.

    AMM deploys only plugins.txt, which for these games lists just the active
    plugins; without loadorder.txt LOOT would start from file dates instead.
    """
    if not _uses_loadorder_txt(game):
        return
    plugins = loot_plugins_txt(game, pfx)
    source = game.get_profile_root() / "profiles" / profile / "loadorder.txt"
    if plugins is None or not source.is_file():
        return
    shutil.copyfile(source, plugins.parent / "loadorder.txt")
    log_fn(f"loadorder.txt: {source} -> {plugins.parent}")


def _data_dir(game: "BaseGame") -> Path | None:
    from Utils.vfs import effective_shadow_data_root, manifest_path
    if manifest_path(game).is_file():
        return effective_shadow_data_root(game)
    return game.get_mod_data_path()


def _timestamp_order(game: "BaseGame") -> list[str]:
    """Deployed plugins sorted by modification time, the way the game loads them."""
    data = _data_dir(game)
    if data is None or not data.is_dir():
        return []
    stamped = []
    for entry in data.iterdir():
        if entry.suffix.lower() in _PLUGIN_EXTS:
            try:
                stamped.append((entry.stat().st_mtime, entry.name.lower(), entry.name))
            except OSError:
                continue
    return [name for _mtime, _key, name in sorted(stamped)]


def read_loot_order(game: "BaseGame", pfx: Path) -> list[str]:
    """Plugin order LOOT left behind, first to last.

    Asterisk games keep it in the tool prefix's plugins.txt, Skyrim LE and
    Enderal in the loadorder.txt beside it, and the timestamp games in the
    dates LOOT set on the deployed plugin files.
    """
    from Utils.plugins import read_loadorder, read_plugins
    if _uses_timestamps(game):
        return _timestamp_order(game)
    plugins = loot_plugins_txt(game, pfx)
    if plugins is None:
        return []
    if _uses_loadorder_txt(game):
        order = read_loadorder(plugins.parent / "loadorder.txt")
        if order:
            return order
    star = getattr(game, "plugins_use_star_prefix", True)
    return [e.name for e in read_plugins(plugins, star_prefix=star)]


def merge_loot_order(current: list[str], vanilla: set[str],
                     loot_order: list[str]) -> tuple[list[str], list[str]]:
    """Fit LOOT's order onto the profile's full plugin list.

    plugins.txt leaves out the vanilla plugins the engine force-loads, so
    those keep their current place at the top. Plugins LOOT didn't list stay
    after the sorted ones; plugins the profile doesn't know are dropped.
    Returns (full order, names dropped from LOOT's list).
    """
    by_lower = {name.lower(): name for name in current}
    vanilla_lower = {name.lower() for name in vanilla}
    seen: set[str] = set()
    sorted_names: list[str] = []
    dropped: list[str] = []
    for name in loot_order:
        key = name.lower()
        if key not in by_lower:
            dropped.append(name)
        elif key not in seen and key not in vanilla_lower:
            seen.add(key)
            sorted_names.append(by_lower[key])
    head = [n for n in current if n.lower() in vanilla_lower]
    tail = [n for n in current
            if n.lower() not in vanilla_lower and n.lower() not in seen]
    return head + sorted_names + tail, dropped
