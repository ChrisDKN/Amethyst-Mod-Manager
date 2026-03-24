"""
skyrim_se.py
Game handler for Skyrim Special Edition.

Mod structure:
  Mods install into <game_path>/Data/
  Staged mods live in Profiles/Skyrim Special Edition/mods/
"""

import shutil
from pathlib import Path

from Games.Bethesda.Bethesda import Fallout_3
from Games.base_game import WizardTool
from Utils.deploy import LinkMode, deploy_core, deploy_filemap, load_per_mod_strip_prefixes, load_separator_deploy_paths, expand_separator_deploy_paths, cleanup_custom_deploy_dirs, restore_data_core, move_to_core
from Utils.modlist import read_modlist


class SkyrimSE(Fallout_3):

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
    def steam_id(self) -> str:
        return "489830"

    @property
    def nexus_game_domain(self) -> str:
        return "skyrimspecialedition"

    @property
    def mod_required_top_level_folders(self) -> set[str]:
        # Skyrim SE subset — excludes Fallout-specific folders (f4se, materials,
        # tools, nvse, config, menus, fose) that Fallout_3 includes.
        return {
            "skse",
            "textures",
            "sound",
            "meshes",
            "mcm",
            "scripts",
            "interface",
            "lightplacer",
            "mapmarkers",
            "music",
            "nemesis_engine",
            "seq",
            "shadercache",
            "shaders",
            "grass",
            "video",
            "source",
            "calientetools",
            "data",
        }

    @property
    def mod_folder_strip_prefixes_post(self) -> set[str]:
        return {"data"}

    @property
    def loot_game_type(self) -> str:
        return "SkyrimSE"

    @property
    def loot_masterlist_url(self) -> str:
        return "https://raw.githubusercontent.com/loot/skyrimse/v0.21/masterlist.yaml"

    @property
    def frameworks(self) -> dict[str, str]:
        return {"Script Extender": "skse64_loader.exe"}

    @property
    def wizard_tools(self) -> list[WizardTool]:
        return [
            WizardTool(
                id="install_se_skyrimse",
                label="Install Script Extender (SKSE64)",
                description="Download and install SKSE64 into the game folder.",
                dialog_class_path="wizards.script_extender.ScriptExtenderWizard",
                extra={
                    "github_api_url": "https://api.github.com/repos/ianpatt/skse64/releases/latest",
                    "archive_keywords": ["skse64"],
                },
            ),
        ]

    # -----------------------------------------------------------------------
    # Deployment
    # -----------------------------------------------------------------------

    # The Skyrim SE AppData folder inside the Proton prefix where the game
    # reads plugins.txt from.
    _APPDATA_SUBPATH = Path("drive_c/users/steamuser/AppData/Local/Skyrim Special Edition")

    # ShaderCache must be a real copy in Data/ — hard links prevent the game
    # from writing to it.  We round-trip it through the overwrite folder.
    _SHADER_CACHE = "ShaderCache"

    @property
    def _script_extender_exe(self) -> str:
        return "skse64_loader.exe"

    def swap_launcher(self, log_fn) -> None:
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

    def _consolidate_mod_shadercaches(self, staging: Path, overwrite_dir: Path,
                                      log_fn) -> None:
        """Move ShaderCache folders from mod staging dirs into overwrite/.

        For each enabled mod that contains a ShaderCache folder:
          - Files not already present in overwrite/ShaderCache/ are copied there.
          - Files already present in overwrite/ShaderCache/ are left as-is
            (overwrite wins).
          - The mod's ShaderCache folder is then deleted.
        """
        if not staging.is_dir():
            return
        dst_root = overwrite_dir / self._SHADER_CACHE
        moved_any = False
        for mod_dir in staging.iterdir():
            if not mod_dir.is_dir():
                continue
            src = mod_dir / self._SHADER_CACHE
            if not src.is_dir():
                continue
            dst_root.mkdir(parents=True, exist_ok=True)
            for f in src.rglob("*"):
                if f.is_file():
                    rel = f.relative_to(src)
                    target = dst_root / rel
                    if not target.exists():
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(f, target)
            shutil.rmtree(src)
            log_fn(f"  Moved {mod_dir.name}/ShaderCache → overwrite/{self._SHADER_CACHE}/")
            moved_any = True
        if not moved_any:
            log_fn("  No mod ShaderCache folders found.")

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
        filemap       = self.get_effective_filemap_path()
        staging       = self.get_effective_mod_staging_path()
        overwrite_dir = self.get_effective_overwrite_path()

        if not data_dir.is_dir():
            raise RuntimeError(f"Data directory not found: {data_dir}")
        if not filemap.is_file():
            raise RuntimeError(
                f"filemap.txt not found: {filemap}\n"
                "Run 'Build Filemap' before deploying."
            )

        _log("Step 1: Saving ShaderCache to overwrite/ ...")
        self._shadercache_to_overwrite(data_dir, overwrite_dir, _log)

        _log("Step 1b: Consolidating mod ShaderCache folders into overwrite/ ...")
        self._consolidate_mod_shadercaches(staging, overwrite_dir, _log)

        _log("Step 2: Moving Data/ → Data_Core/ ...")
        moved = move_to_core(data_dir, log_fn=_log)
        _log(f"  Moved {moved} file(s) to Data_Core/.")

        _log(f"Step 3: Transferring mod files into Data/ ({mode.name}) ...")
        profile_dir = self.get_profile_root() / "profiles" / profile
        per_mod_strip = load_per_mod_strip_prefixes(profile_dir)
        _sep_deploy = load_separator_deploy_paths(profile_dir)
        _sep_entries = read_modlist(profile_dir / "modlist.txt") if _sep_deploy else []
        per_mod_deploy = expand_separator_deploy_paths(_sep_deploy, _sep_entries) or None
        _symlink_exts = set(self.plugin_extensions) if self._symlink_plugins else None
        linked_mod, placed = deploy_filemap(filemap, data_dir, staging,
                                            mode=mode,
                                            strip_prefixes=self.mod_folder_strip_prefixes,
                                            per_mod_strip_prefixes=per_mod_strip,
                                            per_mod_deploy_dirs=per_mod_deploy,
                                            log_fn=_log,
                                            progress_fn=progress_fn,
                                            symlink_exts=_symlink_exts)
        _log(f"  Transferred {linked_mod} mod file(s).")

        _log("Step 4: Filling gaps with vanilla files from Data_Core/ ...")
        linked_core = deploy_core(data_dir, placed, mode=mode, log_fn=_log)
        _log(f"  Transferred {linked_core} vanilla file(s).")

        _log("Step 5: Deploying ShaderCache as full copy ...")
        self._deploy_shadercache_from_overwrite(data_dir, overwrite_dir, _log)

        _log("Step 6: Symlinking plugins.txt into Proton prefix ...")
        self._symlink_plugins_txt(profile, _log)

        _log(
            f"Deploy complete. "
            f"{linked_mod} mod + {linked_core} vanilla "
            f"= {linked_mod + linked_core} total file(s) in Data/."
        )

    def restore(self, log_fn=None, progress_fn=None) -> None:
        """Restore Data/ to its vanilla state."""
        _log = log_fn or (lambda _: None)

        if self._game_path is None:
            raise RuntimeError("Game path is not configured.")

        data_dir      = self._game_path / "Data"
        staging       = self.get_effective_mod_staging_path()
        overwrite_dir = self.get_effective_overwrite_path()

        # Move ShaderCache back to overwrite/ before wiping Data/.
        _log("Restore: saving ShaderCache to overwrite/ ...")
        self._shadercache_to_overwrite(data_dir, overwrite_dir, _log)

        _log("Restore: removing plugins.txt symlink ...")
        self._remove_plugins_txt_symlink(_log)

        _log("Restore: restoring launcher ...")
        self._restore_launcher(_log)

        _profile_dir = self._active_profile_dir
        _entries = read_modlist(_profile_dir / "modlist.txt") if _profile_dir else []
        cleanup_custom_deploy_dirs(_profile_dir, _entries, log_fn=_log)

        _log("Restore: clearing Data/ and moving Data_Core/ back ...")
        try:
            restored = restore_data_core(
                data_dir,
                overwrite_dir=overwrite_dir,
                staging_root=staging,
                strip_prefixes=self.mod_folder_strip_prefixes,
                log_fn=_log,
            )
            _log(f"  Restored {restored} file(s). Data_Core/ removed.")
        except RuntimeError as e:
            _log(f"  Skipping data restore: {e}")

        _log("Restore complete.")
