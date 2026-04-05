"""
openmw.py
Game handler for The Elder Scrolls III: Morrowind running under OpenMW.

Key differences from the vanilla Morrowind handler:
  - OpenMW is a native Linux binary — no Wine/Proton needed.
  - Flatpak install at ~/.var/app/org.openmw.OpenMW/ is auto-detected.
  - Config lives at ~/.config/openmw/openmw.cfg (native) or
    ~/.var/app/org.openmw.OpenMW/config/openmw/openmw.cfg (Flatpak).
  - Load order is the order of 'content=' lines — no mtime manipulation.
  - MGE XE and Morrowind Code Patch are not applicable (OpenMW has these
    capabilities built in).
  - get_launch_command() provides the native launch command; the plugin
    panel uses this instead of a Proton prefix.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from Games.base_game import BaseGame, WizardTool
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

_OPENMW_FLATPAK_ID = "org.openmw.OpenMW"

# Config path candidates — Flatpak first
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

    def __init__(self):
        self._game_path: Path | None = None
        self._openmw_cfg_path: Path | None = None  # None → auto-detect
        self._deploy_mode: LinkMode = LinkMode.HARDLINK
        self._staging_path: Path | None = None
        self._symlink_plugins: bool = False
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
            "fonts",
            "icons",
            "meshes",
            "music",
            "shaders",
            "sound",
            "splash",
            "textures",
            "video",
            "mwse",
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
    def loot_masterlist_url(self) -> str:
        return "https://raw.githubusercontent.com/loot/morrowind/v0.26/masterlist.yaml"

    @property
    def wizard_tools(self) -> list[WizardTool]:
        return self._base_wizard_tools()

    # -----------------------------------------------------------------------
    # Native launch command
    # -----------------------------------------------------------------------

    def _is_flatpak_install(self) -> bool:
        """Return True when the Flatpak openmw.cfg exists on disk."""
        return _OPENMW_CFG_CANDIDATES[0].is_file()

    def get_launch_command(self) -> list[str] | None:
        """Return the native Linux command to launch OpenMW.

        Checks (in order):
          1. Flatpak install  → ['flatpak', 'run', 'org.openmw.OpenMW']
          2. openmw-launcher on PATH
          3. openmw binary on PATH (headless fallback)
          4. None if nothing found.
        """
        if self._is_flatpak_install():
            if shutil.which("flatpak"):
                return ["flatpak", "run", _OPENMW_FLATPAK_ID]
        launcher = shutil.which("openmw-launcher")
        if launcher:
            return [launcher]
        openmw = shutil.which("openmw")
        if openmw:
            return [openmw]
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

    def get_mod_data_path(self) -> Path | None:
        if self._game_path is None:
            return None
        return self._game_path / "Data Files"

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
            self._openmw_cfg_path = None
            self._staging_path = None
            return False
        try:
            data = json.loads(self._paths_file.read_text(encoding="utf-8"))
            raw = data.get("game_path", "")
            if raw:
                self._game_path = Path(raw)
            raw_cfg = data.get("openmw_cfg_path", "")
            if raw_cfg:
                self._openmw_cfg_path = Path(raw_cfg)
            raw_mode = data.get("deploy_mode", "hardlink")
            self._deploy_mode = {
                "symlink": LinkMode.SYMLINK,
                "copy":    LinkMode.COPY,
            }.get(raw_mode, LinkMode.HARDLINK)
            raw_staging = data.get("staging_path", "")
            if raw_staging:
                self._staging_path = Path(raw_staging)
            self._symlink_plugins = data.get("symlink_plugins", False)
            self._validate_staging()
            return bool(self._game_path)
        except (json.JSONDecodeError, OSError):
            pass
        self._game_path = None
        self._openmw_cfg_path = None
        return False

    def save_paths(self) -> None:
        self._paths_file.parent.mkdir(parents=True, exist_ok=True)
        mode_str = {
            LinkMode.SYMLINK: "symlink",
            LinkMode.COPY:    "copy",
        }.get(self._deploy_mode, "hardlink")
        data = {
            "game_path":       str(self._game_path)       if self._game_path       else "",
            "openmw_cfg_path": str(self._openmw_cfg_path) if self._openmw_cfg_path else "",
            "deploy_mode":     mode_str,
            "staging_path":    str(self._staging_path)    if self._staging_path    else "",
            "symlink_plugins": self._symlink_plugins,
        }
        self._paths_file.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def set_game_path(self, path: "Path | str | None") -> None:
        self._game_path = Path(path) if path else None
        self.save_paths()

    def set_staging_path(self, path: "Path | str | None") -> None:
        self._staging_path = Path(path) if path else None
        self.save_paths()

    # OpenMW is a native Linux binary — no Proton prefix.
    def get_prefix_path(self) -> Path | None:
        return None

    def set_prefix_path(self, path: "Path | str | None") -> None:
        pass  # Not applicable for OpenMW.

    def get_deploy_mode(self) -> LinkMode:
        return self._deploy_mode

    def set_deploy_mode(self, mode: LinkMode) -> None:
        self._deploy_mode = mode
        self.save_paths()

    @property
    def symlink_plugins(self) -> bool:
        return self._symlink_plugins

    def set_symlink_plugins(self, value: bool) -> None:
        self._symlink_plugins = value
        self.save_paths()

    # -----------------------------------------------------------------------
    # Deployment
    # -----------------------------------------------------------------------

    def deploy(self, log_fn=None, mode: LinkMode = LinkMode.HARDLINK,
               profile: str = "default", progress_fn=None) -> None:
        _log = log_fn or (lambda _: None)

        if self._game_path is None:
            raise RuntimeError("Game path is not configured.")

        data_dir = self._game_path / "Data Files"
        filemap  = self.get_effective_filemap_path()
        staging  = self.get_effective_mod_staging_path()

        if not data_dir.is_dir():
            raise RuntimeError(f"'Data Files' directory not found: {data_dir}")
        if not filemap.is_file():
            raise RuntimeError(
                f"filemap.txt not found: {filemap}\n"
                "Run 'Build Filemap' before deploying."
            )

        _log("Step 1: Moving 'Data Files/' → 'Data Files_Core/' ...")
        moved = move_to_core(data_dir, log_fn=_log)
        _log(f"  Moved {moved} file(s) to 'Data Files_Core/'.")

        _log(f"Step 2: Transferring mod files into 'Data Files/' ({mode.name}) ...")
        profile_dir    = self.get_profile_root() / "profiles" / profile
        per_mod_strip  = load_per_mod_strip_prefixes(profile_dir)
        _sep_deploy    = load_separator_deploy_paths(profile_dir)
        _sep_entries   = read_modlist(profile_dir / "modlist.txt") if _sep_deploy else []
        per_mod_deploy = expand_separator_deploy_paths(_sep_deploy, _sep_entries) or None
        _symlink_exts  = set(self.plugin_extensions) if self._symlink_plugins else None
        linked_mod, placed = deploy_filemap(
            filemap, data_dir, staging,
            mode=mode,
            strip_prefixes=self.mod_folder_strip_prefixes,
            per_mod_strip_prefixes=per_mod_strip,
            per_mod_deploy_dirs=per_mod_deploy,
            log_fn=_log,
            progress_fn=progress_fn,
            symlink_exts=_symlink_exts,
            core_dir=data_dir.parent / (data_dir.name + "_Core"),
        )
        _log(f"  Transferred {linked_mod} mod file(s).")

        _log("Step 3: Filling gaps with vanilla files from 'Data Files_Core/' ...")
        linked_core = deploy_core(data_dir, placed, mode=mode, log_fn=_log)
        _log(f"  Transferred {linked_core} vanilla file(s).")

        _log("Step 4: Normalising plugin extensions to lowercase in 'Data Files/' ...")
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

        _log("Step 5: Updating openmw.cfg ...")
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

        update_openmw_cfg(
            cfg_path=cfg_path,
            data_dirs=[data_dir],
            plugins_txt=plugins_txt,
            fallback_archives=bsa_archives,
            log_fn=_log,
        )

        _log(
            f"Deploy complete. "
            f"{linked_mod} mod + {linked_core} vanilla "
            f"= {linked_mod + linked_core} total file(s) in 'Data Files/'."
        )

    def restore(self, log_fn=None, progress_fn=None) -> None:
        _log = log_fn or (lambda _: None)

        if self._game_path is None:
            raise RuntimeError("Game path is not configured.")

        data_dir = self._game_path / "Data Files"

        _profile_dir = self._active_profile_dir
        _entries = read_modlist(_profile_dir / "modlist.txt") if _profile_dir else []
        cleanup_custom_deploy_dirs(_profile_dir, _entries, log_fn=_log)

        _log("Restore: removing mod content from openmw.cfg ...")
        from Games.Morrowind.openmw_cfg import restore_openmw_cfg
        cfg_path = self.get_openmw_cfg_path()
        if cfg_path.is_file():
            restore_openmw_cfg(cfg_path, data_dirs=[data_dir], log_fn=_log)

        _log("Restore: clearing 'Data Files/' and moving 'Data Files_Core/' back ...")
        try:
            restored = restore_data_core(
                data_dir,
                overwrite_dir=self.get_effective_overwrite_path(),
                staging_root=self.get_effective_mod_staging_path(),
                strip_prefixes=self.mod_folder_strip_prefixes,
                log_fn=_log,
            )
            _log(f"  Restored {restored} file(s). 'Data Files_Core/' removed.")
        except RuntimeError as e:
            _log(f"  Skipping data restore: {e}")

        _log("Restore complete.")
