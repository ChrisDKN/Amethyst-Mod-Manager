"""
skyrim_se.py
Game handler for Skyrim Special Edition.

Mod structure:
  Mods install into <game_path>/Data/
  Staged mods live in Profiles/Skyrim Special Edition/mods/
"""

import json
import shutil
from pathlib import Path

from Games.base_game import BaseGame, WizardTool
from Utils.deploy import LinkMode, deploy_core, deploy_filemap, load_per_mod_strip_prefixes, move_to_core, restore_data_core
from Utils.config_paths import get_profiles_dir
from Utils.steam_finder import find_prefix

_PROFILES_DIR = get_profiles_dir()


class SkyrimSE(BaseGame):

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
        return "Skyrim Special Edition"

    @property
    def game_id(self) -> str:
        return "skyrim_se"

    @property
    def exe_name(self) -> str:
        return "SkyrimSELauncher.exe"

    @property
    def plugin_extensions(self) -> list[str]:
        return [".esp", ".esl", ".esm"]

    @property
    def steam_id(self) -> str:
        return "489830"

    @property
    def nexus_game_domain(self) -> str:
        return "skyrimspecialedition"

    @property
    def loot_sort_enabled(self) -> bool:
        return True

    @property
    def loot_game_type(self) -> str:
        return "SkyrimSE"

    @property
    def loot_masterlist_url(self) -> str:
        return "https://raw.githubusercontent.com/loot/skyrimse/v0.21/masterlist.yaml"

    @property
    def wizard_tools(self) -> list[WizardTool]:
        return [
            WizardTool(
                id="install_se_skyrimse",
                label="Install Script Extender (SKSE64)",
                description="Download and install SKSE64 into the game folder.",
                dialog_class_path="wizards.script_extender.ScriptExtenderWizard",
                extra={
                    "download_url": "https://skse.silverlock.org/beta/skse64_2_02_06.7z",
                    "archive_keywords": ["skse64"],
                },
            ),
        ]

    # -----------------------------------------------------------------------
    # Paths
    # -----------------------------------------------------------------------

    def get_game_path(self) -> Path | None:
        return self._game_path

    def get_mod_data_path(self) -> Path | None:
        """Mods go into the Data/ subfolder of the Skyrim root directory."""
        if self._game_path is None:
            return None
        return self._game_path / "Data"

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
                "copy":    LinkMode.COPY,
            }.get(raw_mode, LinkMode.HARDLINK)
            raw_staging = data.get("staging_path", "")
            if raw_staging:
                self._staging_path = Path(raw_staging)
            self._validate_staging()
            # If prefix is missing or no longer valid, scan for it and persist
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
            LinkMode.COPY:    "copy",
        }.get(self._deploy_mode, "hardlink")
        data = {
            "game_path":    str(self._game_path)    if self._game_path    else "",
            "prefix_path":  str(self._prefix_path)  if self._prefix_path  else "",
            "deploy_mode":  mode_str,
            "staging_path": str(self._staging_path) if self._staging_path else "",
        }
        self._paths_file.write_text(
            json.dumps(data, indent=2), encoding="utf-8"
        )

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

    def set_staging_path(self, path: "Path | str | None") -> None:
        self._staging_path = Path(path) if path else None
        self.save_paths()

    def set_game_path(self, path: Path | str | None) -> None:
        self._game_path = Path(path) if path else None
        self.save_paths()

    # -----------------------------------------------------------------------
    # Deployment
    # -----------------------------------------------------------------------

    # The Skyrim SE AppData folder inside the Proton prefix where the game
    # reads plugins.txt from.
    _APPDATA_SUBPATH = Path("drive_c/users/steamuser/AppData/Local/Skyrim Special Edition")

    # ShaderCache must be a real copy in Data/ — hard links prevent the game
    # from writing to it.  We round-trip it through the overwrite folder.
    _SHADER_CACHE = "ShaderCache"

    def _plugins_txt_target(self) -> Path | None:
        """Return the in-prefix path where Skyrim SE expects plugins.txt."""
        if self._prefix_path is None:
            return None
        return self._prefix_path / self._APPDATA_SUBPATH / "plugins.txt"

    def _symlink_plugins_txt(self, profile: str, log_fn) -> None:
        """Symlink the active profile's plugins.txt into the Proton prefix."""
        _log = log_fn
        target = self._plugins_txt_target()
        if target is None:
            _log("  WARN: Prefix path not set — skipping plugins.txt symlink.")
            return

        source = self.get_profile_root() / "profiles" / profile / "plugins.txt"
        if not source.is_file():
            _log(f"  WARN: plugins.txt not found at {source} — skipping symlink.")
            return

        if target.exists() or target.is_symlink():
            target.unlink()

        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to(source)
        _log(f"  Linked plugins.txt → {target}")

    def _remove_plugins_txt_symlink(self, log_fn) -> None:
        """Remove the plugins.txt symlink from the Proton prefix on restore."""
        _log = log_fn
        target = self._plugins_txt_target()
        if target is None:
            return
        if target.is_symlink():
            target.unlink()
            _log("  Removed plugins.txt symlink from prefix.")

    def _swap_launcher(self, log_fn) -> None:
        """Replace SkyrimSELauncher.exe with skse64_loader.exe if present."""
        _log = log_fn
        if self._game_path is None:
            return
        skse = self._game_path / "skse64_loader.exe"
        if not skse.is_file():
            _log("  SKSE loader not found — skipping launcher swap.")
            return
        launcher = self._game_path / "SkyrimSELauncher.exe"
        backup   = self._game_path / "SkyrimSELauncher.bak"
        if launcher.is_file():
            launcher.rename(backup)
            _log("  Renamed SkyrimSELauncher.exe → SkyrimSELauncher.bak.")
        shutil.copy2(skse, launcher)
        _log("  Copied skse64_loader.exe → SkyrimSELauncher.exe.")

    def _restore_launcher(self, log_fn) -> None:
        """Reverse the SKSE launcher swap if a backup exists."""
        _log = log_fn
        if self._game_path is None:
            return
        backup   = self._game_path / "SkyrimSELauncher.bak"
        launcher = self._game_path / "SkyrimSELauncher.exe"
        if not backup.is_file():
            return
        if launcher.is_file():
            launcher.unlink()
        backup.rename(launcher)
        _log("  Restored SkyrimSELauncher.exe from .bak.")

    def _shadercache_to_overwrite(self, data_dir: Path, overwrite_dir: Path,
                                  log_fn) -> None:
        """Copy ShaderCache from Data/ into overwrite/, then delete the Data/ copy."""
        src = data_dir / self._SHADER_CACHE
        if not src.is_dir():
            return
        dst = overwrite_dir / self._SHADER_CACHE
        dst.mkdir(parents=True, exist_ok=True)
        for f in src.rglob("*"):
            if f.is_file():
                rel = f.relative_to(src)
                target = dst / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, target)
        shutil.rmtree(src)
        log_fn(f"  Saved ShaderCache → overwrite/{self._SHADER_CACHE}/")

    def _deploy_shadercache_from_overwrite(self, data_dir: Path,
                                           overwrite_dir: Path, log_fn) -> None:
        """Full-copy ShaderCache from overwrite/ into Data/ (never hard-linked)."""
        src = overwrite_dir / self._SHADER_CACHE
        if not src.is_dir():
            return
        dst = data_dir / self._SHADER_CACHE
        # Remove any hard-linked version deploy_filemap may have placed first.
        if dst.exists() or dst.is_symlink():
            shutil.rmtree(dst, ignore_errors=True)
        shutil.copytree(src, dst)
        log_fn(f"  Copied ShaderCache from overwrite/ → Data/ (full copy).")

    def deploy(self, log_fn=None, mode: LinkMode = LinkMode.HARDLINK,
               profile: str = "default", progress_fn=None) -> None:
        """Deploy staged mods into the game's Data directory.

        Workflow:
          1. Copy Data/ShaderCache → overwrite/ShaderCache (if present)
          2. Move Data/ → Data_Core/
          3. Transfer mod files listed in filemap.txt into Data/
          4. Fill gaps with vanilla files from Data_Core/
          5. Replace hard-linked ShaderCache with a full copy from overwrite/
        (Root Folder deployment is handled by the GUI after this returns.)
        """
        _log = log_fn or (lambda _: None)

        if self._game_path is None:
            raise RuntimeError("Game path is not configured.")

        data_dir      = self._game_path / "Data"
        filemap       = self.get_profile_root() / "filemap.txt"
        staging       = self.get_mod_staging_path()
        overwrite_dir = self.get_profile_root() / "overwrite"

        if not data_dir.is_dir():
            raise RuntimeError(f"Data directory not found: {data_dir}")
        if not filemap.is_file():
            raise RuntimeError(
                f"filemap.txt not found: {filemap}\n"
                "Run 'Build Filemap' before deploying."
            )

        _log("Step 1: Saving ShaderCache to overwrite/ ...")
        self._shadercache_to_overwrite(data_dir, overwrite_dir, _log)

        _log("Step 2: Moving Data/ → Data_Core/ ...")
        moved = move_to_core(data_dir, log_fn=_log)
        _log(f"  Moved {moved} file(s) to Data_Core/.")

        _log(f"Step 3: Transferring mod files into Data/ ({mode.name}) ...")
        profile_dir = self.get_profile_root() / "profiles" / profile
        per_mod_strip = load_per_mod_strip_prefixes(profile_dir)
        linked_mod, placed = deploy_filemap(filemap, data_dir, staging,
                                            mode=mode,
                                            strip_prefixes=self.mod_folder_strip_prefixes,
                                            per_mod_strip_prefixes=per_mod_strip,
                                            log_fn=_log,
                                            progress_fn=progress_fn)
        _log(f"  Transferred {linked_mod} mod file(s).")

        _log("Step 4: Filling gaps with vanilla files from Data_Core/ ...")
        linked_core = deploy_core(data_dir, placed, mode=mode, log_fn=_log)
        _log(f"  Transferred {linked_core} vanilla file(s).")

        _log("Step 5: Deploying ShaderCache as full copy ...")
        self._deploy_shadercache_from_overwrite(data_dir, overwrite_dir, _log)

        _log("Step 6: Symlinking plugins.txt into Proton prefix ...")
        self._symlink_plugins_txt(profile, _log)

        _log("Step 7: Swapping launcher for SKSE ...")
        self._swap_launcher(_log)

        _log(
            f"Deploy complete. "
            f"{linked_mod} mod + {linked_core} vanilla "
            f"= {linked_mod + linked_core} total file(s) in Data/."
        )

    def restore(self, log_fn=None) -> None:
        """Restore Data/ to its vanilla state."""
        _log = log_fn or (lambda _: None)

        if self._game_path is None:
            raise RuntimeError("Game path is not configured.")

        data_dir      = self._game_path / "Data"
        staging       = self.get_mod_staging_path()
        overwrite_dir = self.get_profile_root() / "overwrite"

        # Move ShaderCache back to overwrite/ before wiping Data/.
        _log("Restore: saving ShaderCache to overwrite/ ...")
        self._shadercache_to_overwrite(data_dir, overwrite_dir, _log)

        _log("Restore: removing plugins.txt symlink ...")
        self._remove_plugins_txt_symlink(_log)

        _log("Restore: restoring launcher ...")
        self._restore_launcher(_log)

        _log("Restore: clearing Data/ and moving Data_Core/ back ...")
        try:
            restored = restore_data_core(data_dir, log_fn=_log)
            _log(f"  Restored {restored} file(s). Data_Core/ removed.")
        except RuntimeError as e:
            _log(f"  Skipping data restore: {e}")

        _log("Restore complete.")
