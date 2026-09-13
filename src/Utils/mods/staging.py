"""Move a game's staging tree when the staging root changes.

Toolkit-neutral port of the Tk ``ReconfigureGamePanel._maybe_migrate_staging``
worker (gui/add_game_dialog.py). Pure filesystem logic - the caller owns the
prompt and the progress UI.

Semantics (Tk parity):
  * destination wins - a file already present at the new root is skipped;
  * per-file failures are logged and counted, never fatal;
  * afterwards, now-empty directories under the old root (and the old root
    itself) are pruned best-effort.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Callable, Optional


def _same_path(left: Path, right: Path) -> bool:
    try:
        return left.expanduser().resolve(strict=False) \
            == right.expanduser().resolve(strict=False)
    except (OSError, RuntimeError):
        return left.expanduser().absolute() == right.expanduser().absolute()


def staging_root_problem(
    root: Path,
    game_name: str,
    current_root: Optional[Path] = None,
) -> tuple[str, str] | None:
    """Return a problem code and detail for an unsafe staging root."""
    root = Path(root).expanduser()
    if current_root is not None and _same_path(root, Path(current_root)):
        return None
    if root.exists() and not root.is_dir():
        return "not_directory", ""

    from Utils.config_paths import get_config_dir, get_profiles_dir

    games_dir = get_config_dir() / "games"
    try:
        path_files = games_dir.glob("*/paths.json")
        for paths_file in path_files:
            owner = paths_file.parent.name
            if owner == game_name:
                continue
            try:
                data = json.loads(paths_file.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    continue
                saved = str(data.get("staging_path", "") or "").strip()
                owner_root = Path(saved).expanduser() if saved \
                    else get_profiles_dir() / owner
            except (OSError, ValueError, TypeError):
                continue
            if _same_path(root, owner_root):
                return "owned", owner
    except OSError:
        pass

    if not root.is_dir():
        return None
    try:
        entries = list(root.iterdir())
    except OSError as exc:
        return "unreadable", str(exc)
    managed_dirs = {
        entry.name.casefold() for entry in entries if entry.is_dir()
    }
    if entries and not {"mods", "profiles"}.issubset(managed_dirs):
        return "unrecognized", ""
    return None


def staging_move_needed(old_root: Optional[Path],
                        new_root: Optional[Path]) -> bool:
    """True when the staging root changed and the old root has content."""
    if old_root is None or new_root is None:
        return False
    try:
        if old_root.resolve() == new_root.resolve():
            return False
    except OSError:
        if str(old_root) == str(new_root):
            return False
    if not old_root.is_dir():
        return False
    try:
        return any(old_root.iterdir())
    except OSError:
        return False


def collect_staging_files(old_root: Path) -> tuple[list[Path], int]:
    """Flat list of files/symlinks under *old_root* plus their total size.

    The flat list lets the caller drive a per-file progress bar; the size
    feeds the "Move X GB?" prompt.
    """
    files: list[Path] = []
    total_size = 0
    try:
        for p in old_root.rglob("*"):
            if p.is_file() or p.is_symlink():
                files.append(p)
                try:
                    if not p.is_symlink():
                        total_size += p.lstat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return files, total_size


def migrate_staging_files(
    old_root: Path,
    new_root: Path,
    files: list[Path],
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
    log_fn: Callable[[str], None] = print,
) -> tuple[int, int, int]:
    """Move *files* (from ``collect_staging_files``) into *new_root*.

    Returns ``(moved, skipped, failed)``. ``progress_cb(done, total, message)``
    is invoked after every file, from the calling thread.
    """
    total = len(files)
    try:
        new_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log_fn(f"Staging migration: could not create {new_root}: {exc}")
        return 0, 0, total

    moved = skipped = failed = 0
    done = 0
    for src in files:
        try:
            rel = src.relative_to(old_root)
        except ValueError:
            done += 1
            continue
        dst = new_root / rel
        try:
            if dst.exists():
                skipped += 1
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
                moved += 1
        except Exception as exc:
            failed += 1
            log_fn(f"Staging migration: failed to move {src} → {dst}: {exc}")
        done += 1
        if progress_cb is not None:
            progress_cb(done, total, str(src.parent))

    # Best-effort: prune now-empty directories so the old root disappears
    # once everything has moved (skipped/failed files keep their dirs alive).
    try:
        for d in sorted((p for p in old_root.rglob("*") if p.is_dir()),
                        key=lambda p: len(p.parts), reverse=True):
            try:
                d.rmdir()
            except OSError:
                pass
        try:
            old_root.rmdir()
        except OSError:
            pass
    except OSError:
        pass
    return moved, skipped, failed
