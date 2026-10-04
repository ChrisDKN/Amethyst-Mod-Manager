"""
balatro.py
Game handler for Balatro.

Mod structure:
  Mods deploy into the Proton prefix AppData folder:
    drive_c/users/steamuser/AppData/Roaming/Balatro/Mods/
  Staged mods live in Profiles/Balatro/mods/

  Mods are loaded by Lovely (a winmm.dll injector) and usually Steamodded
  on top of it.  Each mod is a named subfolder holding a .lua entry point and
  optionally a manifest:  Mods/<ModName>/<ModName>.lua.  Mods that ship their
  files loose at the staging root are auto-wrapped into <ModName>/ before the
  filemap is built (mod_staging_requires_subdir + mod_staging_wrap_signals).

  Lovely itself ships winmm.dll, which must land at the game install root
  rather than in the AppData Mods folder - routed by custom_routing_rules.
"""

from pathlib import Path

from Games.base_game import BaseGame
from Utils.deployment import (
    CustomRule, LinkMode, RestoreWhitelistRule,
    deploy_filemap, deploy_core, deploy_custom_rules,
    load_per_mod_strip_prefixes, load_separator_deploy_paths,
    expand_separator_deploy_paths, cleanup_custom_deploy_dirs,
    move_to_core, restore_custom_rules, restore_data_core,
)
from Utils.mods.modlist import read_modlist
from Utils.config_paths import get_profiles_dir

_PROFILES_DIR = get_profiles_dir()

# Path inside the Proton prefix where Balatro (via Steamodded) reads mods from
_MODS_SUBPATH = Path("drive_c/users/steamuser/AppData/Roaming/Balatro/Mods")


class Balatro(BaseGame):

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
        return "Balatro"

    @property
    def game_id(self) -> str:
        return "Balatro"

    @property
    def exe_name(self) -> str:
        return "Balatro.exe"

    @property
    def steam_id(self) -> str:
        return "2379780"

    @property
    def nexus_game_domain(self) -> str:
        return "balatro"

    @property
    def thunderstore_community(self) -> str:
        return "balatro"

    @property
    def conflict_ignore_filenames(self) -> set[str]:
        # Documentation only.  manifest.json / lovely.toml are NOT listed:
        # entries here are dropped from the filemap entirely, and Steamodded
        # needs a mod's manifest present to load it at all.
        return {"*.md", "*.txt", "icon.png"}

    @property
    def plugin_extensions(self) -> list[str]:
        return []

    @property
    def loot_sort_enabled(self) -> bool:
        return False

    # -- Mod structure -------------------------------------------------------

    @property
    def mod_staging_requires_subdir(self) -> bool:
        return True

    @property
    def mod_staging_wrap_signals(self) -> "tuple[set[str], set[str]]":
        return ({"manifest.json", "lovely.toml"}, set())

    @property
    def mod_staging_already_structured_markers(self) -> "set[str]":
        return {"manifest.json", "lovely.toml"}

    # -- Prefix / framework --------------------------------------------------

    @property
    def frameworks(self) -> dict[str, str]:
        return {"Lovely": "winmm.dll"}

    @property
    def wine_dll_overrides(self) -> dict[str, str]:
        # Lovely injects through winmm.dll - Wine must prefer the native one.
        return {"winmm": "native,builtin"}

    @property
    def auto_install_deps(self) -> list[str]:
        return ["vcredist"]

    @property
    def custom_routing_rules(self) -> list[CustomRule]:
        # Lovely ships winmm.dll loose at the mod root; it must sit next to
        # Balatro.exe at the game install root, not in the AppData Mods folder.
        return [
            CustomRule(dest="", filenames=["winmm.dll"], loose_only=True,
                       flatten=True),
        ]

    @property
    def restore_whitelist(self) -> list[RestoreWhitelistRule]:
        return super().restore_whitelist + [
            RestoreWhitelistRule(path="lovely", folders=["dump", "game-dump"]),
        ]

    # -----------------------------------------------------------------------
    # Paths
    # -----------------------------------------------------------------------

    def get_game_path(self) -> Path | None:
        return self._game_path

    def get_mod_data_path(self) -> Path | None:
        """Mods deploy into the Proton prefix AppData Mods folder."""
        if self._prefix_path is None:
            return None
        return self._prefix_path / _MODS_SUBPATH

    def get_mod_staging_path(self) -> Path:
        if self._staging_path is not None:
            return self._staging_path / "mods"
        return _PROFILES_DIR / self.name / "mods"

    def get_hardlink_deploy_targets(self) -> list[tuple[str, "Path | None"]]:
        return [("Proton prefix", self._prefix_path)]

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
        """Deploy staged mods into the Proton prefix Mods folder.

        Workflow:
          1. Move everything currently in Mods/ → Mods_Core/  (vanilla backup)
          2. Transfer every file listed in filemap.txt into Mods/
          3. Fill gaps with vanilla files from Mods_Core/
        (Root Folder deployment is handled by the GUI after this returns.)
        """
        _log = log_fn or (lambda _: None)

        if self._prefix_path is None:
            raise RuntimeError("Prefix path is not configured.")

        mods_dir = self._prefix_path / _MODS_SUBPATH
        filemap  = self.get_effective_filemap_path()
        staging  = self.get_effective_mod_staging_path()

        mods_dir.mkdir(parents=True, exist_ok=True)

        from Utils.filegraph.deploy import input_ready
        if not input_ready():
            raise RuntimeError(
                f"filemap.txt not found: {filemap}\n"
                "Run 'Build Filemap' before deploying."
            )

        profile_dir = self.get_profile_root() / "profiles" / profile
        per_mod_strip = load_per_mod_strip_prefixes(profile_dir)
        _sep_deploy = load_separator_deploy_paths(profile_dir)
        _sep_entries = read_modlist(profile_dir / "modlist.txt") if _sep_deploy else []
        per_mod_deploy = expand_separator_deploy_paths(_sep_deploy, _sep_entries) or None

        # Lovely's winmm.dll must sit next to Balatro.exe, not in AppData.
        custom_rules = self.custom_routing_rules
        custom_exclude: set[str] = set()
        if custom_rules and self._game_path:
            _log("Step 1a: Routing loader files to the game root ...")
            custom_exclude = deploy_custom_rules(
                filemap, self._game_path, staging,
                rules=custom_rules,
                mode=mode,
                strip_prefixes=self.mod_folder_strip_prefixes,
                per_mod_strip_prefixes=per_mod_strip,
                log_fn=_log,
            )
            _log(f"  Routed {len(custom_exclude)} file(s) to the game root.")

        _log("Step 1: Moving Mods/ → Mods_Core/ ...")
        move_to_core(mods_dir, log_fn=_log)
        _log("  Backed up existing files → Mods_Core/.")

        _log(f"Step 2: Transferring mod files into Mods/ ({mode.name}) ...")
        linked_mod, placed = deploy_filemap(filemap, mods_dir, staging,
                                            mode=mode,
                                            strip_prefixes=self.mod_folder_strip_prefixes,
                                            per_mod_strip_prefixes=per_mod_strip,
                                            per_mod_deploy_dirs=per_mod_deploy,
                                            log_fn=_log,
                                            progress_fn=progress_fn,
                                            exclude=custom_exclude or None,
                                            core_dir=mods_dir.parent / (mods_dir.name + "_Core"))
        _log(f"  Transferred {linked_mod} mod file(s).")

        _log("Step 3: Filling gaps with vanilla files from Mods_Core/ ...")
        linked_core = deploy_core(
            mods_dir, placed, mode=mode, log_fn=_log,
            copy_matcher=self.restore_whitelist_matcher())
        _log(f"  Transferred {linked_core} vanilla file(s).")

        _log(
            f"Deploy complete. "
            f"{linked_mod} mod + {linked_core} vanilla "
            f"= {linked_mod + linked_core} total file(s) in Mods/."
        )

    def restore(self, log_fn=None, progress_fn=None) -> None:
        """Remove deployed mods and restore the vanilla Mods folder."""
        _log = log_fn or (lambda _: None)

        if self._prefix_path is None:
            raise RuntimeError("Prefix path is not configured.")

        mods_dir = self._prefix_path / _MODS_SUBPATH

        # Undo the loader files routed to the game root on deploy.
        custom_rules = self.custom_routing_rules
        if custom_rules and self._game_path:
            _log("Restore: removing custom-routed loader files ...")
            restore_custom_rules(
                self.get_effective_filemap_path(),
                self._game_path,
                rules=custom_rules,
                log_fn=_log,
            )

        _profile_dir = self._active_profile_dir
        _entries = read_modlist(_profile_dir / "modlist.txt") if _profile_dir else []
        cleanup_custom_deploy_dirs(_profile_dir, _entries, log_fn=_log, game=self)

        _log("Restore: clearing Mods/ and moving Mods_Core/ back ...")
        restored = restore_data_core(
            mods_dir, overwrite_dir=self.get_effective_overwrite_path(),
            log_fn=_log, game=self, profile_dir=self._active_profile_dir,
            restore_whitelist=self.restore_whitelist_matcher())
        _log(f"  Restored {restored} file(s). Mods_Core/ removed.")

        _log("Restore complete.")
