"""
cyberpunk_2077.py
Game handler for Cyberpunk 2077.

Mod structure:
  Mods install directly into the game root (archive/, bin/, r6/, red4ext/, etc.)
  Staged mods live in Profiles/Cyberpunk 2077/mods/

Archive load order:
  REDengine loads archive/pc/mod ASCII-alphabetically and the FIRST loaded
  archive wins conflicts (opposite of Bethesda).  An optional modlist.txt in
  that folder overrides the alphabetical order, so deploy writes one from the
  profile's mod priority (highest-priority mod first).  AMM_CP2077_ARCHIVE_MODLIST=0
  disables the writer.
"""

import os
import shutil
from pathlib import Path

from Games.base_game import BaseGame, MODERN_DIRECTX_DEPS
from Utils.vfs import ProfileVFSGameMixin
from Utils.deploy import (
    CustomRule,
    LinkMode,
    deploy_custom_rules,
    deploy_filemap_to_root,
    load_per_mod_strip_prefixes,
    load_separator_deploy_paths,
    expand_separator_deploy_paths,
    expand_separator_link_modes,
    expand_separator_raw_deploy,
    restore_custom_rules,
    restore_filemap_from_root,
)
from Utils.modlist import read_modlist
from Utils.config_paths import get_profiles_dir

_PROFILES_DIR = get_profiles_dir()


class Cyberpunk2077(ProfileVFSGameMixin, BaseGame):

    profile_overridable_settings = (
        *BaseGame.profile_overridable_settings,
        *ProfileVFSGameMixin.vfs_profile_setting_keys,
    )

    # Many script/ENB-style Cyberpunk mods need the VC++ x64 runtime + fxc2
    # d3dcompiler_47; auto-install them on add/save like the modern Bethesda
    # titles. Installed via the Proton-menu installers, not winetricks.
    auto_install_deps = MODERN_DIRECTX_DEPS

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
        return "Cyberpunk 2077"

    @property
    def game_id(self) -> str:
        return "cyberpunk_2077"

    @property
    def exe_name(self) -> str:
        return "bin/x64/Cyberpunk2077.exe"

    @property
    def steam_id(self) -> str:
        return "1091500"
    
    @property
    def default_deploy_mode(self) -> str:
        return "hardlink"

    @property
    def nexus_game_domain(self) -> str:
        return "cyberpunk2077"

    @property
    def reshade_dll(self) -> str:
        return "dxgi.dll"

    @property
    def mod_required_top_level_folders(self) -> set[str]:
        return {"bin", "r6", "archive", "red4ext","engine","mods","tools"}

    @property
    def mod_required_file_types(self) -> set[str]:
        return {".archive"}

    @property
    def wine_dll_overrides(self) -> dict[str, str]:
        return {
            "winmm": "native,builtin",
            "version": "native,builtin"
            }

    @property
    def conflict_ignore_filenames(self) -> set[str]:
        return {
            "*read*.txt",
            "*.png",
            "*.jpg",
            "*.jpeg"
            }

    @property
    def excluded_loose_filenames(self) -> set[str]:
        return {"*.txt"}

    @property
    def filemap_exclude_unknown_top_level(self) -> bool:
        # Authors often ship extra top-level folders (screenshots, "aboutMods",
        # source dumps, etc.) that must not be deployed into the game root.
        # Drop any foldered entry whose top level isn't a required folder.
        return True

    @property
    def mod_auto_strip_until_required(self) -> bool:
        return True

    @property
    def mod_deploy_path_remap(self) -> dict[str, str]:
        return {"archive/pc/patch/": "archive/pc/mod/"}

    @property
    def custom_routing_rules(self) -> list[CustomRule]:
        return [
            CustomRule(
                dest="archive/pc/mod",
                extensions=[".archive"],
                companion_extensions=[".xl"],
                loose_only=True,
            ),
            CustomRule(
                dest="archive/pc/mod",
                extensions=[".xl"],
                loose_only=True,
            ),
        ]

    @property
    def filemap_casing(self) -> str:
        # REDengine consistently uses lowercase ``archive/pc/mod`` on disk;
        # if even one mod ships ``Mod`` (uppercase) the default upper-wins
        # picker would force every other mod into a non-existent directory
        # on case-sensitive Linux filesystems.  Prefer lowercase canonicals.
        return "lower"

    @property
    def filemap_casing_pins(self) -> dict[str, str]:
        # The engine reads these skeleton folders by lowercase literal paths;
        # pin them so the filemap says e.g. ``archive/pc/mod`` even when the
        # only enabled mod ships ``archive/PC/Mod`` (the "lower" strategy can
        # only pick among casings that mods actually ship).  Deploy then
        # exact-matches the vanilla dirs instead of guessing between
        # case-variant duplicates left by manual installs.
        return {
            "archive": "archive", "pc": "pc", "mod": "mod", "ep1": "ep1",
            "bin": "bin", "x64": "x64", "plugins": "plugins",
            "cyber_engine_tweaks": "cyber_engine_tweaks", "mods": "mods",
            "r6": "r6", "scripts": "scripts", "tweaks": "tweaks",
            "config": "config", "input": "input",
            "red4ext": "red4ext", "engine": "engine", "tools": "tools",
        }


    @property
    def default_launch_args(self) -> list[str]:
        # -modded: REDmod content (mods/ + r6/cache/modded) only loads when
        # the game is started with it; harmless when no REDmods are installed.
        # --launcher-skip: REDprelauncher goes straight to the game.  Both are
        # passed unconditionally on every launch route we control.
        return ["-modded", "--launcher-skip"]

    def default_launch_args_for_exe(self, exe_name: str) -> list[str]:
        # The REDLauncher Run entry exists to SHOW the launcher (and let it
        # run its own redMod deploy) - don't skip it from itself.
        if exe_name.lower() == "redprelauncher.exe":
            return ["-modded"]
        return self.default_launch_args

    @property
    def framework_launch_exes(self) -> dict[str, str]:
        # REDlauncher re-runs redMod.exe deploy itself when the mod list
        # changed, so it's the safest Run entry for script/tweak REDmods.
        # -modded is forwarded to it via default_launch_args_for_exe.
        return {"REDLauncher": "REDprelauncher.exe"}

    @property
    def frameworks(self) -> dict[str, str]:
        return {"Cyber Engine Tweaks": "bin/x64/plugins/cyber_engine_tweaks.asi",
                "RED4ext": "red4ext/RED4ext.dll",
                "ArchiveXL":"red4ext/plugins/ArchiveXL/ArchiveXL.dll",
                "Redscript":"engine/tools/scc.exe",
                "TweakXL":"red4ext/plugins/TweakXL/TweakXL.dll",
                "Codeware":"red4ext/plugins/Codeware/Codeware.dll"
                }

    # -----------------------------------------------------------------------
    # Paths
    # -----------------------------------------------------------------------

    def get_game_path(self) -> Path | None:
        return self._game_path

    def get_mod_data_path(self) -> Path | None:
        """Mods deploy directly into the game root (archive/, r6/, bin/, red4ext/, etc.)."""
        return self._game_path

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
        """Deploy staged mods directly into the game root.

        Workflow:
          1. Back up any vanilla files that mod files will overwrite
          2. Transfer mod files listed in filemap.txt into the game root
        (Root Folder deployment is handled by the GUI after this returns.)
        """
        _log = log_fn or (lambda _: None)

        if self._game_path is None:
            raise RuntimeError("Game path is not configured.")

        game_root = self._game_path
        filemap   = self.get_effective_filemap_path()
        staging   = self.get_effective_mod_staging_path()

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

        profile_dir = self.get_profile_root() / "profiles" / profile
        per_mod_strip = load_per_mod_strip_prefixes(profile_dir)

        # Separator overrides - loaded from the real profile_dir so custom-routed
        # files honour a separator's File Transfer Method (shared-staging safe).
        _sep_deploy = load_separator_deploy_paths(profile_dir)
        _sep_entries = read_modlist(profile_dir / "modlist.txt") if _sep_deploy else []
        per_mod_modes = expand_separator_link_modes(_sep_deploy, _sep_entries) or None
        per_mod_raw = expand_separator_raw_deploy(_sep_deploy, _sep_entries) or None

        custom_rules = self.custom_routing_rules
        custom_exclude: set[str] = set()
        if custom_rules:
            _log("Routing loose .archive/.xl files to archive/pc/mod/ ...")
            custom_exclude = deploy_custom_rules(
                filemap, game_root, staging,
                rules=custom_rules,
                mode=mode,
                strip_prefixes=self.mod_folder_strip_prefixes,
                per_mod_strip_prefixes=per_mod_strip,
                per_mod_link_modes=per_mod_modes,
                raw_mods=per_mod_raw,
                log_fn=_log,
            )

        _log(f"Transferring mod files into game root ({mode.name}) ...")
        linked_mod, _ = deploy_filemap_to_root(filemap, game_root, staging,
                                               mode=mode,
                                               strip_prefixes=self.mod_folder_strip_prefixes,
                                               per_mod_strip_prefixes=per_mod_strip,
                                               log_fn=_log,
                                               progress_fn=progress_fn,
                                               exclude=custom_exclude or None,
                                               path_remap=self.mod_deploy_path_remap or None)

        if os.environ.get("AMM_CP2077_ARCHIVE_MODLIST") != "0":
            try:
                # Raw-deploy mods and custom-location separator mods never
                # land in archive/pc/mod - keep them out of the load order.
                _excl: set[str] = set(per_mod_raw or ())
                if _sep_deploy:
                    _excl |= set(expand_separator_deploy_paths(
                        _sep_deploy, _sep_entries))
                self._write_archive_modlist(
                    filemap, game_root, profile_dir,
                    exclude_mods=_excl or None, log_fn=_log)
            except Exception as exc:
                _log(f"WARN: archive modlist.txt not written: {exc}")

        _log(f"Deploy complete. {linked_mod} mod file(s) placed in game root.")

    # -----------------------------------------------------------------------
    # Archive load order (archive/pc/mod/modlist.txt)
    # -----------------------------------------------------------------------

    @staticmethod
    def _archive_modlist_dest(game_root: Path) -> Path:
        return game_root / "archive" / "pc" / "mod" / "modlist.txt"

    def _ordered_mod_archives(self, filemap: Path, profile_dir: Path,
                              exclude_mods: "set[str] | None" = None) -> list[str]:
        """Deployed .archive filenames in game load order (winner first).

        The game loads modlist.txt top to bottom and the first archive that
        touches a resource wins, so the order is Overwrite first (it beats
        everything in the filemap merge), then mods by profile priority
        (modlist index 0 = highest).  Only flat archive/pc/mod entries count -
        subfolders there aren't reliably scanned by the engine.  exclude_mods
        (raw-deploy / custom-location separator mods) never land in
        archive/pc/mod, so their archives are dropped entirely.
        """
        from Utils.filegraph_deploy import legacy_rows

        mods = [e.name for e in read_modlist(profile_dir / "modlist.txt")
                if e.enabled and not e.is_separator]
        # "[Overwrite]" is filemap.OVERWRITE_NAME; -1 ranks it above index 0.
        rank = {name: i for i, name in enumerate(mods)}
        rank["[Overwrite]"] = -1
        unranked = len(mods)

        prefix = "archive/pc/mod/"
        excluded = exclude_mods or set()
        # Deploy remaps these prefixes (archive/pc/patch → archive/pc/mod), so
        # apply the same substitution before deciding what lands in the dir.
        remap = [(k.lower(), v) for k, v in (self.mod_deploy_path_remap or {}).items()]
        best: dict[str, tuple[int, str]] = {}  # filename_lower → (rank, filename)
        for rel, mod in legacy_rows():
            rl = rel.lower()
            if not rl.endswith(".archive") or mod in excluded:
                continue
            for old_p, new_p in remap:
                if rl.startswith(old_p):
                    rel = new_p + rel[len(old_p):]
                    rl = rel.lower()
                    break
            if "/" not in rel:
                # Loose archive - routed to archive/pc/mod by the custom rule.
                name = rel
            elif rl.startswith(prefix) and "/" not in rel[len(prefix):]:
                name = rel[len(prefix):]
            else:
                continue
            r = rank.get(mod, unranked)
            cur = best.get(name.lower())
            if cur is None or r < cur[0]:
                best[name.lower()] = (r, name)
        return [name for _r, name in
                sorted(best.values(), key=lambda t: (t[0], t[1]))]

    def _write_archive_modlist(self, filemap: Path, game_root: Path,
                               profile_dir: Path,
                               exclude_mods: "set[str] | None" = None,
                               log_fn=None) -> None:
        """Write archive/pc/mod/modlist.txt from the profile's mod priority.

        A modlist.txt we didn't write (hand-made, or deployed by a mod) is
        backed up next to the filemap before being replaced, and put back by
        _cleanup_archive_modlist on restore.  The sidecar state file marks
        ownership: while it exists, the deployed modlist.txt is ours.
        """
        _log = log_fn or (lambda _m: None)
        dest = self._archive_modlist_dest(game_root)
        state = filemap.parent / "archive_modlist.state"
        backup = filemap.parent / "archive_modlist_backup.txt"

        names = self._ordered_mod_archives(filemap, profile_dir, exclude_mods)

        if dest.is_file():
            ours = state.is_file() and dest.read_bytes() == state.read_bytes()
            if not ours and not backup.is_file():
                shutil.copy2(dest, backup)
                _log("Archive load order: existing modlist.txt backed up "
                     "(restored when mods are removed).")
            # Unlink rather than overwrite: the file may be a hardlink to a
            # mod's staged copy, and writing through it would corrupt staging.
            dest.unlink()

        if not names:
            state.unlink(missing_ok=True)
            if backup.is_file():
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(backup), str(dest))
            return

        # CRLF: the game is a Windows binary; MO2 writes the file the same way.
        content = ("\r\n".join(names) + "\r\n").encode("utf-8")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)
        state.write_bytes(content)
        _log(f"Archive load order: wrote modlist.txt with {len(names)} "
             "archive(s) - highest-priority mod loads first (first wins).")

    def _write_vfs_archive_modlist(self, filemap: Path, view_root: Path,
                                   profile_dir: Path,
                                   exclude_mods: "set[str] | None" = None,
                                   log_fn=None) -> None:
        """Generate archive load order inside the disposable private view.

        The physical writer uses sidecar ownership and backup files because it
        replaces a file in the real install. The VFS view already contains a
        complete copy-on-write-safe representation, so replacing its hardlink
        directly is sufficient and deliberately must not claim the physical
        writer's sidecars.
        """
        _log = log_fn or (lambda _m: None)
        names = self._ordered_mod_archives(filemap, profile_dir, exclude_mods)
        if not names:
            return

        dest = self._archive_modlist_dest(view_root)
        try:
            dest.resolve(strict=False).relative_to(view_root.resolve())
        except ValueError as exc:
            raise RuntimeError(
                f"Unsafe private archive modlist path: {dest}"
            ) from exc
        if os.path.lexists(dest):
            # Never write through a hardlink inherited from the install or a
            # staged mod: unlink it from the shadow first, then create ours.
            dest.unlink()
        content = ("\r\n".join(names) + "\r\n").encode("utf-8")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)
        _log(f"Archive load order: wrote private modlist.txt with {len(names)} "
             "archive(s) - highest-priority mod loads first (first wins).")

    def _vfs_post_view_build(self, *, view_root: Path, profile: str,
                             filemap: Path, staging: Path, log_fn) -> None:
        """Create Cyberpunk's generated archive order in the resolved view."""
        _log = log_fn or (lambda _m: None)
        if os.environ.get("AMM_CP2077_ARCHIVE_MODLIST") == "0":
            return
        try:
            # Physical deployment generates this file before the pipeline's
            # Root_Folder/root-flagged pass, so an explicit root payload wins.
            # The VFS resolves root payloads earlier; preserve that same
            # highest-priority contract instead of overwriting it here.
            rel = "archive/pc/mod/modlist.txt"
            profile_dir = self.get_profile_root() / "profiles" / profile
            root_owns = False
            if bool(getattr(self, "_pipeline_root_folder_enabled", True)):
                from Utils.deploy import _resolve_nocase
                root_source = _resolve_nocase(
                    self.get_effective_root_folder_path(), rel)
                root_owns = bool(
                    root_source is not None and root_source.is_file())
            if not root_owns:
                # The pinned plan already incorporates exclusions, routing,
                # and exact staged source identity.
                from Utils.deploy import _resolve_nocase
                from Utils.filegraph_deploy import entries as filegraph_entries
                view_dest = _resolve_nocase(view_root, rel)
                for entry in filegraph_entries(include_root=True):
                    if (entry.destination.replace("\\", "/").casefold()
                            != rel.casefold()
                            or entry.source_path is None):
                        continue
                    source = Path(entry.source_path)
                    if source.is_file() and view_dest is not None and view_dest.is_file():
                        try:
                            root_owns = view_dest.samefile(source)
                        except OSError:
                            root_owns = False
                        if not root_owns:
                            try:
                                root_owns = (
                                    view_dest.stat().st_size
                                    == source.stat().st_size
                                    and view_dest.read_bytes()
                                    == source.read_bytes()
                                )
                            except OSError:
                                root_owns = False
                    if root_owns:
                        break
            if root_owns:
                _log(
                    "Archive load order: keeping root payload modlist.txt "
                    "instead of generating a private replacement."
                )
                return

            sep_deploy = load_separator_deploy_paths(profile_dir)
            sep_entries = (
                read_modlist(profile_dir / "modlist.txt")
                if sep_deploy else []
            )
            raw_mods = expand_separator_raw_deploy(
                sep_deploy, sep_entries) or set()
            excluded = set(raw_mods)
            if sep_deploy:
                excluded.update(expand_separator_deploy_paths(
                    sep_deploy, sep_entries))
            self._write_vfs_archive_modlist(
                filemap,
                view_root,
                profile_dir,
                exclude_mods=excluded or None,
                log_fn=_log,
            )
        except Exception as exc:
            _log(f"WARN: private archive modlist.txt not written: {exc}")

    def _cleanup_vfs_archive_modlist(self, view_root: Path, log_fn=None) -> None:
        """Unlink the generated archive order from the private view only."""
        _log = log_fn or (lambda _m: None)
        dest = self._archive_modlist_dest(view_root)
        try:
            dest.resolve(strict=False).relative_to(view_root.resolve())
        except ValueError as exc:
            raise RuntimeError(
                f"Unsafe private archive modlist path: {dest}"
            ) from exc
        if dest.is_symlink() or dest.is_file():
            dest.unlink()
            _log("Archive load order: removed private generated modlist.txt.")

    def _cleanup_archive_modlist(self, filemap: Path, game_root: Path,
                                 log_fn=None) -> None:
        """Remove our modlist.txt on restore and put back any backed-up one."""
        _log = log_fn or (lambda _m: None)
        dest = self._archive_modlist_dest(game_root)
        state = filemap.parent / "archive_modlist.state"
        backup = filemap.parent / "archive_modlist_backup.txt"
        if state.is_file():
            if dest.is_file():
                dest.unlink()
            state.unlink()
        if backup.is_file():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(backup), str(dest))
            _log("Archive load order: original modlist.txt restored.")

    def _deployed_redmods(self) -> list[str]:
        """Names of REDmods deployed in the game root (mods/<name>/info.json)."""
        if self._game_path is None:
            return []
        root = self._game_path
        if self.vfs_launch_enabled:
            try:
                from Utils.vfs import effective_shadow_root
                root = effective_shadow_root(self)
            except RuntimeError:
                # A failed/unpublished VFS build has no effective REDmod set;
                # do not accidentally report files from the untouched install.
                return []
        try:
            return sorted(p.parent.name
                          for p in (root / "mods").glob("*/info.json"))
        except OSError:
            return []

    def post_deploy(self, log_fn=None) -> None:
        """Warn when REDmods are deployed but an external launcher (Steam /
        Heroic) would start the game without -modded, silently skipping them.
        The manager's own launch routes pass -modded via default_launch_args."""
        _log = log_fn or (lambda _m: None)
        redmods = self._deployed_redmods()
        if not redmods:
            return
        _log(f"REDmod: {len(redmods)} mod(s) deployed under mods/ - the game "
             "only loads them when launched with -modded (the Play button "
             "passes it automatically).")
        warn = self._external_launch_missing_modded(_log)
        if warn:
            _log(f"REDmod: {warn}")
            # Only toast it when no manager launch follows: the Play button
            # passes -modded itself, so the advice would be noise there. It
            # matters for a later launch from Steam/Heroic directly, and the
            # log line above records it either way.
            if not self.deploy_launch_pending:
                self.add_deploy_warning(warn)

    def _external_launch_missing_modded(self, _log) -> str | None:
        """Make sure the game's external launcher passes -modded.

        Tries to write the option into the launcher's own config first -
        Steam's localconfig.vdf (only safe while Steam is closed: it rewrites
        the file from memory on exit) or Heroic's GamesConfig json (safe any
        time, re-read per launch).  Returns a user-facing warning only when
        the option couldn't be added automatically."""
        try:
            from Utils.exe_launch import (
                effective_steam_id, game_is_steam_install,
                game_is_heroic_install, heroic_app_names_for_launch,
            )
            if game_is_steam_install(self):
                from Utils.steam_finder import (
                    add_steam_launch_option, steam_launch_options,
                )
                sid = effective_steam_id(self) or self.steam_id
                missing = [a for a in self.default_launch_args
                           if a not in steam_launch_options(sid).split()]
                if not missing:
                    return None
                results = {a: add_steam_launch_option(sid, a) for a in missing}
                added = [a for a in missing if results[a] == "added"]
                if added:
                    _log(f"REDmod: added {' '.join(added)} to the game's "
                         "Steam Launch Options - applies the next time Steam "
                         "starts.")
                # Only -modded is required for REDmods; --launcher-skip is
                # QoL and never worth a warning on its own.
                modded = results.get("-modded")
                if modded in (None, "added", "already"):
                    return None
                if modded == "steam_running":
                    return (
                        "REDmods need -modded in the game's Steam Launch "
                        "Options. Steam is running and would undo the change "
                        "- add it in Properties → Launch Options, or close "
                        "Steam and deploy again.")
                return (
                    "REDmods need -modded in the game's Steam Launch Options "
                    "(Properties → Launch Options) - it couldn't be added "
                    "automatically.")
            elif game_is_heroic_install(self):
                from Utils.heroic_finder import (
                    add_heroic_launcher_arg, heroic_launcher_args,
                )
                names = heroic_app_names_for_launch(self)
                have = (heroic_launcher_args(names) or "").split()
                missing = [a for a in self.default_launch_args
                           if a not in have]
                if not missing:
                    return None
                results = {a: add_heroic_launcher_arg(names, a)
                           for a in missing}
                added = [a for a in missing if results[a]]
                if added:
                    _log(f"REDmod: added {' '.join(added)} to the game's "
                         "launch arguments in Heroic.")
                if results.get("-modded", True):
                    return None
                return (
                    "REDmods need -modded in Heroic's Game Arguments "
                    "(game settings → Advanced) - it couldn't be added "
                    "automatically.")
        except Exception as exc:
            _log(f"REDmod: launch-options check skipped ({exc})")
        return None

    def restore(self, log_fn=None, progress_fn=None) -> None:
        """Remove deployed mod files from the game root and restore any vanilla files."""
        _log = log_fn or (lambda _: None)

        if self._game_path is None:
            raise RuntimeError("Game path is not configured.")

        filemap   = self.get_effective_filemap_path()
        game_root = self._game_path

        custom_rules = self.custom_routing_rules
        if custom_rules:
            _log("Restore: removing custom-routed .archive files ...")
            restore_custom_rules(filemap, game_root, rules=custom_rules, log_fn=_log)

        # Restore follows the state that actually exists rather than the
        # current toggle. A user can turn VFS off after deploying, and an older
        # physical deployment can coexist with a profile view during migration.
        from Utils.vfs import cleanup_deployment, has_deployment_state
        if has_deployment_state(self):
            try:
                from Utils.vfs import effective_shadow_root
                shadow_root = effective_shadow_root(self)
            except RuntimeError:
                # An interrupted build may have only a pending marker and no
                # published view. cleanup_deployment still owns that state.
                pass
            else:
                try:
                    self._cleanup_vfs_archive_modlist(
                        shadow_root, log_fn=_log)
                except Exception as exc:
                    # Whole-view cleanup below remains the authoritative and
                    # symlink-safe removal path; do not strand VFS state over
                    # an optional generated-file cleanup warning.
                    _log(f"WARN: private archive modlist cleanup failed: {exc}")
            cleanup_deployment(self, preserve_upper=True, log_fn=_log)
            physical_state = any((
                (filemap.parent / "filemap_deployed.txt").is_file(),
                (filemap.parent / "filemap_backup").exists(),
                (filemap.parent / "archive_modlist.state").is_file(),
                (filemap.parent / "archive_modlist_backup.txt").is_file(),
            ))
            if not physical_state:
                _log("Restore complete.")
                return
            _log("Restore: a physical deployment also remains; restoring it now ...")

        _log("Restore: removing mod files and restoring vanilla files ...")
        removed = restore_filemap_from_root(
            filemap, game_root, log_fn=_log,
            restore_whitelist=self.restore_whitelist_matcher())

        try:
            self._cleanup_archive_modlist(filemap, game_root, log_fn=_log)
        except Exception as exc:
            _log(f"WARN: archive modlist.txt cleanup failed: {exc}")

        _log(f"Restore complete. {removed} mod file(s) removed from game root.")
