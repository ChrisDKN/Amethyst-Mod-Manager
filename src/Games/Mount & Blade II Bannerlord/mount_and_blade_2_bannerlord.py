"""
mount_and_blade_2_bannerlord.py
Game handler for Mount & Blade II: Bannerlord.

Steam install: library folder is named Mount___Blade_II__Bannerlord under
  steamapps/common/.

Mod structure:
  Mods install into <game root>/Modules/
  Staged mods live in Profiles/Mount & Blade II: Bannerlord/mods/
"""

import json
from pathlib import Path

from Games.base_game import BaseGame
from Utils.deploy import (
    LinkMode,
    deploy_core,
    deploy_filemap,
    load_per_mod_strip_prefixes,
    load_separator_deploy_paths,
    expand_separator_deploy_paths,
    cleanup_custom_deploy_dirs,
    move_to_core,
    restore_data_core,
)
from Utils.modlist import read_modlist
from Utils.config_paths import get_profiles_dir
from Utils.steam_finder import find_prefix

_PROFILES_DIR = get_profiles_dir()


class MountAndBlade2Bannerlord(BaseGame):

    def __init__(self):
        self._game_path: Path | None = None
        self._prefix_path: Path | None = None
        self._deploy_mode: LinkMode = LinkMode.HARDLINK
        self._staging_path: Path | None = None
        self.load_paths()

    # -----------------------------------------------------------------------
    # Identity
    # -----------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "Mount & Blade II: Bannerlord"

    @property
    def game_id(self) -> str:
        return "Mount___Blade_II__Bannerlord"

    @property
    def exe_name(self) -> str:
        return "bin/Win64_Shipping_Client/Bannerlord.Native.exe"

    @property
    def steam_id(self) -> str:
        return "261550"

    @property
    def nexus_game_domain(self) -> str:
        return "mountandblade2bannerlord"

    @property
    def mods_dir(self) -> str:
        return "Modules"

    @property
    def mod_folder_strip_prefixes(self) -> set[str]:
        return {"modules"}

    # -----------------------------------------------------------------------
    # Paths
    # -----------------------------------------------------------------------

    def get_game_path(self) -> Path | None:
        return self._game_path

    def get_mod_data_path(self) -> Path | None:
        """Mods go into Modules/ inside the game directory."""
        if self._game_path is None:
            return None
        return self._game_path / self.mods_dir

    def get_mod_staging_path(self) -> Path:
        if self._staging_path is not None:
            return self._staging_path / "mods"
        return _PROFILES_DIR / self.name / "mods"

    # -----------------------------------------------------------------------
    # Configuration persistence
    # -----------------------------------------------------------------------

    def load_paths(self) -> bool:
        self._migrate_old_config()
        if not self._paths_file.exists():
            self._game_path = None
            self._prefix_path = None
            self._staging_path = None
            return False
        try:
            data = json.loads(self._paths_file.read_text(encoding="utf-8"))
            raw = data.get("game_path", "")
            if raw:
                self._game_path = Path(raw)
            raw_pfx = data.get("prefix_path", "")
            if raw_pfx:
                self._prefix_path = Path(raw_pfx)
            raw_mode = data.get("deploy_mode", "hardlink")
            self._deploy_mode = {
                "symlink": LinkMode.SYMLINK,
                "copy": LinkMode.COPY,
            }.get(raw_mode, LinkMode.HARDLINK)
            raw_staging = data.get("staging_path", "")
            if raw_staging:
                self._staging_path = Path(raw_staging)
            self._validate_staging()
            if not self._prefix_path or not self._prefix_path.is_dir():
                found = find_prefix(self.steam_id)
                if found:
                    self._prefix_path = found
                    self.save_paths()
            return bool(self._game_path)
        except (json.JSONDecodeError, OSError):
            pass
        self._game_path = None
        self._prefix_path = None
        return False

    def save_paths(self) -> None:
        self._paths_file.parent.mkdir(parents=True, exist_ok=True)
        mode_str = {
            LinkMode.SYMLINK: "symlink",
            LinkMode.COPY: "copy",
        }.get(self._deploy_mode, "hardlink")
        data = {
            "game_path": str(self._game_path) if self._game_path else "",
            "prefix_path": str(self._prefix_path) if self._prefix_path else "",
            "deploy_mode": mode_str,
            "staging_path": str(self._staging_path) if self._staging_path else "",
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

    def get_prefix_path(self) -> Path | None:
        return self._prefix_path

    def get_deploy_mode(self) -> LinkMode:
        return self._deploy_mode

    def set_deploy_mode(self, mode: LinkMode) -> None:
        self._deploy_mode = mode
        self.save_paths()

    def set_prefix_path(self, path: Path | str | None) -> None:
        self._prefix_path = Path(path) if path else None
        self.save_paths()

    # -----------------------------------------------------------------------
    # Deployment
    # -----------------------------------------------------------------------

    def deploy(self, log_fn=None, mode: LinkMode = LinkMode.HARDLINK,
               profile: str = "default", progress_fn=None) -> None:
        """Deploy staged mods into <game root>/Modules/.

        Workflow:
          1. Move Modules/ → Modules_Core/  (vanilla backup)
          2. Transfer mod files listed in filemap.txt into Modules/
          3. Fill gaps with vanilla files from Modules_Core/
        (Root Folder deployment is handled by the GUI after this returns.)
        """
        _log = log_fn or (lambda _: None)

        if self._game_path is None:
            raise RuntimeError("Game path is not configured.")

        modules_dir = self._game_path / self.mods_dir
        filemap = self.get_effective_filemap_path()
        staging = self.get_effective_mod_staging_path()
        core = self.mods_dir + "_Core"

        if not filemap.is_file():
            raise RuntimeError(
                f"filemap.txt not found: {filemap}\n"
                "Run 'Build Filemap' before deploying."
            )

        _log(f"Step 1: Moving {modules_dir.name}/ → {core}/ ...")
        moved = move_to_core(modules_dir, log_fn=_log)
        _log(f"  Moved {moved} file(s) to {core}/.")
        modules_dir.mkdir(parents=True, exist_ok=True)

        _log(f"Step 2: Transferring mod files into {modules_dir} ({mode.name}) ...")
        profile_dir = self.get_profile_root() / "profiles" / profile
        per_mod_strip = load_per_mod_strip_prefixes(profile_dir)
        _sep_deploy = load_separator_deploy_paths(profile_dir)
        _sep_entries = read_modlist(profile_dir / "modlist.txt") if _sep_deploy else []
        per_mod_deploy = expand_separator_deploy_paths(_sep_deploy, _sep_entries) or None
        linked_mod, placed = deploy_filemap(
            filemap, modules_dir, staging,
            mode=mode,
            strip_prefixes=self.mod_folder_strip_prefixes,
            per_mod_strip_prefixes=per_mod_strip,
            per_mod_deploy_dirs=per_mod_deploy,
            log_fn=_log,
            progress_fn=progress_fn,
            core_dir=modules_dir.parent / (modules_dir.name + "_Core"),
        )
        _log(f"  Transferred {linked_mod} mod file(s).")

        _log(f"Step 3: Filling gaps with vanilla files from {core}/ ...")
        linked_core = deploy_core(modules_dir, placed, mode=mode, log_fn=_log)
        _log(f"  Transferred {linked_core} vanilla file(s).")

        _log(
            f"Deploy complete. "
            f"{linked_mod} mod + {linked_core} vanilla "
            f"= {linked_mod + linked_core} total file(s) in {modules_dir.name}/."
        )

    def restore(self, log_fn=None, progress_fn=None) -> None:
        """Restore Modules/ to vanilla: clear deployed mods and move Modules_Core/ back."""
        _log = log_fn or (lambda _: None)

        if self._game_path is None:
            raise RuntimeError("Game path is not configured.")

        modules_dir = self._game_path / self.mods_dir
        core = self.mods_dir + "_Core"
        core_dir = self._game_path / core

        _profile_dir = self._active_profile_dir
        _entries = read_modlist(_profile_dir / "modlist.txt") if _profile_dir else []
        cleanup_custom_deploy_dirs(_profile_dir, _entries, log_fn=_log)

        _log(f"Restore: clearing {modules_dir.name}/ and moving {core}/ back if present ...")
        restored = restore_data_core(
            modules_dir, core_dir=core_dir,
            overwrite_dir=self.get_effective_overwrite_path(), log_fn=_log
        )
        if restored > 0:
            _log(f"  Restored {restored} file(s). {core}/ removed.")
        _log("Restore complete.")
