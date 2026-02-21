"""
TCG_Card_Shop_Simulator.py
Game handler for TCG Card Shop Simulator.

Mod structure:
  Mods install into <game_path>/BepInEx/Plugins/
  Staged mods live in Profiles/TCG Card Shop Simulator/mods/

  Root_Folder/ files deploy straight to the game install root (handled by GUI).
"""

import json
from pathlib import Path

from Games.base_game import BaseGame
from Utils.deploy import LinkMode, deploy_core, deploy_filemap, move_to_core, restore_data_core
from Utils.steam_finder import find_prefix

_PROJECT_ROOT = Path(__file__).parent.parent.parent
_PROFILES_DIR = _PROJECT_ROOT / "Profiles"


class TCG_Card_Shop_Simulator(BaseGame):

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
        return "TCG Card Shop Simulator"

    @property
    def game_id(self) -> str:
        return "TCG_Card_Shop_Simulator"

    @property
    def exe_name(self) -> str:
        return "TCG Card Shop Simulator.exe"

    @property
    def steam_id(self) -> str:
        return "3070070"

    @property
    def mod_folder_strip_prefixes(self) -> set[str]:
        return {"plugins", "bepinex"}

    @property
    def plugin_extensions(self) -> list[str]:
        return []

    @property
    def loot_sort_enabled(self) -> bool:
        return False

    @property
    def loot_game_type(self) -> str:
        return ""

    @property
    def loot_masterlist_url(self) -> str:
        return ""

    # -----------------------------------------------------------------------
    # Paths
    # -----------------------------------------------------------------------

    def get_game_path(self) -> Path | None:
        return self._game_path

    def get_mod_data_path(self) -> Path | None:
        """Mods go into BepInEx/Plugins/ inside the game directory."""
        if self._game_path is None:
            return None
        return self._game_path / "BepInEx" / "plugins"

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
                "copy":    LinkMode.COPY,
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
        """Deploy staged mods into BepInEx/Plugins/.

        Workflow:
          1. Move BepInEx/Plugins/ → BepInEx/Plugins_Core/  (vanilla backup)
          2. Transfer mod files listed in filemap.txt into BepInEx/Plugins/
          3. Fill gaps with vanilla files from Plugins_Core/
        (Root Folder deployment is handled by the GUI after this returns.)
        """
        _log = log_fn or (lambda _: None)

        if self._game_path is None:
            raise RuntimeError("Game path is not configured.")

        plugins_dir = self._game_path / "BepInEx" / "plugins"
        filemap     = self.get_profile_root() / "filemap.txt"
        staging     = self.get_mod_staging_path()

        plugins_dir.mkdir(parents=True, exist_ok=True)
        if not filemap.is_file():
            raise RuntimeError(
                f"filemap.txt not found: {filemap}\n"
                "Run 'Build Filemap' before deploying."
            )

        _log("Step 1: Moving BepInEx/plugins/ → plugins_Core/ ...")
        moved = move_to_core(plugins_dir, log_fn=_log)
        _log(f"  Moved {moved} file(s) to plugins_Core/.")

        _log(f"Step 2: Transferring mod files into plugins/ ({mode.name}) ...")
        linked_mod, placed = deploy_filemap(filemap, plugins_dir, staging,
                                            mode=mode,
                                            strip_prefixes=self.mod_folder_strip_prefixes,
                                            log_fn=_log,
                                            progress_fn=progress_fn)
        _log(f"  Transferred {linked_mod} mod file(s).")

        _log("Step 3: Filling gaps with vanilla files from plugins_Core/ ...")
        linked_core = deploy_core(plugins_dir, placed, mode=mode, log_fn=_log)
        _log(f"  Transferred {linked_core} vanilla file(s).")

        _log(
            f"Deploy complete. "
            f"{linked_mod} mod + {linked_core} vanilla "
            f"= {linked_mod + linked_core} total file(s) in Plugins/."
        )

    def restore(self, log_fn=None) -> None:
        """Restore BepInEx/Plugins/ to its vanilla state."""
        _log = log_fn or (lambda _: None)

        if self._game_path is None:
            raise RuntimeError("Game path is not configured.")

        plugins_dir = self._game_path / "BepInEx" / "plugins"

        core_dir = plugins_dir.parent / "plugins_Core"
        if core_dir.is_dir():
            _log("Restore: clearing plugins/ and moving plugins_Core/ back ...")
            restored = restore_data_core(plugins_dir, core_dir=core_dir, log_fn=_log)
            _log(f"  Restored {restored} file(s). plugins_Core/ removed.")
        elif plugins_dir.is_dir():
            _log("Restore: no plugins_Core/ found — clearing plugins/ ...")
            from Utils.deploy import _clear_dir
            removed = _clear_dir(plugins_dir)
            _log(f"  Removed {removed} file(s) from plugins/.")

        _log("Restore complete.")
