"""
resident_evil_requiem.py
Game handler for Resident Evil Requiem.

Mod structure:
  Mods install into the game root (like Cyberpunk 2077).
  Staged mods live in Profiles/Resident Evil Requiem/mods/

  Mod authors usually ship with a reframework/ and/or natives/ top-level
  folder.  Both are accepted as required top-level folders, and "reframework"
  is stripped post-install so files stage without the redundant prefix.

  Files already in the game root (natives/, etc.) are backed up before deploy
  and restored on remove — handled by deploy_filemap_to_root /
  restore_filemap_from_root (same mechanism as Cyberpunk).

  .pak files are routed to game_root/pak_mods/ via a custom rule.

  REFramework loads via dinput8.dll — the DLL override is applied to the
  Proton prefix on every deploy.
"""

import json
from pathlib import Path

from Games.base_game import BaseGame
from Utils.deploy import (
    CustomRule,
    LinkMode,
    apply_wine_dll_overrides,
    deploy_custom_rules,
    deploy_filemap_to_root,
    load_per_mod_strip_prefixes,
    cleanup_custom_deploy_dirs,
    restore_custom_rules,
    restore_filemap_from_root,
)
from Utils.modlist import read_modlist
from Utils.config_paths import get_profiles_dir
from Utils.steam_finder import find_prefix

_PROFILES_DIR = get_profiles_dir()


class ResidentEvilRequiem(BaseGame):

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
        return "Resident Evil Requiem"

    @property
    def game_id(self) -> str:
        return "resident_evil_requiem"

    @property
    def exe_name(self) -> str:
        return "re9.exe"

    @property
    def steam_id(self) -> str:
        return "3764200"

    @property
    def nexus_game_domain(self) -> str:
        return "residentevilrequiem"

    @property
    def mod_required_top_level_folders(self) -> set[str]:
        return {"reframework", "natives", "pak_mods"}
    
    @property
    def mod_install_as_is_if_no_match(self) -> bool:
        return True

    @property
    def mod_auto_strip_until_required(self) -> bool:
        return True

    @property
    def mod_supports_bundles(self) -> bool:
        return True

    @property
    def conflict_ignore_filenames(self) -> set[str]:
        return {"modinfo.ini","readme.txt","*.png","*.jpg"}

    @property
    def wine_dll_overrides(self) -> dict[str, str]:
        return {"dinput8": "native,builtin"}

    @property
    def reshade_dll(self) -> str:
        return "dxgi.dll"

    @property
    def frameworks(self) -> dict[str, str]:
        return {"ReFramework": "dinput8.dll"}

    @property
    def custom_routing_rules(self) -> list[CustomRule]:
        return [
            CustomRule(
                dest="pak_mods",
                extensions=[".pak"],
            ),
        ]

    # -----------------------------------------------------------------------
    # Paths
    # -----------------------------------------------------------------------

    def get_game_path(self) -> Path | None:
        return self._game_path

    def get_mod_data_path(self) -> Path | None:
        return self._game_path

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
            LinkMode.COPY: "copy",
        }.get(self._deploy_mode, "hardlink")
        data = {
            "game_path": str(self._game_path) if self._game_path else "",
            "prefix_path": str(self._prefix_path) if self._prefix_path else "",
            "deploy_mode": mode_str,
            "staging_path": str(self._staging_path) if self._staging_path else "",
        }
        self._paths_file.write_text(
            json.dumps(data, indent=2), encoding="utf-8"
        )

    def set_game_path(self, path: Path | str | None) -> None:
        self._game_path = Path(path) if path else None
        self.save_paths()

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
        """Deploy staged mods into the game root (Cyberpunk-style).

        Workflow:
          1. Route .pak files to game_root/pak_mods/ via custom rule
          2. Deploy remaining mod files to game root, backing up any vanilla
             files they overwrite (natives/ etc.)
          3. Apply dinput8.dll DLL override to the Proton prefix
        (Root Folder deployment is handled by the GUI after this returns.)
        """
        _log = log_fn or (lambda _: None)

        if self._game_path is None:
            raise RuntimeError("Game path is not configured.")

        filemap = self.get_effective_filemap_path()
        staging = self.get_effective_mod_staging_path()

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
            _log("Step 1: Routing .pak files to pak_mods/ ...")
            custom_exclude = deploy_custom_rules(
                filemap, self._game_path, staging,
                rules=custom_rules,
                mode=mode,
                strip_prefixes=self.mod_folder_strip_prefixes,
                log_fn=_log,
            )

        _log(f"Step 2: Deploying mod files to game root ({mode.name}), backing up any overwritten vanilla files ...")
        linked_mod, _ = deploy_filemap_to_root(
            filemap, self._game_path, staging,
            mode=mode,
            strip_prefixes=self.mod_folder_strip_prefixes,
            per_mod_strip_prefixes=per_mod_strip,
            log_fn=_log,
            progress_fn=progress_fn,
            exclude=custom_exclude or None,
        )
        _log(f"  Deployed {linked_mod} mod file(s).")

        if self._prefix_path and self._prefix_path.is_dir():
            _log("Step 3: Applying DLL overrides to Proton prefix ...")
            apply_wine_dll_overrides(self._prefix_path, self.wine_dll_overrides, log_fn=_log)

        _log(f"Deploy complete. {linked_mod} mod file(s) deployed to game root.")

    def restore(self, log_fn=None, progress_fn=None) -> None:
        """Remove deployed mod files and restore any backed-up vanilla files."""
        _log = log_fn or (lambda _: None)

        if self._game_path is None:
            raise RuntimeError("Game path is not configured.")

        _profile_dir = self._active_profile_dir
        _entries = read_modlist(_profile_dir / "modlist.txt") if _profile_dir else []
        cleanup_custom_deploy_dirs(_profile_dir, _entries, log_fn=_log)

        custom_rules = self.custom_routing_rules
        if custom_rules:
            _log("Restore: removing pak_mods/ custom-deployed files ...")
            restore_custom_rules(
                self.get_effective_filemap_path(),
                self._game_path,
                rules=custom_rules,
                log_fn=_log,
            )

        _log("Restore: removing mod files from game root and restoring vanilla backups ...")
        restore_filemap_from_root(
            self.get_effective_filemap_path(),
            self._game_path,
            log_fn=_log,
        )
        _log("Restore complete.")
