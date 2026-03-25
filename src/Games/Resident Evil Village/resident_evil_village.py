"""
resident_evil_village.py
Game handler for Resident Evil Village (RE8).

Mod structure:
  Mods install into the game root (like Cyberpunk 2077 / RE Requiem).
  Staged mods live in Profiles/Resident Evil Village/mods/

  Mod authors ship with a reframework/ and/or natives/ top-level folder.
  Both are accepted as required top-level folders.

  Unlike RE Requiem, RE Village's REFramework does NOT support loading loose
  files from natives/ automatically.  Instead, we patch the game's PAK files:
  for every deployed mod file we zero out its 8-byte hash entry in the PAK
  so the engine can't find it there and falls back to the loose file on disk.

  Original PAK hash bytes are saved to:
    Profiles/Resident Evil Village/<profile>/pak_patches/<pak_stem>.json
  and restored on undeploy.

  Deploy workflow:
    1. Deploy mod files to game root via deploy_filemap_to_root()
       (natives/, reframework/ land at game root with vanilla backup)
    2. Compute RE Engine filepath hashes for every deployed file
    3. Scan re_chunk_000.pak (and .patch_NNN.pak files) and zero matching entries
    4. Apply dinput8.dll DLL override to the Proton prefix

  Restore workflow:
    1. Restore original PAK hash bytes from pak_patches/ backups
    2. Remove mod files from game root and restore vanilla backups
    3. Clean pak_patches/ directory
"""

import json
from pathlib import Path

from Games.base_game import BaseGame
from Utils.deploy import (
    LinkMode,
    apply_wine_dll_overrides,
    cleanup_custom_deploy_dirs,
    deploy_filemap_to_root,
    load_per_mod_strip_prefixes,
    restore_filemap_from_root,
)
from Utils.modlist import read_modlist
from Utils.config_paths import get_profiles_dir
from Utils.steam_finder import find_prefix
from Utils.re_pak_patcher import find_pak_files, hash_filepath, patch_pak_file, restore_pak_file

_PROFILES_DIR = get_profiles_dir()


class ResidentEvilVillage(BaseGame):

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
        return "Resident Evil Village"

    @property
    def game_id(self) -> str:
        return "resident_evil_village"

    @property
    def exe_name(self) -> str:
        return "re8.exe"

    @property
    def steam_id(self) -> str:
        return "1196590"

    @property
    def nexus_game_domain(self) -> str:
        return "residentevilvillage"

    @property
    def mod_required_top_level_folders(self) -> set[str]:
        return {"reframework", "natives"}

    @property
    def mod_install_as_is_if_no_match(self) -> bool:
        return True

    @property
    def mod_auto_strip_until_required(self) -> bool:
        return True

    @property
    def conflict_ignore_filenames(self) -> set[str]:
        return {"modinfo.ini", "readme.txt", "*.png", "*.jpg"}

    @property
    def wine_dll_overrides(self) -> dict[str, str]:
        return {"dinput8": "native,builtin"}

    @property
    def reshade_dll(self) -> str:
        return "dxgi.dll"

    @property
    def frameworks(self) -> dict[str, str]:
        return {"ReFramework": "dinput8.dll"}

    @property
    def mod_supports_bundles(self) -> bool:
        return True

    # -----------------------------------------------------------------------
    # Paths
    # -----------------------------------------------------------------------

    def get_game_path(self) -> Path | None:
        return self._game_path

    def get_mod_data_path(self) -> Path | None:
        return self._game_path

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
    # PAK patch helpers
    # -----------------------------------------------------------------------

    def _pak_patches_dir(self, profile: str = "default") -> Path:
        return self.get_profile_root() / "profiles" / profile / "pak_patches"

    def _backup_path_for_pak(self, pak_path: Path, profile: str = "default") -> Path:
        return self._pak_patches_dir(profile) / (pak_path.name + ".json")

    # -----------------------------------------------------------------------
    # Deployment
    # -----------------------------------------------------------------------

    def deploy(self, log_fn=None, mode: LinkMode = LinkMode.HARDLINK,
               profile: str = "default", progress_fn=None) -> None:
        """Deploy staged mods into the game root and patch PAK files.

        Workflow:
          1. Deploy mod files to game root, backing up any overwritten vanilla files
          2. Compute RE Engine filepath hashes for all deployed files
          3. Find PAK files and zero out entries matching the deployed hashes
          4. Apply dinput8.dll DLL override to the Proton prefix
        (Root Folder deployment is handled by the GUI after this returns.)
        """
        _log = log_fn or (lambda _: None)

        if self._game_path is None:
            raise RuntimeError("Game path is not configured.")

        filemap = self.get_effective_filemap_path()
        staging = self.get_effective_mod_staging_path()

        if not filemap.is_file():
            raise RuntimeError(
                f"filemap.txt not found: {filemap}\n"
                "Run 'Build Filemap' before deploying."
            )

        profile_dir = self.get_profile_root() / "profiles" / profile
        per_mod_strip = load_per_mod_strip_prefixes(profile_dir)

        _log("Step 1: Deploying mod files to game root, backing up overwritten vanilla files ...")
        linked_mod, placed_lower = deploy_filemap_to_root(
            filemap, self._game_path, staging,
            mode=mode,
            strip_prefixes=self.mod_folder_strip_prefixes,
            per_mod_strip_prefixes=per_mod_strip,
            log_fn=_log,
            progress_fn=progress_fn,
            path_remap=self.mod_deploy_path_remap or None,
        )
        _log(f"  Deployed {linked_mod} mod file(s).")

        if placed_lower:
            _log("Step 2: Patching PAK files to allow loose-file loading ...")
            _ext_remap = self.pak_hash_extension_remap
            def _remap_path(p: str) -> str:
                if _ext_remap:
                    for old_ext, new_ext in _ext_remap.items():
                        if p.endswith(old_ext):
                            return p[:-len(old_ext)] + new_ext
                return p
            hashes: set[tuple[int, int]] = {hash_filepath(_remap_path(p)) for p in placed_lower}
            pak_files = find_pak_files(self._game_path)
            if not pak_files:
                _log("  [WARN] No re_chunk_000.pak found — PAK patching skipped.")
            else:
                total_patched = 0
                for pak in pak_files:
                    backup = self._backup_path_for_pak(pak, profile)
                    count = patch_pak_file(pak, hashes, backup, log_fn=_log)
                    total_patched += count
                if total_patched == 0:
                    _log(
                        "  [INFO] No matching PAK entries found for deployed files.\n"
                        "  This is expected if the mod only adds new files (not replacements),\n"
                        "  or if the RE Engine path format needs adjustment."
                    )
                else:
                    _log(f"  PAK patching complete — {total_patched} total entr{'y' if total_patched == 1 else 'ies'} invalidated.")

        if self._prefix_path and self._prefix_path.is_dir():
            _log("Step 3: Applying DLL overrides to Proton prefix ...")
            apply_wine_dll_overrides(self._prefix_path, self.wine_dll_overrides, log_fn=_log)

        _log(f"Deploy complete. {linked_mod} mod file(s) deployed.")

    def restore(self, log_fn=None, progress_fn=None) -> None:
        """Remove deployed mod files, restore vanilla files and PAK entries."""
        _log = log_fn or (lambda _: None)

        if self._game_path is None:
            raise RuntimeError("Game path is not configured.")

        _profile_dir = self._active_profile_dir
        _entries = read_modlist(_profile_dir / "modlist.txt") if _profile_dir else []
        cleanup_custom_deploy_dirs(_profile_dir, _entries, log_fn=_log)

        # Restore PAK entries from all backup files found under pak_patches/
        _log("Restore: restoring PAK entries from backups ...")
        patches_dir = self._pak_patches_dir()
        restored_entries = 0
        if patches_dir.exists():
            for backup_file in sorted(patches_dir.glob("*.json")):
                try:
                    saved = json.loads(backup_file.read_text(encoding="utf-8"))
                    pak_path = Path(saved.get("pak", ""))
                except (json.JSONDecodeError, KeyError, OSError):
                    backup_file.unlink(missing_ok=True)
                    continue
                restored_entries += restore_pak_file(pak_path, backup_file, log_fn=_log)
            # Clean up the directory if empty
            try:
                patches_dir.rmdir()
            except OSError:
                pass
        if restored_entries == 0:
            _log("  No PAK backups found (nothing to restore).")

        _log("Restore: removing mod files from game root and restoring vanilla backups ...")
        restore_filemap_from_root(
            self.get_effective_filemap_path(),
            self._game_path,
            log_fn=_log,
        )
        _log("Restore complete.")


class ResidentEvil4(ResidentEvilVillage):
    """Resident Evil 4 Remake (2023) — identical workflow to RE Village."""

    @property
    def name(self) -> str:
        return "Resident Evil 4"

    @property
    def game_id(self) -> str:
        return "resident_evil_4"

    @property
    def exe_name(self) -> str:
        return "re4.exe"

    @property
    def steam_id(self) -> str:
        return "2050650"

    @property
    def nexus_game_domain(self) -> str:
        return "residentevil42023"


class ResidentEvil3(ResidentEvilVillage):
    """Resident Evil 3 Remake (2020).

    Uses natives/STM/ instead of natives/x64/ — mods ship with x64 paths
    but must be deployed to STM.
    """

    @property
    def name(self) -> str:
        return "Resident Evil 3"

    @property
    def game_id(self) -> str:
        return "resident_evil_3"

    @property
    def exe_name(self) -> str:
        return "re3.exe"

    @property
    def steam_id(self) -> str:
        return "952060"

    @property
    def nexus_game_domain(self) -> str:
        return "residentevil32020"

    @property
    def mod_deploy_path_remap(self) -> dict[str, str]:
        return {"natives/x64/": "natives/STM/"}

    @property
    def pak_hash_extension_remap(self) -> dict[str, str]:
        return {".tex.10": ".tex.34"}


class ResidentEvil2(ResidentEvilVillage):
    """Resident Evil 2 Remake (2019).

    Uses natives/STM/ instead of natives/x64/ — mods ship with x64 paths
    but must be deployed to STM.
    """

    @property
    def name(self) -> str:
        return "Resident Evil 2"

    @property
    def game_id(self) -> str:
        return "resident_evil_2"

    @property
    def exe_name(self) -> str:
        return "re2.exe"

    @property
    def steam_id(self) -> str:
        return "883710"

    @property
    def nexus_game_domain(self) -> str:
        return "residentevil22019"

    @property
    def mod_deploy_path_remap(self) -> dict[str, str]:
        return {"natives/x64/": "natives/STM/"}

    @property
    def pak_hash_extension_remap(self) -> dict[str, str]:
        return {".tex.10": ".tex.34"}


class ResidentEvil7(ResidentEvilVillage):
    """Resident Evil 7: Biohazard — identical workflow to RE Village."""

    @property
    def name(self) -> str:
        return "Resident Evil 7"

    @property
    def game_id(self) -> str:
        return "resident_evil_7"

    @property
    def exe_name(self) -> str:
        return "re7.exe"

    @property
    def steam_id(self) -> str:
        return "418370"

    @property
    def nexus_game_domain(self) -> str:
        return "residentevil7"
    
    @property
    def mod_deploy_path_remap(self) -> dict[str, str]:
        return {"natives/x64/": "natives/STM/"}

    @property
    def pak_hash_extension_remap(self) -> dict[str, str]:
        return {".tex.10": ".tex.34"}
