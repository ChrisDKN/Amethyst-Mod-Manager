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
from Utils.deploy import LinkMode, deploy_core, deploy_filemap, load_per_mod_strip_prefixes, load_separator_deploy_paths, expand_separator_deploy_paths, cleanup_custom_deploy_dirs, move_to_core, restore_data_core
from Utils.modlist import read_modlist
from Utils.config_paths import get_profiles_dir

_PROFILES_DIR = get_profiles_dir()

# Split-texture filename AT loads via a case-sensitive GetFiles("texture_*.png").
_AT_SPLIT_PNG_RE = re.compile(r"^texture_\d+\.png$", re.IGNORECASE)

class StardewValley(BaseGame):

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

        if not filemap.is_file():
            raise RuntimeError(
                f"filemap.txt not found: {filemap}\n"
                "Run 'Build Filemap' before deploying."
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
        _at_fixed = self._fix_alt_textures_casing(filemap, staging)
        if _at_fixed:
            _log(f"  Fixed 'textures' → 'Textures' casing for {_at_fixed} "
                 "Alternative Textures content pack file(s).")
        _orphan_configs = self._orphaned_overwrite_configs(filemap)
        if _orphan_configs:
            _log(f"  Skipping {len(_orphan_configs)} orphaned overwrite file(s) "
                 "(no matching manifest.json deployed).")
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
        if not filemap.is_file():
            return 0

        try:
            lines = filemap.read_text(encoding="utf-8").splitlines()
        except OSError:
            return 0

        # Lowercased deploy-path prefixes of the directory holding an AT content
        # pack's manifest.json (e.g. "test/[at] drink water...").  An empty
        # string marks a pack whose manifest sits at the deploy root.  Its
        # Textures folder is the path segment immediately after this prefix.
        at_prefixes: set[str] = set()
        for line in lines:
            if "\t" not in line:
                continue
            rel_str, mod_name = line.split("\t", 1)
            if rel_str.rsplit("/", 1)[-1].lower() != "manifest.json":
                continue
            mod_src = staging / mod_name / rel_str
            try:
                data = json.loads(mod_src.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                continue
            cp = data.get("ContentPackFor")
            uid = cp.get("UniqueID") if isinstance(cp, dict) else None
            if isinstance(uid, str) and uid == "PeacefulEnd.AlternativeTextures":
                pack_dir = rel_str.rsplit("/", 1)[0] if "/" in rel_str else ""
                at_prefixes.add(pack_dir.lower())

        if not at_prefixes:
            return 0

        def _match_prefix(rel_lower: str) -> "str | None":
            # The Textures folder must be the segment right after the pack dir.
            for pre in at_prefixes:
                if pre == "":
                    if "/" in rel_lower:
                        return ""
                elif rel_lower.startswith(pre + "/"):
                    return pre
            return None

        fixed = 0
        out: list[str] = []
        for line in lines:
            if "\t" in line:
                rel_str, mod_name = line.split("\t", 1)
                parts = rel_str.split("/")
                pre = _match_prefix(rel_str.lower())
                # depth of the segment right after the pack dir (the Textures lvl)
                tex_idx = -1 if pre is None else (0 if pre == "" else pre.count("/") + 1)
                # Need a Textures folder AND at least one file beneath it.
                if tex_idx >= 0 and len(parts) >= tex_idx + 3 \
                        and parts[tex_idx].lower() == "textures":
                    changed = False
                    # Folder: .../textures/... → .../Textures/...
                    if parts[tex_idx] != "Textures":
                        parts[tex_idx] = "Textures"
                        changed = True
                    # Filename: texture.json / texture.png / texture_N.png -
                    # AT gates these via case-sensitive File.Exists / GetFiles.
                    base = parts[-1]
                    if base != base.lower() and (
                            base.lower() in ("texture.json", "texture.png")
                            or _AT_SPLIT_PNG_RE.match(base)):
                        parts[-1] = base.lower()
                        changed = True
                    if changed:
                        line = "/".join(parts) + "\t" + mod_name
                        fixed += 1
            out.append(line)

        if fixed:
            try:
                filemap.write_text("\n".join(out) + "\n", encoding="utf-8")
            except OSError:
                return 0
        return fixed

    def _orphaned_overwrite_configs(self, filemap: Path) -> set[str]:
        """Lowercased rel paths of [Overwrite] files to skip on deploy.

        SMAPI errors when a Mods/<Name>/ folder holds files but no manifest.json.
        The [Overwrite] folder keeps each mod's runtime files (config.json and
        more), which would otherwise deploy even after the owning mod is
        disabled/removed - leaving a <Name>/ folder with no manifest. Skip any
        [Overwrite] file under a <Name>/ whose <Name>/manifest.json is not in the
        filemap (i.e. no enabled mod provides it). Overwrite files at the root
        (no <Name>/ subfolder) and the manifest.json itself are never skipped.
        """
        from Utils.filemap import OVERWRITE_NAME

        if not filemap.is_file():
            return set()

        manifest_dirs: set[str] = set()              # top dirs (lower) with a manifest.json
        overwrite_files: list[tuple[str, str]] = []  # (rel_lower, top_dir_lower)
        try:
            with filemap.open(encoding="utf-8") as f:
                for line in f:
                    if "\t" not in line:
                        continue
                    rel_str, mod_name = line.rstrip("\n").split("\t", 1)
                    rel_lower = rel_str.lower()
                    slash = rel_lower.find("/")
                    top_dir = rel_lower[:slash] if slash != -1 else ""
                    base = rel_lower.rsplit("/", 1)[-1]
                    if base == "manifest.json" and top_dir:
                        manifest_dirs.add(top_dir)
                    if mod_name == OVERWRITE_NAME and top_dir and base != "manifest.json":
                        overwrite_files.append((rel_lower, top_dir))
        except OSError:
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
        cleanup_custom_deploy_dirs(_profile_dir, _entries, log_fn=_log)

        if core_dir.is_dir():
            _log(f"Restore: clearing {plugins_dir.name}/ and moving {core}/ back ...")
            restored = restore_data_core(plugins_dir, core_dir=core_dir, overwrite_dir=self.get_effective_overwrite_path(), log_fn=_log)
            _log(f"  Restored {restored} file(s). {core}/ removed.")
        else:
            _log(f"Restore: no {core}/ found - nothing to restore.")

        moved = self.capture_runtime_files_to_root_folder(log_fn=_log)
        if moved:
            _log(f"  Moved {moved} runtime file(s) to Root_Folder/.")

        _log("Restore complete.")