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

  Physical deploy workflow:
    1. Deploy mod files to game root via deploy_filemap_to_root()
       (natives/, reframework/ land at game root with vanilla backup)
    2. Compute RE Engine filepath hashes for every deployed file
    3. Scan re_chunk_000.pak (and .patch_NNN.pak files) and zero matching entries
    4. Apply dinput8.dll DLL override to the Proton prefix

  Profile VFS workflow:
    1. Resolve loose files into the profile-private game view
    2. Apply the same required hash invalidation to the physical vanilla PAKs
    3. Keep the existing per-profile backup and game-root repair ledger

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
from Utils.vfs import ProfileVFSGameMixin
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


class ResidentEvilVillage(ProfileVFSGameMixin, BaseGame):

    profile_overridable_settings = (
        *BaseGame.profile_overridable_settings,
        *ProfileVFSGameMixin.vfs_profile_setting_keys,
    )

    # Loose files live only in the private view, but these games still require
    # transactional hash invalidation inside their physical vanilla PAKs.
    vfs_physical_game_mutation_note = (
        "loose files are private; required PAK invalidation is physical and "
        "restore-tracked"
    )

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

    def _deploy_loose_filemap(
        self,
        *,
        filemap: Path,
        destination: Path,
        staging: Path,
        profile_dir: Path,
        mode: LinkMode,
        per_mod_strip: dict[str, list[str]],
        log_fn,
        progress_fn=None,
        state_dir: Path | None = None,
        write_snapshot: bool = True,
    ) -> tuple[int, set[str]]:
        """Place loose files physically or into a private VFS build layer."""
        _log = log_fn or (lambda _: None)

        # RTX-updated RE2/RE3/RE7 need both a destination extension remap and
        # a converted TEX payload. Keep the generated source in profile-local
        # temporary storage until hardlink/copy placement has completed.
        tex_ext_remap = self.pak_hash_extension_remap
        tex_tmp_dir: str | None = None
        file_transform = None
        convert_count = [0]
        if tex_ext_remap:
            profile_dir.mkdir(parents=True, exist_ok=True)
            tex_tmp_dir = tempfile.mkdtemp(prefix="mm_tex_", dir=profile_dir)

            def _tex_transform(src_path: str, _dst_rel: str) -> str | None:
                src_lower = src_path.lower()
                for old_ext, new_ext in tex_ext_remap.items():
                    if src_lower.endswith(old_ext):
                        break
                else:
                    return None
                src_p = Path(src_path)
                if not tex_needs_conversion(src_p):
                    return None
                target_ext = int(new_ext.rsplit(".", 1)[-1])
                converted = (
                    Path(tex_tmp_dir)
                    / f"tex_{convert_count[0]}{new_ext}"
                )
                convert_count[0] += 1
                try:
                    if convert_tex_v10_to_v34(
                        src_p, converted, target_extension=target_ext,
                    ):
                        return str(converted)
                except OSError as exc:
                    if exc.errno == errno.ENOSPC:
                        raise RuntimeError(
                            "Not enough space in the profile directory to "
                            "convert TEX files.\nFree up space on the "
                            f"filesystem containing {profile_dir} and try "
                            "again."
                        ) from exc
                    raise
                return None

            file_transform = _tex_transform

        try:
            result = deploy_filemap_to_root(
                filemap,
                destination,
                staging,
                mode=mode,
                strip_prefixes=self.mod_folder_strip_prefixes,
                per_mod_strip_prefixes=per_mod_strip,
                log_fn=_log,
                progress_fn=progress_fn,
                path_remap=self.mod_deploy_path_remap or None,
                ext_remap=tex_ext_remap or None,
                file_transform=file_transform,
                state_dir=state_dir,
                write_snapshot=write_snapshot,
            )
        finally:
            # Symlink mode must retain converted sources. VFS always requests
            # hardlinks and therefore safely removes these temporary names.
            if tex_tmp_dir and mode is not LinkMode.SYMLINK:
                shutil.rmtree(tex_tmp_dir, ignore_errors=True)

        if tex_ext_remap and file_transform:
            _log(
                f"  Converted {convert_count[0]} TEX file(s) from pre-RTX "
                "to post-RTX format."
            )
        return result

    def _vfs_populate_data_layer(
        self,
        *,
        destination: Path,
        profile: str,
        filemap: Path,
        staging: Path,
        log_fn=None,
        progress_fn=None,
        **_unused,
    ) -> int:
        """Build the private loose-file root and retain its PAK hash paths."""
        profile_dir = self.get_profile_root() / "profiles" / profile
        metadata_dir = destination.parent / "re-invalidation-metadata"
        metadata_dir.mkdir(parents=True, exist_ok=True)
        try:
            linked, placed_lower = self._deploy_loose_filemap(
                filemap=filemap,
                destination=destination,
                staging=staging,
                profile_dir=profile_dir,
                mode=LinkMode.HARDLINK,
                per_mod_strip=load_per_mod_strip_prefixes(profile_dir),
                log_fn=log_fn,
                progress_fn=progress_fn,
                state_dir=metadata_dir,
                write_snapshot=False,
            )
        finally:
            shutil.rmtree(metadata_dir, ignore_errors=True)
        self._vfs_re_placed_lower = placed_lower
        return linked

    def _vfs_prepare_filemap(
        self, filemap: Path, _staging: Path, log_fn=None,
    ) -> set[str]:
        """Keep transformed/remapped Overwrite entries at one final path.

        The specialized callback above resolves every filemap owner, including
        ``[Overwrite]``. The generic builder must therefore not materialize
        those same upper files again under their original pre-remap names.
        Runtime-created upper files absent from the filemap remain unaffected.
        """
        entries: set[str] = set()
        with filemap.open(
            encoding="utf-8", errors="surrogateescape",
        ) as handle:
            for line in handle:
                line = line.rstrip("\n")
                if "\t" not in line:
                    continue
                relative, owner = line.split("\t", 1)
                if owner == "[Overwrite]":
                    entries.add(relative.replace("\\", "/").lower())
        return entries

    def _patch_pak_files(
        self,
        placed_lower: set[str],
        *,
        profile: str,
        view_root: Path | None = None,
        log_fn=None,
    ) -> int:
        """Apply physical invalidation and mirror it into copied view PAKs.

        The shadow normally hardlinks (or symlinks) vanilla PAKs, so editing
        the physical PAK is immediately visible in the view. On filesystems
        where neither link type is available, materialization falls back to a
        copy; patch that private copy too. Its disposable journal lives inside
        VFS state and never participates in physical Restore.
        """
        _log = log_fn or (lambda _: None)
        if not placed_lower or self._game_path is None:
            return 0

        _log("Step 2: Patching physical PAK files to allow loose-file loading ...")
        ext_remap = self.pak_hash_extension_remap

        def _remap_path(path: str) -> str:
            if ext_remap:
                for old_ext, new_ext in ext_remap.items():
                    if path.endswith(old_ext):
                        return path[:-len(old_ext)] + new_ext
            return path

        hashes = {hash_filepath(_remap_path(path)) for path in placed_lower}
        pak_files = find_pak_files(self._game_path)
        if not pak_files:
            _log("  [WARN] No re_chunk_000.pak found - PAK patching skipped.")
            return 0

        total_patched = 0
        private_backup_dir: Path | None = None
        if view_root is not None:
            from Utils.vfs import state_dir
            private_backup_dir = state_dir(self, profile) / "view-pak-patches"
        try:
            for index, pak in enumerate(pak_files):
                backup = self._backup_path_for_pak(pak, profile)
                total_patched += patch_pak_file(
                    pak, hashes, backup, log_fn=_log)
                # This physical ledger is intentionally retained as a failsafe
                # even in VFS mode; the Repair PAK Files wizard consumes it.
                if backup.exists():
                    update_root_manifest(
                        self._game_path, pak, backup, log_fn=_log)

                if view_root is None:
                    continue
                try:
                    pak_relative = pak.relative_to(self._game_path)
                except ValueError as exc:
                    raise RuntimeError(
                        f"RE Engine PAK is outside the configured game root: {pak}"
                    ) from exc
                view_pak = view_root / pak_relative
                if not view_pak.is_file():
                    raise RuntimeError(
                        "The private VFS view is missing a required RE Engine "
                        f"PAK: {pak_relative}"
                    )
                # For a hardlink/symlink this is an idempotent no-op because
                # the physical pass already zeroed the shared entry. A copied
                # base PAK receives the same invalidation here.
                patch_pak_file(
                    view_pak,
                    hashes,
                    private_backup_dir / f"{index}.json",
                    log_fn=_log,
                )
        finally:
            if private_backup_dir is not None:
                shutil.rmtree(private_backup_dir, ignore_errors=True)

        if total_patched == 0:
            _log(
                "  [INFO] No matching PAK entries found for deployed files.\n"
                "  This is expected if the mod only adds new files (not "
                "replacements),\n  or if the RE Engine path format needs "
                "adjustment."
            )
        else:
            suffix = "y" if total_patched == 1 else "ies"
            _log(
                f"  PAK patching complete - {total_patched} total "
                f"entr{suffix} invalidated."
            )
        return total_patched

    def _restore_pak_backups(self, patch_dirs, *, log_fn=None) -> int:
        """Restore PAK journals from the supplied profile directories."""
        _log = log_fn or (lambda _: None)
        restored_entries = 0
        for patches_dir in patch_dirs:
            if not patches_dir.is_dir():
                continue
            for backup_file in sorted(patches_dir.glob("*.json")):
                try:
                    saved = json.loads(
                        backup_file.read_text(encoding="utf-8"))
                    pak_path = Path(saved.get("pak", ""))
                except (json.JSONDecodeError, KeyError, OSError):
                    backup_file.unlink(missing_ok=True)
                    continue
                restored_entries += restore_pak_file(
                    pak_path, backup_file, log_fn=_log)
            try:
                patches_dir.rmdir()
            except OSError:
                pass
        return restored_entries

    def deploy(self, log_fn=None, mode: LinkMode = LinkMode.HARDLINK,
               profile: str = "default", progress_fn=None) -> None:
        """Deploy staged mods and apply the required physical PAK edits.

        Physical mode places loose files in the game root. Profile VFS keeps
        those files in its private resolved view. Both modes must zero matching
        entries in the real PAK archives so the engine falls back to loose
        files; every physical edit retains the established repair journals.

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

        if self.vfs_launch_enabled:
            _log(
                "Step 1: Building a private VFS view for loose RE Engine "
                "mod files ..."
            )
            self._vfs_re_placed_lower: set[str] = set()
            try:
                self._deploy_vfs(
                    profile=profile,
                    filemap=filemap,
                    staging=staging,
                    log_fn=_log,
                    progress_fn=progress_fn,
                )
                placed_lower = set(self._vfs_re_placed_lower)
                from Utils.vfs import effective_shadow_root
                self._patch_pak_files(
                    placed_lower,
                    profile=profile,
                    view_root=effective_shadow_root(self),
                    log_fn=_log,
                )
            except BaseException:
                # PAK patching happens only after the view is published. If a
                # later PAK fails, restore completed journals first and then
                # unpublish the loose-file view so no half-deploy survives.
                self._restore_pak_backups(
                    [self._pak_patches_dir(profile)], log_fn=_log)
                try:
                    from Utils.vfs import cleanup_deployment, has_deployment_state
                    if has_deployment_state(self):
                        cleanup_deployment(
                            self, preserve_upper=True, log_fn=_log)
                except Exception as cleanup_exc:
                    _log(
                        "  WARN: hybrid VFS rollback could not remove the "
                        f"private view: {cleanup_exc}"
                    )
                raise
            finally:
                try:
                    del self._vfs_re_placed_lower
                except AttributeError:
                    pass
            _log(
                f"Hybrid VFS deploy complete. {len(placed_lower)} loose "
                "file(s) are private; vanilla PAK invalidation is physical."
            )
            return

        _log(
            "Step 1: Deploying mod files to game root, backing up "
            "overwritten vanilla files ..."
        )
        linked_mod, placed_lower = self._deploy_loose_filemap(
            filemap=filemap,
            destination=self._game_path,
            staging=staging,
            profile_dir=profile_dir,
            mode=mode,
            per_mod_strip=per_mod_strip,
            log_fn=_log,
            progress_fn=progress_fn,
        )
        _log(f"  Deployed {linked_mod} mod file(s).")
        self._patch_pak_files(
            placed_lower, profile=profile, log_fn=_log)
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
        restored_entries = self._restore_pak_backups(
            self._all_pak_patch_dirs(), log_fn=_log)
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

        from Utils.vfs import cleanup_deployment, has_deployment_state
        if has_deployment_state(self):
            _log("Restore: removing the private loose-file VFS view ...")
            cleanup_deployment(self, preserve_upper=True, log_fn=_log)
            filemap_dir = self.get_effective_filemap_path().parent
            physical_state = (
                filemap_dir / "filemap_deployed.txt"
            ).is_file() or (
                filemap_dir / "filemap_backup"
            ).is_dir()
            if not physical_state:
                _log("Restore complete.")
                return
            _log(
                "Restore: a physical loose-file deployment also remains; "
                "restoring it now ..."
            )

        _log("Restore: removing mod files from game root and restoring vanilla backups ...")
        restore_filemap_from_root(
            self.get_effective_filemap_path(),
            self._game_path,
            log_fn=_log,
            restore_whitelist=self.restore_whitelist_matcher(),
        )
        _log("Restore complete.")
