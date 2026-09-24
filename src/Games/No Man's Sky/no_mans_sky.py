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

from Games.Custom.custom_game import StandardCustomGame
from Utils.app_log import safe_log
from Utils.atomic_write import write_atomic, write_atomic_text
from Utils.deployment import LinkMode
from Utils.nms.gcmodsettings import write_gcmodsettings

# ---------------------------------------------------------------------------
# GCMODSETTINGS.MXML backup / restore
#
# NOTE: _digest, _replace_with_symlink, _read_settings_state,
# _backup_settings, _record_generated_settings and _restore_settings are a
# near-verbatim copy of the modsettings.lsx helpers in
# Games/Baldur's Gate 3/baldurs_gate_3.py (only file names, the target path
# and message wording differ). Keep the two in step; if a third handler
# needs the same exact-restore behaviour, move them into a shared helper
# parameterised by file names and target path instead of copying again.
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Definition - identical to the Resources branch "No_Man_s_Sky.json" custom
# handler it replaces, so existing profiles and settings carry over.
# ---------------------------------------------------------------------------

NMS_DEFINITION: dict = {
    "name": "No Man's Sky",
    "game_id": "No_Man_s_Sky",
    "version": 1,
    "exe_name": "Binaries\\NMS.exe",
    "deploy_type": "standard",
    "mod_data_path": "GAMEDATA/MODS",
    "steam_id": "275850",
    "nexus_game_domain": "nomanssky",
    "editable": False,
    "image_url": "https://cdn2.steamgriddb.com/icon_thumb/179e48cecceef8d753185783917e5bd8.png",
    "mod_folder_strip_prefixes": ["GAMEDATA", "MODS"],
    "conflict_ignore_filenames": ["*.txt"],
    "mod_folder_strip_prefixes_post": [],
    "mod_install_prefix": "",
    "mod_required_top_level_folders": [],
    "mod_auto_strip_until_required": False,
    "mod_required_file_types": [],
    "mod_install_as_is_if_no_match": False,
    "restore_before_deploy": True,
    "normalize_folder_case": True,
    "wine_dll_overrides": {},
    "custom_routing_rules": [],
}


# ---------------------------------------------------------------------------
# Deployed folder ownership
# ---------------------------------------------------------------------------

def _deploy_entries(game, profile_dir: Path):
    """The deploy winners: the pinned plan during deploy, else the last commit."""
    from Utils.filegraph.deploy import current, deployed_entries_for, entries
    if current() is not None:
        return list(entries())
    return list(deployed_entries_for(game, profile_dir))


def deployed_nms_folders(game, mods_dir: Path, deploy_entries) -> dict[str, set[str]]:
    """Map each deployed GAMEDATA/MODS/<folder> to the mods that supplied it.

    Loose files placed directly in MODS/ belong to no folder and are skipped.
    Case variants of one folder merge under the first spelling seen.
    """
    from Utils.filegraph.deploy import absolute_destination

    prefix = os.path.abspath(os.fspath(mods_dir)) + os.sep
    prefix_key = prefix.casefold()
    spelling: dict[str, str] = {}
    owners: dict[str, set[str]] = {}
    for entry in deploy_entries:
        destination = absolute_destination(game, entry)
        if destination is None:
            continue
        path = os.path.abspath(os.fspath(destination))
        if not path.casefold().startswith(prefix_key):
            continue
        folder, sep, _rest = path[len(prefix):].partition(os.sep)
        if not sep or not folder:
            continue
        name = spelling.setdefault(folder.casefold(), folder)
        owners.setdefault(name, set()).add(entry.mod_name)
    return owners


def unmanaged_nms_folders(mods_dir: Path, folder_owners: dict[str, set[str]]) -> set[str]:
    """Folders present that Amethyst didn't deploy (mods installed by hand).

    After a physical deploy the pre-existing MODS/ content lives in MODS_Core/;
    under the VFS the real MODS/ is untouched.
    """
    core = mods_dir.parent / f"{mods_dir.name}_Core"
    source = core if core.is_dir() else mods_dir
    managed = {f.casefold() for f in folder_owners}
    try:
        return {p.name for p in source.iterdir()
                if p.is_dir() and p.name.casefold() not in managed}
    except OSError:
        return set()


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

class NoMansSky(StandardCustomGame):
    """No Man's Sky: standard GAMEDATA/MODS deploy plus GCMODSETTINGS.MXML."""

    def __init__(self) -> None:
        super().__init__(dict(NMS_DEFINITION))

    @property
    def is_custom(self) -> bool:
        # Built-in handler, not a user/Resources custom definition: no
        # "Edit custom game" or "Force update handler" actions.
        return False

    def _settings_path(self) -> Path | None:
        return self._game_path / _SETTINGS_REL if self._game_path else None

    def _root_folder_owns_settings(self) -> bool:
        """True when Root_Folder ships its own GCMODSETTINGS.MXML.

        The pipeline deploys Root_Folder after game.deploy(), so that copy
        would replace the generated file every time (e.g. one captured as a
        runtime file under the old custom handler). As with Cyberpunk's
        archive modlist, an explicit root payload wins.
        """
        if not bool(getattr(self, "_pipeline_root_folder_enabled", True)):
            return False
        from Utils.deployment import _resolve_nocase
        source = _resolve_nocase(
            self.get_effective_root_folder_path(), _SETTINGS_REL.as_posix())
        return bool(source is not None and source.is_file())

    def _write_settings(self, target: Path, profile_dir: Path,
                        preserved: Path | None, log_fn) -> bool:
        """Write GCMODSETTINGS.MXML to *target*. Never raises.

        Returns True when the file was written.
        """
        _log = safe_log(log_fn)
        if self._root_folder_owns_settings():
            _log("  Root_Folder provides Binaries/SETTINGS/GCMODSETTINGS.MXML - "
                 "keeping it instead of generating one.")
            self.add_deploy_warning(
                "Root_Folder contains Binaries/SETTINGS/GCMODSETTINGS.MXML, which "
                "replaces the generated file on every deploy, so No Man's Sky "
                "mod priority won't follow the mod list. Remove it from "
                "Root_Folder to let Amethyst manage it.")
            return False
        _log("Writing GCMODSETTINGS.MXML ...")
        try:
            mods_dir = self.get_mod_data_path()
            folder_owners = deployed_nms_folders(
                self, mods_dir, _deploy_entries(self, profile_dir))
            write_gcmodsettings(
                target, profile_dir / "modlist.txt", folder_owners,
                log_fn=_log,
                preserved_settings=preserved,
                unmanaged_folders=unmanaged_nms_folders(mods_dir, folder_owners),
                warn_fn=self.add_deploy_warning)
            return True
        except Exception as exc:
            _log(f"  WARN: could not write GCMODSETTINGS.MXML: {exc}")
            self.add_deploy_warning(
                "GCMODSETTINGS.MXML could not be updated, so No Man's Sky mod "
                "priority may not match the mod list. See the deploy log.")
            return False

    def deploy(self, log_fn=None, mode: LinkMode = LinkMode.HARDLINK,
               profile: str = "default", progress_fn=None) -> None:
        if self.vfs_launch_enabled:
            # The private view gets its generated file from
            # _vfs_post_view_build; the real game folder is never modified.
            super().deploy(log_fn=log_fn, mode=mode, profile=profile,
                           progress_fn=progress_fn)
            return

        _log = safe_log(log_fn)
        settings = self._settings_path()
        profile_dir = self.get_profile_root() / "profiles" / profile
        preserved = (_backup_settings(profile_dir, settings, _log)
                     if settings is not None else None)

        super().deploy(log_fn=log_fn, mode=mode, profile=profile,
                       progress_fn=progress_fn)
        if settings is None:
            return
        if self._write_settings(settings, profile_dir, preserved, _log):
            _record_generated_settings(profile_dir, settings)

    def _vfs_post_view_build(self, *, view_root: Path, profile: str,
                             filemap: Path, staging: Path, log_fn) -> None:
        """Generate GCMODSETTINGS.MXML inside the resolved VFS view.

        The view hardlinks the game root, so the file is replaced there
        (atomic write = new inode) and the real game's copy is only read,
        as the source of the hand-installed entries to preserve.
        """
        real = self._settings_path()
        if real is None:
            return
        profile_dir = self.get_profile_root() / "profiles" / profile
        self._write_settings(Path(view_root) / _SETTINGS_REL, profile_dir,
                             real if real.is_file() else None, log_fn)

    def restore(self, log_fn=None, progress_fn=None) -> None:
        _log = safe_log(log_fn)
        super().restore(log_fn=log_fn, progress_fn=progress_fn)

        settings = self._settings_path()
        profile_dir = self._active_profile_dir
        if settings is None or profile_dir is None:
            return
        _log("Restore: restoring the original GCMODSETTINGS.MXML ...")
        profile_dir = Path(profile_dir)
        restored = _restore_settings(profile_dir, settings, _log)
        if not restored and not (profile_dir / _SETTINGS_STATE).exists():
            _log("  No manager-owned GCMODSETTINGS.MXML backup needed restoration.")

    def post_clean_game_folder(self, log_fn=None) -> None:
        """Restore manager-owned GCMODSETTINGS.MXML state after cleaning."""
        settings = self._settings_path()
        profile_dir = self._active_profile_dir
        if settings is None or profile_dir is None:
            return
        _restore_settings(Path(profile_dir), settings, safe_log(log_fn))
