"""
custom_game.py
Dynamic game handler loaded from a user-supplied JSON definition.

JSON format (~/.config/AmethystModManager/custom_games/<game_id>.json):
{
  "name":              "My Game",
  "game_id":           "my_game",
  "exe_name":          "MyGame.exe",
  "deploy_type":       "standard",   // "standard" | "root" | "ue5"
  "mod_data_path":     "Data",       // relative path (standard only; ignored for root/ue5)
  "steam_id":          "",           // optional Steam App ID
  "nexus_game_domain": "",           // optional Nexus domain slug
  "image_url":         ""            // optional banner image URL
}

Deploy types
------------
standard — mods go into a single subdirectory (mod_data_path) inside the
           game root, same pattern as Bethesda games and BepInEx.
           Uses the Core backup + filemap deploy approach.

root     — mods are deployed directly to the game's root folder,
           same pattern as The Witcher 3 and Cyberpunk 2077.
           Uses the root filemap deploy approach (backed-up log).

ue5      — uses the UE5 multi-target manifest deploy; with no routing
           rules everything lands in the game root via the deployed.txt
           manifest, same as Oblivion Remastered / Hogwarts Legacy.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from Games.base_game import BaseGame
from Games.ue5_game import UE5Game, UE5Rule
from Utils.deploy import (
    LinkMode,
    deploy_core,
    deploy_filemap,
    deploy_filemap_to_root,
    load_per_mod_strip_prefixes,
    move_to_core,
    restore_data_core,
    restore_filemap_from_root,
)
from Utils.config_paths import get_profiles_dir, get_custom_games_dir

_PROFILES_DIR = get_profiles_dir()


# ---------------------------------------------------------------------------
# Definition → Python type helpers
# ---------------------------------------------------------------------------

def _defn_to_set(defn: dict, key: str) -> set[str]:
    """Return a set[str] from a JSON field that may be a list or comma string."""
    raw = defn.get(key, [])
    if isinstance(raw, list):
        return {s.strip().lower() for s in raw if s.strip()}
    if isinstance(raw, str):
        return {s.strip().lower() for s in raw.split(",") if s.strip()}
    return set()


def _defn_to_list(defn: dict, key: str) -> list[str]:
    """Return a list[str] from a JSON field that may be a list or comma string."""
    raw = defn.get(key, [])
    if isinstance(raw, list):
        return [s.strip() for s in raw if s.strip()]
    if isinstance(raw, str):
        return [s.strip() for s in raw.split(",") if s.strip()]
    return []


def _defn_to_dll_overrides(defn: dict) -> dict[str, str]:
    """Return a dict[str, str] from wine_dll_overrides.

    Accepts either a JSON object ({"winhttp": "native,builtin"}) or a
    newline/comma-separated string ("winhttp=native,builtin").
    """
    raw = defn.get("wine_dll_overrides", {})
    if isinstance(raw, dict):
        return {k.strip(): v.strip() for k, v in raw.items() if k.strip()}
    if isinstance(raw, str):
        result: dict[str, str] = {}
        for entry in raw.replace(",", "\n").splitlines():
            if "=" in entry:
                k, _, v = entry.partition("=")
                result[k.strip()] = v.strip()
        return result
    return {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_custom_game_definitions() -> list[dict]:
    """Return all valid custom game definition dicts from the custom_games dir."""
    defs: list[dict] = []
    folder = get_custom_games_dir()
    for f in sorted(folder.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if data.get("name") and data.get("exe_name"):
                data["_source_file"] = str(f)
                defs.append(data)
        except Exception:
            pass
    return defs


def save_custom_game_definition(defn: dict) -> Path:
    """Write a custom game definition to JSON.  Returns the file path."""
    folder = get_custom_games_dir()
    game_id = defn.get("game_id") or _make_game_id(defn.get("name", "custom_game"))
    defn["game_id"] = game_id
    dest = folder / f"{game_id}.json"
    clean = {k: v for k, v in defn.items() if not k.startswith("_")}
    dest.write_text(json.dumps(clean, indent=2, ensure_ascii=False), encoding="utf-8")
    return dest


def delete_custom_game_definition(game_id: str) -> None:
    """Delete the JSON file for a custom game."""
    folder = get_custom_games_dir()
    target = folder / f"{game_id}.json"
    target.unlink(missing_ok=True)


def _make_game_id(name: str) -> str:
    """Turn a display name into a filesystem-safe game_id."""
    safe = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in name)
    return safe.strip("_") or "custom_game"


# ---------------------------------------------------------------------------
# Standard-deploy custom game
# ---------------------------------------------------------------------------

class StandardCustomGame(BaseGame):
    """Custom game that deploys mods into a single subdirectory ('standard' mode)."""

    def __init__(self, defn: dict) -> None:
        self._defn = defn
        self._game_path: Path | None = None
        self._prefix_path: Path | None = None
        self._deploy_mode: LinkMode = LinkMode.HARDLINK
        self._staging_path: Path | None = None
        self.load_paths()

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return self._defn["name"]

    @property
    def game_id(self) -> str:
        return self._defn.get("game_id") or _make_game_id(self.name)

    @property
    def exe_name(self) -> str:
        return self._defn.get("exe_name", "")

    @property
    def steam_id(self) -> str:
        return self._defn.get("steam_id", "")

    @property
    def nexus_game_domain(self) -> str:
        return self._defn.get("nexus_game_domain", "")

    @property
    def image_url(self) -> str:
        return self._defn.get("image_url", "")

    @property
    def is_custom(self) -> bool:
        return True

    # ------------------------------------------------------------------
    # Advanced mod-handling properties (read from JSON definition)
    # ------------------------------------------------------------------

    @property
    def mod_folder_strip_prefixes(self) -> set[str]:
        return _defn_to_set(self._defn, "mod_folder_strip_prefixes")

    @property
    def conflict_ignore_filenames(self) -> set[str]:
        return _defn_to_set(self._defn, "conflict_ignore_filenames")

    @property
    def mod_folder_strip_prefixes_post(self) -> set[str]:
        return _defn_to_set(self._defn, "mod_folder_strip_prefixes_post")

    @property
    def mod_install_prefix(self) -> str:
        return self._defn.get("mod_install_prefix", "")

    @property
    def mod_required_top_level_folders(self) -> set[str]:
        return _defn_to_set(self._defn, "mod_required_top_level_folders")

    @property
    def mod_auto_strip_until_required(self) -> bool:
        return bool(self._defn.get("mod_auto_strip_until_required", False))

    @property
    def heroic_app_names(self) -> list[str]:
        return _defn_to_list(self._defn, "heroic_app_names")

    @property
    def wine_dll_overrides(self) -> dict[str, str]:
        return _defn_to_dll_overrides(self._defn)

    @property
    def restore_before_deploy(self) -> bool:
        return bool(self._defn.get("restore_before_deploy", True))

    # ------------------------------------------------------------------
    # Paths
    # ------------------------------------------------------------------

    def get_game_path(self) -> Path | None:
        return self._game_path

    def get_mod_data_path(self) -> Path | None:
        if self._game_path is None:
            return None
        rel = self._defn.get("mod_data_path", "").strip("/\\")
        if rel:
            return self._game_path / rel
        return self._game_path

    def get_mod_staging_path(self) -> Path:
        if self._staging_path is not None:
            return self._staging_path / "mods"
        return _PROFILES_DIR / self.name / "mods"

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

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
            self._deploy_mode = {"symlink": LinkMode.SYMLINK, "copy": LinkMode.COPY}.get(
                raw_mode, LinkMode.HARDLINK
            )
            raw_staging = data.get("staging_path", "")
            if raw_staging:
                self._staging_path = Path(raw_staging)
            self._validate_staging()
            return bool(self._game_path)
        except (json.JSONDecodeError, OSError):
            pass
        return False

    def save_paths(self) -> None:
        self._paths_file.parent.mkdir(parents=True, exist_ok=True)
        mode_str = {LinkMode.SYMLINK: "symlink", LinkMode.COPY: "copy"}.get(
            self._deploy_mode, "hardlink"
        )
        data = {
            "game_path":    str(self._game_path)    if self._game_path    else "",
            "prefix_path":  str(self._prefix_path)  if self._prefix_path  else "",
            "deploy_mode":  mode_str,
            "staging_path": str(self._staging_path) if self._staging_path else "",
        }
        self._paths_file.write_text(json.dumps(data, indent=2), encoding="utf-8")

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

    # ------------------------------------------------------------------
    # Deployment
    # ------------------------------------------------------------------

    def deploy(self, log_fn=None, mode: LinkMode = LinkMode.HARDLINK,
               profile: str = "default", progress_fn=None) -> None:
        _log = log_fn or (lambda _: None)
        if self._game_path is None:
            raise RuntimeError("Game path is not configured.")

        data_dir = self.get_mod_data_path()
        filemap  = self.get_profile_root() / "filemap.txt"
        staging  = self.get_mod_staging_path()

        if data_dir is None:
            raise RuntimeError("Mod data path could not be resolved.")
        if not filemap.is_file():
            raise RuntimeError(f"filemap.txt not found: {filemap}\nRun 'Build Filemap' before deploying.")

        data_dir.mkdir(parents=True, exist_ok=True)
        _log(f"Step 1: Moving {data_dir.name}/ → {data_dir.name}_Core/ ...")
        moved = move_to_core(data_dir, log_fn=_log)
        _log(f"  Moved {moved} file(s) to {data_dir.name}_Core/.")

        _log(f"Step 2: Transferring mod files into {data_dir.name}/ ({mode.name}) ...")
        profile_dir = self.get_profile_root() / "profiles" / profile
        per_mod_strip = load_per_mod_strip_prefixes(profile_dir)
        linked_mod, placed = deploy_filemap(
            filemap, data_dir, staging,
            mode=mode,
            strip_prefixes=self.mod_folder_strip_prefixes,
            per_mod_strip_prefixes=per_mod_strip,
            log_fn=_log,
            progress_fn=progress_fn,
        )
        _log(f"  Transferred {linked_mod} mod file(s).")

        _log(f"Step 3: Filling gaps with vanilla files from {data_dir.name}_Core/ ...")
        linked_core = deploy_core(data_dir, placed, mode=mode, log_fn=_log)
        _log(f"  Transferred {linked_core} vanilla file(s).")
        _log(f"Deploy complete. {linked_mod} mod + {linked_core} vanilla = {linked_mod + linked_core} total file(s).")

    def restore(self, log_fn=None, progress_fn=None) -> None:
        _log = log_fn or (lambda _: None)
        if self._game_path is None:
            raise RuntimeError("Game path is not configured.")
        data_dir = self.get_mod_data_path()
        if data_dir is None:
            raise RuntimeError("Mod data path could not be resolved.")
        _log(f"Restore: clearing {data_dir.name}/ and moving {data_dir.name}_Core/ back ...")
        restored = restore_data_core(
            data_dir,
            overwrite_dir=self.get_profile_root() / "overwrite",
            log_fn=_log,
        )
        _log(f"  Restored {restored} file(s). {data_dir.name}_Core/ removed.")
        _log("Restore complete.")


# ---------------------------------------------------------------------------
# Root-deploy custom game
# ---------------------------------------------------------------------------

class RootCustomGame(StandardCustomGame):
    """Custom game that deploys mods directly to the game root ('root' mode)."""

    def get_mod_data_path(self) -> Path | None:
        """Root deploy: the 'data' path is the game root itself."""
        return self._game_path

    def deploy(self, log_fn=None, mode: LinkMode = LinkMode.HARDLINK,
               profile: str = "default", progress_fn=None) -> None:
        _log = log_fn or (lambda _: None)
        if self._game_path is None:
            raise RuntimeError("Game path is not configured.")

        game_root = self._game_path
        filemap   = self.get_profile_root() / "filemap.txt"
        staging   = self.get_mod_staging_path()

        if not filemap.is_file():
            raise RuntimeError(f"filemap.txt not found: {filemap}\nRun 'Build Filemap' before deploying.")

        _log(f"Transferring mod files into game root ({mode.name}) ...")
        profile_dir = self.get_profile_root() / "profiles" / profile
        per_mod_strip = load_per_mod_strip_prefixes(profile_dir)
        linked_mod, _ = deploy_filemap_to_root(
            filemap, game_root, staging,
            mode=mode,
            strip_prefixes=self.mod_folder_strip_prefixes,
            per_mod_strip_prefixes=per_mod_strip,
            log_fn=_log,
            progress_fn=progress_fn,
        )
        _log(f"Deploy complete. {linked_mod} mod file(s) placed in game root.")

    def restore(self, log_fn=None, progress_fn=None) -> None:
        _log = log_fn or (lambda _: None)
        if self._game_path is None:
            raise RuntimeError("Game path is not configured.")
        filemap   = self.get_profile_root() / "filemap.txt"
        game_root = self._game_path
        _log("Restore: removing mod files and restoring vanilla files ...")
        removed = restore_filemap_from_root(filemap, game_root, log_fn=_log)
        _log(f"Restore complete. {removed} mod file(s) removed from game root.")


# ---------------------------------------------------------------------------
# UE5-deploy custom game
# ---------------------------------------------------------------------------

class Ue5CustomGame(UE5Game):
    """Custom game that uses the UE5 manifest deploy ('ue5' mode).

    No routing rules are defined, so every file goes to the game root and is
    tracked via ue5_deployed.txt — identical to how Oblivion Remastered works
    when all rules fall through to the default destination.
    """

    def __init__(self, defn: dict) -> None:
        self._defn = defn
        # UE5Game.__init__ calls load_paths() — run after setting _defn
        super().__init__()

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return self._defn["name"]

    @property
    def game_id(self) -> str:
        return self._defn.get("game_id") or _make_game_id(self.name)

    @property
    def exe_name(self) -> str:
        return self._defn.get("exe_name", "")

    @property
    def steam_id(self) -> str:
        return self._defn.get("steam_id", "")

    @property
    def nexus_game_domain(self) -> str:
        return self._defn.get("nexus_game_domain", "")

    @property
    def image_url(self) -> str:
        return self._defn.get("image_url", "")

    @property
    def is_custom(self) -> bool:
        return True

    # ------------------------------------------------------------------
    # Advanced mod-handling properties (read from JSON definition)
    # ------------------------------------------------------------------

    @property
    def mod_folder_strip_prefixes(self) -> set[str]:
        return _defn_to_set(self._defn, "mod_folder_strip_prefixes")

    @property
    def conflict_ignore_filenames(self) -> set[str]:
        return _defn_to_set(self._defn, "conflict_ignore_filenames")

    @property
    def mod_folder_strip_prefixes_post(self) -> set[str]:
        return _defn_to_set(self._defn, "mod_folder_strip_prefixes_post")

    @property
    def mod_install_prefix(self) -> str:
        return self._defn.get("mod_install_prefix", "")

    @property
    def mod_required_top_level_folders(self) -> set[str]:
        return _defn_to_set(self._defn, "mod_required_top_level_folders")

    @property
    def mod_auto_strip_until_required(self) -> bool:
        return bool(self._defn.get("mod_auto_strip_until_required", False))

    @property
    def heroic_app_names(self) -> list[str]:
        return _defn_to_list(self._defn, "heroic_app_names")

    @property
    def wine_dll_overrides(self) -> dict[str, str]:
        return _defn_to_dll_overrides(self._defn)

    @property
    def restore_before_deploy(self) -> bool:
        return bool(self._defn.get("restore_before_deploy", True))

    # ------------------------------------------------------------------
    # UE5 routing — Hogwarts Legacy rules (pak → Content/Paks/~mods,
    # ue4ss → Binaries/Win64/ue4ss, lua → Binaries/Win64/Mods, etc.)
    # ------------------------------------------------------------------

    @property
    def ue5_routing_rules(self) -> list[UE5Rule]:
        return [
            # Paths already starting with Binaries/ or Content/ → game root,
            # path preserved as-is.
            UE5Rule(dest="", folder="binaries"),
            UE5Rule(dest="", folder="content"),
            # ue4ss/ folder → Binaries/Win64/ue4ss/
            UE5Rule(
                dest="Binaries/Win64/ue4ss",
                folder="ue4ss",
                strip=["ue4ss"],
            ),
            # Pak / streaming files and companion txt → Content/Paks/~mods/
            UE5Rule(
                dest="Content/Paks/~mods",
                extensions=[".pak", ".utoc", ".ucas", ".txt"],
                strip=["Content/Paks/~mods", "Content/Paks", "Paks", "~mods"],
            ),
            # Lua UE4SS scripts → Binaries/Win64/Mods/
            UE5Rule(
                dest="Binaries/Win64/Mods",
                extensions=[".lua"],
                strip=[
                    "Binaries/Win64/Mods",
                    "Binaries/Win64/ue4ss/Mods",
                    "Binaries/Win64/ue4ss",
                    "ue4ss/Mods",
                    "UE4SS/Mods",
                    "UE4SS",
                    "ue4ss",
                ],
            ),
            # Loose UE4SS proxy/runtime files (dwmapi.dll, UE4SS.dll, etc.) → Binaries/Win64/
            UE5Rule(
                dest="Binaries/Win64",
                extensions=[".dll", ".pdb"],
            ),
            # Bink video replacers → Content/Movies/
            UE5Rule(
                dest="Content/Movies",
                extensions=[".bk2"],
                strip=["Content/Movies"],
            ),
        ]

    @property
    def ue5_default_dest(self) -> str:
        return ""


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

_DEPLOY_CLASSES = {
    "standard": StandardCustomGame,
    "root":     RootCustomGame,
    "ue5":      Ue5CustomGame,
}


def make_custom_game(defn: dict) -> BaseGame:
    """Instantiate the correct handler class for the given definition dict."""
    deploy_type = defn.get("deploy_type", "standard").lower()
    cls = _DEPLOY_CLASSES.get(deploy_type, StandardCustomGame)
    return cls(defn)


def load_all_custom_games() -> dict[str, BaseGame]:
    """Load every custom game definition and return {game.name: instance}."""
    games: dict[str, BaseGame] = {}
    for defn in load_custom_game_definitions():
        try:
            instance = make_custom_game(defn)
            games[instance.name] = instance
        except Exception:
            pass
    return games
