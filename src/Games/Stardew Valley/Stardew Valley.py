"""
Stardew Valley.py
Game handler for Stardew Valley.

Mod structure:
  Mods install into <game_path>/Mods/
  Staged mods live in Profiles/Stardew Valley/mods/

  Root_Folder/ files deploy straight to the game install root (handled by GUI).
"""

import json
import re
from pathlib import Path

from Games.base_game import BaseGame, WizardTool
from Utils.vfs import ProfileVFSGameMixin
from Utils.deploy import LinkMode, deploy_core, deploy_filemap, load_per_mod_strip_prefixes, load_separator_deploy_paths, expand_separator_deploy_paths, cleanup_custom_deploy_dirs, move_to_core, restore_data_core
from Utils.modlist import read_modlist
from Utils.config_paths import get_profiles_dir

_PROFILES_DIR = get_profiles_dir()

# Split-texture filename AT loads via a case-sensitive GetFiles("texture_*.png").
_AT_SPLIT_PNG_RE = re.compile(r"^texture_\d+\.png$", re.IGNORECASE)

class StardewValley(ProfileVFSGameMixin, BaseGame):

    # The native launcher and SMAPI resolve their payload relative to cwd/the
    # executable, so the complete profile shadow can be launched directly.
    vfs_direct_shadow_launch = True
    native_steam_client_required = True

    profile_overridable_settings = (
        *BaseGame.profile_overridable_settings,
        *ProfileVFSGameMixin.vfs_profile_setting_keys,
    )

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
        return "Stardew Valley"

    @property
    def game_id(self) -> str:
        return "Stardew_Valley"

    @property
    def exe_name(self) -> str:
        return "StardewValley"

    @property
    def steam_id(self) -> str:
        return "413150"

    @property
    def nexus_game_domain(self) -> str:
        return "stardewvalley"

    @property
    def mods_dir(self) -> str:
        return "Mods"

    def runtime_snapshot_exclude_dirs(self) -> set[str] | None:
        # Mods/ is reverted via its _Core backup; capture only files outside it.
        return {self.mods_dir.split("/")[0]}

    @staticmethod
    def filegraph_manifest_spelling(
        mod_root: Path, entries: list[tuple[str, str]],
    ) -> dict[str, str]:
        """Apply Alternative Textures' case-sensitive spelling before resolve."""
        at_prefixes: set[str] = set()
        for raw_relative, staged_relative in entries:
            if staged_relative.rsplit("/", 1)[-1].lower() != "manifest.json":
                continue
            try:
                data = json.loads(
                    (mod_root / raw_relative).read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                continue
            content_pack = data.get("ContentPackFor")
            unique_id = (content_pack.get("UniqueID")
                         if isinstance(content_pack, dict) else None)
            if unique_id == "PeacefulEnd.AlternativeTextures":
                at_prefixes.add(
                    staged_relative.rsplit("/", 1)[0].lower()
                    if "/" in staged_relative else "")

        replacements: dict[str, str] = {}
        for _raw_relative, staged_relative in entries:
            parts = staged_relative.split("/")
            lower = staged_relative.lower()
            matching = next((
                prefix for prefix in at_prefixes
                if ((not prefix and "/" in lower)
                    or (prefix and lower.startswith(prefix + "/")))
            ), None)
            if matching is None:
                continue
            texture_index = 0 if matching == "" else matching.count("/") + 1
            if (len(parts) < texture_index + 3
                    or parts[texture_index].lower() != "textures"):
                continue
            parts[texture_index] = "Textures"
            basename = parts[-1]
            if (basename.lower() in ("texture.json", "texture.png")
                    or _AT_SPLIT_PNG_RE.match(basename)):
                parts[-1] = basename.lower()
            replacements[lower] = "/".join(parts)
        return replacements

    @property
    def mod_folder_strip_prefixes(self) -> set[str]:
        return {"mods"}
    
    @property
    def plugin_extensions(self) -> list[str]:
        return []

    @property
    def loot_sort_enabled(self) -> bool:
        return False

    @property
    def normalize_folder_case(self) -> bool:
        return False

    @property
    def mod_staging_requires_subdir(self) -> bool:
        return True

    @property
    def frameworks(self) -> dict[str, str]:
        return {"SMAPI": "StardewModdingAPI.dll","Content Patcher":"Mods/ContentPatcher/ContentPatcher.dll"}

    @property
    def wizard_tools(self) -> list[WizardTool]:
        return self._base_wizard_tools()

    @property
    def loot_game_type(self) -> str:
        return ""

    @property
    def loot_masterlist_url(self) -> str:
        return ""

    # -----------------------------------------------------------------------
    # Paths
    # -----------------------------------------------------------------------

    def get_game_path(self) -> Path | None:
        return self._game_path

    def get_mod_data_path(self) -> Path | None:
        """Mods go into Mods/ inside the game directory."""
        if self._game_path is None:
            return None
        return self._game_path / self.mods_dir

    def get_mod_staging_path(self) -> Path:
        if self._staging_path is not None:
            return self._staging_path / "mods"
        return _PROFILES_DIR / self.name / "mods"

    def set_staging_path(self, path: "Path | str | None") -> None:
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
        """Deploy staged mods into Mods/.

        Workflow:
          1. Move Mods/ → Mods_Core/  (vanilla backup)
          2. Transfer mod files listed in filemap.txt into Mods/
          3. Fill gaps with vanilla files from Mods_Core/
        (Root Folder deployment is handled by the GUI after this returns.)
        """
        _log = log_fn or (lambda _: None)

        if self._game_path is None:
            raise RuntimeError("Game path is not configured.")

        plugins_dir = self._game_path / self.mods_dir
        filemap     = self.get_effective_filemap_path()
        staging     = self.get_effective_mod_staging_path()
        core        = self.mods_dir + "_Core"

        from Utils.filegraph_deploy import input_ready
        if not input_ready():
            raise RuntimeError(
                f"filemap.txt not found: {filemap}\n"
                "Run 'Build Filemap' before deploying."
            )

        if self.vfs_launch_enabled:
            return self._deploy_vfs(
                profile=profile,
                filemap=filemap,
                staging=staging,
                log_fn=_log,
                progress_fn=progress_fn,
            )

        _log(f"Step 1: Moving {plugins_dir.name}/ → {core}/ ...")
        move_to_core(plugins_dir, log_fn=_log)
        _log(f"  Backed up existing files → {core}/.")
        plugins_dir.mkdir(parents=True, exist_ok=True)

        _log(f"Step 2: Transferring mod files into {plugins_dir} ({mode.name}) ...")
        profile_dir = self.get_profile_root() / "profiles" / profile
        per_mod_strip = load_per_mod_strip_prefixes(profile_dir)
        _sep_deploy = load_separator_deploy_paths(profile_dir)
        _sep_entries = read_modlist(profile_dir / "modlist.txt") if _sep_deploy else []
        per_mod_deploy = expand_separator_deploy_paths(_sep_deploy, _sep_entries) or None
        _orphan_configs = self._prepare_mod_filemap(
            filemap, staging, log_fn=_log)
        linked_mod, placed = deploy_filemap(filemap, plugins_dir, staging,
                                            mode=mode,
                                            strip_prefixes=self.mod_folder_strip_prefixes,
                                            per_mod_strip_prefixes=per_mod_strip,
                                            per_mod_deploy_dirs=per_mod_deploy,
                                            exclude=_orphan_configs or None,
                                            log_fn=_log,
                                            progress_fn=progress_fn,
                                            core_dir=plugins_dir.parent / (plugins_dir.name + "_Core"))
        _log(f"  Transferred {linked_mod} mod file(s).")

        _log(f"Step 3: Filling gaps with vanilla files from {core}/ ...")
        linked_core = deploy_core(plugins_dir, placed, mode=mode, log_fn=_log)
        _log(f"  Transferred {linked_core} vanilla file(s).")

        _log(
            f"Deploy complete. "
            f"{linked_mod} mod + {linked_core} vanilla "
            f"= {linked_mod + linked_core} total file(s) in {plugins_dir.name}/."
        )

        # Capture runtime files generated outside Mods/ on the next restore.
        self.snapshot_root_for_runtime_capture(log_fn=_log)

    def _prepare_mod_filemap(self, filemap: Path, staging: Path,
                             log_fn=None) -> set[str]:
        """Apply SMAPI-specific filemap corrections shared by all backends."""
        _log = log_fn or (lambda _: None)
        fixed = self._fix_alt_textures_casing(filemap, staging)
        if fixed:
            _log(f"  Fixed 'textures' → 'Textures' casing for {fixed} "
                 "Alternative Textures content pack file(s).")
        orphan_configs = self._orphaned_overwrite_configs(filemap)
        if orphan_configs:
            _log(f"  Skipping {len(orphan_configs)} orphaned overwrite file(s) "
                 "(no matching manifest.json deployed).")
        return orphan_configs

    def _vfs_prepare_filemap(self, filemap: Path, staging: Path,
                             log_fn=None) -> set[str]:
        """Preserve Stardew's SMAPI deployment rules in the private view."""
        return self._prepare_mod_filemap(filemap, staging, log_fn=log_fn)

    def _fix_alt_textures_casing(self, filemap: Path, staging: Path) -> int:
        """Canonicalise Alternative Textures content pack casing in the filemap.

        Alternative Textures uses raw .NET filesystem calls (bypassing SMAPI's
        case-insensitive resolver) on a content pack's files, so casing the mod
        author got "wrong" (harmless on Windows, common when authored there)
        breaks on Linux. AT scans <ContentPack>/Textures (next to manifest.json)
        via GetDirectories, and gates per-folder texture.json / texture.png /
        texture_N.png via case-sensitive File.Exists / GetFiles. Detect content
        packs for PeacefulEnd.AlternativeTextures (at ANY nesting depth - authors
        commonly group [CP]+[AT] folders under a parent) and canonicalise both
        the 'Textures' folder and those filenames in the filemap. Source
        resolution stays case-insensitive, so the on-disk casing is still found.
        Returns the number of filemap lines rewritten.
        """
        # Candidate derivation applies this transform before the immutable
        # deployment generation is published, so deploy-time rewriting is no
        # longer necessary or permitted.
        return 0

    def _orphaned_overwrite_configs(
        self, filemap: Path | None = None, *, snapshot=None,
    ) -> set[str]:
        """Lowercased rel paths of [Overwrite] files to skip on deploy.

        SMAPI errors when a Mods/<Name>/ folder holds files but no manifest.json.
        The [Overwrite] folder keeps each mod's runtime files (config.json and
        more), which would otherwise deploy even after the owning mod is
        disabled/removed - leaving a <Name>/ folder with no manifest. Skip any
        [Overwrite] file under a <Name>/ whose <Name>/manifest.json is not in the
        filemap (i.e. no enabled mod provides it). Overwrite files at the root
        (no <Name>/ subfolder) and the manifest.json itself are never skipped.
        """
        from Utils.filegraph_constants import OVERWRITE_NAME

        if snapshot is not None:
            rows = (
                (entry.legacy_rel, entry.mod_name)
                for entry in snapshot.deployment_plan().entries
                if entry.provider_kind != "archive_member" and entry.legacy_rel
            )
        else:
            from Utils.filegraph_deploy import input_ready, legacy_rows
            if not input_ready():
                return set()
            rows = legacy_rows()

        manifest_dirs: set[str] = set()              # top dirs (lower) with a manifest.json
        overwrite_files: list[tuple[str, str]] = []  # (rel_lower, top_dir_lower)
        try:
            for rel_str, mod_name in rows:
                rel_lower = rel_str.lower()
                slash = rel_lower.find("/")
                top_dir = rel_lower[:slash] if slash != -1 else ""
                base = rel_lower.rsplit("/", 1)[-1]
                if base == "manifest.json" and top_dir:
                    manifest_dirs.add(top_dir)
                if mod_name == OVERWRITE_NAME and top_dir and base != "manifest.json":
                    overwrite_files.append((rel_lower, top_dir))
        except (OSError, RuntimeError):
            return set()

        return {
            rel for rel, top_dir in overwrite_files
            if top_dir not in manifest_dirs
        }

    def restore(self, log_fn=None, progress_fn=None) -> None:
        """Restore Mods/ to its vanilla state."""
        _log = log_fn or (lambda _: None)

        if self._game_path is None:
            raise RuntimeError("Game path is not configured.")

        plugins_dir = self._game_path / self.mods_dir
        core = self.mods_dir + "_Core"
        core_dir = self._game_path / core
        
        _profile_dir = self._active_profile_dir
        _entries = read_modlist(_profile_dir / "modlist.txt") if _profile_dir else []
        cleanup_custom_deploy_dirs(_profile_dir, _entries, log_fn=_log, game=self)

        # Restore according to what is actually deployed. This also handles a
        # profile whose VFS setting was changed after its private view was
        # published, without touching the physical Mods directory.
        from Utils.vfs import cleanup_deployment, has_deployment_state
        if has_deployment_state(self):
            cleanup_deployment(self, preserve_upper=True, log_fn=_log)
            if not core_dir.is_dir():
                _log("Restore complete.")
                return
            _log("Restore: a physical deployment also remains; restoring it now ...")

        if core_dir.is_dir():
            _log(f"Restore: clearing {plugins_dir.name}/ and moving {core}/ back ...")
            restored = restore_data_core(
                plugins_dir, core_dir=core_dir,
                overwrite_dir=self.get_effective_overwrite_path(),
                log_fn=_log, game=self, profile_dir=self._active_profile_dir)
            _log(f"  Restored {restored} file(s). {core}/ removed.")
        else:
            _log(f"Restore: no {core}/ found - nothing to restore.")

        moved = self.capture_runtime_files_to_root_folder(log_fn=_log)
        if moved:
            _log(f"  Moved {moved} runtime file(s) to Root_Folder/.")

        _log("Restore complete.")
