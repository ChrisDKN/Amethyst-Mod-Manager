"""
red_dead_redemption_2.py
Game handler for Red Dead Redemption 2.

Mod structure:
  Mods install into <game root>/lml/ (Lenny's Mod Loader).
  Staged mods live in Profiles/Red Dead Redemption 2/mods/.

  Loader binaries (dinput8.dll, ScriptHookRDR2.dll, *.asi, lml.ini,
  vfs.asi, NLog.dll, ModManager.*.dll and the x64/ folder) are routed to
  the game install root via custom routing rules.
"""

from pathlib import Path

from Games.base_game import BaseGame
from Utils.deploy import (
    CustomRule,
    LinkMode,
    deploy_core,
    deploy_custom_rules,
    deploy_filemap,
    load_per_mod_strip_prefixes,
    load_separator_deploy_paths,
    expand_separator_deploy_paths,
    expand_separator_link_modes, expand_separator_raw_deploy,
    cleanup_custom_deploy_dirs,
    move_to_core,
    restore_custom_rules,
    restore_data_core,
)
from Utils.modlist import read_modlist
from Utils.config_paths import get_profiles_dir

_PROFILES_DIR = get_profiles_dir()


class RedDeadRedemption2(BaseGame):

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
        return "Red Dead Redemption 2"

    @property
    def game_id(self) -> str:
        return "red_dead_redemption_2"

    @property
    def exe_name(self) -> str:
        return "RDR2.exe"

    @property
    def steam_id(self) -> str:
        return "1174180"

    @property
    def nexus_game_domain(self) -> str:
        return "reddeadredemption2"

    # Profile Groups: disabled for now — only Stardew Valley currently opts
    # in (see base_game.py's profile_groups_supported docstring) pending
    # wider review of the virtual-merge rework. Uncomment to enable once
    # this game's deploy path has been reviewed/tested.
    # @property
    # def profile_groups_supported(self) -> bool:
    #     return True

    @property
    def reshade_dll(self) -> str:
        return "dxgi.dll"

    @property
    def mods_dir(self) -> str:
        return "lml"

    def runtime_snapshot_exclude_dirs(self) -> set[str] | None:
        # lml/ is reverted via its _Core backup; capture only files outside it.
        return {self.mods_dir.split("/")[0]}

    @property
    def mod_required_top_level_folders(self) -> set[str]:
        return {"lml"}

    @property
    def mod_folder_strip_prefixes_post(self) -> set[str]:
        return {"lml"}

    @property
    def mod_auto_strip_until_required(self) -> bool:
        return True

    @property
    def mod_install_as_is_if_no_match(self) -> bool:
        return True

    @property
    def conflict_ignore_filenames(self) -> set[str]:
        return {"*.txt","*.pdf"}

    @property
    def wine_dll_overrides(self) -> dict[str, str]:
        return {"dinput8": "native,builtin"}

    @property
    def custom_routing_rules(self) -> list[CustomRule]:
        return [
            CustomRule(dest="", filenames=["dinput8.dll"], flatten=True),
            CustomRule(dest="", filenames=["ScriptHookRDR2.dll"], flatten=True),
            # ASI plugins ship alongside same-named .ini configs that belong
            # next to the .asi at the game root (Lenny's Mod Loader / ASI
            # Loader convention).
            CustomRule(dest="", extensions=[".asi"], companion_extensions=[".ini"], flatten=True),
            CustomRule(dest="", filenames=["ModManager.Core.dll"], flatten=True),
            CustomRule(dest="", filenames=["ModManager.NativeInterop.dll"], flatten=True),
            CustomRule(dest="", filenames=["NLog.dll"], flatten=True),
            CustomRule(dest="", filenames=["lml.ini"], flatten=True),
            CustomRule(dest="", filenames=["vfs.asi"], flatten=True),
            CustomRule(dest="", folders=["x64"], flatten=True),
            CustomRule(dest="", folders=["RampageFiles"], flatten=True),
        ]

    @property
    def frameworks(self) -> dict[str, str]:
        return {
            "ScriptHookRDR2": "ScriptHookRDR2.dll",
            "Lenny's Mod Loader": "ModManager.Core.dll",
        }

    # -----------------------------------------------------------------------
    # Paths
    # -----------------------------------------------------------------------

    def get_game_path(self) -> Path | None:
        return self._game_path

    def get_mod_data_path(self) -> Path | None:
        """Mods go into lml/ inside the game directory."""
        if self._game_path is None:
            return None
        return self._game_path / self.mods_dir

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
    # Deployment
    # -----------------------------------------------------------------------

    def deploy(self, log_fn=None, mode: LinkMode = LinkMode.HARDLINK,
               profile: str = "default", progress_fn=None) -> None:
        """Deploy staged mods into <game root>/lml/.

        Workflow:
          1. Route loader binaries (*.asi, dinput8.dll, x64/, etc.) to game root
          2. Move lml/ → lml_Core/  (vanilla backup)
          3. Transfer mod files listed in filemap.txt into lml/
          4. Fill gaps with vanilla files from lml_Core/
        (Root Folder deployment is handled by the GUI after this returns.)
        """
        _log = log_fn or (lambda _: None)

        if self._game_path is None:
            raise RuntimeError("Game path is not configured.")

        game_root = self._game_path
        data_dir = self._game_path / self.mods_dir
        filemap = self.get_effective_filemap_path()
        staging = self.get_effective_mod_staging_path()
        core = self.mods_dir + "_Core"

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
        if custom_rules:
            _log("Step 1: Routing loader binaries to game root ...")
            custom_exclude = deploy_custom_rules(
                filemap, game_root, staging,
                rules=custom_rules,
                mode=mode,
                strip_prefixes=self.mod_folder_strip_prefixes,
                per_mod_strip_prefixes=per_mod_strip,
                per_mod_link_modes=per_mod_modes,
                log_fn=_log,
                raw_mods=per_mod_raw,
            )
            _log(f"Step 2: Moving {data_dir.name}/ → {core}/ ...")
        else:
            _log(f"Step 1: Moving {data_dir.name}/ → {core}/ ...")
        move_to_core(data_dir, log_fn=_log)
        _log(f"  Backed up existing files → {core}/.")
        data_dir.mkdir(parents=True, exist_ok=True)

        _log(f"{'Step 3' if custom_rules else 'Step 2'}: Transferring mod files into {data_dir} ({mode.name}) ...")
        linked_mod, placed = deploy_filemap(
            filemap, data_dir, staging,
            mode=mode,
            strip_prefixes=self.mod_folder_strip_prefixes,
            per_mod_strip_prefixes=per_mod_strip,
            per_mod_deploy_dirs=per_mod_deploy,
            per_mod_link_modes=per_mod_modes,
            log_fn=_log,
            progress_fn=progress_fn,
            exclude=custom_exclude or None,
            core_dir=data_dir.parent / (data_dir.name + "_Core"),
        )
        _log(f"  Transferred {linked_mod} mod file(s).")

        _log(f"{'Step 4' if custom_rules else 'Step 3'}: Filling gaps with vanilla files from {core}/ ...")
        linked_core = deploy_core(data_dir, placed, mode=mode, log_fn=_log)
        _log(f"  Transferred {linked_core} vanilla file(s).")

        _log(
            f"Deploy complete. "
            f"{linked_mod} mod + {linked_core} vanilla "
            f"= {linked_mod + linked_core} total file(s) in {data_dir.name}/."
        )

        # Capture runtime files generated outside lml/ on the next restore.
        self.snapshot_root_for_runtime_capture(log_fn=_log)

    def restore(self, log_fn=None, progress_fn=None) -> None:
        """Restore lml/ to vanilla and remove custom-routed loader binaries."""
        _log = log_fn or (lambda _: None)

        if self._game_path is None:
            raise RuntimeError("Game path is not configured.")

        game_root = self._game_path
        data_dir = self._game_path / self.mods_dir
        core = self.mods_dir + "_Core"
        core_dir = self._game_path / core

        _profile_dir = self._active_profile_dir
        _entries = read_modlist(_profile_dir / "modlist.txt") if _profile_dir else []
        cleanup_custom_deploy_dirs(_profile_dir, _entries, log_fn=_log)

        custom_rules = self.custom_routing_rules
        if custom_rules:
            _log("Restore: removing custom-routed loader binaries ...")
            restore_custom_rules(
                self.get_effective_filemap_path(), game_root,
                rules=custom_rules, log_fn=_log,
            )

        _log(f"Restore: clearing {data_dir.name}/ and moving {core}/ back if present ...")
        restored = restore_data_core(
            data_dir, core_dir=core_dir,
            overwrite_dir=self.get_effective_overwrite_path(), log_fn=_log,
        )
        if restored > 0:
            _log(f"  Restored {restored} file(s). {core}/ removed.")

        moved = self.capture_runtime_files_to_root_folder(log_fn=_log)
        if moved:
            _log(f"  Moved {moved} runtime file(s) to Root_Folder/.")

        _log("Restore complete.")
