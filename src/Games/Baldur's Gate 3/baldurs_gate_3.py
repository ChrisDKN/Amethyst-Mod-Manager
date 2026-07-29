"""
baldurs_gate_3.py
Game handler for Baldur's Gate 3.

Mod structure:
  Mods install into the Larian AppData Mods folder.  Two layouts are
  supported:
    - Proton prefix:  <prefix>/drive_c/users/steamuser/AppData/Local/
                      Larian Studios/Baldur's Gate 3/Mods/
    - Native Linux:   ~/.local/share/Larian Studios/Baldur's Gate 3/Mods/
  The prefix is preferred when configured; otherwise the native Linux
  root is used if it exists.
  Staged mods live in Profiles/Baldur's Gate 3/mods/

  Custom routing rules send loose-mod folders (Generated/, Public/, Mods/,
  etc.) to the game's Data directory and bin/ files to the install root;
  .pak files are excluded from the Mods-folder routing rule and instead
  deploy flat at the top of the Larian AppData Mods folder, the only place
  the game loads them from.  Remaining files (readmes, images, ...) deploy
  alongside them and are harmless.

  After deploying .pak files, modsettings.lsx is generated automatically so
  BG3 recognises the installed mods.  Mod load order follows the modlist
  priority, with dependencies topologically sorted to appear before the
  mods that require them.  Adventure (custom campaign) mods replace the
  GustavX campaign entry; pure override paks stay out of the load order.
"""

import json
from pathlib import Path

from Games.base_game import BaseGame, WizardTool
from Utils.deploy import (
    CustomRule, LinkMode, deploy_filemap, deploy_core, move_to_core, restore_data_core,
    deploy_custom_rules, load_per_mod_strip_prefixes,
    load_separator_deploy_paths, expand_separator_deploy_paths,
    expand_separator_link_modes, expand_separator_raw_deploy,
    cleanup_custom_deploy_dirs, restore_custom_rules,
)
from Utils.modlist import read_modlist
from Utils.config_paths import get_profiles_dir
from Utils.modsettings import write_modsettings, write_vanilla_modsettings

_PROFILES_DIR = get_profiles_dir()

# Path inside the Proton prefix where the Larian data folder lives
_PREFIX_LARIAN_SUBPATH = Path(
    "drive_c/users/steamuser/AppData/Local/Larian Studios/Baldur's Gate 3"
)

# Native Linux (Steam Deck) build stores its data here
_NATIVE_LARIAN_ROOT = (
    Path.home() / ".local/share/Larian Studios/Baldur's Gate 3"
)

# Subpaths within the Larian root
_MODS_REL = Path("Mods")
_MODSETTINGS_REL = Path("PlayerProfiles/Public/modsettings.lsx")


def _suppress_launcher_mod_warnings(larian_root: Path, log_fn=None) -> None:
    """Mark the Larian launcher's mod/data warnings as already shown.

    The launcher otherwise prompts about third-party mods on every start and
    can deactivate them.  Only the warning flags are touched (not telemetry),
    and only if the launcher has run before (preferences.json exists).
    """
    _log = log_fn or (lambda _: None)
    prefs = larian_root.parent / "Launcher" / "Settings" / "preferences.json"
    if not prefs.is_file():
        return
    desired = {
        "ModsWarningShown": True,
        "DataWarningShown": True,
        "DisplayFilesValidationMsg": False,
        "DisplayModsDetectedMsg": False,
    }
    try:
        data = json.loads(prefs.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            return
        if all(data.get(k) == v for k, v in desired.items()):
            return
        data.update(desired)
        prefs.write_text(json.dumps(data, indent=4), encoding="utf-8")
        _log(f"  Suppressed Larian launcher mod warnings ({prefs}).")
    except (OSError, ValueError) as exc:
        _log(f"  Note: could not update launcher preferences: {exc}")


class BaldursGate3(BaseGame):

    # patch_version is a configured option, so make it per-profile (stored as a
    # paths.json extra via _load/_save_paths_extra).
    profile_overridable_paths_extras = ("patch_version",)

    def __init__(self):
        self._game_path: Path | None = None
        self._prefix_path: Path | None = None
        self._deploy_mode: LinkMode = LinkMode.HARDLINK
        self._staging_path: Path | None = None
        self._patch_version: int = 8
        self.load_paths()

    # -----------------------------------------------------------------------
    # Identity
    # -----------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "Baldur's Gate 3"

    @property
    def game_id(self) -> str:
        return "baldurs_gate_3"

    @property
    def exe_name(self) -> str:
        return "bin/bg3.exe"

    @property
    def exe_name_alts(self) -> list[str]:
        # Native Linux build ships a bare ELF binary at bin/bg3
        return ["bin/bg3"]

    @property
    def steam_id(self) -> str:
        return "1086940"

    @property
    def nexus_game_domain(self) -> str:
        return "baldursgate3"

    # Profile Groups: disabled for now — only Stardew Valley currently opts
    # in (see base_game.py's profile_groups_supported docstring) pending
    # wider review of the virtual-merge rework. Uncomment to enable once
    # this game's deploy path has been reviewed/tested.
    # @property
    # def profile_groups_supported(self) -> bool:
    #     return True

    @property
    def wizard_tools(self) -> list[WizardTool]:
        return self._base_wizard_tools() + [
            WizardTool(
                id="modio_api_key",
                label="mod.io API Key",
                description="Enter a mod.io key to enable update checks for "
                            "manually-installed mod.io mods.",
                dialog_class_path="wizards.modio_settings.ModioSettingsWizard",
                category="Update tracking",
            ),
        ]

    @property
    def mod_required_top_level_folders(self) -> set[str]:
        return {"data","bin","generated","public","video","mods"}
    
    @property
    def mod_required_file_types(self) -> set[str]:
        return {".pak"}
    
    @property
    def mod_auto_strip_until_required(self) -> bool:
        return True
    
    @property
    def mod_install_as_is_if_no_match(self) -> bool:
        return True

    @property
    def custom_routing_rules(self) -> list:
        return [
            CustomRule(dest="Data", folders=["generated"], flatten=True),
            CustomRule(dest="Data", folders=["public"], flatten=True),
            CustomRule(dest="Data", folders=["video"], flatten=True),
            # Loose files under Mods/ (unpacked mods) go to game Data/Mods,
            # but .pak files under Mods/ must reach the Larian AppData Mods
            # folder via the normal deploy (a common Nexus packaging layout).
            CustomRule(dest="Data", folders=["mods"], flatten=True,
                       exclude_extensions=[".pak"]),
            CustomRule(dest="Data", folders=["Cursors"], flatten=True),
            CustomRule(dest="bin", filenames=["DWrite.dll"], flatten=True),
            CustomRule(dest="", folders=["bin"], flatten=True),
            CustomRule(dest="", folders=["data"], flatten=True),
        ]
    
    @property
    def plugin_extensions(self) -> list[str]:
        return []

    @property
    def has_override_pak_tab(self) -> bool:
        return True

    @property
    def conflict_ignore_filenames(self) -> set[str]:
        return {"info.json","*.txt"}
    
    @property
    def frameworks(self) -> dict[str, str]:
        return {
                "Script Extender": "bin/DWrite.dll",
                "Native Mod Loader":"bin/bink2w64_original.dll"
            }

    @property
    def wine_dll_overrides(self) -> dict[str, str]:
        return {"DWrite": "native,builtin"}

    def runtime_snapshot_exclude_dirs(self) -> set[str] | None:
        # Custom rules route loose mods into Data/ (undone via restore_custom_rules)
        # and the .pak Mods folder lives outside the game root, so only capture
        # runtime-generated files sitting outside Data/.
        return {"Data"}

    # -----------------------------------------------------------------------
    # Paths
    # -----------------------------------------------------------------------

    def get_game_path(self) -> Path | None:
        return self._game_path

    def _larian_root(self) -> Path | None:
        """Return the Larian data root, preferring Proton prefix, then native Linux."""
        if self._prefix_path is not None:
            return self._prefix_path / _PREFIX_LARIAN_SUBPATH
        if _NATIVE_LARIAN_ROOT.is_dir():
            return _NATIVE_LARIAN_ROOT
        return None

    def get_mod_data_path(self) -> Path | None:
        """Mods deploy into the Larian AppData Mods folder (prefix or native)."""
        root = self._larian_root()
        return root / _MODS_REL if root is not None else None

    def get_mod_staging_path(self) -> Path:
        if self._staging_path is not None:
            return self._staging_path / "mods"
        return _PROFILES_DIR / self.name / "mods"

    def get_hardlink_deploy_targets(self) -> list[tuple[str, "Path | None"]]:
        if self._prefix_path is None and _NATIVE_LARIAN_ROOT.is_dir():
            data_target: Path | None = _NATIVE_LARIAN_ROOT
            label = "Larian data (native Linux)"
        else:
            data_target = self._prefix_path
            label = "Proton prefix"
        return [
            ("Game directory", self._game_path),
            (label, data_target),
        ]

    # -----------------------------------------------------------------------
    # Configuration persistence
    # -----------------------------------------------------------------------

    # load_paths / save_paths are inherited from BaseGame (profile-aware).
    # patch_version is persisted via the _load/_save_paths_extra hooks.
    def _load_paths_extra(self, data: dict) -> None:
        try:
            pv = int(data.get("patch_version", 8))
        except (TypeError, ValueError):
            pv = 8
        self._patch_version = pv if pv in (6, 7, 8) else 8

    def _save_paths_extra(self) -> dict:
        return {"patch_version": self._patch_version}

    # When the native Linux Larian build is present there is no Proton prefix to
    # look up; otherwise fall back to the standard steam_id lookup.
    def _find_prefix_for_load(self) -> "Path | None":
        if _NATIVE_LARIAN_ROOT.is_dir():
            return None
        return super()._find_prefix_for_load()

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

    def get_patch_version(self) -> int:
        return self._patch_version

    def set_patch_version(self, version: int) -> None:
        if version not in (6, 7, 8):
            version = 8
        self._patch_version = version
        self.save_paths()

    # -----------------------------------------------------------------------
    # Deployment
    # -----------------------------------------------------------------------

    def deploy(self, log_fn=None, mode: LinkMode = LinkMode.HARDLINK,
               profile: str = "default", progress_fn=None) -> None:
        """Deploy staged .pak mods into the Proton prefix Mods folder.

        Workflow:
          1. Move everything currently in the Mods folder → Mods_Core/
          2. Hard-link every .pak listed in filemap.txt into the Mods folder
          3. Hard-link vanilla .pak files from Mods_Core/ for anything not
             provided by a mod
          4. Generate modsettings.lsx so BG3 recognises the installed mods
        (Root Folder deployment is handled by the GUI after this returns.)
        """
        _log = log_fn or (lambda _: None)

        larian_root = self._larian_root()
        if larian_root is None:
            raise RuntimeError(
                "No Larian data folder found. Configure the Proton prefix, "
                f"or install the native Linux build so {_NATIVE_LARIAN_ROOT} exists."
            )

        mods_dir = larian_root / _MODS_REL
        filemap  = self.get_effective_filemap_path()
        staging  = self.get_effective_mod_staging_path()
        modlist  = self.get_profile_root() / "profiles" / profile / "modlist.txt"

        # Tell the user exactly where mods are being deployed. The Larian data
        # folder lives either inside the Proton prefix or in the native Linux
        # build's ~/.local/share — diagnosing "mods not loading" almost always
        # starts with confirming which one we targeted.
        if self._prefix_path is not None:
            _log(f"Deploy target: Proton prefix ({self._prefix_path})")
        else:
            _log("Deploy target: native Linux build")
        _log(f"  Larian data root: {larian_root}")
        _log(f"  Mods folder:      {mods_dir}")
        _log(f"  Game path:        {self._game_path or '(not set)'}")
        _log(f"  Staging:          {staging}")
        _log(f"  Deploy mode:      {mode.name}")

        mods_dir.mkdir(parents=True, exist_ok=True)

        if not filemap.is_file():
            raise RuntimeError(
                f"filemap.txt not found: {filemap}\n"
                "Run 'Build Filemap' before deploying."
            )

        profile_dir = self.get_profile_root() / "profiles" / profile
        per_mod_strip = load_per_mod_strip_prefixes(profile_dir)

        # Separator overrides — loaded from the real profile_dir and passed
        # explicitly so shared-staging layouts get the right link modes.
        _sep_deploy = load_separator_deploy_paths(profile_dir)
        _sep_entries = read_modlist(profile_dir / "modlist.txt") if _sep_deploy else []
        per_mod_deploy = expand_separator_deploy_paths(_sep_deploy, _sep_entries) or None
        per_mod_modes = expand_separator_link_modes(_sep_deploy, _sep_entries) or None
        per_mod_raw = expand_separator_raw_deploy(_sep_deploy, _sep_entries) or None

        custom_rules = self.custom_routing_rules
        custom_exclude: set[str] = set()
        if custom_rules and self._game_path:
            _log("Step 1a: Routing bin/ and generated/ files via custom rules ...")
            custom_exclude = deploy_custom_rules(
                filemap, self._game_path, staging,
                rules=custom_rules,
                mode=mode,
                strip_prefixes=self.mod_folder_strip_prefixes,
                per_mod_strip_prefixes=per_mod_strip,
                per_mod_link_modes=per_mod_modes,
                log_fn=_log,
                raw_mods=per_mod_raw,
            )
            _log(f"  Routed {len(custom_exclude)} file(s) to Data/.")

        _log("Step 1: Moving Mods/ → Mods_Core/ ...")
        move_to_core(mods_dir, log_fn=_log)
        _log("  Backed up existing files → Mods_Core/.")

        _log(f"Step 2: Transferring mod .pak files into Mods/ ({mode.name}) ...")
        linked_mod, placed = deploy_filemap(filemap, mods_dir, staging,
                                            mode=mode,
                                            strip_prefixes=self.mod_folder_strip_prefixes,
                                            per_mod_strip_prefixes=per_mod_strip,
                                            per_mod_deploy_dirs=per_mod_deploy,
                                            per_mod_link_modes=per_mod_modes,
                                            log_fn=_log,
                                            progress_fn=progress_fn,
                                            exclude=custom_exclude or None,
                                            core_dir=mods_dir.parent / (mods_dir.name + "_Core"),
                                            # The game only loads .pak files at
                                            # the top level of the Mods folder.
                                            flatten_extensions={".pak"})
        _log(f"  Transferred {linked_mod} mod file(s).")

        _log("Step 3: Filling gaps with vanilla files from Mods_Core/ ...")
        linked_core = deploy_core(mods_dir, placed, mode=mode, log_fn=_log)
        _log(f"  Transferred {linked_core} vanilla file(s).")

        modsettings = larian_root / _MODSETTINGS_REL
        _log(f"Step 4: Generating modsettings.lsx → {modsettings}")
        if not modsettings.parent.is_dir():
            _log(f"  Note: profile folder {modsettings.parent} does not exist "
                 "yet — creating it (game may not have generated a profile).")
        game_data = self._game_path / "Data" if self._game_path else None
        # If this profile was created from a collection, the manifest's
        # loadOrder array drives the pak ordering. Curators interleave paks
        # from different mods (e.g. load-order divider packs whose 30+ entries
        # span the full LO), which the default folder-walk order destroys.
        manifest_lo = None
        collection_json = profile_dir / "collection.json"
        if collection_json.is_file():
            try:
                cj = json.loads(collection_json.read_text(encoding="utf-8"))
                lo = cj.get("loadOrder")
                if isinstance(lo, list) and lo:
                    manifest_lo = lo
                    _log(f"  Using collection manifest load order ({len(lo)} entries).")
            except (OSError, json.JSONDecodeError) as exc:
                _log(f"  Warning: could not read collection.json: {exc}")
        se_dll = (self._game_path / "bin" / "DWrite.dll"
                  if self._game_path else None)
        if se_dll is not None and not se_dll.is_file():
            alt = se_dll.with_name("dwrite.dll")
            if alt.is_file():
                se_dll = alt
        mod_count = write_modsettings(modsettings, modlist, staging,
                                      log_fn=_log,
                                      game_data_path=game_data,
                                      patch_version=self._patch_version,
                                      manifest_load_order=manifest_lo,
                                      script_extender_dll=se_dll)

        _suppress_launcher_mod_warnings(larian_root, log_fn=_log)

        # Snapshot the game root so restore() can sweep any runtime-generated
        # files (outside Data/) into Root_Folder/ and preserve them. Deferred
        # by the pipeline so the snapshot lands after Root_Folder files deploy.
        self.snapshot_root_for_runtime_capture(log_fn=_log)

        _log(
            f"Deploy complete. "
            f"{linked_mod} mod + {linked_core} vanilla "
            f"= {linked_mod + linked_core} total file(s) in {mods_dir}. "
            f"modsettings.lsx ({mod_count} mod(s)) at {modsettings}."
        )

    def restore(self, log_fn=None, progress_fn=None) -> None:
        """Remove deployed mods and restore the vanilla Mods folder."""
        _log = log_fn or (lambda _: None)

        larian_root = self._larian_root()
        if larian_root is None:
            raise RuntimeError(
                "No Larian data folder found. Configure the Proton prefix, "
                f"or install the native Linux build so {_NATIVE_LARIAN_ROOT} exists."
            )

        mods_dir = larian_root / _MODS_REL

        # Undo custom-routed files (bin/ and generated/ → game root / Data/)
        if self._game_path:
            custom_rules = self.custom_routing_rules
            if custom_rules:
                _log("Restore: removing custom-routed files (bin/, generated/) ...")
                restore_custom_rules(
                    self.get_effective_filemap_path(),
                    self._game_path,
                    rules=custom_rules,
                    log_fn=_log,
                )

        _profile_dir = self._active_profile_dir
        _entries = read_modlist(_profile_dir / "modlist.txt") if _profile_dir else []
        cleanup_custom_deploy_dirs(_profile_dir, _entries, log_fn=_log)

        _log("Restore: clearing Mods/ and moving Mods_Core/ back ...")
        restored = restore_data_core(mods_dir, overwrite_dir=self.get_effective_overwrite_path(), log_fn=_log)
        _log(f"  Restored {restored} file(s). Mods_Core/ removed.")

        _log("Restore: resetting modsettings.lsx to vanilla ...")
        modsettings = larian_root / _MODSETTINGS_REL
        write_vanilla_modsettings(modsettings, log_fn=_log,
                                  patch_version=self._patch_version)

        # Sweep runtime-generated files (outside Data/) into Root_Folder/ so they
        # re-deploy next time instead of being clobbered by the vanilla restore.
        moved = self.capture_runtime_files_to_root_folder(log_fn=_log)
        if moved:
            _log(f"  Moved {moved} runtime file(s) to Root_Folder/.")

        _log("Restore complete.")

    def post_clean_game_folder(self, log_fn=None) -> None:
        """Reset modsettings.lsx to vanilla after Clean Game Folder."""
        larian_root = self._larian_root()
        if larian_root is None:
            return
        modsettings = larian_root / _MODSETTINGS_REL
        write_vanilla_modsettings(modsettings, log_fn=log_fn,
                                  patch_version=self._patch_version)
