"""
daggerfall_unity.py
Game handler for Daggerfall Unity.

Mod structure:
  Everything a mod ships lands under
    <game_path>/DaggerfallUnity_Data/StreamingAssets/
  Packaged mods are single .dfmod asset bundles in StreamingAssets/Mods/ -
  DFU scans that folder recursively, so per-mod subfolders are fine.  Loose
  replacement assets go in the sibling StreamingAssets folders instead
  (Textures/, Quests/, QuestPacks/, Sound/, Text/, …), which is why the whole
  of StreamingAssets is the deploy target rather than just Mods/.

  Staged mods live in Profiles/Daggerfall Unity/mods/.
  Root_Folder/ files deploy straight to the game install root (handled by GUI).

Notes:
  - DFU is a native Linux binary shipped as a standalone zip - there is no
    Steam app id and no Proton prefix.  get_launch_command() runs the player
    directly; the binary is normally <game_path>/DaggerfallUnity.x86_64 but a
    user-set override is honoured (persisted as a paths.json extra).
  - The load order lives outside the game folder, in DFU's own Mods.json.
    Amethyst synchronises it by default; users can instead leave it entirely
    to DFU with the "Manage load order in DFU" option.
  - Per-mod runtime state also lives outside the game folder, under
    <PersistentDataPath>/Mods/: settings in GameData/<GUID>/ and unpacked
    assets in ExtractedFiles/<mod title>/.  DFU discards both for mods it is
    not loading, so restore() stashes them in overwrite/ and deploy() links
    them back - otherwise switching profiles resets them.  They are linked,
    not moved, so overwrite/ keeps the data even if the game (or a crash)
    removes what is in place.
"""

from __future__ import annotations

import importlib.util
import stat
import sys
from pathlib import Path

from Games.base_game import BaseGame
from Utils.deploy import (
    LinkMode,
    cleanup_custom_deploy_dirs,
    deploy_core,
    deploy_filemap,
    expand_separator_deploy_paths,
    load_per_mod_strip_prefixes,
    load_separator_deploy_paths,
    move_to_core,
    restore_data_core,
)
from Utils.modlist import read_modlist
from Utils.config_paths import get_profiles_dir

_PROFILES_DIR = get_profiles_dir()

_DATA_DIR   = "DaggerfallUnity_Data"
_STREAMING  = "StreamingAssets"
_EXE        = "DaggerfallUnity.x86_64"

# The folders DFU itself reads out of StreamingAssets.  Doubles as the set of
# valid mod install roots, so a mod zipped as DaggerfallUnity_Data/
# StreamingAssets/Textures/… is auto-stripped down to Textures/….
_STREAMING_FOLDERS = {
    "biogs", "books", "docs", "factions", "fonts", "gamefiles", "mods",
    "movies", "presets", "questpacks", "quests", "sound", "soundfonts",
    "spellicons", "tables", "text", "textures", "worlddata",
}

# Folders DFU reads from INSIDE Textures/, which authors routinely ship at the
# archive root (every paperdoll mod does).  Left where they land they deploy to
# StreamingAssets/CifRci and the game never looks there.
_TEXTURE_SUBFOLDERS = {"cifrci", "img"}

# Loose files DFU reads from a specific folder rather than the StreamingAssets
# root.  NameHelper looks for a NameGen.txt override in StreamingAssets/Text.
_LOOSE_FILE_HOMES = {"namegen.txt": "Text"}

# Unity builds one asset bundle per target and DFU mods are often zipped with a
# folder per platform.  We run the native Linux player, so prefer its bundle;
# the rest are build siblings that must not be staged.  Authors mostly use the
# Unity build-target names, but Vortex's extension matches on a substring
# because plainer spellings ("Windows Version", "Mac") turn up too.
_PLATFORM_DIRS = ("standalonelinux64", "standalonewindows64",
                  "standalonewindows", "standaloneosx")
# Whole words only, and never bare "win"/"mac" - this collection alone has a
# "Windmills of Daggerfall" that a substring match would call a Windows build.
_PLATFORM_WORDS = ("linux", "windows", "osx", "macos")
# Rank order: exact build-target folders first, then loose spellings, Linux
# ahead of the rest in both tiers.
_PLATFORM_PREFERENCE = ("linux", "windows", "osx", "macos")

# DFU builds every one of these paths from a hardcoded string
# (Path.Combine(streamingAssetsPath, "Mods"), …), so on a case-sensitive
# filesystem a mod that shipped "textures/" would deploy into a second folder
# the game never reads.  Pin the engine's exact spelling.
_CASING_PINS = {
    "biogs": "BIOGs",
    "books": "Books",
    "cifrci": "CifRci",          # Textures/CifRci
    "docs": "Docs",
    "factions": "Factions",
    "fonts": "Fonts",
    "gamefiles": "GameFiles",
    "img": "Img",                # Textures/Img
    "mods": "Mods",
    "movies": "Movies",
    "presets": "Presets",
    "questpacks": "QuestPacks",
    "quests": "Quests",
    "sound": "Sound",
    "soundfonts": "SoundFonts",
    "spellicons": "SpellIcons",
    "tables": "Tables",
    "text": "Text",
    "textures": "Textures",
    "worlddata": "WorldData",
}


def _mods_json():
    """Load the sibling dfu_mods_json module (the folder name has a space)."""
    # Handlers are loaded by file path, and "Games.Daggerfall Unity.x" is not a
    # valid dotted import - same trick BG3 uses for its mod.io helpers.
    mod_name = "dfu_mods_json_dfu"
    cached = sys.modules.get(mod_name)
    if cached is not None:
        return cached
    sibling = Path(__file__).resolve().parent / "dfu_mods_json.py"
    spec = importlib.util.spec_from_file_location(mod_name, str(sibling))
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _platform_folder(path: Path, dest_root: Path) -> str:
    """Return the platform-specific folder *path* sits under, or ''."""
    for part in path.relative_to(dest_root).parts[:-1]:
        low = part.lower()
        if low in _PLATFORM_DIRS or any(w in low for w in _PLATFORM_WORDS):
            return part
    return ""


def _platform_rank(path: Path, dest_root: Path) -> int:
    """Rank a .dfmod by how well its platform folder suits the Linux player.

    Lower is better.  A bundle in no platform folder outranks every
    platform-specific one - it is the author's single cross-platform build.
    """
    folder = _platform_folder(path, dest_root)
    if not folder:
        return -1
    low = folder.lower()
    exact = low in _PLATFORM_DIRS
    for i, word in enumerate(_PLATFORM_PREFERENCE):
        if word in low:
            return (0 if exact else len(_PLATFORM_PREFERENCE)) + i
    return 2 * len(_PLATFORM_PREFERENCE)


def _move_into(path: Path, target_dir: Path) -> bool:
    """Move *path* under *target_dir*, leaving an existing target alone."""
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / path.name
    if target.exists():
        return False
    path.rename(target)
    return True


def normalise_dfu_mod(dest_root: Path, mod_name: str, log_fn) -> None:
    """Put a staged mod's files where DFU actually reads them."""
    moved = 0

    # 1. .dfmod bundles → Mods/.  Nexus archives are as often a bare
    #    MyMod.dfmod as a StreamingAssets tree, and plenty ship one folder per
    #    Unity build target; keep only the bundle that suits this player.
    bundles: dict[str, list[Path]] = {}
    for path in dest_root.rglob("*.dfmod*"):
        low = path.name.lower()
        if path.is_file() and (low.endswith(".dfmod") or low.endswith(".dfmod.json")):
            bundles.setdefault(low, []).append(path)
    for name, paths in sorted(bundles.items()):
        best = min(paths, key=lambda p: (_platform_rank(p, dest_root), str(p)))
        if (dest_root / "Mods") not in best.parents and _move_into(best, dest_root / "Mods"):
            moved += 1
        # Only ever delete a *duplicate* of a bundle we already took, and only
        # when it sits in a platform folder - a lone bundle under a folder that
        # merely reads like a platform name must be left alone.
        for other in paths:
            if other == best or not other.is_file():
                continue
            folder = _platform_folder(other, dest_root)
            if folder:
                other.unlink()
                log_fn(f"Dropped the {folder} build of '{name}' for "
                       f"'{mod_name}' (keeping {_platform_folder(best, dest_root) or 'the shared build'}).")

    # 2. Textures/ subfolders shipped at the root (paperdolls, CIF/RCI packs).
    for child in list(dest_root.iterdir()):
        if child.is_dir() and child.name.lower() in _TEXTURE_SUBFOLDERS:
            if _move_into(child, dest_root / "Textures"):
                moved += 1
                log_fn(f"Moved {child.name}/ under Textures/ for '{mod_name}'.")

    # 3. Loose files DFU reads from a named folder.
    for child in list(dest_root.iterdir()):
        home = _LOOSE_FILE_HOMES.get(child.name.lower()) if child.is_file() else None
        if home and _move_into(child, dest_root / home):
            moved += 1
            log_fn(f"Moved {child.name} into {home}/ for '{mod_name}'.")

    if moved:
        log_fn(f"Normalised {moved} path(s) for '{mod_name}'.")


class DaggerfallUnity(BaseGame):

    # The launch binary is a configured path, so make it per-profile like the
    # game/staging paths (stored as a paths.json extra).
    profile_overridable_paths_extras = ("launch_binary_path",)
    profile_overridable_settings = (
        *BaseGame.profile_overridable_settings,
        "manage_load_order_in_dfu",
    )

    def __init__(self):
        self._game_path: Path | None = None
        self._prefix_path: Path | None = None
        self._launch_binary_path: Path | None = None   # None → derive from game path
        self._deploy_mode: LinkMode = LinkMode.HARDLINK
        self._staging_path: Path | None = None
        self.load_paths()

    # -----------------------------------------------------------------------
    # Identity
    # -----------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "Daggerfall Unity"

    @property
    def game_id(self) -> str:
        return "daggerfall_unity"

    @property
    def exe_name(self) -> str:
        return _EXE

    @property
    def steam_id(self) -> str:
        # DFU is a standalone download from the Daggerfall Workshop GitHub
        # releases - it has no Steam app id of its own.  (Steam 1812390 is the
        # DOS original, useful only as a source of the ARENA2 game files.)
        return ""

    @property
    def auto_drive_scan(self) -> bool:
        # No storefront ships DFU, so the store-library scan can never find
        # it - go straight to the all-drives scan.
        return True

    @property
    def nexus_game_domain(self) -> str:
        return "daggerfallunity"

    @property
    def mod_folder_strip_prefixes(self) -> set[str]:
        # Also applied when the filemap is built, so a mod copied into staging
        # by hand is normalised the same way an installed one is.
        return {_DATA_DIR.lower(), _STREAMING.lower()}

    @property
    def filemap_casing_pins(self) -> dict[str, str]:
        return dict(_CASING_PINS)

    @property
    def mod_required_top_level_folders(self) -> set[str]:
        return set(_STREAMING_FOLDERS)

    @property
    def mod_auto_strip_until_required(self) -> bool:
        return True

    @property
    def mod_required_file_types(self) -> set[str]:
        # A bare MyMod.dfmod at the archive root is a valid mod on its own;
        # _route_loose_dfmods puts it in Mods/ afterwards.
        return {".dfmod"}

    @property
    def mod_install_as_is_if_no_match(self) -> bool:
        return True

    @property
    def additional_install_logic(self) -> list:
        return [normalise_dfu_mod]

    @property
    def conflict_ignore_filenames(self) -> set[str]:
        # .meta/.manifest are Unity build artefacts every hand-zipped mod
        # carries; .psd/.pdf are the author's sources and instructions.  None
        # of them are ever read by the game.
        return {"readme.txt", "*.md", "*.meta", "*.manifest", "*.psd", "*.pdf"}

    @property
    def excluded_loose_filenames(self) -> set[str]:
        # Nothing DFU loads sits loose at a mod's top level - every asset lives
        # in a StreamingAssets subfolder, and normalise_dfu_mod has already
        # rehomed the files that belong somewhere.  What is left is changelogs,
        # posters, guides and per-platform bundle manifests.
        return {"*.txt", "*.png", "*.jpg", "*.jpeg", "*.bmp", "*.doc", "*.docx",
                "*.xls", "*.xlsx", "*.rtf", "*.url", "*.html",
                "standalonelinux64*", "standalonewindows*", "standaloneosx*"}

    # -----------------------------------------------------------------------
    # Paths
    # -----------------------------------------------------------------------

    def get_game_path(self) -> Path | None:
        return self._game_path

    def get_mod_data_path(self) -> Path | None:
        """Mods deploy into DaggerfallUnity_Data/StreamingAssets/."""
        if self._game_path is None:
            return None
        return self._game_path / _DATA_DIR / _STREAMING

    def extra_save_paths(self) -> list[tuple[str, str, str]]:
        """DFU's native Linux save folder, which the Ludusavi data omits.

        The manifest lists only the Windows location (AppData/LocalLow/…).  A
        Linux build is the normal way to run DFU here and writes to the Unity
        persistent-data path instead, so without this the Saves tab finds
        nothing unless the user is running under Wine.  Resolved rather than
        tokenised because a Portable.txt next to the player moves the whole
        persistent path into the install folder - see persistent_data_dir.
        """
        if self._game_path is None:
            return []
        saves = _mods_json().persistent_data_dir(self._game_path) / "Saves"
        return [(str(saves), "linux", "")]

    def runtime_snapshot_exclude_dirs(self) -> set[str] | None:
        # StreamingAssets/ is reverted via its _Core backup; capture only
        # files that appear outside the player's data folder.
        return {_DATA_DIR}

    def get_mod_staging_path(self) -> Path:
        if self._staging_path is not None:
            return self._staging_path / "mods"
        return _PROFILES_DIR / self.name / "mods"

    def set_staging_path(self, path: "Path | str | None") -> None:
        self._staging_path = Path(path) if path else None
        self.save_paths()

    def get_deploy_mode(self) -> LinkMode:
        return self._deploy_mode

    def set_deploy_mode(self, mode: LinkMode) -> None:
        self._deploy_mode = mode
        self.save_paths()

    @property
    def manage_load_order_in_dfu(self) -> bool:
        """Whether DFU, rather than Amethyst, owns the .dfmod load order."""
        return self._load_settings().get("manage_load_order_in_dfu", False)

    def set_manage_load_order_in_dfu(self, value: bool) -> None:
        data = self._load_settings()
        data["manage_load_order_in_dfu"] = bool(value)
        self._save_settings(data)

    # DFU is a native Linux binary - no Proton prefix, so never look one up.
    def _find_prefix_for_load(self) -> "Path | None":
        return None

    def get_prefix_path(self) -> Path | None:
        return None

    def set_prefix_path(self, path: "Path | str | None") -> None:
        pass  # Not applicable for Daggerfall Unity.

    def _load_paths_extra(self, data: dict) -> None:
        raw = data.get("launch_binary_path", "")
        self._launch_binary_path = Path(raw) if raw else None

    def _save_paths_extra(self) -> dict:
        return {
            "launch_binary_path": (str(self._launch_binary_path)
                                   if self._launch_binary_path else ""),
        }

    # -----------------------------------------------------------------------
    # Native launch
    # -----------------------------------------------------------------------

    def get_launch_binary_path(self) -> Path | None:
        """Return the player binary to run, or None if it cannot be found."""
        if self._launch_binary_path and self._launch_binary_path.is_file():
            return self._launch_binary_path
        if self._game_path is None:
            return None
        exe = self._game_path / _EXE
        if exe.is_file():
            return exe
        # A future release could rename the player; fall back to the only
        # Unity binary in the install root rather than failing outright.
        candidates = sorted(p for p in self._game_path.glob("*.x86_64")
                            if p.is_file())
        return candidates[0] if len(candidates) == 1 else None

    def set_launch_binary_path(self, path: "Path | str | None") -> None:
        self._launch_binary_path = Path(path) if path else None
        self.save_paths()

    def get_launch_command(self) -> "list[str] | None":
        """Return the native command for the Play button."""
        exe = self.get_launch_binary_path()
        if exe is None:
            return None
        # Extracting the release zip with a tool that drops the mode bits is a
        # common way to end up with a Play button that silently does nothing.
        try:
            mode = exe.stat().st_mode
            if not mode & stat.S_IXUSR:
                exe.chmod(mode | stat.S_IXUSR)
        except OSError:
            pass
        return [str(exe)]

    # -----------------------------------------------------------------------
    # Validation
    # -----------------------------------------------------------------------

    def validate_install(self) -> list[str]:
        errors = super().validate_install()
        if self._game_path is not None and (self._game_path / _DATA_DIR).is_dir():
            if self.get_launch_binary_path() is None:
                errors.append(
                    f"'{_EXE}' not found in {self._game_path}. Set the launch "
                    "binary in the configure dialog if the player was renamed."
                )
        return errors

    # -----------------------------------------------------------------------
    # Deployment
    # -----------------------------------------------------------------------

    def _ordered_dfmods(self, profile: str) -> list[Path]:
        """Deployed .dfmod paths in top-to-bottom mod-list order."""
        deploy_dir = self.get_mod_data_path()
        if deploy_dir is None:
            return []
        mods_dir = deploy_dir / "Mods"
        if not mods_dir.is_dir():
            return []
        staging = self.get_effective_mod_staging_path()
        profile_dir = self.get_profile_root() / "profiles" / profile

        # Map deployed file name → deployed path, then walk the mod list so the
        # order reflects what the user actually set rather than readdir order.
        deployed = {p.name: p for p in mods_dir.rglob("*.dfmod") if p.is_file()}
        ordered: list[Path] = []
        seen: set[str] = set()
        for entry in read_modlist(profile_dir / "modlist.txt"):
            if entry.is_separator or not entry.enabled:
                continue
            mod_dir = staging / entry.name
            if not mod_dir.is_dir():
                continue
            for src in sorted(mod_dir.rglob("*.dfmod")):
                hit = deployed.get(src.name)
                if hit is not None and src.name not in seen:
                    seen.add(src.name)
                    ordered.append(hit)
        return ordered

    def _sync_mods_json(self, profile: str, log_fn) -> None:
        """Sync Amethyst's order, unless the user delegated it to DFU."""
        if self.manage_load_order_in_dfu:
            log_fn("Step 4: Leaving Mods.json for DFU to manage.")
            return

        log_fn("Step 4: Writing the load order to DFU's Mods.json ...")
        mods_json = _mods_json()
        # Parsed ModInfo lives beside the profile's modindex.bin, keyed by size
        # and mtime, so repeat deploys skip decompressing every bundle again.
        cache_path = (self.get_profile_root() / "profiles" / profile /
                      mods_json.CACHE_NAME)
        try:
            mods_json.sync_mods_json(
                self._game_path, self._ordered_dfmods(profile), log_fn=log_fn,
                cache_path=cache_path)
        except OSError as exc:
            # The load order is a convenience - never fail a deploy over it.
            log_fn(f"  Could not write Mods.json ({exc}); order mods in DFU's "
                   "Mod Loader instead.")

    def deploy(self, log_fn=None, mode: LinkMode = LinkMode.HARDLINK,
               profile: str = "default", progress_fn=None) -> None:
        """Deploy staged mods into DaggerfallUnity_Data/StreamingAssets/.

        Workflow:
          1. Move StreamingAssets/ → StreamingAssets_Core/  (vanilla backup)
          2. Transfer mod files listed in filemap.txt into StreamingAssets/
          3. Fill gaps with vanilla files from StreamingAssets_Core/
          4. Write the mod-list order into DFU's Mods.json, unless delegated
             to DFU in configuration
        (Root Folder deployment is handled by the GUI after this returns.)
        """
        _log = log_fn or (lambda _: None)

        if self._game_path is None:
            raise RuntimeError("Game path is not configured.")

        data_dir = self.get_mod_data_path()
        filemap  = self.get_effective_filemap_path()
        staging  = self.get_effective_mod_staging_path()
        core     = _STREAMING + "_Core"

        if not (self._game_path / _DATA_DIR).is_dir():
            raise RuntimeError(
                f"'{_DATA_DIR}' not found in {self._game_path} - the game path "
                "must be the folder containing DaggerfallUnity.x86_64."
            )
        if not filemap.is_file():
            raise RuntimeError(
                f"filemap.txt not found: {filemap}\n"
                "Run 'Build Filemap' before deploying."
            )

        _log(f"Step 1: Moving {_STREAMING}/ → {core}/ ...")
        move_to_core(data_dir, log_fn=_log)
        _log(f"  Backed up existing files → {core}/.")
        data_dir.mkdir(parents=True, exist_ok=True)

        _log(f"Step 2: Transferring mod files into {data_dir} ({mode.name}) ...")
        profile_dir    = self.get_profile_root() / "profiles" / profile
        per_mod_strip  = load_per_mod_strip_prefixes(profile_dir)
        _sep_deploy    = load_separator_deploy_paths(profile_dir)
        _sep_entries   = read_modlist(profile_dir / "modlist.txt") if _sep_deploy else []
        per_mod_deploy = expand_separator_deploy_paths(_sep_deploy, _sep_entries) or None
        linked_mod, placed = deploy_filemap(
            filemap, data_dir, staging,
            mode=mode,
            strip_prefixes=self.mod_folder_strip_prefixes,
            per_mod_strip_prefixes=per_mod_strip,
            per_mod_deploy_dirs=per_mod_deploy,
            log_fn=_log,
            progress_fn=progress_fn,
            core_dir=data_dir.parent / core,
        )
        _log(f"  Transferred {linked_mod} mod file(s).")

        _log(f"Step 3: Filling gaps with vanilla files from {core}/ ...")
        linked_core = deploy_core(data_dir, placed, mode=mode, log_fn=_log)
        _log(f"  Transferred {linked_core} vanilla file(s).")

        self._sync_mods_json(profile, _log)

        _log("Step 5: Linking stashed mod settings and extracted files back ...")
        settings = _mods_json().restore_mod_settings(
            self._game_path, self.get_effective_overwrite_path(),
            mode=mode, log_fn=_log)
        if not settings:
            _log("  Nothing stashed to restore.")

        _log(
            f"Deploy complete. "
            f"{linked_mod} mod + {linked_core} vanilla "
            f"= {linked_mod + linked_core} total file(s) in {_STREAMING}/."
        )

        # Capture runtime files generated outside StreamingAssets/ on the next
        # restore.
        self.snapshot_root_for_runtime_capture(log_fn=_log)

    def restore(self, log_fn=None, progress_fn=None) -> None:
        """Restore StreamingAssets/ to its vanilla state."""
        _log = log_fn or (lambda _: None)

        if self._game_path is None:
            raise RuntimeError("Game path is not configured.")

        data_dir = self.get_mod_data_path()
        core     = _STREAMING + "_Core"
        core_dir = data_dir.parent / core

        _profile_dir = self._active_profile_dir
        _entries = read_modlist(_profile_dir / "modlist.txt") if _profile_dir else []
        cleanup_custom_deploy_dirs(_profile_dir, _entries, log_fn=_log)

        _mj = _mods_json()

        # Before anything else: DFU deletes the GameData and ExtractedFiles
        # folders of any mod it no longer loads, so the next profile's deploy
        # would otherwise wipe this profile's settings.  Park them in
        # overwrite/.
        _log("Restore: stashing per-mod settings and extracted files ...")
        stashed = _mj.stash_mod_settings(
            self._game_path, self.get_effective_overwrite_path(), log_fn=_log)
        if not stashed:
            _log("  No per-mod runtime folders found.")

        _log("Restore: reverting DFU's Mods.json ...")
        _mj.restore_mods_json(self._game_path, log_fn=_log)

        if core_dir.is_dir():
            _log(f"Restore: clearing {_STREAMING}/ and moving {core}/ back ...")
            restored = restore_data_core(
                data_dir,
                core_dir=core_dir,
                overwrite_dir=self.get_effective_overwrite_path(),
                staging_root=self.get_effective_mod_staging_path(),
                strip_prefixes=self.mod_folder_strip_prefixes,
                log_fn=_log,
            )
            _log(f"  Restored {restored} file(s). {core}/ removed.")
        else:
            _log(f"Restore: no {core}/ found - nothing to restore.")

        moved = self.capture_runtime_files_to_root_folder(log_fn=_log)
        if moved:
            _log(f"  Moved {moved} runtime file(s) to Root_Folder/.")

        _log("Restore complete.")
