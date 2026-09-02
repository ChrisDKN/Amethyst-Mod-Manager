"""
dragon_age_origins.py
Game handler for Dragon Age: Origins (DAO).

Mod structure
-------------
DAO has no single mod folder.  Content is spread across several locations
under the per-user data folder:

    <prefix>/drive_c/users/steamuser/Documents/BioWare/Dragon Age/
        packages/core/override/   loose-file overrides (.mop/.mmh/.tnt/.dds/.gda...)
        AddIns/<UID>/             DLC-style content (from .dazip)  + Manifest.xml
        Offers/<UID>/             store/offer content (from .dazip) + Manifest.xml
        Settings/AddIns.xml       generated registry of all installed AddIns
        Settings/Offers.xml       generated registry of all installed Offers
        packages/core/override/chargenmorphcfg.xml   generated char-gen registry

Binaries (patched DAOrigins.exe, DXVK, DAFIX, *.dll/.asi) live in the *game
install* directory under ``bin_ship/`` - NOT the data folder.

This handler deploys loose overrides + AddIns/Offers into the data folder
(which lives inside the Proton prefix), routes ``bin_ship/`` files to the game
root via a custom rule, and regenerates AddIns.xml / Offers.xml /
chargenmorphcfg.xml at deploy time (undone on restore).

Note: DAO is commonly run via a non-Steam shortcut (custom appid) or a
GOG/loose install, so the data path cannot be derived from a fixed Steam
appid - the prefix path is user-configurable, with a best-effort search.
"""

from pathlib import Path

from Games.base_game import BaseGame
from Utils.deployment import (
    CustomRule, LinkMode, deploy_filemap, deploy_core, move_to_core,
    restore_data_core, deploy_custom_rules, load_per_mod_strip_prefixes,
    load_separator_deploy_paths, expand_separator_deploy_paths,
    expand_separator_link_modes, expand_separator_raw_deploy,
    cleanup_custom_deploy_dirs, restore_custom_rules,
)
from Utils.mods.modlist import read_modlist
from Utils.config_paths import get_profiles_dir

_PROFILES_DIR = get_profiles_dir()

# Relative path from a Proton prefix root to the DAO data folder.
_PREFIX_DAO_SUBPATH = Path(
    "drive_c/users/steamuser/Documents/BioWare/Dragon Age"
)

# Mod-managed subfolders within the DAO data folder. These are the ONLY
# folders the manager ever moves/clears during deploy/restore. The game's own
# files - Settings/ (AddIns.xml, INIs, Profile.dap), Logs/, and the vanilla
# packages/core/data - must never be touched.
_MANAGED_SUBDIRS = (
    Path("packages/core/override"),
    Path("AddIns"),
    Path("Offers"),
)


class DragonAgeOrigins(BaseGame):

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
        return "Dragon Age Origins"

    @property
    def game_id(self) -> str:
        return "dragon_age_origins"

    @property
    def exe_name(self) -> str:
        return "bin_ship/DAOrigins.exe"

    @property
    def exe_name_alts(self) -> list[str]:
        # GOG/loose installs ship a lowercase binary; launcher is an option too.
        return ["bin_ship/daorigins.exe", "DAOriginsLauncher.exe"]

    @property
    def steam_id(self) -> str:
        return "47810"

    @property
    def nexus_game_domain(self) -> str:
        return "dragonage"

    # DAO mods ship in wildly inconsistent shapes. Rather than pre-strip to a
    # required top-level folder (which can't capture every layout), we install
    # everything as-is and let normalize_dao_mod restructure the staged tree
    # into packages/core/override (see additional_install_logic). This keeps
    # the filemap destinations correct without per-mod guesswork.
    @property
    def mod_install_as_is_if_no_match(self) -> bool:
        return True

    @property
    def additional_install_logic(self) -> list:
        return [self._load_dao_module("dao_install").normalize_dao_mod]

    @staticmethod
    def _load_dao_module(stem: str):
        """Load a sibling DAO module (dao_install, dao_xml) by file path.

        The handler is imported via spec_from_file_location, so its folder is
        not on sys.path and a plain ``import`` fails. Resolve the sibling file
        relative to this module instead.
        """
        import importlib.util
        sibling = Path(__file__).resolve().parent / f"{stem}.py"
        spec = importlib.util.spec_from_file_location(
            f"{stem}_dao_origins", str(sibling)
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    @property
    def custom_routing_rules(self) -> list:
        # bin_ship binaries belong in the game install root, not the data
        # folder.  Everything else (packages/, AddIns/, Offers/) is already
        # normalized into the data-folder layout at install time and deploys
        # via the normal filemap path.
        return [
            CustomRule(dest="", folders=["bin_ship"], flatten=False),
        ]

    @property
    def conflict_ignore_filenames(self) -> set[str]:
        # NOTE: conflict_ignore_filenames excludes matched files from the
        # filemap ENTIRELY (filemap.py skips them on deploy), not just from
        # conflict reporting.
        #   - chargenmorphcfg.xml: MUST be excluded. DAO reads every
        #     chargenmorphcfg.xml under packages/core/override RECURSIVELY, so
        #     deploying each mod's fragment makes them fight (chargen breaks).
        #     We instead merge them all into ONE file at the override root in
        #     deploy Step 5 (read from staging, so exclusion here is fine).
        #   - Manifest.xml: NOT excluded - filed under addins/<uid>/ and the
        #     game expects it there; registry XML is built from staging anyway.
        #   - *.txt: readme clutter, safe to drop.
        return {"chargenmorphcfg.xml", "*.txt"}

    @property
    def restore_on_close_eligible(self) -> bool:
        # Restore is cheap (link removal + XML regen); keep eligible.
        return True

    # -----------------------------------------------------------------------
    # Paths
    # -----------------------------------------------------------------------

    def get_game_path(self) -> Path | None:
        return self._game_path

    def _dao_data_root(self) -> Path | None:
        """Return the DAO data folder inside the Proton prefix."""
        if self._prefix_path is not None:
            return self._prefix_path / _PREFIX_DAO_SUBPATH
        return None

    def get_mod_data_path(self) -> Path | None:
        """Mods deploy into the DAO data folder (inside the prefix)."""
        return self._dao_data_root()

    def get_mod_staging_path(self) -> Path:
        if self._staging_path is not None:
            return self._staging_path / "mods"
        return _PROFILES_DIR / self.name / "mods"

    def get_hardlink_deploy_targets(self) -> list[tuple[str, "Path | None"]]:
        return [
            ("Game directory", self._game_path),
            ("Proton prefix (data folder)", self._prefix_path),
        ]

    # -----------------------------------------------------------------------
    # Configuration persistence
    # -----------------------------------------------------------------------

    # load_paths / save_paths are inherited from BaseGame (profile-aware).
    # DAO deploys via symlink/hardlink only, so the default copy→symlink mapping
    # and prefix auto-discovery via steam_id are exactly what the base provides.

    def set_staging_path(self, path: "Path | str | None") -> None:
        self._staging_path = Path(path) if path else None
        self.save_paths()

    def get_prefix_path(self) -> Path | None:
        return self._prefix_path

    def set_prefix_path(self, path: Path | str | None) -> None:
        self._prefix_path = Path(path) if path else None
        self.save_paths()

    def get_deploy_mode(self) -> LinkMode:
        return self._deploy_mode

    def set_deploy_mode(self, mode: LinkMode) -> None:
        self._deploy_mode = mode
        self.save_paths()

    # -----------------------------------------------------------------------
    # Deployment
    # -----------------------------------------------------------------------

    def _core_dir_for(self, subdir: Path) -> Path:
        """Return the per-subfolder vanilla backup dir, e.g.
        packages/core/override → packages/core/override_Core."""
        return subdir.parent / (subdir.name + "_Core")

    def deploy(self, log_fn=None, mode: LinkMode = LinkMode.HARDLINK,
               profile: str = "default", progress_fn=None) -> None:
        """Deploy staged DAO mods into the data folder.

        Workflow (Phase 1 - overrides + AddIns/Offers + bin_ship routing):
          1. Route bin_ship/ files to the game install root (custom rule)
          2. Back up ONLY the managed subfolders (override, AddIns, Offers)
             - each to its own <subdir>_Core. The game's Settings/, Logs/ and
             vanilla packages/core/data are never touched.
          3. Link mod files from filemap.txt into the data root (filemap paths
             are data-root-relative so they land in the right subfolders)
          4. Fill gaps per-subfolder from the matching <subdir>_Core

        Phase 2 will append AddIns.xml / Offers.xml / chargenmorphcfg.xml
        generation here.
        """
        _log = log_fn or (lambda _: None)

        data_root = self._dao_data_root()
        if data_root is None:
            raise RuntimeError(
                "No Dragon Age data folder found. Configure the Proton prefix "
                "in the game settings (Documents/BioWare/Dragon Age lives "
                "inside it)."
            )

        deploy_dir = data_root
        filemap = self.get_effective_filemap_path()
        staging = self.get_effective_mod_staging_path()

        _log(f"Deploy target: Proton prefix ({self._prefix_path})")
        _log(f"  DAO data root: {data_root}")
        _log(f"  Game path:     {self._game_path or '(not set)'}")
        _log(f"  Staging:       {staging}")
        _log(f"  Deploy mode:   {mode.name}")
        _log(f"  Managed subfolders: "
             f"{', '.join(str(s) for s in _MANAGED_SUBDIRS)}")

        deploy_dir.mkdir(parents=True, exist_ok=True)

        from Utils.filegraph.deploy import input_ready
        if not input_ready():
            raise RuntimeError(
                f"filemap.txt not found: {filemap}\n"
                "Run 'Build Filemap' before deploying."
            )

        profile_dir = self.get_profile_root() / "profiles" / profile
        per_mod_strip = load_per_mod_strip_prefixes(profile_dir)

        # Separator overrides - loaded from the real profile_dir and passed
        # explicitly so shared-staging layouts get the right link modes.
        _sep_deploy = load_separator_deploy_paths(profile_dir)
        _sep_entries = read_modlist(profile_dir / "modlist.txt") if _sep_deploy else []
        per_mod_deploy = expand_separator_deploy_paths(_sep_deploy, _sep_entries) or None
        per_mod_modes = expand_separator_link_modes(_sep_deploy, _sep_entries) or None
        per_mod_raw = expand_separator_raw_deploy(_sep_deploy, _sep_entries) or None

        custom_rules = self.custom_routing_rules
        custom_exclude: set[str] = set()
        if custom_rules and self._game_path:
            _log("Step 1a: Routing bin_ship/ files to game root ...")
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
            _log(f"  Routed {len(custom_exclude)} file(s) to game root.")

        _log("Step 1: Backing up managed subfolders → <subdir>_Core/ ...")
        for sub in _MANAGED_SUBDIRS:
            sub_path = data_root / sub
            core_path = data_root / self._core_dir_for(sub)
            move_to_core(sub_path, core_dir=core_path, log_fn=_log)
            _log(f"  {sub}: backed up existing files → {core_path.name}/.")

        _log(f"Step 2: Transferring mod files into data folder ({mode.name}) ...")
        linked_mod, placed = deploy_filemap(
            filemap, deploy_dir, staging,
            mode=mode,
            strip_prefixes=self.mod_folder_strip_prefixes,
            per_mod_strip_prefixes=per_mod_strip,
            per_mod_deploy_dirs=per_mod_deploy,
            per_mod_link_modes=per_mod_modes,
            log_fn=_log,
            progress_fn=progress_fn,
            exclude=custom_exclude or None,
        )
        _log(f"  Transferred {linked_mod} mod file(s).")

        _log("Step 3: Filling gaps with vanilla files per subfolder ...")
        linked_core = 0
        for sub in _MANAGED_SUBDIRS:
            sub_path = data_root / sub
            core_path = data_root / self._core_dir_for(sub)
            if not core_path.is_dir():
                continue
            # placed is data-root-relative; deploy_core compares against the
            # subfolder's own tree, so filter to this subfolder's placements.
            sub_prefix = str(sub).replace("\\", "/").lower() + "/"
            sub_placed = {
                p[len(sub_prefix):] for p in placed
                if p.lower().startswith(sub_prefix)
            }
            n = deploy_core(sub_path, sub_placed, mode=mode,
                            core_dir=core_path, log_fn=_log)
            linked_core += n
        _log(f"  Transferred {linked_core} vanilla file(s).")

        _log("Step 4: Building AddIns.xml / Offers.xml registries ...")
        try:
            dao_xml = self._load_dao_module("dao_xml")
            n_reg = dao_xml.build_registry_xml(
                data_root, mod_staging=staging,
                game_path=self._game_path, log_fn=_log
            )
            _log(f"  Registries built ({n_reg} item(s)).")
        except Exception as exc:
            _log(f"  Warning: registry build failed: {exc}")

        _log("Step 5: Merging chargenmorphcfg.xml ...")
        try:
            dao_chargen = self._load_dao_module("dao_chargen")
            n_chg = dao_chargen.build_chargenmorph(
                data_root, mod_staging=staging, log_fn=_log
            )
            _log(f"  chargenmorphcfg.xml built ({n_chg} mod resource(s)).")
        except Exception as exc:
            _log(f"  Warning: chargenmorph merge failed: {exc}")

        _log(
            f"Deploy complete. "
            f"{linked_mod} mod + {linked_core} vanilla "
            f"= {linked_mod + linked_core} total file(s) in {deploy_dir}."
        )

    def restore(self, log_fn=None, progress_fn=None) -> None:
        """Remove deployed mods and restore the vanilla data folder."""
        _log = log_fn or (lambda _: None)

        data_root = self._dao_data_root()
        if data_root is None:
            raise RuntimeError(
                "No Dragon Age data folder found. Configure the Proton prefix "
                "in the game settings."
            )

        if self._game_path:
            custom_rules = self.custom_routing_rules
            if custom_rules:
                _log("Restore: removing custom-routed bin_ship/ files ...")
                restore_custom_rules(
                    self.get_effective_filemap_path(),
                    self._game_path,
                    rules=custom_rules,
                    log_fn=_log,
                )

        _profile_dir = self._active_profile_dir
        _entries = read_modlist(_profile_dir / "modlist.txt") if _profile_dir else []
        cleanup_custom_deploy_dirs(_profile_dir, _entries, log_fn=_log, game=self)

        # Filemap-driven cleanup. DAO mods deploy as bare, mod-relative paths
        # under the data root (e.g. "50 Tactics Slots/exptable.gda"), so we
        # cannot rely on the managed-subfolder backups alone - we must remove
        # exactly what the filemap says was deployed, wherever it landed.
        removed = self._remove_deployed_files(data_root, log_fn=_log)
        _log(f"  Removed {removed} deployed file(s) from the data folder.")

        # Restore the genuine game subfolders from their _Core backups (these
        # hold the vanilla override/AddIns/Offers state captured at deploy).
        restored = 0
        overwrite_dir = self.get_effective_overwrite_path()
        # modindex.bin lives in the profile root (next to filemap.txt), NOT
        # under overwrite/. We restore per-subfolder, so overwrite_dir is a
        # sub-path of overwrite/ - pass index_path explicitly so the rescued-
        # files index update doesn't land in overwrite/<sub>/modindex.bin.
        index_path = self.get_effective_filemap_path().parent / "modindex.bin"
        for sub in _MANAGED_SUBDIRS:
            sub_path = data_root / sub
            core_path = data_root / self._core_dir_for(sub)
            if not core_path.is_dir():
                continue
            n = restore_data_core(
                sub_path,
                core_dir=core_path,
                overwrite_dir=overwrite_dir / sub if overwrite_dir else None,
                index_path=index_path,
                log_fn=_log,
                game=self, profile_dir=self._active_profile_dir,
            )
            _log(f"  {sub}: restored {n} vanilla file(s).")
            restored += n
        if restored:
            _log(f"  Restored {restored} vanilla file(s) total. Core dirs removed.")

        # Tidy any empty directories left behind by removed mod files.
        self._prune_empty_dirs(data_root, log_fn=_log)

        _log("Restore: resetting AddIns.xml / Offers.xml to empty ...")
        try:
            dao_xml = self._load_dao_module("dao_xml")
            dao_xml.reset_registry_xml(data_root, log_fn=_log)
        except Exception as exc:
            _log(f"  Warning: registry reset failed: {exc}")

        _log("Restore: removing generated chargenmorphcfg.xml ...")
        try:
            dao_chargen = self._load_dao_module("dao_chargen")
            dao_chargen.reset_chargenmorph(data_root, log_fn=_log)
        except Exception as exc:
            _log(f"  Warning: chargenmorph reset failed: {exc}")

        _log("Restore complete.")

    def _remove_deployed_files(self, data_root: Path, log_fn=None) -> int:
        """Delete every file listed in the active filemap from the data root.

        Only removes files that are deployed links/copies (not pre-existing
        game files), and never touches the genuine game subfolders' contents
        that aren't in the filemap. Returns the count removed.
        """
        _log = log_fn or (lambda _: None)
        if self._active_profile_dir is None:
            _log("  No active profile - nothing to remove.")
            return 0
        removed = 0
        try:
            from Utils.filegraph.deploy import deployed_paths_below
            deployed_paths = deployed_paths_below(
                self, self._active_profile_dir, data_root)
        except Exception as exc:
            _log(f"  Warning: could not read deployed catalog state: {exc}")
            return 0
        for rel in deployed_paths:
            target = data_root / rel
            try:
                if target.is_symlink() or target.is_file():
                    target.unlink()
                    removed += 1
            except OSError as exc:
                _log(f"  Warning: failed to remove {rel}: {exc}")
        return removed

    def _prune_empty_dirs(self, data_root: Path, log_fn=None) -> None:
        """Remove empty directories under the data root, leaving the genuine
        game folders (Settings, Logs, packages/core/data) intact even if empty."""
        import os
        _log = log_fn or (lambda _: None)
        keep = {"Settings", "Logs"}
        for dirpath, dirnames, filenames in os.walk(data_root, topdown=False):
            p = Path(dirpath)
            if p == data_root:
                continue
            if p.name in keep or p.name.endswith("_Core"):
                continue
            try:
                if not any(p.iterdir()):
                    p.rmdir()
            except OSError:
                pass
