"""
resident_evil_village.py
Game handler for Resident Evil Village (RE8).

Base class for RE Engine games that use PAK archive invalidation - the other
handlers in RE_Engine_Invalidation/ subclass this one.

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

import errno
import json
import shutil
import tempfile
from pathlib import Path

from Games.base_game import BaseGame
from Utils.deploy import (
    LinkMode,
    cleanup_custom_deploy_dirs,
    deploy_filemap_to_root,
    load_per_mod_strip_prefixes,
    restore_filemap_from_root,
)
from Utils.modlist import read_modlist
from Utils.config_paths import get_profiles_dir
from Utils.re_pak_patcher import (
    find_pak_files,
    hash_filepath,
    patch_pak_file,
    restore_from_root_manifest,
    restore_pak_file,
    update_root_manifest,
)
from Utils.steam_finder import parse_acf_beta_key
from Utils.tex_convert import convert_tex_v10_to_v34, tex_needs_conversion

_PROFILES_DIR = get_profiles_dir()


class ResidentEvilVillage(BaseGame):

    # Remaps that only apply to the current (post-RT-update) game build.
    # RE2/RE3/RE7 set these; on the dx11_non-rt Steam beta branch the legacy
    # engine reads natives/x64 and .tex.10 directly, so both are skipped (GH#365).
    _rt_path_remap: dict[str, str] = {}
    _rt_ext_remap: dict[str, str] = {}
    _NON_RT_BETA_KEY = "dx11_non-rt"

    def __init__(self):
        self._game_path: Path | None = None
        self._prefix_path: Path | None = None
        self._deploy_mode: LinkMode = LinkMode.HARDLINK
        self._staging_path: Path | None = None
        self._beta_branch_cache: tuple[tuple, str | None] | None = None
        self.load_paths()

    # -----------------------------------------------------------------------
    # Steam beta branch detection
    # -----------------------------------------------------------------------

    def _steam_beta_branch(self) -> str | None:
        """Installed Steam beta branch from the appmanifest, None for default."""
        if self._game_path is None:
            return None
        acf = self._game_path.parent.parent / f"appmanifest_{self.steam_id}.acf"
        try:
            st = acf.stat()
        except OSError:
            return None
        cache_key = (str(acf), st.st_mtime_ns, st.st_size)
        if self._beta_branch_cache and self._beta_branch_cache[0] == cache_key:
            return self._beta_branch_cache[1]
        branch = parse_acf_beta_key(acf)
        self._beta_branch_cache = (cache_key, branch)
        return branch

    def _is_non_rt_branch(self) -> bool:
        """True when the legacy pre-RT-update build is installed."""
        return self._steam_beta_branch() == self._NON_RT_BETA_KEY

    @property
    def mod_deploy_path_remap(self) -> dict[str, str]:
        if self._rt_path_remap and self._is_non_rt_branch():
            return {}
        return dict(self._rt_path_remap)

    @property
    def pak_hash_extension_remap(self) -> dict[str, str]:
        if self._rt_ext_remap and self._is_non_rt_branch():
            return {}
        return dict(self._rt_ext_remap)

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

    @property
    def wizard_tools(self):
        from Games.base_game import WizardTool
        return self._base_wizard_tools() + [
            WizardTool(
                id="re_pak_restore",
                label="Repair PAK files",
                description=(
                    "Restore vanilla PAK entries from the failsafe manifest in the "
                    "game root. Use if the game won't load after mods were removed."
                ),
                dialog_class_path="wizards.re_pak_restore.RePakRestoreWizard",
            ),
        ]

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

    def _all_pak_patch_dirs(self) -> list[Path]:
        """Every profile's pak_patches/ dir that currently holds backups.

        Restore must find the PAK backups regardless of which profile was
        active when deploy patched the PAKs.  Deploy writes backups under the
        active profile (e.g. ``profiles/MyProfile/pak_patches/``); a restore
        that only looked in ``profiles/default/`` would silently leave every
        zeroed PAK entry invalidated forever (vanilla files become
        unloadable → black screen).  Scanning all profiles also rescues
        backups orphaned by older builds that had that exact bug.
        """
        profiles_root = self.get_profile_root() / "profiles"
        dirs: list[Path] = []
        seen: set[Path] = set()
        # Prefer the active/last-deployed profile first so its backups win.
        preferred = None
        if self._active_profile_dir is not None:
            preferred = self._active_profile_dir / "pak_patches"
        for cand in (preferred, *sorted(profiles_root.glob("*/pak_patches"))):
            if cand is None:
                continue
            rcand = cand.resolve()
            if rcand in seen or not cand.is_dir():
                continue
            seen.add(rcand)
            dirs.append(cand)
        return dirs

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

        if (self._rt_path_remap or self._rt_ext_remap) and self._is_non_rt_branch():
            _log(f"  Steam beta branch '{self._NON_RT_BETA_KEY}' detected - "
                 "legacy build: keeping natives/x64 paths and .tex.10 textures.")

        _log("Step 1: Deploying mod files to game root, backing up overwritten vanilla files ...")

        # Set up TEX conversion for RTX-updated games (RE2/RE3/RE7).
        # When pak_hash_extension_remap is set, .tex.10 files need both a
        # header conversion (v10→v34 format) and extension rename on deploy.
        _tex_ext_remap = self.pak_hash_extension_remap  # e.g. {".tex.10": ".tex.34"}
        _tex_tmp_dir: str | None = None
        _file_transform = None
        if _tex_ext_remap:
            _tex_tmp_dir = tempfile.mkdtemp(prefix="mm_tex_", dir=profile_dir)
            _convert_count = [0]

            def _tex_transform(src_path: str, dst_rel: str) -> str | None:
                """Convert .tex.10 files to .tex.34 format via a temp copy."""
                src_lower = src_path.lower()
                for old_ext, new_ext in _tex_ext_remap.items():
                    if src_lower.endswith(old_ext):
                        break
                else:
                    return None
                src_p = Path(src_path)
                if not tex_needs_conversion(src_p):
                    return None
                target_ext = int(new_ext.rsplit(".", 1)[-1])
                converted = Path(_tex_tmp_dir) / f"tex_{_convert_count[0]}{new_ext}"
                _convert_count[0] += 1
                try:
                    if convert_tex_v10_to_v34(src_p, converted, target_extension=target_ext):
                        return str(converted)
                except OSError as e:
                    if e.errno == errno.ENOSPC:
                        raise RuntimeError(
                            f"Not enough space in the profile directory to convert TEX files.\n"
                            f"Free up space on the filesystem containing {profile_dir} and try again."
                        ) from e
                    raise
                return None

            _file_transform = _tex_transform

        try:
            linked_mod, placed_lower = deploy_filemap_to_root(
                filemap, self._game_path, staging,
                mode=mode,
                strip_prefixes=self.mod_folder_strip_prefixes,
                per_mod_strip_prefixes=per_mod_strip,
                log_fn=_log,
                progress_fn=progress_fn,
                path_remap=self.mod_deploy_path_remap or None,
                ext_remap=_tex_ext_remap or None,
                file_transform=_file_transform,
            )
        finally:
            # Clean up temp converted files (safe for hardlink/copy since the
            # deployed files are independent copies; skip for symlink mode
            # since symlinks would point back to these temp files).
            if _tex_tmp_dir and mode is not LinkMode.SYMLINK:
                shutil.rmtree(_tex_tmp_dir, ignore_errors=True)

        if _tex_ext_remap and _file_transform:
            _log(f"  Converted {_convert_count[0]} TEX file(s) from pre-RTX to post-RTX format.")
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
                _log("  [WARN] No re_chunk_000.pak found - PAK patching skipped.")
            else:
                total_patched = 0
                for pak in pak_files:
                    backup = self._backup_path_for_pak(pak, profile)
                    count = patch_pak_file(pak, hashes, backup, log_fn=_log)
                    total_patched += count
                    # Mirror the backup into the game root as a failsafe so the
                    # PAKs can be repaired even if Profiles/ is wiped while
                    # deployed (see the PAK-restore wizard).
                    if backup.exists():
                        update_root_manifest(self._game_path, pak, backup, log_fn=_log)
                if total_patched == 0:
                    _log(
                        "  [INFO] No matching PAK entries found for deployed files.\n"
                        "  This is expected if the mod only adds new files (not replacements),\n"
                        "  or if the RE Engine path format needs adjustment."
                    )
                else:
                    _log(f"  PAK patching complete - {total_patched} total entr{'y' if total_patched == 1 else 'ies'} invalidated.")

        _log(f"Deploy complete. {linked_mod} mod file(s) deployed.")

    def restore(self, log_fn=None, progress_fn=None) -> None:
        """Remove deployed mod files, restore vanilla files and PAK entries."""
        _log = log_fn or (lambda _: None)

        if self._game_path is None:
            raise RuntimeError("Game path is not configured.")

        _profile_dir = self._active_profile_dir
        _entries = read_modlist(_profile_dir / "modlist.txt") if _profile_dir else []
        cleanup_custom_deploy_dirs(_profile_dir, _entries, log_fn=_log)

        # Restore PAK entries from every profile's pak_patches/ backups.
        # Deploy writes backups under whichever profile was active, so restore
        # must scan all profiles - looking only in default/ would permanently
        # strand zeroed entries patched under a non-default profile.
        _log("Restore: restoring PAK entries from backups ...")
        restored_entries = 0
        for patches_dir in self._all_pak_patch_dirs():
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
        # If the per-profile backups were missing (e.g. stranded by an older
        # build, or Profiles/ partially wiped) fall back to the game-root
        # failsafe manifest so the PAKs are still healed.
        if restored_entries == 0:
            manifest_restored = restore_from_root_manifest(self._game_path, log_fn=_log)
            if manifest_restored:
                _log(f"  Restored {manifest_restored} entr"
                     f"{'y' if manifest_restored == 1 else 'ies'} from game-root manifest.")
                restored_entries += manifest_restored

        if restored_entries == 0:
            _log("  No PAK backups found (nothing to restore).")

        # NB the game-root manifest (.mm_pak_restore.json) is intentionally
        # kept - it is an append-only ledger of every entry the manager has
        # ever invalidated, so the "Repair PAK files" wizard can always re-heal
        # the PAKs even if a future deploy/restore leaves them stranded.

        _log("Restore: removing mod files from game root and restoring vanilla backups ...")
        restore_filemap_from_root(
            self.get_effective_filemap_path(),
            self._game_path,
            log_fn=_log,
            restore_whitelist=self.restore_whitelist_matcher(),
        )
        _log("Restore complete.")