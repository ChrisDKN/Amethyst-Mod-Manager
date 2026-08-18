"""
elden_ring.py
Game handler for ELDEN RING, modded through the me3 loader.

Why this handler normally deploys nothing:
  ELDEN RING reads its assets from encrypted DVDBND archives (.bhd/.bdt), never
  from loose files on disk, and the retail exe is wrapped in Arxan anti-tamper.
  Copying mod files into the game folder would silently do nothing.  The me3
  loader (https://github.com/garyttierney/me3) injects a DLL that hooks the
  archive layer and serves overrides from a VFS, driven by a TOML mod list.

  So deploy() writes a .me3 profile pointing at the staged mod folders and
  get_launch_command() hands the Play button over to ``me3 launch``, which
  discovers Proton and starts the game itself.  Mods stay in staging
  permanently.  The exception is Elden Mod Loader: its proxy DLL and DLL mods
  must be copied beside the game executable.  A deploy-time root snapshot lets
  restore rescue any files/folders those DLL mods generate at runtime before it
  removes the copied loader files.

Mod structure:
  Staged mods live in Profiles/Elden Ring/mods/, each holding either DVDBND
  asset folders (parts/, chr/, msg/, ...) or a .dll - or both.  The generated
  profile lives at Profiles/Elden Ring/amethyst.me3, a sibling of mods/, because
  paths inside a .me3 resolve relative to the profile file itself.

Notes:
  - Unlike the other native-launch handlers (OpenMW, Daggerfall Unity) this game
    DOES run under Proton, so the prefix is left intact and the Proton tooling
    button stays available for protontricks/winetricks work.
  - Save isolation is on by default: each profile gets its own savefile name, so
    a modded run never touches the vanilla save.  FROMSOFTWARE saves are
    ban- and corruption-sensitive, which makes sharing one a real hazard.
  - Conflicts need no special handling: build_filemap() works purely off
    staging-relative paths, and overlapping paths between package folders are
    exactly what wins/loses in me3's VFS.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from Games.base_game import BaseGame, WizardTool
from Games.FromSoftware import me3_profile, me3_runtime
from Utils.atomic_write import write_atomic_text
from Utils.config_paths import get_profiles_dir
from Utils.deploy import (
    LinkMode,
    _FILEMAP_SNAPSHOT_NAME,
    _move_runtime_files,
    deploy_root_folder,
    restore_root_folder,
)
from Utils.mod_files import excluded_raw_by_mod
from Utils.modlist import read_modlist

_PROFILES_DIR = get_profiles_dir()

# Name of the generated mod list, written beside mods/.
_PROFILE_FILENAME = "amethyst.me3"

# Folder the routing rules place loose DVDBND files into, beside mods/.  It is
# emitted as one more package, so me3 sees them at the paths the game asks for.
_ROUTED_DIRNAME = "routed"

# Package name shown for that folder in logs / the .me3 id.
_ROUTED_PACKAGE = "Routed Files"

# Vanilla save file name, used to derive the per-profile isolated name.
_SAVE_SUFFIX = ".sl2"


class EldenRing(BaseGame):

    # Normal me3 packages stay in staging, while captured runtime files are
    # linked back from overwrite/.  Copy mode would not preserve later edits.
    deploy_mode_supports_copy = False

    # me3 owns normal asset/DLL loading, but a proxy loader (Elden Mod Loader)
    # HAS to live next to the exe.  Its DLL mods can create configs/logs under
    # Game/mods at runtime.  Capture those additions into overwrite/ and deploy
    # overwrite as a root payload; arbitrary Root_Folder installs remain off.
    root_folder_deploy_enabled = False
    restore_before_deploy = True

    profile_overridable_settings = (
        *BaseGame.profile_overridable_settings,
        "me3_save_isolation",
        "me3_start_online",
        "me3_disable_arxan",
        "me3_mem_patch",
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
        return "Elden Ring"

    @property
    def game_id(self) -> str:
        return "elden_ring"

    @property
    def exe_name(self) -> str:
        # Steam installs the game one level down, in "ELDEN RING/Game/", so the
        # subpath is part of the name (same shape as The Sims 4's Game/Bin/).
        # Detection still resolves game_path to the Steam install root.
        return "Game/eldenring.exe"

    @property
    def exe_name_alts(self) -> list[str]:
        # For a user who pointed straight at the Game/ folder.  Deliberately no
        # bare "start_protected_game.exe": that anti-cheat stub ships with many
        # games and a bare scan matches the wrong one (Fall Guys, for instance).
        return ["eldenring.exe", "Game/start_protected_game.exe"]

    @property
    def steam_id(self) -> str:
        return "1245620"

    @property
    def nexus_game_domain(self) -> str:
        return "eldenring"

    @property
    def me3_cli_id(self) -> str:
        """The game id me3's CLI expects for -g."""
        return "eldenring"

    @property
    def default_deploy_mode(self) -> str:
        return "symlink"

    # -----------------------------------------------------------------------
    # Mod shaping
    # -----------------------------------------------------------------------

    @property
    def mod_required_top_level_folders(self) -> set[str]:
        return set(me3_profile.ACCEPTABLE_FOLDERS)

    @property
    def mod_auto_strip_until_required(self) -> bool:
        return True

    @property
    def mod_required_file_types(self) -> set[str]:
        return {".dll", ".bin"}

    @property
    def mod_install_as_is_if_no_match(self) -> bool:
        # DLL-only mods carry none of the DVDBND folders; keep them as they are.
        return True

    @property
    def filemap_exclude_unknown_top_level(self) -> bool:
        # Keeps stray readmes out of the conflict map.
        return True

    @property
    def conflict_ignore_filenames(self) -> set[str]:
        return {"*.md", "*.txt", "*.jpg", "*.png", "*.pdf"}

    @property
    def plugin_extensions(self) -> list[str]:
        # No plugin system - the Plugins tab stays empty.
        return []

    # No wine_dll_overrides: an override only matters for a DLL that exists in
    # the game folder, and this handler deploys nothing there. A dinput8-style
    # proxy loader cannot work alongside me3 at all (see PROXY_LOADER_DLLS);
    # deploy warns about those mods instead of pretending an override helps.

    @property
    def wizard_tools(self) -> list[WizardTool]:
        return self._base_wizard_tools() + [
            WizardTool(
                id="install_me3",
                label="Install me3",
                description=("Download and install the me3 mod loader that "
                             "loads mods for this game."),
                dialog_class_path="wizards.me3_install.Me3InstallWizard",
            ),
            WizardTool(
                id="merge_regulation",
                label="Merge regulation.bin",
                description=("Combine the param edits of every enabled mod "
                             "into one regulation.bin, instead of only the "
                             "highest-priority one taking effect."),
                dialog_class_path=(
                    "wizards.regulation_merge.RegulationMergeWizard"),
            ),
        ]

    # -----------------------------------------------------------------------
    # Paths
    # -----------------------------------------------------------------------

    def get_game_path(self) -> Path | None:
        return self._game_path

    def set_game_path(self, path: "Path | str | None") -> None:
        self._game_path = Path(path) if path else None
        self.save_paths()

    def get_mod_data_path(self) -> Path | None:
        # Nothing is ever deployed into the game folder.  Returning None keeps
        # validate_install() from demanding a deploy target that does not exist.
        return None

    def get_mod_staging_path(self) -> Path:
        if self._staging_path is not None:
            return self._staging_path / "mods"
        return _PROFILES_DIR / self.name / "mods"

    def get_me3_profile_path(self) -> Path:
        """Return the generated .me3 path (a sibling of the mods folder)."""
        return self.get_effective_mod_staging_path().parent / _PROFILE_FILENAME

    # -----------------------------------------------------------------------
    # Options
    # -----------------------------------------------------------------------

    @property
    def me3_save_isolation(self) -> bool:
        """Whether each profile gets its own savefile instead of the vanilla one."""
        return self._load_settings().get("me3_save_isolation", True)

    def set_me3_save_isolation(self, value: bool) -> None:
        data = self._load_settings()
        data["me3_save_isolation"] = bool(value)
        self._save_settings(data)

    @property
    def me3_start_online(self) -> bool:
        """Whether to allow the official matchmaking servers (off by default)."""
        return self._load_settings().get("me3_start_online", False)

    def set_me3_start_online(self, value: bool) -> None:
        data = self._load_settings()
        data["me3_start_online"] = bool(value)
        self._save_settings(data)

    @property
    def me3_disable_arxan(self) -> bool:
        """Whether me3 neutralizes Arxan anti-tamper for mod stability."""
        return self._load_settings().get("me3_disable_arxan", True)

    def set_me3_disable_arxan(self, value: bool) -> None:
        data = self._load_settings()
        data["me3_disable_arxan"] = bool(value)
        self._save_settings(data)

    @property
    def me3_mem_patch(self) -> bool:
        """Whether me3 raises the game's memory limits."""
        return self._load_settings().get("me3_mem_patch", False)

    def set_me3_mem_patch(self, value: bool) -> None:
        data = self._load_settings()
        data["me3_mem_patch"] = bool(value)
        self._save_settings(data)

    def _savefile_for(self, profile: str) -> "str | None":
        """Return the isolated savefile name for a profile, or None when off."""
        if not self.me3_save_isolation:
            return None
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in profile)
        return f"Amethyst_{safe or 'default'}{_SAVE_SUFFIX}"

    # -----------------------------------------------------------------------
    # Validation
    # -----------------------------------------------------------------------

    def validate_install(self) -> list[str]:
        errors = super().validate_install()
        if not me3_runtime.me3_available():
            errors.append(
                "The me3 mod loader was not found. ELDEN RING mods are loaded "
                f"by me3 rather than copied into the game. {me3_runtime.install_hint()}"
            )
        return errors

    # -----------------------------------------------------------------------
    # Deployment - writes the mod list, moves no files
    # -----------------------------------------------------------------------

    # -----------------------------------------------------------------------
    # Proxy loader (Elden Mod Loader) support
    #
    # techiew's DLL mods scan the game's code for byte patterns, starting from
    # EnumProcessModules()[0].  Windows documents that as the main executable;
    # under Wine the module order is not guaranteed, and by the time me3 has
    # injected its host there are enough modules loaded that the scan can start
    # past the exe and never find its patterns ("Could not find signature!").
    # Loaded through EML's dinput8.dll proxy the game itself pulls them in far
    # earlier, when the enumeration still starts at the exe - so those mods go
    # into the game folder alongside EML rather than into [[natives]].  It also
    # puts each mod's config.ini where the mod actually looks for it
    # (mods/<Name>/config.ini, resolved relative to the game's working dir).
    # -----------------------------------------------------------------------

    def get_exe_dir(self) -> "Path | None":
        """Directory holding eldenring.exe - Steam nests it in Game/."""
        if self._game_path is None:
            return None
        for candidate in (self._game_path / "Game", self._game_path):
            if (candidate / self.exe_name.split("/")[-1]).is_file():
                return candidate
        return self._game_path / "Game"

    def _eml_manifest_path(self) -> Path:
        """Records what we put in the game folder, so restore removes just that."""
        return self.get_me3_profile_path().with_name("eml_deployed.txt")

    @staticmethod
    def _is_proxy_loader(entry) -> bool:
        """Whether this mod IS the proxy loader rather than a mod for it."""
        return bool(entry.proxies) and entry.package_root is None

    def _deploy_proxy_loader(self, loader, dll_mods, log_fn) -> None:
        """Copy the loader next to the exe and its DLL mods into mods/."""
        exe_dir = self.get_exe_dir()
        if exe_dir is None or not exe_dir.is_dir():
            raise RuntimeError(
                f"Could not find the folder containing {self.exe_name}.")

        staged: list[str] = []

        def _copy(src: Path, dest: Path) -> None:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            staged.append(dest.relative_to(exe_dir).as_posix())

        # 1. The loader itself: dinput8.dll (+ its config) beside the exe, so
        #    the game's import table picks it up at startup.
        loader_dir = self._staged_dir_of(loader)
        for src in sorted(loader_dir.rglob("*")):
            if not src.is_file():
                continue
            name = src.name.lower()
            if name in me3_profile.PROXY_LOADER_SIDECARS or \
                    name in me3_profile.PROXY_LOADER_DLLS:
                _copy(src, exe_dir / src.name)

        # 2. Each DLL mod's contents into mods/, preserving the
        #    "<Name>.dll + <Name>/config.ini" layout the loader expects.
        mods_dir = exe_dir / me3_profile.PROXY_LOADER_MODS_DIR
        for entry in dll_mods:
            mod_dir = self._staged_dir_of(entry)
            # Archives ship a top-level mods/ wrapper; staging strips it, but
            # honour it if a mod kept it so we never write mods/mods/.
            root = mod_dir
            inner = mod_dir / me3_profile.PROXY_LOADER_MODS_DIR
            if inner.is_dir():
                root = inner
            for src in sorted(root.rglob("*")):
                if not src.is_file() or src.name == "meta.ini":
                    continue
                _copy(src, mods_dir / src.relative_to(root))

        write_atomic_text(self._eml_manifest_path(), "\n".join(staged) + "\n")
        log_fn(f"  {len(staged)} file(s) copied into {exe_dir}")

    def _staged_dir_of(self, entry) -> Path:
        return self.get_effective_mod_staging_path() / entry.name

    def _restore_proxy_loader(self, log_fn) -> None:
        """Remove exactly the files _deploy_proxy_loader recorded."""
        manifest = self._eml_manifest_path()
        exe_dir = self.get_exe_dir()
        if not manifest.is_file() or exe_dir is None:
            return
        try:
            rels = [line.strip() for line in
                    manifest.read_text(encoding="utf-8").splitlines()
                    if line.strip()]
        except OSError:
            return

        removed = 0
        dirs: set[Path] = set()
        for rel in rels:
            # Never step outside the game folder, whatever the manifest says.
            target = (exe_dir / rel).resolve()
            try:
                target.relative_to(exe_dir.resolve())
            except ValueError:
                continue
            try:
                if target.is_file():
                    target.unlink()
                    removed += 1
                dirs.add(target.parent)
            except OSError as exc:
                log_fn(f"  Could not remove {target} ({exc}).")

        # Prune the directories we created, deepest first, only when empty.
        for d in sorted(dirs, key=lambda p: len(p.parts), reverse=True):
            try:
                if d != exe_dir and d.is_dir() and not any(d.iterdir()):
                    d.rmdir()
            except OSError:
                pass
        try:
            manifest.unlink()
        except OSError:
            pass
        if removed:
            log_fn(f"  Removed {removed} mod-loader file(s) from the game folder.")

    def _rescue_legacy_proxy_runtime(self, log_fn) -> int:
        """Preserve untracked Game/mods files from a pre-snapshot deployment.

        This migration only runs when restore saw an old Amethyst EML manifest
        but no deploy snapshot.  The manifest-owned payload has already been
        removed, so everything remaining is conservatively moved rather than
        deleted.  Once a new deploy writes its snapshot, normal runtime capture
        takes over and this path is no longer used.
        """
        exe_dir = self.get_exe_dir()
        game_root = self.get_game_path()
        if exe_dir is None or game_root is None:
            return 0
        mods_dir = exe_dir / me3_profile.PROXY_LOADER_MODS_DIR
        if not mods_dir.is_dir():
            return 0
        try:
            rel_root = mods_dir.relative_to(game_root)
        except ValueError:
            return 0

        destination = self.get_effective_overwrite_path() / rel_root
        moved = 0
        try:
            files = sorted(
                p for p in mods_dir.rglob("*")
                if p.is_file() and not p.is_symlink()
            )
        except OSError:
            return 0
        for src in files:
            dst = destination / src.relative_to(mods_dir)
            if dst.exists() or dst.is_symlink():
                log_fn(f"  Could not rescue {src}: overwrite/{rel_root}/"
                       f"{src.relative_to(mods_dir)} already exists.")
                continue
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
                moved += 1
            except OSError as exc:
                log_fn(f"  Could not rescue {src} ({exc}).")

        # Remove only directories left empty by the moves above.  Conflicts or
        # unreadable files remain in place rather than being destroyed.
        try:
            dirs = sorted(
                (p for p in mods_dir.rglob("*") if p.is_dir()),
                key=lambda p: len(p.parts), reverse=True,
            )
        except OSError:
            dirs = []
        for directory in [*dirs, mods_dir]:
            try:
                directory.rmdir()
            except OSError:
                pass
        return moved

    def _capture_runtime_files_to_overwrite(self, log_fn) -> int:
        """Move game-root additions since deploy into this profile's overwrite."""
        game_root = self.get_game_path()
        snapshot = (
            self.get_effective_filemap_path().parent / _FILEMAP_SNAPSHOT_NAME
        )
        if game_root is None or not snapshot.is_file():
            return 0
        moved = _move_runtime_files(
            Path(game_root), snapshot, self.get_effective_overwrite_path(),
            log_fn=log_fn,
            restore_whitelist=self.restore_whitelist_matcher(),
        )
        try:
            snapshot.unlink()
        except OSError:
            pass
        return moved

    def _restore_overwrite_runtime(self, log_fn) -> int:
        """Remove the root payload previously deployed from overwrite/."""
        game_root = self.get_game_path()
        if game_root is None:
            return 0
        return restore_root_folder(
            self.get_effective_overwrite_path(), Path(game_root), log_fn=log_fn,
        )

    def _deploy_overwrite_runtime(self, mode: LinkMode, log_fn) -> int:
        """Deploy overwrite/ at game-root-relative paths."""
        game_root = self.get_game_path()
        if game_root is None:
            return 0
        return deploy_root_folder(
            self.get_effective_overwrite_path(), Path(game_root),
            mode=mode, log_fn=log_fn,
        )

    def _report_unloadable(self, entries, log_fn, loader=None,
                           routed_mods=None) -> None:
        """Tell the user about enabled mods that will not actually load.

        *routed_mods* names mods whose loose files the routing rules placed;
        a mod made up entirely of those has no package of its own and would
        otherwise be reported as empty right after it was loaded successfully.
        """
        routed_mods = routed_mods or set()
        for entry in entries:
            if entry is loader:
                continue
            if entry.proxies and loader is None:
                names = ", ".join(p.name for p in entry.proxies)
                msg = (f"'{entry.name}' is a proxy mod loader ({names}) that "
                       "me3 cannot load. Enable it in the mod list and Amethyst "
                       "will install it into the game folder instead.")
                log_fn(f"  WARNING: {msg}")
                self.add_deploy_warning(msg)
            if entry.bundled_profile is not None and not entry.loadable:
                msg = (f"'{entry.name}' ships its own me3 profile "
                       f"({entry.bundled_profile.name}) and no mod files "
                       "Amethyst can load. Its profile cannot be merged into "
                       "this one.")
                log_fn(f"  WARNING: {msg}")
                self.add_deploy_warning(msg)
            elif entry.bundled_profile is not None:
                log_fn(f"  Note: '{entry.name}' ships a me3 profile "
                       f"({entry.bundled_profile.name}); it is ignored and the "
                       "mod's own files are loaded instead.")
            if entry.has_unexpressable_exclusions:
                msg = (f"Files disabled inside '{entry.name}' still load: me3 "
                       "takes the whole mod folder, so individual files cannot "
                       "be switched off. Remove them from the mod instead.")
                log_fn(f"  WARNING: {msg}")
                self.add_deploy_warning(msg)
            if not entry.loadable and entry.bundled_profile is None \
                    and not entry.proxies:
                if entry.name in routed_mods:
                    log_fn(f"  '{entry.name}' ships loose files - routed to the "
                           "folders the game reads them from.")
                else:
                    log_fn(f"  '{entry.name}' has no assets or DLLs - skipped.")

    def deploy(self, log_fn=None, mode: LinkMode = LinkMode.HARDLINK,
               profile: str = "default", progress_fn=None) -> None:
        """Write the me3 mod list and, when enabled, install the proxy loader."""
        _log = log_fn or (lambda _: None)

        if self._game_path is None:
            raise RuntimeError("Game path is not configured.")

        staging = self.get_effective_mod_staging_path()
        profile_dir = self.get_profile_root() / "profiles" / profile
        out_path = self.get_me3_profile_path()

        _log("Step 1: Reading the mod list ...")
        ordered = [(e.name, staging / e.name)
                   for e in read_modlist(profile_dir / "modlist.txt")
                   if e.enabled and not e.is_separator]

        _log("Step 2: Classifying staged mods ...")
        # Files the user switched off in Mod Files must not reach the profile.
        excluded_by_mod = excluded_raw_by_mod(profile_dir)
        entries = me3_profile.build_entries(ordered, excluded_by_mod)

        # A proxy loader takes ownership of every DLL mod: those load far
        # earlier through it than through me3, which is what their code scans
        # need, and it puts their config.ini where they look for it.
        loader = next((e for e in entries if self._is_proxy_loader(e)), None)
        me3_entries = [e for e in entries if e is not loader]
        dll_mods: list = []
        if loader is not None:
            dll_mods = [e for e in me3_entries
                        if e.natives and e.package_root is None]
            # Their DLLs must not ALSO be listed as me3 natives.
            me3_entries = [e for e in me3_entries if e not in dll_mods]
            for e in entries:
                if e.package_root is not None and e.natives:
                    e.natives = []
                    _log(f"  '{e.name}': DLLs routed to {loader.name}, "
                         "assets stay with me3.")

        # Loose DVDBND files ("put these in chr/") are inert until they sit at
        # the path the game asks for - place them into their own package.  It
        # goes FIRST in the list, i.e. highest priority: its contents come from
        # the filemap, which already picked one winner per destination using
        # modlist order, so it has to outrank a losing copy that another mod
        # happens to ship correctly nested.
        routed_dir, routed_mods = self._route_loose_files(_log)
        if routed_dir is not None:
            me3_entries.insert(0, me3_profile.Me3ModEntry(
                _ROUTED_PACKAGE, me3_profile.KIND_PACKAGE, routed_dir, []))

        packages = sum(1 for e in me3_entries if e.package_root is not None)
        natives = sum(len(e.natives) for e in me3_entries)
        self._report_unloadable(entries, _log, loader=loader,
                               routed_mods=routed_mods)

        # A direct deploy caller may not have run restore() first.  Undo the
        # previous overwrite payload before clearing/replacing the EML files;
        # this also restores any manifest-owned file overwrite had shadowed.
        self._restore_overwrite_runtime(_log)

        if loader is not None:
            _log(f"Step 3: Installing {loader.name} and {len(dll_mods)} DLL "
                 "mod(s) into the game folder ...")
            self._restore_proxy_loader(_log)     # clear a previous deploy first
            self._deploy_proxy_loader(loader, dll_mods, _log)
        else:
            # Nothing of ours should be left behind when the loader is removed.
            self._restore_proxy_loader(_log)

        overwrite_count = self._deploy_overwrite_runtime(mode, _log)
        if overwrite_count:
            _log(f"  Deployed {overwrite_count} overwrite file(s) to the game "
                 "root.")

        _log(f"Step 4: Writing {out_path.name} "
             f"({packages} package(s), {natives} native(s)) ...")
        text = me3_profile.render_profile(
            me3_entries,
            game=self.me3_cli_id,
            profile_dir=out_path.parent,
            savefile=self._savefile_for(profile),
            start_online=self.me3_start_online,
            disable_arxan=self.me3_disable_arxan,
            mem_patch=self.me3_mem_patch,
        )
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            write_atomic_text(out_path, text)
        except OSError as exc:
            raise RuntimeError(f"Could not write {out_path}: {exc}") from exc

        _log(f"  Mod list written to {out_path}")
        # Highest priority is emitted last because me3 is last-wins; say so, so
        # a user reading the file does not think the order is upside down.
        _log("  Highest-priority mods are listed last (me3 loads last-wins).")

        self._ensure_loader(_log)

        # Elden Mod Loader and its DLL mods can create configs, logs and empty
        # folders under Game/mods while the game runs.  Snapshot after both the
        # proxy files and overwrite payload have landed so restore can
        # distinguish runtime additions from manager-deployed paths.  The
        # shared pipeline defers the walk until every root mutation has landed.
        self.snapshot_root_for_runtime_capture(log_fn=_log)

    def _ensure_loader(self, log_fn) -> None:
        """Install me3 if it is missing, so Play works right after a deploy."""
        if me3_runtime.me3_available() and \
                me3_runtime.find_windows_payload() is not None:
            return

        # Deploy already runs on a worker thread, so the ~10 MB fetch is safe
        # here. Same on-demand policy as umu-run: never bundled, fetched when
        # it is actually needed, one attempt per session.
        log_fn("Step 5: The me3 mod loader is missing - installing it ...")
        try:
            got = me3_runtime.ensure_me3(
                log_fn=lambda m: log_fn(f"  me3: {m}"))
        except Exception as exc:
            # A failed download must never fail the deploy - the mod list is
            # already written and valid.
            log_fn(f"  me3 install failed: {exc}")
            got = None

        if got is not None:
            log_fn(f"  me3 is ready at {got}.")
            return

        if me3_runtime._in_flatpak():
            self.add_deploy_warning(
                "The me3 mod loader is not installed. Amethyst is running as a "
                "Flatpak, so it must be installed on the host system - see "
                "Wizards → Install me3.")
        else:
            self.add_deploy_warning(
                "The me3 mod loader could not be installed automatically, so "
                "the Play button cannot load these mods yet. Try Wizards → "
                "Install me3. " + me3_runtime.install_hint())
        log_fn("  WARNING: me3 is still unavailable - see the deploy warning.")

    def restore(self, log_fn=None, progress_fn=None) -> None:
        """Capture runtime output, then remove generated/proxy-loader files."""
        _log = log_fn or (lambda _: None)

        snapshot_path = (
            self.get_effective_filemap_path().parent / _FILEMAP_SNAPSHOT_NAME
        )
        # Upgrade path for an EML deploy made before runtime snapshots were
        # supported.  The manifest proves Amethyst owns an active proxy deploy;
        # without it, an unrelated manually-managed mods/ folder is untouched.
        rescue_legacy_runtime = (
            not snapshot_path.is_file() and self._eml_manifest_path().is_file()
        )

        # Capture before deleting the proxy manifest's files: paths present in
        # the deploy snapshot are ours, while anything new is runtime output.
        # Moving the latter first also lets _restore_proxy_loader prune the now
        # empty Game/mods directories cleanly.
        moved = self._capture_runtime_files_to_overwrite(_log)
        if moved:
            _log(f"  Moved {moved} runtime file(s) to overwrite/.")

        # Undo overwrite before the manifest payload.  If overwrite shadowed an
        # EML file, root restore first puts that manifest-owned file back and
        # _restore_proxy_loader then removes it normally.
        self._restore_overwrite_runtime(_log)

        try:
            self._restore_proxy_loader(_log)
        except Exception as exc:
            _log(f"Could not clean the mod-loader files ({exc}).")

        if rescue_legacy_runtime:
            moved_legacy = self._rescue_legacy_proxy_runtime(_log)
            if moved_legacy:
                _log(f"  Rescued {moved_legacy} pre-snapshot runtime file(s) "
                     "to overwrite/.")

        # Routed files are hardlinks we made, not the user's mod content.
        try:
            from Utils.deploy_custom_rules import restore_custom_rules
            routed = self.get_routed_package_path()
            restore_custom_rules(self.get_effective_filemap_path(), routed,
                                 self.custom_routing_rules,
                                 log_fn=lambda m: None)
            if routed.is_dir():
                shutil.rmtree(routed)
                _log(f"Removed {routed.name}/.")
        except Exception as exc:
            _log(f"Could not clean the routed files ({exc}).")

        out_path = self.get_me3_profile_path()
        try:
            if out_path.is_file():
                out_path.unlink()
                _log(f"Removed {out_path.name}.")
            else:
                _log("Nothing to restore - no generated mod list.")
        except OSError as exc:
            # Never raise: the pipeline treats a restore failure as fatal, and
            # a leftover profile is harmless (deploy overwrites it).
            _log(f"Could not remove {out_path} ({exc}); it will be overwritten.")

    # -----------------------------------------------------------------------
    # Routing: loose DVDBND files -> the folder the game reads them from
    # -----------------------------------------------------------------------

    @property
    def custom_routing_rules(self) -> list:
        """Place loose DVDBND files into the folder the engine looks in.

        Mods are routinely distributed as bare files - "put these in chr/" - and
        under me3 that silently does nothing: the VFS keys every file by its path
        relative to the package root (``VfsKey::for_asset_path``), so a file at
        the root is offered as ``c0000.anibnd.dcx`` while the game asks for
        ``chr/c0000.anibnd.dcx`` and falls back to vanilla.

        The bnd type in the extension names its folder - ``partsbnd`` -> parts/,
        ``chrbnd``/``anibnd`` -> chr/ - which is how DSMapStudio's AssetLocator
        builds these paths too (``chr\\{id}.chrbnd.dcx``, ``map\\{id}\\...``).

        Only the FLAT categories are listed.  ``mapbnd`` (map/<mapid>/),
        ``msgbnd`` (msg/<language>/), ``geombnd`` (asset/aeg/aeg<nnn>/) and
        ``parambnd`` (param/gameparam/) each need a subfolder the filename does
        not determine, and ``.tpf.dcx`` appears under chr, parts, map, menu and
        sfx alike - guessing those would put files somewhere plausible but wrong,
        so they are left alone and reported instead.

        ``flatten=True`` puts the bare filename under ``dest``, so the rule works
        whether the file sits at the mod root or inside an optional-variant
        subfolder (only the copy Mod Files leaves enabled reaches the filemap).
        """
        from Utils.deploy import CustomRule
        return [
            CustomRule(dest="chr",
                       extensions=[".anibnd.dcx", ".chrbnd.dcx", ".behbnd.dcx"],
                       flatten=True),
            CustomRule(dest="parts", extensions=[".partsbnd.dcx"], flatten=True),
            CustomRule(dest="sfx", extensions=[".ffxbnd.dcx"], flatten=True),
            CustomRule(dest="material", extensions=[".matbinbnd.dcx"],
                       flatten=True),
            CustomRule(dest="event", extensions=[".emevd.dcx"], flatten=True),
            CustomRule(dest="script", extensions=[".luabnd.dcx"], flatten=True),
        ]

    def get_routed_package_path(self) -> Path:
        """Return the folder routed files are placed in (a sibling of mods/)."""
        return self.get_effective_mod_staging_path().parent / _ROUTED_DIRNAME

    def _route_loose_files(self, log_fn) -> "tuple[Path | None, set[str]]":
        """Place loose DVDBND files into a package.

        Returns ``(package_dir_or_None, mods_that_contributed)`` - the mod names
        are needed so a mod made up ENTIRELY of loose files is not then reported
        as having nothing loadable.

        Uses the shared routing engine, pointed at our package folder instead of
        the game root - Elden Ring copies nothing into the game, so the rules'
        destinations are resolved inside a folder me3 serves from.
        """
        from Utils.deploy_custom_rules import (deploy_custom_rules,
                                               restore_custom_rules)
        routed = self.get_routed_package_path()
        filemap = self.get_effective_filemap_path()

        # Always clear the previous run first: a file that stopped matching (mod
        # disabled, or moved into chr/ by hand) must not linger and keep winning.
        try:
            restore_custom_rules(filemap, routed, self.custom_routing_rules,
                                 log_fn=lambda m: None)
        except Exception:
            pass
        try:
            if routed.is_dir():
                shutil.rmtree(routed)
        except OSError as exc:
            log_fn(f"  Could not clear {routed.name}/ ({exc}).")

        if not filemap.is_file():
            return None, set()
        try:
            handled = deploy_custom_rules(
                filemap, routed, self.get_effective_mod_staging_path(),
                self.custom_routing_rules, mode=LinkMode.HARDLINK,
                log_fn=lambda m: log_fn(f"  {m}"),
            )
        except Exception as exc:
            # A routing failure must not lose the whole mod list.
            log_fn(f"  Could not place loose files ({exc}).")
            return None, set()
        if not handled:
            return None, set()

        # Map the handled paths back to the mods they came from.
        mods: set[str] = set()
        try:
            with filemap.open(encoding="utf-8", errors="surrogateescape") as fh:
                for line in fh:
                    if "\t" not in line:
                        continue
                    rel, mod_name = line.rstrip("\n").split("\t", 1)
                    if rel.lower() in handled:
                        mods.add(mod_name)
        except OSError:
            pass

        log_fn(f"  Placed {len(handled)} file(s) into {routed.name}/ "
               "at the paths the game reads them from.")
        try:
            if not any(routed.iterdir()):
                return None, mods
        except OSError:
            return None, mods
        return routed, mods

    # -----------------------------------------------------------------------
    # Native launch (me3 owns Proton discovery)
    # -----------------------------------------------------------------------

    def get_launch_command(self) -> "list[str] | None":
        """Return the me3 command for the Play button, or None when unusable."""
        profile_path = self.get_me3_profile_path()
        if not profile_path.is_file():
            return None
        return me3_runtime.launch_command(self.me3_cli_id, profile_path)

    @property
    def native_launch_required(self) -> bool:
        # Without me3 the game still starts under Proton, but with NO mods and
        # none of this profile's save isolation - a silent vanilla run that
        # looks like success. Refuse instead of falling through.
        return True

    def native_launch_blocked_reason(self) -> str:
        """Explain what to fix when the me3 launch command is unavailable."""
        if not self.get_me3_profile_path().is_file():
            return (
                "the mod list has not been generated yet.\n\n"
                "Press Deploy first - that writes the me3 profile listing your "
                "mods. Launching without it would start the game unmodded.")
        if not me3_runtime.me3_available():
            return (
                "the me3 mod loader is not installed.\n\n"
                "Open Wizards → Install me3 to set it up. "
                f"{me3_runtime.install_hint()}")
        return ""
