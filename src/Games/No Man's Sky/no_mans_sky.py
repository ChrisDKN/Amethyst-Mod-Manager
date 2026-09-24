"""
no_mans_sky.py
Game handler for No Man's Sky.

Deploys exactly like the former "No Man's Sky" custom handler definition
(standard deploy into GAMEDATA/MODS) and additionally keeps
Binaries/SETTINGS/GCMODSETTINGS.MXML in step with the modlist, which is
where NMS keeps each mod's ModPriority and enabled state.

The user's original GCMODSETTINGS.MXML is backed up per profile and put
back exactly on restore - the same approach the Baldur's Gate 3 handler
uses for modsettings.lsx.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path

from Utils.atomic_write import write_atomic, write_atomic_text

_SETTINGS_REL = Path("Binaries/SETTINGS/GCMODSETTINGS.MXML")
_SETTINGS_BACKUP = "nms_gcmodsettings_original.mxml"
_SETTINGS_STATE = "nms_gcmodsettings_state.json"


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _replace_with_symlink(path: Path, link_target: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.amethyst-{uuid.uuid4().hex}")
    try:
        temporary.symlink_to(link_target)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_settings_state(profile_dir: Path) -> dict | None:
    try:
        data = json.loads(
            (profile_dir / _SETTINGS_STATE).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) and data.get("version") == 1 else None
    except (OSError, ValueError):
        return None


def _backup_settings(
    profile_dir: Path, settings: Path, log_fn,
) -> Path | None:
    state_path = profile_dir / _SETTINGS_STATE
    backup_path = profile_dir / _SETTINGS_BACKUP
    state = _read_settings_state(profile_dir)
    if state is not None:
        if state.get("had_original") and not backup_path.is_file():
            raise RuntimeError(
                "NMS GCMODSETTINGS restore state exists but its original backup is missing.")
        if (state.get("had_original")
                and state.get("original_sha256")
                and _digest(backup_path.read_bytes())
                != state["original_sha256"]):
            raise RuntimeError(
                "NMS GCMODSETTINGS original backup failed its integrity check.")
        return backup_path if state.get("had_original") else None
    if state_path.exists():
        raise RuntimeError(
            "NMS GCMODSETTINGS restore state is unreadable; refusing to replace it.")

    if settings.is_symlink() and not settings.is_file():
        raise RuntimeError(
            "GCMODSETTINGS.MXML is a dangling symlink; refusing to replace it.")
    original_symlink = os.readlink(settings) if settings.is_symlink() else ""
    original = settings.read_bytes() if settings.is_file() else None
    if original is not None:
        write_atomic(backup_path, original)
    state = {
        "version": 1,
        "target": str(settings),
        "had_original": original is not None,
        "original_symlink": original_symlink,
        "original_sha256": _digest(original) if original is not None else "",
        "generated_sha256": "",
    }
    write_atomic_text(state_path, json.dumps(state, indent=2))
    log_fn("  Preserved the existing GCMODSETTINGS.MXML for exact restore.")
    return backup_path if original is not None else None


def _record_generated_settings(profile_dir: Path, settings: Path) -> None:
    state = _read_settings_state(profile_dir)
    if state is None or not settings.is_file():
        return
    state["generated_sha256"] = _digest(settings.read_bytes())
    write_atomic_text(
        profile_dir / _SETTINGS_STATE, json.dumps(state, indent=2))


def _restore_settings(profile_dir: Path, fallback: Path | None, log_fn) -> bool:
    state = _read_settings_state(profile_dir)
    if state is None:
        if (profile_dir / _SETTINGS_STATE).exists():
            log_fn("  WARN: NMS GCMODSETTINGS restore state is unreadable; "
                   "managed files were retained.")
        return False
    target = Path(state.get("target") or fallback or "")
    suffix = _SETTINGS_REL.parts
    if not target.parts or tuple(target.parts[-len(suffix):]) != suffix:
        log_fn("  WARN: invalid NMS GCMODSETTINGS restore target; backup retained.")
        return False
    if (fallback is None
            or os.path.abspath(os.fspath(target))
            != os.path.abspath(os.fspath(fallback))):
        log_fn("  WARN: NMS GCMODSETTINGS restore target does not match the "
               "configured game folder; backup retained.")
        return False

    backup_path = profile_dir / _SETTINGS_BACKUP
    generated_hash = state.get("generated_sha256") or ""
    original_hash = state.get("original_sha256") or ""
    original: bytes | None = None
    if state.get("had_original"):
        try:
            original = backup_path.read_bytes()
        except OSError:
            log_fn("  WARN: NMS GCMODSETTINGS backup is missing or unreadable; "
                   "restore remains retryable.")
            return False
        if original_hash and _digest(original) != original_hash:
            log_fn("  WARN: NMS GCMODSETTINGS backup failed its integrity check; "
                   "restore remains retryable.")
            return False

    current = target.read_bytes() if target.is_file() else None
    if (current is not None
            and (not generated_hash or _digest(current) != generated_hash)
            and _digest(current) != original_hash):
        recovery = profile_dir / "nms_gcmodsettings_runtime.mxml"
        index = 1
        while recovery.exists():
            recovery = profile_dir / f"nms_gcmodsettings_runtime.{index}.mxml"
            index += 1
        write_atomic(recovery, current)
        log_fn(f"  Preserved runtime-modified GCMODSETTINGS.MXML at {recovery}.")

    try:
        if state.get("had_original"):
            original_symlink = state.get("original_symlink") or ""
            if original_symlink:
                _replace_with_symlink(target, original_symlink)
            else:
                write_atomic(target, original if original is not None else b"")
            log_fn("  Restored the original GCMODSETTINGS.MXML.")
        elif target.exists() or target.is_symlink():
            target.unlink()
            log_fn("  Removed the manager-generated GCMODSETTINGS.MXML.")
        (profile_dir / _SETTINGS_STATE).unlink(missing_ok=True)
        backup_path.unlink(missing_ok=True)
        return True
    except OSError as exc:
        log_fn(f"  WARN: could not restore GCMODSETTINGS.MXML: {exc}")
        return False
