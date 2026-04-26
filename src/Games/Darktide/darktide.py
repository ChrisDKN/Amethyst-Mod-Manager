"""
darktide.py
Game handler for Warhammer 40,000: Darktide.

Mod structure:
  Mods install into <game_path>/mods/<ModFolder>/
  Staged mods live in Profiles/Darktide/mods/

  Each mod staging entry must have a single named subdirectory at its root
  (the actual folder that lands inside <game>/mods/).

  Darktide Mod Loader (DML) and its associated files are NOT managed as
  regular mods — they are installed to the game root via Root_Folder staging.

  After every deploy, mod_load_order.txt is written from the enabled modlist
  in priority order (bottom of modlist → first line in file, per DML spec).
  The folders 'base' (DML core) and 'dmf' (Darktide Mod Framework) are
  excluded from mod_load_order.txt automatically.

  The game must be patched with dtkit-patch after every game update.
  dtkit-patch is run automatically during deploy (--patch) and restore (--unpatch).
  If the binary is not present it is downloaded from GitHub on first use.
  A wizard is also available for manual control (see wizards/dtkit_patch.py).
"""

import json
from pathlib import Path

from Games.base_game import BaseGame, WizardTool
from Utils.deploy import (
    LinkMode,
    cleanup_custom_deploy_dirs,
    deploy_core,
    deploy_custom_rules,
    deploy_filemap,
    expand_separator_deploy_paths,
    load_per_mod_strip_prefixes,
    load_separator_deploy_paths,
    move_to_core,
    restore_custom_rules,
    restore_data_core,
)
from Utils.modlist import read_modlist
from Utils.config_paths import get_profiles_dir
from Utils.steam_finder import find_prefix
from Utils.dtkit_patch_helper import run_dtkit_patch as _dtkit_run

_PROFILES_DIR = get_profiles_dir()

class Darktide(BaseGame):

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
        return "Darktide"

    @property
    def game_id(self) -> str:
        return "darktide"

    @property
    def exe_name(self) -> str:
        # Darktide.exe lives inside the binaries/ subfolder
        return "binaries/Darktide.exe"

    @property
    def exe_name_alts(self) -> list[str]:
        return ["launcher/Launcher.exe"]

    @property
    def steam_id(self) -> str:
        return "1361210"

    @property
    def nexus_game_domain(self) -> str:
        return "warhammer40kdarktide"

    # -----------------------------------------------------------------------
    # Mod structure
    # -----------------------------------------------------------------------

    @property
    def mods_dir(self) -> str:
        return "mods"

    @property
    def mod_folder_strip_prefixes(self) -> set[str]:
        return {"mods"}

    @property
    def normalize_folder_case(self) -> bool:
        return False

    @property
    def loot_sort_enabled(self) -> bool:
        return False

    @property
    def conflict_ignore_filenames(self) -> set[str]:
        return {"readme.md", "meta.ini"}

    @property
    def custom_routing_rules(self) -> list:
        from Utils.deploy import CustomRule
        return [
            CustomRule(dest="", folders=["binaries"], flatten=True),
            CustomRule(dest="", folders=["bundle"], flatten=True),
            CustomRule(dest="", folders=["tools"], flatten=True),
            CustomRule(dest="", filenames=["toggle_darktide_mods.bat"], flatten=True),
        ]

    # -----------------------------------------------------------------------
    # Frameworks / DLL detection
    # -----------------------------------------------------------------------

    @property
    def frameworks(self) -> dict[str, str]:
        return {"Darktide Mod Loader": "binaries/mod_loader","Darktide Mod Framework": "mods/dmf/dmf.mod"}

    # -----------------------------------------------------------------------
    # Wizard tools
    # -----------------------------------------------------------------------

    @property
    def wizard_tools(self) -> list[WizardTool]:
        return self._base_wizard_tools() + [
            WizardTool(
                id="run_dtkit_patch",
                label="Patch Game (dtkit-patch)",
                description=(
                    "Download and run dtkit-patch to enable Darktide Mod Loader. "
                    "Re-run this wizard after every game update."
                ),
                dialog_class_path="wizards.dtkit_patch.DtkitPatchWizard",
            ),
        ]

    # -----------------------------------------------------------------------
    # Paths
    # -----------------------------------------------------------------------

    def get_game_path(self) -> Path | None:
        return self._game_path

    def get_mod_data_path(self) -> Path | None:
        """Mods go into the mods/ subfolder of the game root directory."""
        if self._game_path is None:
            return None
        return self._game_path / self.mods_dir

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
                "copy":    LinkMode.SYMLINK,
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

    def set_game_path(self, path: Path | str | None) -> None:
        self._game_path = Path(path) if path else None
        self.save_paths()

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
        """Deploy staged mods into mods/.

        Workflow:
          1. Move mods/ → mods_Core/  (vanilla backup)
          2. Transfer mod files listed in filemap.txt into mods/
          3. Fill gaps with vanilla files from mods_Core/
          4. Write mod_load_order.txt from the enabled modlist
        (Root Folder deployment is handled by the GUI after this returns.)
        """
        _log = log_fn or (lambda _: None)

        if self._game_path is None:
            raise RuntimeError("Game path is not configured.")

        mods_dir = self._game_path / self.mods_dir
        filemap  = self.get_effective_filemap_path()
        staging  = self.get_effective_mod_staging_path()
        core     = self.mods_dir + "_Core"

        if not filemap.is_file():
            raise RuntimeError(
                f"filemap.txt not found: {filemap}\n"
                "Run 'Build Filemap' before deploying."
            )

        profile_dir = self.get_profile_root() / "profiles" / profile
        per_mod_strip = load_per_mod_strip_prefixes(profile_dir)

        custom_rules = self.custom_routing_rules
        custom_exclude: set[str] = set()
        if custom_rules:
            _log("Step 0: Routing game-root files via custom rules ...")
            custom_exclude = deploy_custom_rules(
                filemap, self._game_path, staging,
                rules=custom_rules,
                mode=mode,
                strip_prefixes=self.mod_folder_strip_prefixes,
                per_mod_strip_prefixes=per_mod_strip,
                log_fn=_log,
                progress_fn=progress_fn,
            )

        _log(f"Step 1: Moving {mods_dir.name}/ → {core}/ ...")
        moved = move_to_core(mods_dir, log_fn=_log)
        _log(f"  Moved {moved} file(s) to {core}/.")
        mods_dir.mkdir(parents=True, exist_ok=True)

        _log(f"Step 2: Transferring mod files into {mods_dir} ({mode.name}) ...")
        _sep_deploy = load_separator_deploy_paths(profile_dir)
        _sep_entries = read_modlist(profile_dir / "modlist.txt") if _sep_deploy else []
        per_mod_deploy = expand_separator_deploy_paths(_sep_deploy, _sep_entries) or None
        linked_mod, placed = deploy_filemap(
            filemap, mods_dir, staging,
            mode=mode,
            strip_prefixes=self.mod_folder_strip_prefixes,
            per_mod_strip_prefixes=per_mod_strip,
            per_mod_deploy_dirs=per_mod_deploy,
            log_fn=_log,
            progress_fn=progress_fn,
            exclude=custom_exclude or None,
            core_dir=mods_dir.parent / (mods_dir.name + "_Core"),
        )
        _log(f"  Transferred {linked_mod} mod file(s).")

        _log(f"Step 3: Filling gaps with vanilla files from {core}/ ...")
        linked_core = deploy_core(mods_dir, placed, mode=mode, log_fn=_log)
        _log(f"  Transferred {linked_core} vanilla file(s).")

        _log("Step 4: Writing mod_load_order.txt ...")
        self._write_mod_load_order(profile_dir, _log)

        _log("Step 5: Patching game with dtkit-patch ...")
        _dtkit_run(self._game_path, "--patch", _log)

        _log(
            f"Deploy complete. "
            f"{linked_mod} mod + {linked_core} vanilla "
            f"= {linked_mod + linked_core} total file(s) in {mods_dir.name}/."
        )

    def restore(self, log_fn=None, progress_fn=None) -> None:
        """Restore mods/ to its vanilla state."""
        _log = log_fn or (lambda _: None)

        if self._game_path is None:
            raise RuntimeError("Game path is not configured.")

        mods_dir = self._game_path / self.mods_dir
        core     = self.mods_dir + "_Core"
        core_dir = self._game_path / core

        _profile_dir = self._active_profile_dir
        _entries = read_modlist(_profile_dir / "modlist.txt") if _profile_dir else []
        cleanup_custom_deploy_dirs(_profile_dir, _entries, log_fn=_log)

        custom_rules = self.custom_routing_rules
        if custom_rules and self._game_path:
            _log("Restore: removing custom-routed files ...")
            restore_custom_rules(
                self.get_effective_filemap_path(),
                self._game_path,
                rules=custom_rules,
                log_fn=_log,
            )

        if core_dir.is_dir():
            _log(f"Restore: clearing {mods_dir.name}/ and moving {core}/ back ...")
            restored = restore_data_core(
                mods_dir,
                core_dir=core_dir,
                overwrite_dir=self.get_effective_overwrite_path(),
                log_fn=_log,
            )
            _log(f"  Restored {restored} file(s). {core}/ removed.")
        else:
            _log(f"Restore: no {core}/ found — nothing to restore.")

        _log("Restore: unpatching game with dtkit-patch ...")
        _dtkit_run(self._game_path, "--unpatch", _log)

        _log("Restore complete.")

    # -----------------------------------------------------------------------
    # Post-deploy: mod_load_order.txt and launcher swap hook
    # -----------------------------------------------------------------------

    def swap_launcher(self, log_fn=None) -> None:
        """Re-write mod_load_order.txt after Root_Folder deploy.

        The GUI calls swap_launcher() after Root_Folder files are copied to
        the game root.  If the user has DML's template mod_load_order.txt in
        their Root_Folder staging, it would overwrite the file written during
        deploy().  Re-writing it here ensures our generated file always wins.
        """
        _log = log_fn or (lambda _: None)
        profile_dir = self._active_profile_dir
        if profile_dir is None:
            return
        self._write_mod_load_order(profile_dir, _log)

    def _write_mod_load_order(self, profile_dir: Path, log_fn) -> None:
        """Generate <game_path>/mods/mod_load_order.txt from the enabled modlist.

        DML loads mods in the order listed in the file.  We write the modlist
        in reverse priority order so that the highest-priority mod in the mod
        manager loads last (and therefore wins conflicts) — matching the
        behaviour users expect from a standard mod manager.

        Only subdirectories containing a .mod file are included; the stem of
        that file is used as the entry name (e.g. range_finder.mod → range_finder).
        """
        _log = log_fn or (lambda _: None)

        if self._game_path is None:
            return

        staging = self.get_effective_mod_staging_path()
        modlist_path = profile_dir / "modlist.txt"

        if not modlist_path.is_file():
            _log("  mod_load_order.txt: modlist.txt not found — skipping.")
            return

        entries = read_modlist(modlist_path)

        # Collect the deployed subfolder names for each enabled, non-separator mod.
        # Each staging entry is a folder; its subdirectory is the actual mod folder
        # that lands in <game>/mods/ (because mod_staging_requires_subdir=True).
        # These subdirectory names are always excluded from mod_load_order.txt.
        # 'base' is the DML core folder; 'dmf' is Darktide Mod Framework.
        _EXCLUDED_SUBDIRS = {"base", "dmf"}

        load_order: list[str] = []
        for entry in reversed(entries):
            if not entry.enabled or entry.is_separator:
                continue
            staging_mod_dir = staging / entry.name
            if not staging_mod_dir.is_dir():
                continue
            for sub in staging_mod_dir.iterdir():
                if not sub.is_dir():
                    continue
                if sub.name in _EXCLUDED_SUBDIRS:
                    continue
                mod_files = list(sub.glob("*.mod"))
                if not mod_files:
                    continue
                load_order.append(mod_files[0].stem)

        mods_dir = self._game_path / self.mods_dir
        mods_dir.mkdir(parents=True, exist_ok=True)
        order_file = mods_dir / "mod_load_order.txt"

        order_file.write_text("\n".join(load_order) + "\n", encoding="utf-8")
        _log(f"  Wrote mod_load_order.txt ({len(load_order)} mod(s)).")
