from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path

from Utils.atomic_write import atomic_writer
from .laa import PEFormatError, is_large_address_aware, write_large_address_aware

EXE_NAME = "Oblivion.exe"
BACKUP_NAME = "Oblivion_backup.exe"


def inspect_exe(game_root: Path) -> dict:
    exe = Path(game_root) / EXE_NAME
    backup = Path(game_root) / BACKUP_NAME
    info = {
        "exe": exe,
        "backup": backup,
        "backup_exists": backup.is_file() and not backup.is_symlink(),
        "state": "missing",
        "variant": None,
        "hash": None,
        "error": "",
    }
    if not exe.is_file():
        return info
    info["hash"] = hashlib.sha1(exe.read_bytes()).hexdigest().upper()
    try:
        info["state"] = "patched" if is_large_address_aware(exe) else "patchable"
    except PEFormatError as exc:
        info["state"] = "unknown"
        info["error"] = str(exc)
    return info


def apply_4gb_patch(game_root: Path) -> None:
    game_root = Path(game_root)
    exe = game_root / EXE_NAME
    backup = game_root / BACKUP_NAME
    if not exe.is_file():
        raise RuntimeError(f"{EXE_NAME} not found in {game_root}")
    if exe.is_symlink():
        raise RuntimeError(f"{EXE_NAME} is a symbolic link; restore the game before patching")
    if backup.is_symlink() or (backup.exists() and not backup.is_file()):
        raise RuntimeError(f"{BACKUP_NAME} is not a regular backup file")
    try:
        if is_large_address_aware(exe):
            raise RuntimeError(f"{EXE_NAME} is already 4GB patched.")
    except PEFormatError as exc:
        raise RuntimeError(f"{EXE_NAME} is not a supported Windows executable: {exc}") from exc
    if not backup.exists():
        with exe.open("rb") as source, atomic_writer(
                backup, "wb", encoding=None) as target:
            shutil.copyfileobj(source, target, 1024 * 1024)
        try:
            shutil.copystat(exe, backup)
        except OSError:
            pass
    write_large_address_aware(exe, exe)
    if not is_large_address_aware(exe):
        raise RuntimeError(f"{EXE_NAME} did not retain the 4GB patch")


def restore_backup(game_root: Path) -> None:
    game_root = Path(game_root)
    backup = game_root / BACKUP_NAME
    if not backup.is_file() or backup.is_symlink():
        raise RuntimeError(f"{BACKUP_NAME} not found in {game_root}")
    os.replace(backup, game_root / EXE_NAME)
