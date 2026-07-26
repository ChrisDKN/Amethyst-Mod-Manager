"""
eligibility.py
ESL / medium-plugin eligibility checks backed by libloot.

libloot's own format-aware scan (via ``Plugin.is_valid_as_light_plugin``)
is stricter than a simple FormID-range walk: it also validates that every
referenced FormID resolves correctly once the plugin sits in the 0xFE slot.

The check needs a ``loot.Game`` instance with ``load_plugin_headers`` called
for the file being tested. We keep one Game per (game_type, tempdir) so batch
checks (e.g. the "Mark selected as Light" menu) don't rebuild it per file.
"""

from __future__ import annotations

import atexit
import shutil
from pathlib import Path

from Utils.tmp_dirs import make_tracked_tmpdir, sweep_stale_tmpdirs

try:
    import loot
    _AVAILABLE = True
except ImportError:
    loot = None
    _AVAILABLE = False


# game_type_attr -> (Game, tempdir_path, data_dir_path)
_GAME_CACHE: dict[str, tuple[object, Path, Path]] = {}

_TMP_PREFIX = "mm_loot_elig_"
sweep_stale_tmpdirs(_TMP_PREFIX)


def _cleanup() -> None:
    for _, tmp, _ in _GAME_CACHE.values():
        shutil.rmtree(tmp, ignore_errors=True)
    _GAME_CACHE.clear()


atexit.register(_cleanup)


def is_available() -> bool:
    return _AVAILABLE


def _get_game(game_type_attr: str):
    """Return ``(game, data_dir)`` for ``game_type_attr`` or ``None`` on failure."""
    if not _AVAILABLE:
        return None
    cached = _GAME_CACHE.get(game_type_attr)
    if cached is not None:
        return cached[0], cached[2]

    try:
        gt = getattr(loot.GameType, game_type_attr)
    except AttributeError:
        return None

    tmp = make_tracked_tmpdir(_TMP_PREFIX)
    data_dir = tmp / "Data"
    data_dir.mkdir()
    try:
        game = loot.Game(gt, str(tmp), str(tmp))
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        return None
    _GAME_CACHE[game_type_attr] = (game, tmp, data_dir)
    return game, data_dir


def _stage_plugin(path: Path, data_dir: Path) -> Path | None:
    """Symlink ``path`` into the fake Data dir so libloot can read it.

    libloot expects plugins under ``<game_path>/Data``. Returns the staged path
    (reusing an existing link if it already points at the same file).
    """
    dest = data_dir / path.name
    try:
        if dest.is_symlink() or dest.exists():
            try:
                if dest.resolve() == path.resolve():
                    return dest
            except OSError:
                pass
            dest.unlink()
        dest.symlink_to(path)
    except OSError:
        return None
    return dest


def check_esl_eligible(plugin_path: Path, game_type_attr: str) -> bool:
    """Return ``True`` if libloot considers the plugin safe to ESL-flag.

    Unlike the prior FormID-range scan, libloot also validates that every
    referenced record resolves correctly in the 0xFE slot.

    Returns ``False`` if libloot is unavailable, the game type is unknown,
    or the plugin cannot be parsed.
    """
    g = _get_game(game_type_attr)
    if g is None:
        return False
    game, data_dir = g
    staged = _stage_plugin(plugin_path, data_dir)
    if staged is None:
        return False
    try:
        # Must use load_plugins (full record data), NOT load_plugin_headers —
        # headers alone lack record FormIDs, so is_valid_as_light_plugin would
        # return True for everything (including Skyrim.esm).
        game.load_plugins([str(staged)])
        p = game.plugin(plugin_path.name)
        if p is None:
            return False
        return bool(p.is_valid_as_light_plugin())
    except Exception:
        return False
