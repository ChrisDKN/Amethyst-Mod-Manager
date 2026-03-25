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
  "editable":          true          // false = skip definition editor on reconfigure (for repo handlers)
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

import io
import json
import shutil
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from Games.base_game import BaseGame
from Games.ue5_game import UE5Game, UE5Rule
from Utils.deploy import (
    CustomRule,
    LinkMode,
    deploy_core,
    deploy_custom_rules,
    deploy_filemap,
    deploy_filemap_to_root,
    load_per_mod_strip_prefixes,
    load_separator_deploy_paths,
    expand_separator_deploy_paths,
    cleanup_custom_deploy_dirs,
    move_to_core,
    restore_custom_rules,
    restore_data_core,
    restore_filemap_from_root,
)
from Utils.modlist import read_modlist
from Utils.config_paths import get_profiles_dir, get_custom_games_dir, get_custom_game_images_dir

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


def _defn_to_custom_rules(defn: dict) -> list[CustomRule]:
    """Return a list[CustomRule] from the JSON ``custom_routing_rules`` field.

    Each entry is a dict with keys ``dest``, and optionally ``extensions``
    and/or ``folders``.
    """
    raw = defn.get("custom_routing_rules", [])
    if not isinstance(raw, list):
        return []
    rules: list[CustomRule] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        dest = entry.get("dest", "")
        extensions = [s.strip().lower() for s in entry.get("extensions", []) if s.strip()]
        folders = [s.strip().lower() for s in entry.get("folders", []) if s.strip()]
        if dest or extensions or folders:
            rules.append(CustomRule(dest=dest, extensions=extensions, folders=folders))
    return rules


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


def download_missing_custom_game_images(
    on_done: "callable[[str], None] | None" = None,
) -> None:
    """
    For every custom game definition that has an ``image_url`` but no
    cached banner image yet, download and cache the image in a background
    thread.  Safe to call at any time; games that already have a cached
    image are skipped.

    Parameters
    ----------
    on_done:
        Optional callback invoked with the *game_id* after each image is
        successfully saved.  It is called from the worker thread — if you
        need to update the UI, use ``widget.after(0, ...)`` inside the
        callback.
    """
    images_dir = get_custom_game_images_dir()

    def _download_one(game_id: str, url: str) -> None:
        try:
            import requests
            from PIL import Image as _PilImage

            resp = requests.get(url, timeout=15)
            resp.raise_for_status()
            img = _PilImage.open(io.BytesIO(resp.content)).convert("RGBA")
            out = images_dir / f"{game_id}.png"
            img.save(out, "PNG")
            if on_done is not None:
                on_done(game_id)
        except Exception:
            pass  # silent – don't crash on missing images

    for defn in load_custom_game_definitions():
        game_id = defn.get("game_id") or _make_game_id(defn.get("name", ""))
        url = defn.get("image_url", "").strip()
        if not url:
            continue
        cached = images_dir / f"{game_id}.png"
        if cached.is_file():
            continue  # already cached
        threading.Thread(
            target=_download_one, args=(game_id, url), daemon=True
        ).start()


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
        # Normalise Windows-style separators so game_path / exe_name resolves
        # correctly on Linux (e.g. "Binaries\\NMS.exe" → "Binaries/NMS.exe").
        return self._defn.get("exe_name", "").replace("\\", "/")

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

    @property
    def editable(self) -> bool:
        return self._defn.get("editable", True)

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
    def normalize_folder_case(self) -> bool:
        return bool(self._defn.get("normalize_folder_case", True))

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
    def mod_required_file_types(self) -> set[str]:
        return _defn_to_set(self._defn, "mod_required_file_types")

    @property
    def mod_install_as_is_if_no_match(self) -> bool:
        return bool(self._defn.get("mod_install_as_is_if_no_match", False))

    @property
    def wine_dll_overrides(self) -> dict[str, str]:
        return _defn_to_dll_overrides(self._defn)

    @property
    def restore_before_deploy(self) -> bool:
        return bool(self._defn.get("restore_before_deploy", True))

    @property
    def custom_routing_rules(self) -> list[CustomRule]:
        return _defn_to_custom_rules(self._defn)

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
        filemap  = self.get_effective_filemap_path()
        staging  = self.get_effective_mod_staging_path()

        if data_dir is None:
            raise RuntimeError("Mod data path could not be resolved.")
        if not filemap.is_file():
            raise RuntimeError(f"filemap.txt not found: {filemap}\nRun 'Build Filemap' before deploying.")

        data_dir.mkdir(parents=True, exist_ok=True)

        profile_dir = self.get_profile_root() / "profiles" / profile
        per_mod_strip = load_per_mod_strip_prefixes(profile_dir)

        custom_rules = self.custom_routing_rules
        custom_exclude: set[str] = set()
        if custom_rules:
            _log("Step 1: Routing files via custom rules ...")
            custom_exclude = deploy_custom_rules(
                filemap, self._game_path, staging,
                rules=custom_rules,
                mode=mode,
                strip_prefixes=self.mod_folder_strip_prefixes,
                log_fn=_log,
            )
            _log(f"Step 2: Moving {data_dir.name}/ → {data_dir.name}_Core/ ...")
        else:
            _log(f"Step 1: Moving {data_dir.name}/ → {data_dir.name}_Core/ ...")
        moved = move_to_core(data_dir, log_fn=_log)
        _log(f"  Moved {moved} file(s) to {data_dir.name}_Core/.")

        _log(f"{'Step 3' if custom_rules else 'Step 2'}: Transferring mod files into {data_dir.name}/ ({mode.name}) ...")
        _sep_deploy = load_separator_deploy_paths(profile_dir)
        _sep_entries = read_modlist(profile_dir / "modlist.txt") if _sep_deploy else []
        per_mod_deploy = expand_separator_deploy_paths(_sep_deploy, _sep_entries) or None
        linked_mod, placed = deploy_filemap(
            filemap, data_dir, staging,
            mode=mode,
            strip_prefixes=self.mod_folder_strip_prefixes,
            per_mod_strip_prefixes=per_mod_strip,
            per_mod_deploy_dirs=per_mod_deploy,
            log_fn=_log,
            progress_fn=progress_fn,
            exclude=custom_exclude or None,
            core_dir=data_dir.parent / (data_dir.name + "_Core"),
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
        _profile_dir = self._active_profile_dir
        _entries = read_modlist(_profile_dir / "modlist.txt") if _profile_dir else []
        cleanup_custom_deploy_dirs(_profile_dir, _entries, log_fn=_log)

        custom_rules = self.custom_routing_rules
        if custom_rules:
            _log("Restore: removing custom-routed files ...")
            restore_custom_rules(
                self.get_effective_filemap_path(), self._game_path,
                rules=custom_rules, log_fn=_log,
            )

        _log(f"Restore: clearing {data_dir.name}/ and moving {data_dir.name}_Core/ back ...")
        restored = restore_data_core(
            data_dir,
            overwrite_dir=self.get_effective_overwrite_path(),
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
        filemap   = self.get_effective_filemap_path()
        staging   = self.get_effective_mod_staging_path()

        if not filemap.is_file():
            raise RuntimeError(f"filemap.txt not found: {filemap}\nRun 'Build Filemap' before deploying.")

        profile_dir = self.get_profile_root() / "profiles" / profile
        per_mod_strip = load_per_mod_strip_prefixes(profile_dir)

        custom_rules = self.custom_routing_rules
        custom_exclude: set[str] = set()
        if custom_rules:
            _log("Routing files via custom rules ...")
            custom_exclude = deploy_custom_rules(
                filemap, game_root, staging,
                rules=custom_rules,
                mode=mode,
                strip_prefixes=self.mod_folder_strip_prefixes,
                log_fn=_log,
            )

        _log(f"Transferring mod files into game root ({mode.name}) ...")
        linked_mod, _ = deploy_filemap_to_root(
            filemap, game_root, staging,
            mode=mode,
            strip_prefixes=self.mod_folder_strip_prefixes,
            per_mod_strip_prefixes=per_mod_strip,
            log_fn=_log,
            progress_fn=progress_fn,
            exclude=custom_exclude or None,
        )
        _log(f"Deploy complete. {linked_mod} mod file(s) placed in game root.")

    def restore(self, log_fn=None, progress_fn=None) -> None:
        _log = log_fn or (lambda _: None)
        if self._game_path is None:
            raise RuntimeError("Game path is not configured.")
        filemap   = self.get_effective_filemap_path()
        game_root = self._game_path

        _profile_dir = self._active_profile_dir
        _entries = read_modlist(_profile_dir / "modlist.txt") if _profile_dir else []
        cleanup_custom_deploy_dirs(_profile_dir, _entries, log_fn=_log)

        custom_rules = self.custom_routing_rules
        if custom_rules:
            _log("Restore: removing custom-routed files ...")
            restore_custom_rules(filemap, game_root, rules=custom_rules, log_fn=_log)

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
        # Normalise Windows-style separators so game_path / exe_name resolves
        # correctly on Linux (e.g. "Binaries\\NMS.exe" → "Binaries/NMS.exe").
        return self._defn.get("exe_name", "").replace("\\", "/")

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

    @property
    def editable(self) -> bool:
        return self._defn.get("editable", True)

    # ------------------------------------------------------------------
    # Paths
    # ------------------------------------------------------------------

    def get_game_path(self) -> Path | None:
        """If mod_data_path names a subfolder of the install dir, resolve it.

        Mirrors how OblivionRemastered / HogwartsLegacy work: the user sets
        their Steam install directory as the game path, and we automatically
        descend into the named subfolder (e.g. 'OblivionRemastered' or
        'Phoenix') to find the actual game root.  Case-insensitive so it
        works on both Proton/Windows and Linux.
        """
        if self._game_path is None:
            return None
        subdir = self._defn.get("mod_data_path", "").strip("/\\")
        if not subdir:
            return self._game_path
        # Exact match first
        sub = self._game_path / subdir
        if sub.is_dir():
            return sub
        # Case-insensitive scan
        needle = subdir.lower()
        try:
            for child in self._game_path.iterdir():
                if child.is_dir() and child.name.lower() == needle:
                    return child
        except OSError:
            pass
        # Fallback: user may have pointed directly at the subfolder already
        return self._game_path

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
    def normalize_folder_case(self) -> bool:
        return bool(self._defn.get("normalize_folder_case", True))

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
    def mod_required_file_types(self) -> set[str]:
        return _defn_to_set(self._defn, "mod_required_file_types")

    @property
    def mod_install_as_is_if_no_match(self) -> bool:
        return bool(self._defn.get("mod_install_as_is_if_no_match", False))

    @property
    def wine_dll_overrides(self) -> dict[str, str]:
        return _defn_to_dll_overrides(self._defn)

    @property
    def restore_before_deploy(self) -> bool:
        return bool(self._defn.get("restore_before_deploy", True))

    @property
    def custom_routing_rules(self) -> list[CustomRule]:
        return _defn_to_custom_rules(self._defn)

    # ------------------------------------------------------------------
    # UE5 routing — generic UE5 rules (pak → Content/Paks/~mods,
    # ue4ss → Binaries/Win64/ue4ss, lua → Binaries/Win64/Mods, etc.)
    # ------------------------------------------------------------------

    @property
    def ue5_routing_rules(self) -> list[UE5Rule]:
        rules = [
            # Pak / streaming files → Content/Paks/~mods/  (checked before the
            # generic folder="content" catch-all so mods shipped as
            # Content/Paks/… are routed here rather than to the game root as-is)
            UE5Rule(
                dest="Content/Paks/~mods",
                extensions=[".pak", ".utoc", ".ucas"],
                strip=["Content/Paks/~mods", "Content/Paks/~Mods", "Content/Paks", "Paks", "Content", "~mods", "~Mods"],
            ),
            # Files already inside Content/Paks/~Mods (any casing) → normalise
            # to lowercase ~mods dest so only one folder is created on disk.
            UE5Rule(
                dest="Content/Paks/~mods",
                prefix="Content/Paks/~Mods",
                strip=["Content/Paks/~Mods", "Content/Paks/~mods"],
            ),
            # Mods shipping Binaries/Win64/UE4SS/… → normalise to lowercase
            # ue4ss dest so only one folder is ever created on disk.
            UE5Rule(
                dest="Binaries/Win64/ue4ss",
                prefix="Binaries/Win64/UE4SS",
                strip=["Binaries/Win64/UE4SS", "Binaries/Win64/ue4ss"],
            ),
            # ue4ss/ or UE4SS/ top-level folder → Binaries/Win64/ue4ss/
            # (catches loose ue4ss files like UE4SS-settings.ini before the
            # extension rules can misroute them)
            UE5Rule(
                dest="Binaries/Win64/ue4ss",
                folder="ue4ss",
                strip=["ue4ss", "UE4SS"],
            ),
            # Paths already starting with Binaries/ or Content/ → game root,
            # path preserved as-is.
            UE5Rule(dest="", folder="binaries"),
            UE5Rule(dest="", folder="content"),
            # Lua UE4SS scripts and companion files (config.ini, data .json)
            # → Binaries/Win64/Mods/
            UE5Rule(
                dest="Binaries/Win64/Mods",
                extensions=[".lua", ".ini", ".json"],
                filenames=["enabled.txt"],
                strip=[
                    "Binaries/Win64/Mods",
                    "Binaries/Win64/ue4ss/Mods",
                    "Binaries/Win64/ue4ss",
                    "ue4ss/Mods",
                    "UE4SS/Mods",
                    "UE4SS",
                    "ue4ss",
                    "Mods",
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
        # Append user-defined custom routing rules (from the UI / JSON definition).
        # Each CustomRule may have multiple folders, so expand into one UE5Rule per
        # folder.  Extension-only rules produce a single UE5Rule with no folder.
        for cr in self.custom_routing_rules:
            if cr.folders:
                for folder in cr.folders:
                    rules.append(UE5Rule(
                        dest=cr.dest,
                        extensions=list(cr.extensions),
                        folder=folder,
                    ))
            else:
                rules.append(UE5Rule(
                    dest=cr.dest,
                    extensions=list(cr.extensions),
                ))
        return rules

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
