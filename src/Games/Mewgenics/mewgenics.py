"""
mewgenics.py
Game handler for Mewgenics.

Mod structure:
  The game uses resources.gpak in the game root. Deploy unpacks it to
  Unpacked/, backs up vanilla files (from filemap paths) to vanilla_backup/,
  merges mod files from Profiles/Mewgenics/mods/ (via filemap), repacks to
  resources.gpak, then removes Unpacked/.

  Restore: unpack → remove filemap paths from Unpacked → restore those paths
  from vanilla_backup/ → repack → remove Unpacked.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from Games.base_game import BaseGame, WizardTool
from Utils.config_paths import get_profiles_dir
from Utils.deploy import (
    LinkMode,
    deploy_filemap,
    load_per_mod_strip_prefixes,
)
from Utils.modlist import read_modlist

_PROFILES_DIR = get_profiles_dir()

_RESOURCES_GPAK = "resources.gpak"
_UNPACKED_DIR = "Unpacked"
_VANILLA_BACKUP_DIR = "vanilla_backup"


def _remove_filemap_paths_from_dir(
    unpack_dir: Path,
    filemap_path: Path,
    log_fn,
    progress_fn=None,
    phase: str = "Removing mod files",
    backup_dir: Path | None = None,
) -> int:
    """Delete from unpack_dir every file path listed in filemap_path.
    If backup_dir is set, copy each file there (preserving rel path) before deleting.
    Returns count removed.
    """
    removed = 0
    if not filemap_path.is_file():
        return 0
    lines = [
        line.rstrip("\n").split("\t", 1)[0]
        for line in filemap_path.read_text(encoding="utf-8").splitlines()
        if "\t" in line
    ]
    seen: set[str] = set()
    total = len(lines)
    report = (lambda d, t: progress_fn(d, t, phase)) if progress_fn else None
    for i, rel_str in enumerate(lines):
        rel_norm = rel_str.replace("\\", "/")
        if rel_norm.lower() in seen:
            if report:
                report(i + 1, total)
            continue
        seen.add(rel_norm.lower())
        target = unpack_dir / rel_str
        if target.is_file():
            if backup_dir is not None:
                backup_file = backup_dir / rel_norm
                backup_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, backup_file)
            target.unlink()
            removed += 1
        if report:
            report(i + 1, total)
    return removed


def _restore_vanilla_from_backup(
    unpack_dir: Path,
    filemap_path: Path,
    backup_dir: Path,
    log_fn,
    progress_fn=None,
    phase: str = "Restoring vanilla files",
) -> int:
    """Copy from backup_dir into unpack_dir for every path in filemap_path where backup exists. Returns count restored."""
    restored = 0
    if not filemap_path.is_file() or not backup_dir.is_dir():
        return 0
    lines = [
        line.rstrip("\n").split("\t", 1)[0]
        for line in filemap_path.read_text(encoding="utf-8").splitlines()
        if "\t" in line
    ]
    seen: set[str] = set()
    total = len(lines)
    report = (lambda d, t: progress_fn(d, t, phase)) if progress_fn else None
    for i, rel_str in enumerate(lines):
        rel_norm = rel_str.replace("\\", "/")
        if rel_norm.lower() in seen:
            if report:
                report(i + 1, total)
            continue
        seen.add(rel_norm.lower())
        backup_file = backup_dir / rel_norm
        if backup_file.is_file():
            dest = unpack_dir / rel_str
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup_file, dest)
            restored += 1
        if report:
            report(i + 1, total)
    return restored


class Mewgenics(BaseGame):

    def __init__(self) -> None:
        self._game_path: Path | None = None
        self._staging_path: Path | None = None
        self._deploy_mode: LinkMode = LinkMode.COPY
        self.load_paths()

    # -----------------------------------------------------------------------
    # Identity
    # -----------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "Mewgenics"

    @property
    def game_id(self) -> str:
        return "mewgenics"

    @property
    def exe_name(self) -> str:
        return "Mewgenics.exe"

    @property
    def steam_id(self) -> str:
        return "686060"

    @property
    def nexus_game_domain(self) -> str:
        return "mewgenics"

    @property
    def mod_folder_strip_prefixes(self) -> set[str]:
        return set()

    @property
    def mod_install_prefix(self) -> str:
        return ""

    @property
    def mod_required_top_level_folders(self) -> set[str]:
        return {"textures", "swfs", "shaders", "levels", "data", "audio"}

    @property
    def mod_auto_strip_until_required(self) -> bool:
        return True

    @property
    def restore_before_deploy(self) -> bool:
        return False  # deploy() does unpack → remove modded → add mods → repack in one cycle

    @property
    def wizard_tools(self) -> list[WizardTool]:
        return [
            WizardTool(
                id="mewgenics_gpak",
                label="GPAK unpack / repack",
                description="Unpack resources.gpak to Unpacked/ or repack Unpacked/ to resources.gpak in the game root.",
                dialog_class_path="wizards.mewgenics_gpak.MewgenicsGpakWizard",
            ),
        ]

    # -----------------------------------------------------------------------
    # Paths
    # -----------------------------------------------------------------------

    def get_game_path(self) -> Path | None:
        return self._game_path

    def get_mod_data_path(self) -> Path | None:
        """Intended deploy target once unpack/deploy are implemented: unpacked gpak content."""
        if self._game_path is None:
            return None
        return self._game_path / "Unpacked"

    def get_mod_staging_path(self) -> Path:
        if self._staging_path is not None:
            return self._staging_path / "mods"
        return _PROFILES_DIR / self.name / "mods"

    def get_modpaths_launch_string(self, profile: str) -> str:
        """Build -modpaths launch string for Steam/Lutris from current profile modlist.
        Returns e.g. -modpaths \"Z:/path/mods/ModA\" \"Z:/path/mods/ModB\"
        """
        modlist_path = self.get_profile_root() / "profiles" / profile / "modlist.txt"
        if not modlist_path.is_file():
            return "-modpaths"
        staging = self.get_mod_staging_path().resolve()
        entries = read_modlist(modlist_path)
        enabled = [e for e in entries if e.enabled and not e.is_separator]
        paths: list[str] = []
        for e in enabled:
            mod_dir = staging / e.name
            if mod_dir.is_dir():
                # Wine path: Z:/ with forward slashes
                wine_path = "Z:" + str(mod_dir).replace("\\", "/")
                paths.append(wine_path)
        if not paths:
            return "-modpaths"
        return "-modpaths " + " ".join(f'"{p}"' for p in paths)

    # -----------------------------------------------------------------------
    # Configuration persistence
    # -----------------------------------------------------------------------

    def load_paths(self) -> bool:
        self._migrate_old_config()
        if not self._paths_file.exists():
            return False
        try:
            data = json.loads(self._paths_file.read_text(encoding="utf-8"))
            raw = data.get("game_path", "")
            if raw:
                self._game_path = Path(raw)
            raw_staging = data.get("staging_path", "")
            if raw_staging:
                self._staging_path = Path(raw_staging)
            raw_mode = data.get("deploy_mode", "copy")
            self._deploy_mode = {
                "symlink": LinkMode.SYMLINK,
                "hardlink": LinkMode.HARDLINK,
            }.get(raw_mode, LinkMode.COPY)
            self._validate_staging()
            return bool(self._game_path)
        except (json.JSONDecodeError, OSError):
            pass
        self._game_path = None
        self._staging_path = None
        self._deploy_mode = LinkMode.COPY
        return False

    def save_paths(self) -> None:
        self._paths_file.parent.mkdir(parents=True, exist_ok=True)
        mode_str = {
            LinkMode.SYMLINK: "symlink",
            LinkMode.HARDLINK: "hardlink",
            LinkMode.COPY: "copy",
        }.get(self._deploy_mode, "copy")
        data = {
            "game_path": str(self._game_path) if self._game_path else "",
            "staging_path": str(self._staging_path) if self._staging_path else "",
            "deploy_mode": mode_str,
        }
        self._paths_file.write_text(
            json.dumps(data, indent=2), encoding="utf-8"
        )

    def set_game_path(self, path: Path | str | None) -> None:
        self._game_path = Path(path) if path else None
        self.save_paths()

    def set_staging_path(self, path: Path | str | None) -> None:
        self._staging_path = Path(path) if path else None
        self.save_paths()

    def get_deploy_mode(self) -> LinkMode:
        return self._deploy_mode

    def set_deploy_mode(self, mode: LinkMode) -> None:
        self._deploy_mode = mode
        self.save_paths()

    # -----------------------------------------------------------------------
    # Deploy (unpack → merge mods → repack → remove Unpacked)
    # -----------------------------------------------------------------------

    def deploy(
        self,
        log_fn=None,
        mode: LinkMode = LinkMode.COPY,
        profile: str = "default",
        progress_fn=None,
    ) -> None:
        from gpak import extract_gpak, pack_gpak

        _log = log_fn or (lambda _: None)
        if self._game_path is None:
            raise RuntimeError("Game path is not configured.")

        game_root = self._game_path
        resources_gpak = game_root / _RESOURCES_GPAK
        unpack_dir = game_root / _UNPACKED_DIR

        if not resources_gpak.is_file():
            raise RuntimeError(
                f"'{_RESOURCES_GPAK}' not found in game root: {game_root}"
            )

        # 1. Remove any previous Unpacked dir, then unpack resources.gpak
        if unpack_dir.exists():
            _log("Removing previous Unpacked folder…")
            shutil.rmtree(unpack_dir)
        _log("Unpacking resources.gpak…")
        unpack_progress = (
            (lambda d, t: progress_fn(d, t, "Unpacking")) if progress_fn else None
        )
        extract_gpak(
            resources_gpak, unpack_dir, try_zlib=True, progress_fn=unpack_progress
        )
        _log("Unpack complete.")

        # 2. Remove modded files (backup vanilla first), then deploy current mods
        filemap = self.get_profile_root() / "filemap.txt"
        backup_dir = self.get_profile_root() / _VANILLA_BACKUP_DIR
        removed = _remove_filemap_paths_from_dir(
            unpack_dir, filemap, _log, progress_fn, phase="Removing mod files",
            backup_dir=backup_dir,
        )
        if removed:
            _log(f"Removed {removed} modded file(s) from Unpacked (vanilla backed up).")
        elif filemap.is_file():
            _log("No modded files to remove.")

        staging = self.get_mod_staging_path()
        profile_dir = self.get_profile_root() / "profiles" / profile
        per_mod_strip = load_per_mod_strip_prefixes(profile_dir)

        if filemap.is_file():
            linked, _ = deploy_filemap(
                filemap,
                unpack_dir,
                staging,
                mode=mode,
                strip_prefixes=self.mod_folder_strip_prefixes,
                per_mod_strip_prefixes=per_mod_strip,
                log_fn=_log,
                progress_fn=progress_fn,
            )
            _log(f"Deployed {linked} mod file(s) into Unpacked.")
        else:
            _log("No filemap.txt — skipping mod merge.")

        # 3. Repack Unpacked → resources.gpak (uncompressed)
        _log("Repacking to resources.gpak…")
        repack_progress = (
            (lambda d, t: progress_fn(d, t, "Repacking")) if progress_fn else None
        )
        pack_gpak(
            unpack_dir, resources_gpak, compress=False, progress_fn=repack_progress
        )
        _log("Repack complete.")

        # 4. Remove Unpacked folder
        shutil.rmtree(unpack_dir)
        _log("Removed Unpacked folder.")
        _log("Deploy complete.")

    def restore(self, log_fn=None, progress_fn=None) -> None:
        """Unpack resources.gpak, remove modded files, restore vanilla from backup, repack, remove Unpacked."""
        from gpak import extract_gpak, pack_gpak

        _log = log_fn or (lambda _: None)
        if self._game_path is None:
            raise RuntimeError("Game path is not configured.")

        game_root = self._game_path
        resources_gpak = game_root / _RESOURCES_GPAK
        unpack_dir = game_root / _UNPACKED_DIR

        if not resources_gpak.is_file():
            raise RuntimeError(
                f"'{_RESOURCES_GPAK}' not found in game root: {game_root}"
            )

        # 1. Remove any previous Unpacked dir, then unpack resources.gpak
        if unpack_dir.exists():
            _log("Removing previous Unpacked folder…")
            shutil.rmtree(unpack_dir)
        _log("Unpacking resources.gpak…")
        unpack_progress = (
            (lambda d, t: progress_fn(d, t, "Unpacking")) if progress_fn else None
        )
        extract_gpak(
            resources_gpak, unpack_dir, try_zlib=True, progress_fn=unpack_progress
        )
        _log("Unpack complete.")

        # 2. Remove modded files (paths from filemap.txt)
        filemap = self.get_profile_root() / "filemap.txt"
        removed = _remove_filemap_paths_from_dir(
            unpack_dir, filemap, _log, progress_fn, phase="Removing mod files"
        )
        if removed:
            _log(f"Removed {removed} modded file(s) from Unpacked.")
        else:
            _log("No filemap.txt — nothing to remove.")

        # 2b. Restore vanilla files from backup so repacked gpak has originals
        backup_dir = self.get_profile_root() / _VANILLA_BACKUP_DIR
        restored = _restore_vanilla_from_backup(
            unpack_dir, filemap, backup_dir, _log, progress_fn,
            phase="Restoring vanilla files",
        )
        if restored:
            _log(f"Restored {restored} vanilla file(s) from backup.")
        if backup_dir.is_dir():
            shutil.rmtree(backup_dir)
            _log("Removed vanilla backup folder.")

        # 3. Repack Unpacked → resources.gpak (uncompressed)
        _log("Repacking to resources.gpak…")
        repack_progress = (
            (lambda d, t: progress_fn(d, t, "Repacking")) if progress_fn else None
        )
        pack_gpak(
            unpack_dir, resources_gpak, compress=False, progress_fn=repack_progress
        )
        _log("Repack complete.")

        # 4. Remove Unpacked folder
        shutil.rmtree(unpack_dir)
        _log("Removed Unpacked folder.")
        _log("Restore complete.")

    def validate_install(self) -> list[str]:
        """Only require game path for now; deploy/mod data dir not used yet."""
        errors: list[str] = []
        if not self.is_configured():
            errors.append(
                f"Game path not set or does not exist for '{self.name}'."
            )
        return errors
