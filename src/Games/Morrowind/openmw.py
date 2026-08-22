"""
openmw.py
Game handler for The Elder Scrolls III: Morrowind running under OpenMW.

Key differences from the vanilla Morrowind handler:
  - OpenMW is a native Linux binary - no Wine/Proton needed.
  - Flatpak install at ~/.var/app/org.openmw.OpenMW/ is auto-detected.
  - Config lives at ~/.config/openmw/openmw.cfg (native) or
    ~/.var/app/org.openmw.OpenMW/config/openmw/openmw.cfg (Flatpak).
  - Load order is the order of 'content=' lines - no mtime manipulation.
  - MGE XE and Morrowind Code Patch are not applicable (OpenMW has these
    capabilities built in).
  - get_launch_command() provides the native launch command; the plugin
    panel uses this instead of a Proton prefix.
  - The game's 'Data Files/' is never modified.  openmw.cfg accepts multiple
    'data=' directories in increasing priority order, so mods deploy into a
    profile-local folder that is appended below the vanilla one and OpenMW's
    own VFS layers them.  No Data Files_Core backup, no vanilla gap-fill.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from stat import S_ISLNK

from Games.base_game import BaseGame, LaunchToggle, WizardTool
from Utils.deploy import (
    LinkMode,
    cleanup_custom_deploy_dirs,
    deploy_filemap,
    expand_separator_deploy_paths,
    load_per_mod_strip_prefixes,
    load_separator_deploy_paths,
    restore_data_core,
    sweep_deploy_trash,
)
from Utils.modlist import read_modlist
from Utils.config_paths import get_profiles_dir

_PROFILES_DIR = get_profiles_dir()

_OPENMW_FLATPAK_ID = "org.openmw.OpenMW"

# Launch settings checkbox: run the engine binary instead of the launcher GUI.
_SKIP_LAUNCHER_KEY = "skip_launcher"

# Profile-local directory that openmw.cfg points at with its own 'data=' line.
# Everything inside is derived from staging, so restore reclaims anything it
# cannot account for and then deletes the whole tree.
_DEPLOYED_DIR_NAME = "deployed"

# Config path candidates - Flatpak first
_OPENMW_CFG_CANDIDATES: list[Path] = [
    Path.home() / ".var" / "app" / _OPENMW_FLATPAK_ID / "config" / "openmw" / "openmw.cfg",
    Path.home() / ".config" / "openmw" / "openmw.cfg",
]


def _detect_openmw_cfg() -> Path | None:
    """Return the first openmw.cfg candidate that exists on disk, or None."""
    for candidate in _OPENMW_CFG_CANDIDATES:
        if candidate.is_file():
            return candidate
    return None


class OpenMW(BaseGame):

    # OpenMW can deploy by copying, so the saved "copy" mode must be honoured.
    deploy_mode_supports_copy = True
    # Root-flagged mods still deploy verbatim into <game>/Data Files/, so the
    # filemap_root consumers need that prefix even though the normal deploy
    # dir now lives outside the install.
    game_data_subpath_override = "Data Files"
    # The openmw.cfg path is a configured path, so make it per-profile like the
    # game/prefix paths (stored as a paths.json extra).
    profile_overridable_paths_extras = ("openmw_cfg_path",)

    vanilla_plugins = ["Morrowind.esm", "Tribunal.esm", "Bloodmoon.esm"]

    @property
    def supports_bain(self) -> bool:
        return True

    def __init__(self):
        self._game_path: Path | None = None
        self._prefix_path: Path | None = None
        self._openmw_cfg_path: Path | None = None  # None → auto-detect
        self._deploy_mode: LinkMode = LinkMode.HARDLINK
        self._staging_path: Path | None = None
        self.load_paths()

    # -----------------------------------------------------------------------
    # Identity
    # -----------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "Morrowind (OpenMW)"

    @property
    def game_id(self) -> str:
        return "morrowind_openmw"

    @property
    def exe_name(self) -> str:
        return "openmw-launcher"

    @property
    def exe_name_alts(self) -> list[str]:
        return ["Morrowind Launcher.exe", "Morrowind.exe"]

    @property
    def plugin_extensions(self) -> list[str]:
        return [".esp", ".esm", ".omwscripts", ".omwaddon"]

    @property
    def steam_id(self) -> str:
        return "22320"

    @property
    def nexus_game_domain(self) -> str:
        return "morrowind"

    @property
    def mod_folder_strip_prefixes(self) -> set[str]:
        return {"Data Files"}

    @property
    def mod_required_top_level_folders(self) -> set[str]:
        return {
            "bookart",
            "distantlod",
            "fonts",
            "icons",
            "iwy",
            "kw",
            "meshes",
            "music",
            "mwse",
            "shaders",
            "sound",
            "splash",
            "textures",
            "video",
        }

    @property
    def mod_auto_strip_until_required(self) -> bool:
        return True

    @property
    def mod_required_file_types(self) -> set[str]:
        return {".esp", ".esm", ".omwscripts", ".omwaddon", ".ini"}

    @property
    def mod_install_as_is_if_no_match(self) -> bool:
        return True

    @property
    def conflict_ignore_filenames(self) -> set[str]:
        return {"info.xml", "readme.txt", "*.jpg"}

    @property
    def loot_sort_enabled(self) -> bool:
        return True

    @property
    def loot_game_type(self) -> str:
        return "OpenMW"

    @property
    def loot_masterlist_repo(self) -> str:
        return "morrowind"

    @property
    def wizard_tools(self) -> list[WizardTool]:
        return self._base_wizard_tools()

    @property
    def launch_toggles(self) -> list[LaunchToggle]:
        return [LaunchToggle(
            key=_SKIP_LAUNCHER_KEY,
            label="Skip the OpenMW launcher (start the game directly)",
            hint=("The launcher keeps its own copy of the load order and "
                  "writes it back to openmw.cfg, which can overwrite what "
                  "Amethyst deployed. Off: the launcher opens as usual."),
        )]

    # -----------------------------------------------------------------------
    # Native launch command
    # -----------------------------------------------------------------------

    def _is_flatpak_install(self) -> bool:
        """Return True when the Flatpak openmw.cfg exists on disk."""
        return _OPENMW_CFG_CANDIDATES[0].is_file()

    def _skip_launcher(self) -> bool:
        """True when Play should start the engine, not the OpenMW launcher."""
        from Utils.exe_launch import load_launch_toggle
        return load_launch_toggle(self, _SKIP_LAUNCHER_KEY, default=False)

    def get_launch_command(self) -> list[str] | None:
        """Return the native Linux command to launch OpenMW.

        Checks (in order):
          1. Flatpak install  → ['flatpak', 'run', 'org.openmw.OpenMW']
          2. openmw-launcher on PATH
          3. openmw binary on PATH (headless fallback)
          4. None if nothing found.

        With the "skip the launcher" toggle on, the engine binary is used
        instead at every step - ``--command=openmw`` for the Flatpak, the
        ``openmw`` binary for a native install.
        """
        skip = self._skip_launcher()
        if self._is_flatpak_install():
            # `--command=` picks the binary inside the sandbox; without it the
            # manifest's default command (the launcher) runs.
            app_args = (["--command=openmw", _OPENMW_FLATPAK_ID] if skip
                        else [_OPENMW_FLATPAK_ID])
            # Inside our own Flatpak sandbox there is no `flatpak` CLI -
            # forward the launch to the host via flatpak-spawn.
            if Path("/.flatpak-info").exists():
                if shutil.which("flatpak-spawn"):
                    return ["flatpak-spawn", "--host", "--directory=/",
                            "flatpak", "run", *app_args]
            elif shutil.which("flatpak"):
                return ["flatpak", "run", *app_args]
        names = ("openmw", "openmw-launcher") if skip else ("openmw-launcher", "openmw")
        for name in names:
            found = shutil.which(name)
            if found:
                return [found]
        return None

    # -----------------------------------------------------------------------
    # openmw.cfg path
    # -----------------------------------------------------------------------

    def get_openmw_cfg_path(self) -> Path:
        """Return the openmw.cfg to manage.

        Priority:
          1. User-configured override (persisted in paths.json).
          2. Auto-detected existing cfg (Flatpak or native).
          3. Default native location (created on first deploy).
        """
        if self._openmw_cfg_path:
            return self._openmw_cfg_path
        detected = _detect_openmw_cfg()
        if detected:
            return detected
        return _OPENMW_CFG_CANDIDATES[-1]

    def set_openmw_cfg_path(self, path: "Path | str | None") -> None:
        self._openmw_cfg_path = Path(path) if path else None
        self.save_paths()

    # -----------------------------------------------------------------------
    # Paths
    # -----------------------------------------------------------------------

    def get_game_path(self) -> Path | None:
        return self._game_path

    def get_vanilla_data_path(self) -> Path | None:
        """Return the game's own 'Data Files/' - read-only as far as we care."""
        if self._game_path is None:
            return None
        return self._game_path / "Data Files"

    def get_deployed_data_path(self, profile: str | None = None) -> Path:
        """Return the profile-local dir openmw.cfg loads mod files from."""
        if profile:
            profile_dir = self.get_profile_root() / "profiles" / profile
        else:
            profile_dir = (self._active_profile_dir
                           or self.get_profile_root() / "profiles" / "default")
        return Path(profile_dir) / _DEPLOYED_DIR_NAME

    def get_mod_data_path(self) -> Path | None:
        # Mods deploy outside the install; the Data tab, incremental deploy and
        # mod removal all follow this path rather than the game's own folder.
        return self.get_deployed_data_path()

    def get_vanilla_plugins_path(self) -> Path | None:
        return self.get_vanilla_data_path()

    def runtime_snapshot_exclude_dirs(self) -> set[str] | None:
        # Deployment never writes into 'Data Files/', so anything appearing
        # there is the user's own; capture only files outside it.
        return {"Data Files"}

    def get_mod_staging_path(self) -> Path:
        if self._staging_path is not None:
            return self._staging_path / "mods"
        return _PROFILES_DIR / self.name / "mods"

    # -----------------------------------------------------------------------
    # Configuration persistence
    # -----------------------------------------------------------------------

    # OpenMW is a native Linux binary - never look up a Proton prefix.
    def _find_prefix_for_load(self) -> "Path | None":
        return None

    # load_paths / save_paths are inherited from BaseGame (profile-aware);
    # openmw_cfg_path is persisted via the _load/_save_paths_extra hooks below.
    def _load_paths_extra(self, data: dict) -> None:
        raw_cfg = data.get("openmw_cfg_path", "")
        self._openmw_cfg_path = Path(raw_cfg) if raw_cfg else None

    def _save_paths_extra(self) -> dict:
        return {
            "openmw_cfg_path": str(self._openmw_cfg_path) if self._openmw_cfg_path else "",
        }

    def set_staging_path(self, path: "Path | str | None") -> None:
        self._staging_path = Path(path) if path else None
        self.save_paths()

    # OpenMW is a native Linux binary - no Proton prefix.
    def get_prefix_path(self) -> Path | None:
        return None

    def set_prefix_path(self, path: "Path | str | None") -> None:
        pass  # Not applicable for OpenMW.

    def get_deploy_mode(self) -> LinkMode:
        return self._deploy_mode

    def set_deploy_mode(self, mode: LinkMode) -> None:
        self._deploy_mode = mode
        self.save_paths()

    # -----------------------------------------------------------------------
    # Deployment
    # -----------------------------------------------------------------------

    def deploy(self, log_fn=None, mode: LinkMode = LinkMode.HARDLINK,
               profile: str = "default", progress_fn=None) -> None:
        _log = log_fn or (lambda _: None)

        if self._game_path is None:
            raise RuntimeError("Game path is not configured.")

        vanilla_dir = self.get_vanilla_data_path()
        data_dir    = self.get_deployed_data_path(profile)
        filemap     = self.get_effective_filemap_path()
        staging     = self.get_effective_mod_staging_path()

        if not vanilla_dir.is_dir():
            raise RuntimeError(f"'Data Files' directory not found: {vanilla_dir}")
        if not filemap.is_file():
            raise RuntimeError(
                f"filemap.txt not found: {filemap}\n"
                "Run 'Build Filemap' before deploying."
            )

        _log("Step 1: Preparing the profile's OpenMW data directory ...")
        self._clear_deployed_dir(data_dir, log_fn=_log)
        data_dir.mkdir(parents=True, exist_ok=True)
        _log(f"  {data_dir}")

        _log(f"Step 2: Transferring mod files into '{data_dir.name}/' ({mode.name}) ...")
        profile_dir    = self.get_profile_root() / "profiles" / profile
        per_mod_strip  = load_per_mod_strip_prefixes(profile_dir)
        _sep_deploy    = load_separator_deploy_paths(profile_dir)
        _sep_entries   = read_modlist(profile_dir / "modlist.txt") if _sep_deploy else []
        per_mod_deploy = expand_separator_deploy_paths(_sep_deploy, _sep_entries) or None
        linked_mod, _placed = deploy_filemap(
            filemap, data_dir, staging,
            mode=mode,
            strip_prefixes=self.mod_folder_strip_prefixes,
            per_mod_strip_prefixes=per_mod_strip,
            per_mod_deploy_dirs=per_mod_deploy,
            log_fn=_log,
            progress_fn=progress_fn,
        )
        _log(f"  Transferred {linked_mod} mod file(s).")

        _log(f"Step 3: Normalising plugin extensions to lowercase in "
             f"'{data_dir.name}/' ...")
        _renamed = 0
        for _f in data_dir.iterdir():
            if _f.is_file():
                _dot = _f.name.rfind(".")
                if _dot != -1:
                    _ext = _f.name[_dot:]
                    if _ext != _ext.lower():
                        _f.rename(_f.parent / (_f.name[:_dot] + _ext.lower()))
                        _renamed += 1
        _log(f"  Renamed {_renamed} file(s).")

        _log("Step 4: Updating openmw.cfg ...")
        from Games.Morrowind.openmw_cfg import update_openmw_cfg
        plugins_txt = profile_dir / "plugins.txt"
        cfg_path    = self.get_openmw_cfg_path()

        # Collect mod .bsa files from the filemap (in priority order, top wins).
        # These are BSAs that were deployed from mods and need fallback-archive= entries.
        bsa_archives: list[str] = []
        _seen_bsa: set[str] = set()
        if filemap.is_file():
            for _line in filemap.read_text(encoding="utf-8", errors="replace").splitlines():
                _line = _line.strip()
                if not _line or _line.startswith("#"):
                    continue
                parts = _line.split("\t", 1)
                rel_path = parts[0]
                if rel_path.lower().endswith(".bsa"):
                    _bsa_name = Path(rel_path).name
                    _key = _bsa_name.lower()
                    if _key not in _seen_bsa:
                        _seen_bsa.add(_key)
                        bsa_archives.append(_bsa_name)

        # The vanilla dir comes first, the profile's dir second: OpenMW reads
        # data= entries in increasing priority, so mods win every collision
        # while the game's own files stay exactly where Steam put them.
        update_openmw_cfg(
            cfg_path=cfg_path,
            data_dirs=[vanilla_dir, data_dir],
            plugins_txt=plugins_txt,
            fallback_archives=bsa_archives,
            log_fn=_log,
        )

        _log(
            f"Deploy complete. {linked_mod} mod file(s) in '{data_dir}'; "
            f"the game's 'Data Files/' was not modified."
        )

        # Root_Folder / root-flagged payload still lands in the install, so keep
        # capturing whatever appears alongside it.
        self.snapshot_root_for_runtime_capture(log_fn=_log)

    def restore(self, log_fn=None, progress_fn=None) -> None:
        _log = log_fn or (lambda _: None)

        if self._game_path is None:
            raise RuntimeError("Game path is not configured.")

        vanilla_dir = self.get_vanilla_data_path()

        _profile_dir = self._active_profile_dir
        _entries = read_modlist(_profile_dir / "modlist.txt") if _profile_dir else []
        cleanup_custom_deploy_dirs(_profile_dir, _entries, log_fn=_log)

        _log("Restore: removing mod content from openmw.cfg ...")
        from Games.Morrowind.openmw_cfg import restore_openmw_cfg
        cfg_path = self.get_openmw_cfg_path()
        if cfg_path.is_file():
            restore_openmw_cfg(cfg_path, data_dirs=[vanilla_dir], log_fn=_log)

        _log("Restore: clearing the profile OpenMW data directories ...")
        cleared = 0
        for deployed in self._deployed_data_dirs():
            cleared += self._clear_deployed_dir(deployed, log_fn=_log)
        _log(f"  Removed {cleared} deployed director(y/ies).")

        self._restore_legacy_data_core(vanilla_dir, log_fn=_log)

        moved = self.capture_runtime_files_to_root_folder(log_fn=_log)
        if moved:
            _log(f"  Moved {moved} runtime file(s) to Root_Folder/.")

        _log("Restore complete.")

    # -----------------------------------------------------------------------
    # Deployed-directory lifecycle
    # -----------------------------------------------------------------------

    def _deployed_data_dirs(self) -> list[Path]:
        """Return every profile's deployed dir that currently exists on disk."""
        found: list[Path] = []
        profiles_root = self.get_profile_root() / "profiles"
        try:
            entries = sorted(profiles_root.iterdir())
        except OSError:
            return found
        for entry in entries:
            candidate = entry / _DEPLOYED_DIR_NAME
            if candidate.is_dir() and not candidate.is_symlink():
                found.append(candidate)
        return found

    def _clear_deployed_dir(self, deployed: Path, log_fn=None) -> int:
        """Reclaim anything we cannot account for, then delete *deployed*.

        Returns 1 when a directory was removed, 0 when there was nothing to do.
        """
        _log = log_fn or (lambda _: None)
        sweep_deploy_trash(deployed.parent, log_fn=_log)
        if not deployed.is_dir() or deployed.is_symlink():
            return 0

        rescued = self._rescue_foreign_files(deployed, log_fn=_log)
        if rescued:
            _log(f"  Moved {rescued} unrecognised file(s) from "
                 f"'{deployed.name}/' into overwrite/.")

        # Rename first so an interrupted delete leaves a sweepable '.mm_trash-'
        # directory rather than a half-populated deploy dir the next run trusts.
        trash = deployed.parent / f"{deployed.name}.mm_trash-{os.getpid()}"
        try:
            deployed.rename(trash)
        except OSError:
            trash = deployed
        shutil.rmtree(trash, ignore_errors=True)
        return 1

    def _rescue_foreign_files(self, deployed: Path, log_fn=None) -> int:
        """Move files we did not deploy from *deployed* into overwrite/.

        Symlinks and multiply-linked files came from staging. A plain file is
        ours too when it still matches the size/mtime deploy_filemap recorded
        (COPY mode, or a hardlink that fell back to a copy). Anything else was
        written by the user or a tool and must survive the wipe.
        """
        _log = log_fn or (lambda _: None)
        from Utils.deploy_standard import (
            _DEPLOY_STATS_NAME, _MTIME_TOLERANCE_NS, _load_deploy_stats)

        stats = _load_deploy_stats(
            self.get_effective_filemap_path().parent / _DEPLOY_STATS_NAME)
        overwrite = Path(self.get_effective_overwrite_path())
        rescued = 0
        prefix_len = len(str(deployed)) + 1

        for dirpath, _dirnames, filenames in os.walk(deployed):
            for name in filenames:
                src = Path(dirpath) / name
                try:
                    st = src.lstat()
                except OSError:
                    continue
                if S_ISLNK(st.st_mode) or st.st_nlink > 1:
                    continue
                rel = str(src)[prefix_len:].replace("\\", "/")
                recorded = stats.get(rel.lower())
                if (recorded is not None
                        and recorded[0] == st.st_size
                        and abs(recorded[1] - st.st_mtime_ns)
                        <= _MTIME_TOLERANCE_NS):
                    continue
                dest = overwrite / rel
                try:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(src), str(dest))
                    rescued += 1
                except OSError as exc:
                    _log(f"  WARN: could not rescue {src}: {exc}")
        return rescued

    def _restore_legacy_data_core(self, vanilla_dir: Path, log_fn=None) -> None:
        """Unwind an in-place deployment left by the pre-data= implementation."""
        _log = log_fn or (lambda _: None)
        core_dir = vanilla_dir.parent / (vanilla_dir.name + "_Core")
        if not core_dir.is_dir():
            return
        if not self.get_deploy_active():
            # The vanilla Morrowind handler shares this install and makes the
            # same backup. Without a deployment of our own on record the folder
            # is not ours to unwind - say so rather than tear down its deploy.
            _log(f"  NOTE: '{core_dir.name}/' exists but this game has no "
                 "deployment on record - leaving it alone (run Restore from "
                 "the Morrowind handler if it belongs to that install).")
            return

        _log("Restore: unwinding a legacy in-place deployment "
             "('Data Files_Core/' from an older Amethyst) ...")
        try:
            restored = restore_data_core(
                vanilla_dir,
                overwrite_dir=self.get_effective_overwrite_path(),
                staging_root=self.get_effective_mod_staging_path(),
                strip_prefixes=self.mod_folder_strip_prefixes,
                log_fn=_log,
            )
            _log(f"  Restored {restored} file(s). 'Data Files_Core/' removed.")
        except RuntimeError as e:
            _log(f"  Skipping legacy data restore: {e}")
