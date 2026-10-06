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

from Games.base_game import LaunchToggle
from Games.Custom.custom_game import StandardCustomGame
from Utils.app_log import safe_log
from Utils.atomic_write import write_atomic, write_atomic_text
from Utils.deployment import LinkMode
from Utils.nms.gcmodsettings import (
    read_gcmodsettings_preferences,
    write_gcmodsettings,
)

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
_DISABLE_ALL_KEY = "disable_all_mods"
_DISABLE_ALL_REQUEST_KEY = "nms_disable_all_mods_request"
_PREFERENCES_KEY = "nms_mod_settings"


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _settings_digest(path: Path | None) -> str:
    try:
        return _digest(path.read_bytes()) if path is not None else ""
    except OSError:
        return ""


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

def _deploy_entries(game, profile_dir: Path, *, include_root: bool = False):
    """The deploy winners: the pinned plan during deploy, else the last commit."""
    from Utils.filegraph.deploy import current, deployed_entries_for, entries
    if current() is not None:
        return list(entries(include_root=include_root))
    return [entry for entry in deployed_entries_for(game, profile_dir)
            if bool(getattr(entry, "legacy_root", False)
                    or getattr(entry, "provider_kind", "") == "root") == include_root]


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

    @property
    def vortex_mod_types(self) -> dict:
        return {
            "nomanssky-binaries": {"dest": "Binaries"},
            "nomanssky-deprecated-pak": {"dest": "GAMEDATA/PCBANKS/MODS"},
        }

    def __init__(self) -> None:
        super().__init__(dict(NMS_DEFINITION))

    @property
    def is_custom(self) -> bool:
        # Built-in handler, not a user/Resources custom definition: no
        # "Edit custom game" or "Force update handler" actions.
        return False

    @property
    def launch_toggles(self) -> list[LaunchToggle]:
        # NMS has no in-game switch for GCMODSETTINGS' DisableAllMods.
        return [LaunchToggle(
            key=_DISABLE_ALL_KEY,
            label="Disable all mods (No Man's Sky DisableAllMods)",
            hint=("Starts the game with every mod switched off, without "
                  "undeploying them. Applied the next time Amethyst deploys; "
                  "Play deploys first when 'Deploy before launch' is on."),
        )]

    def _disable_all_mods(self) -> bool | None:
        from Utils.executables.launch import load_launch_toggle
        return load_launch_toggle(self, _DISABLE_ALL_REQUEST_KEY, default=None)

    def launch_toggle_value(self, key: str) -> bool | None:
        if key != _DISABLE_ALL_KEY:
            return None
        pending = self._disable_all_mods()
        if pending is not None:
            return pending
        profile_dir = (self._active_profile_dir
                       or self.get_profile_root() / "profiles" / "default")
        return bool(self._settings_preferences(Path(profile_dir)).get("disable_all_mods"))

    def set_launch_toggle_value(self, key: str, enabled: bool | None) -> bool:
        if key != _DISABLE_ALL_KEY:
            return False
        if enabled is not None and enabled != self.launch_toggle_value(key):
            from Utils.executables.launch import save_launch_toggle
            save_launch_toggle(self, _DISABLE_ALL_REQUEST_KEY, enabled)
        return True

    def _settings_path(self) -> Path | None:
        return self._game_path / _SETTINGS_REL if self._game_path else None

    def _managed_settings_path(self, profile_dir: Path) -> Path | None:
        from Utils.vfs import effective_shadow_root, has_deployment_state, state_dir
        from Utils.vfs.overlay import INCOMPLETE_VIEW_NAME
        if has_deployment_state(self):
            if (state_dir(self) / INCOMPLETE_VIEW_NAME).exists():
                return None
            try:
                return effective_shadow_root(self) / _SETTINGS_REL
            except (OSError, ValueError, RuntimeError):
                return None
        state = _read_settings_state(profile_dir)
        settings = self._settings_path()
        return (settings if state and state.get("generated_sha256")
                and state.get("target") == str(settings) else None)

    def _settings_preferences(self, profile_dir: Path, *, live: bool = True) -> dict:
        from Utils.profiles.state import read_profile_settings
        settings = self._settings_path()
        if settings is None:
            return {}
        preferences = read_profile_settings(profile_dir).get(_PREFERENCES_KEY)
        if not isinstance(preferences, dict) or preferences.get("target") != str(settings):
            preferences = {}
        else:
            preferences = dict(preferences)
        if not live:
            return preferences
        current = self._managed_settings_path(profile_dir)
        if current is None and (not preferences
                                or preferences.get("original_sha256") != _settings_digest(settings)):
            current = settings
        latest = read_gcmodsettings_preferences(current) if current is not None else None
        if latest is not None:
            preferences.update(latest)
        return preferences

    def _save_settings_preferences(self, profile_dir: Path, settings: Path,
                                  original: Path | None = None) -> None:
        from Utils.profiles.state import merge_profile_settings
        preferences = read_gcmodsettings_preferences(settings)
        if preferences is not None:
            baseline = read_gcmodsettings_preferences(original) if original is not None else None
            if baseline is not None:
                preferences["disable_all_mods"] = (
                    preferences["disable_all_mods"] or baseline["disable_all_mods"])
                preferences["enabled_vr"].update(
                    {name: False for name, enabled in baseline["enabled_vr"].items()
                     if not enabled})
            preferences["target"] = str(self._settings_path())
            state = _read_settings_state(profile_dir)
            preferences["original_sha256"] = (
                state.get("original_sha256", "") if state is not None
                else _settings_digest(self._settings_path()))
            merge_profile_settings(profile_dir, {_PREFERENCES_KEY: preferences})

    def _capture_settings_preferences(self, profile_dir: Path) -> None:
        preferences = self._settings_preferences(profile_dir, live=False)
        current = self._managed_settings_path(profile_dir)
        if (current is None and preferences
                and preferences.get("original_sha256") != _settings_digest(self._settings_path())):
            current = self._settings_path()
        if current is not None:
            # Recover disable flags overwritten by the original handler once.
            original = None
            if not preferences:
                backup = profile_dir / _SETTINGS_BACKUP
                original = backup if backup.is_file() else self._settings_path()
            self._save_settings_preferences(profile_dir, current, original)

    def _settings_root_owner(self, profile_dir: Path) -> str | None:
        if not getattr(self, "root_folder_deploy_enabled", True):
            return None
        from Utils.deployment import _resolve_nocase
        if bool(getattr(self, "_pipeline_root_folder_enabled", True)):
            source = _resolve_nocase(
                self.get_effective_root_folder_path(), _SETTINGS_REL.as_posix())
            if source is not None and source.is_file():
                return "Root_Folder"
        from Utils.filegraph.deploy import absolute_destination
        target = self._settings_path()
        if target is not None:
            target_key = os.path.abspath(target).casefold()
            for entry in [*_deploy_entries(self, profile_dir, include_root=True),
                          *_deploy_entries(self, profile_dir)]:
                if entry.mod_name == "[Root_Folder]":
                    continue
                destination = absolute_destination(self, entry)
                if (destination is not None
                        and os.path.abspath(destination).casefold() == target_key):
                    return f"root-deployed mod '{entry.mod_name}'"
        return None

    def _write_settings(self, target: Path, profile_dir: Path,
                        preserved: Path | None, log_fn) -> bool:
        """Write GCMODSETTINGS.MXML to *target*. Never raises.

        Returns True when the file was written.
        """
        _log = safe_log(log_fn)
        try:
            owner = self._settings_root_owner(profile_dir)
            if owner is not None:
                _log(f"  {owner} provides Binaries/SETTINGS/GCMODSETTINGS.MXML - "
                     "keeping it instead of generating one.")
                self.add_deploy_warning(
                    f"{owner} provides Binaries/SETTINGS/GCMODSETTINGS.MXML, so "
                    "No Man's Sky mod priority won't follow the mod list. Remove "
                    "or disable that settings file to let Amethyst manage it.")
                return False
            _log("Writing GCMODSETTINGS.MXML ...")
            disable_all = self._disable_all_mods()
            mods_dir = self.get_mod_data_path()
            folder_owners = deployed_nms_folders(
                self, mods_dir, _deploy_entries(self, profile_dir))
            write_gcmodsettings(
                target, profile_dir / "modlist.txt", folder_owners,
                log_fn=_log,
                preserved_settings=preserved,
                unmanaged_folders=unmanaged_nms_folders(mods_dir, folder_owners),
                warn_fn=self.add_deploy_warning,
                disable_all=disable_all,
                preferences=self._settings_preferences(profile_dir, live=False))
            self._save_settings_preferences(profile_dir, target)
            if disable_all is not None and self._disable_all_mods() == disable_all:
                from Utils.executables.launch import save_launch_toggle
                save_launch_toggle(self, _DISABLE_ALL_REQUEST_KEY, None)
            return True
        except Exception as exc:
            _log(f"  WARN: could not write GCMODSETTINGS.MXML: {exc}")
            self.add_deploy_warning(
                "GCMODSETTINGS.MXML could not be updated, so No Man's Sky mod "
                "priority may not match the mod list. See the deploy log.")
            return False

    def deploy(self, log_fn=None, mode: LinkMode = LinkMode.HARDLINK,
               profile: str = "default", progress_fn=None) -> None:
        profile_dir = self.get_profile_root() / "profiles" / profile
        self._capture_settings_preferences(profile_dir)
        if self.vfs_launch_enabled:
            # The private view gets its generated file from
            # _vfs_post_view_build; the real game folder is never modified.
            super().deploy(log_fn=log_fn, mode=mode, profile=profile,
                           progress_fn=progress_fn)
            return

        _log = safe_log(log_fn)
        settings = self._settings_path()
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
        if self._active_profile_dir is not None:
            self._capture_settings_preferences(Path(self._active_profile_dir))
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
        self._capture_settings_preferences(Path(profile_dir))
        _restore_settings(Path(profile_dir), settings, safe_log(log_fn))
