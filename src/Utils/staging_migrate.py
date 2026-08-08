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

import shutil
from pathlib import Path
from typing import Callable, Optional


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
