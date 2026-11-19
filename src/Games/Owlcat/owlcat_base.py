"""
owlcat_base.py
Shared support for Owlcat Games' Unity RPGs (Pathfinder: Kingmaker,
Pathfinder: Wrath of the Righteous, Warhammer 40,000: Rogue Trader).

Two mod ecosystems:
  - Unity Mod Manager (UMM) mods carry ``Info.json``.  Kingmaker/Wrath load
    them from ``<game>/Mods`` (UMM installed by the user); Rogue Trader ships
    UMM and loads them from ``<LocalLow>/UnityModManager``.
  - Owlcat Template mods carry ``OwlcatModificationManifest.json`` and load
    from ``<LocalLow>/Modifications`` once their manifest ``UniqueName`` is
    listed in ``<LocalLow>/OwlcatModificationManagerSettings.json``
    (``EnabledModifications``).  Not supported by Kingmaker.

A folder with ``Info.json`` is a UMM mod even if it also carries Owlcat
manifest files - Owlcat's own Workshop tool uses that rule.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal

UMM_MANIFEST = "Info.json"
TEMPLATE_MANIFEST = "OwlcatModificationManifest.json"
SETTINGS_FILE = "OwlcatModificationManagerSettings.json"


def locallow_rel(prefix: Path | None, dir_name: str) -> str | None:
    """Path of ``AppData/LocalLow/Owlcat Games/<dir_name>``, relative to *prefix*.

    Accepts a prefix given as its ``pfx/`` dir or its parent.  Prefers the
    ``steamuser`` account, then ``$USER``; falls back to ``steamuser`` when no
    user folder exists yet.  None when there is no prefix or no drive_c.
    """
    if prefix is None:
        return None
    for drive_rel in ("drive_c", "pfx/drive_c"):
        if (prefix / drive_rel).is_dir():
            break
    else:
        return None
    users = prefix / drive_rel / "users"
    user = "steamuser"
    for candidate in ("steamuser", os.environ.get("USER", "")):
        if candidate and (users / candidate).is_dir():
            user = candidate
            break
    return f"{drive_rel}/users/{user}/AppData/LocalLow/Owlcat Games/{dir_name}"


def find_child(parent: Path, name: str) -> Path | None:
    """*parent*'s entry called *name*, ignoring case (exact match preferred)."""
    exact = parent / name
    if exact.exists():
        return exact
    try:
        children = list(parent.iterdir())
    except OSError:
        return None
    folded = name.casefold()
    return next((c for c in sorted(children) if c.name.casefold() == folded), None)


def _manifest(path: Path, name: str) -> Path | None:
    found = find_child(path, name)
    return found if found is not None and found.is_file() else None


def classify_mod_dir(path: Path) -> Literal["template", "umm", None]:
    """Classify one mod folder by the manifest it holds directly."""
    if _manifest(path, UMM_MANIFEST) is not None:
        return "umm"
    if _manifest(path, TEMPLATE_MANIFEST) is not None:
        return "template"
    return None


def template_id(mod_unit: Path) -> str | None:
    """The ``UniqueName`` a Template mod is enabled by, or None."""
    manifest = _manifest(mod_unit, TEMPLATE_MANIFEST)
    if manifest is None:
        return None
    try:
        data = json.loads(manifest.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    name = data.get("UniqueName") if isinstance(data, dict) else None
    if isinstance(name, str) and name.strip():
        return name.strip()
    return None


def sync_enabled(existing: list[str], wanted: list[str], state: dict
                 ) -> tuple[list[str], dict]:
    """Merge Amethyst's enabled Template mods into an ``EnabledModifications`` list.

    *wanted* is the ids Amethyst wants enabled, in priority order.  *state*
    holds ``managed`` (ids Amethyst added) and ``user_owned`` (ids that were
    already listed when Amethyst first saw them - never removed).  Entries
    Amethyst doesn't manage keep their order; comparison ignores case.
    """
    managed = {str(v).casefold() for v in state.get("managed", ())}
    existing_keys = {e.casefold(): e for e in existing}
    # A user-owned id the user has since removed by hand is no longer theirs.
    user_owned = [str(v) for v in state.get("user_owned", ())
                  if str(v).casefold() in existing_keys]
    owned = {v.casefold() for v in user_owned}

    for wanted_id in wanted:
        key = wanted_id.casefold()
        if key in existing_keys and key not in managed and key not in owned:
            user_owned.append(existing_keys[key])
            owned.add(key)

    result = [e for e in existing
              if e.casefold() not in managed or e.casefold() in owned]
    present = {e.casefold() for e in result}
    new_managed: list[str] = []
    for wanted_id in wanted:
        key = wanted_id.casefold()
        if key not in owned and key not in {m.casefold() for m in new_managed}:
            new_managed.append(wanted_id)
        if key not in present:
            result.append(wanted_id)
            present.add(key)
    return result, {"managed": new_managed, "user_owned": user_owned}


def apply_settings_sync(settings_path: Path, wanted: list[str],
                        state_path: Path, log) -> bool:
    """Sync *wanted* into the game's settings file; False if it was left alone."""
    from Utils.atomic_write import write_atomic

    data: dict = {"EnabledModifications": []}
    if settings_path.is_file():
        try:
            data = json.loads(settings_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            log(f"Owlcat mods: {settings_path} is not valid JSON ({exc}); "
                "left it unchanged - fix or delete it, then deploy again.")
            return False
        if not isinstance(data, dict) or not isinstance(
                data.setdefault("EnabledModifications", []), list):
            log(f"Owlcat mods: {settings_path} has an unexpected layout; "
                "left it unchanged - fix or delete it, then deploy again.")
            return False

    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            state = {}
    except (OSError, ValueError):
        state = {}

    existing = [str(v) for v in data["EnabledModifications"]]
    result, new_state = sync_enabled(existing, wanted, state)
    # Don't create a settings file the game never wrote just to list nothing.
    if result != existing or (result and not settings_path.is_file()):
        data["EnabledModifications"] = result
        write_atomic(settings_path, json.dumps(
            data, indent=2, ensure_ascii=False).encode("utf-8"))
    write_atomic(state_path, json.dumps(new_state, indent=2).encode("utf-8"))
    log(f"Owlcat mods: {len(new_state['managed'])} Template mod(s) enabled in {settings_path}.")
    return True


# ---------------------------------------------------------------------------
# Handler base
# ---------------------------------------------------------------------------

from Games.Custom.custom_game import StandardCustomGame  # noqa: E402
from Utils.mods.modlist import read_modlist  # noqa: E402

_UMM_LABEL = "Unity Mod Manager"
_MODIFICATIONS = "Modifications"
_PORTRAITS = "Portraits"
# A custom portrait is a folder holding these images (any subset), deployed to
# LocalLow/.../Portraits/<folder>/ - the same in all three games.
_PORTRAIT_FILES = {"fulllength.png", "medium.png", "small.png"}
_MANIFEST_NAMES = {UMM_MANIFEST.lower(), TEMPLATE_MANIFEST.lower()}
_BUNDLED_UMM_FILES = {"winhttp.dll", "doorstop_config.ini"}
_SYNC_STATE = "owlcat_managed_modifications.json"
# Folder names the handler strips, routes or excludes - never wrap a mod into one.
_RESERVED_FOLDERS = {"mods", "unitymodmanager", _MODIFICATIONS.casefold(),
                     _PORTRAITS.casefold(), "fomod"}


def _has_manifest(path: Path) -> bool:
    return classify_mod_dir(path) is not None


def _is_portrait_dir(path: Path) -> bool:
    return any(child.name.casefold() in _PORTRAIT_FILES and child.is_file()
               for child in path.iterdir())


def _portrait_dirs(mod_dir: Path) -> list[Path] | None:
    """Folders holding portrait images, outermost only; None if not a portrait mod.

    A mod with a UMM or Template manifest anywhere is never a portrait mod:
    images inside it are that mod's own assets.
    """
    found: list[Path] = []
    for directory, dirs, files in os.walk(mod_dir, followlinks=False):
        if any(name.casefold() in _MANIFEST_NAMES for name in files):
            return None
        dirs[:] = sorted(name for name in dirs if not name.startswith(".")
                         and not Path(directory, name).is_symlink())
        here = Path(directory)
        if any(name.casefold() in _PORTRAIT_FILES for name in files) and not any(
                parent in found for parent in here.parents):
            found.append(here)
    return found


class OwlcatBase(StandardCustomGame):
    """Shared handler for Owlcat's Unity RPGs; subclasses set identity."""

    game_name: str = ""
    game_id_value: str = ""
    exe: str = ""
    steam_app_id: str = ""
    nexus_domain: str = ""
    locallow_dir_name: str = ""
    umm_builtin: bool = False
    supports_template_mods: bool = True

    post_deploy_failure_is_fatal = False

    def __init__(self) -> None:
        super().__init__({
            "name": self.game_name,
            "game_id": self.game_id_value,
            "exe_name": self.exe,
            "deploy_type": "standard",
            "mod_data_path": "UnityModManager" if self.umm_builtin else "Mods",
            "steam_id": self.steam_app_id,
            "nexus_game_domain": self.nexus_domain,
            "mod_folder_strip_prefixes": ["Mods", "UnityModManager"],
            "restore_before_deploy": True,
            "normalize_folder_case": True,
            "editable": False,
        })

    @property
    def is_custom(self) -> bool:
        return False

    @property
    def supports_profile_vfs(self) -> bool:
        # Mods land outside the game root (Proton prefix), which the profile
        # VFS doesn't virtualize.
        return False

    # -- paths --------------------------------------------------------------

    def _locallow_rel(self) -> str | None:
        return locallow_rel(self.get_prefix_path(), self.locallow_dir_name)

    def locallow_root(self) -> Path | None:
        rel = self._locallow_rel()
        prefix = self.get_prefix_path()
        return prefix / rel if rel and prefix is not None else None

    def get_mod_data_path(self) -> Path | None:
        if not self.umm_builtin:
            return super().get_mod_data_path()
        root = self.locallow_root()
        return root / "UnityModManager" if root is not None else None

    def runtime_snapshot_exclude_dirs(self) -> set[str] | None:
        return None if self.umm_builtin else super().runtime_snapshot_exclude_dirs()

    # -- routing ------------------------------------------------------------

    @property
    def custom_routing_rules(self) -> list:
        rel = self._locallow_rel()
        if rel is None:
            return []
        from Utils.deployment import CustomRule
        folders = [_MODIFICATIONS, _PORTRAITS] if self.supports_template_mods \
            else [_PORTRAITS]
        return [CustomRule(dest=rel, folders=folders, to_prefix=True,
                           rule_id="owlcat-locallow")]

    @property
    def filemap_exclude_dirs(self) -> frozenset[str]:
        dirs = super().filemap_exclude_dirs
        if self._locallow_rel() is None:
            # No prefix to route LocalLow content into: keep it out of Mods/.
            dirs = dirs | {_PORTRAITS.casefold()}
            if self.supports_template_mods:
                dirs = dirs | {_MODIFICATIONS.casefold()}
        return dirs

    @property
    def restore_whitelist(self) -> list:
        rules = list(super().restore_whitelist)
        if self.umm_builtin:
            return rules
        # UMM is installed by the user into the game; never adopt its files.
        from Utils.deployment import RestoreWhitelistRule
        return rules + [
            RestoreWhitelistRule(path="", filenames=sorted(_BUNDLED_UMM_FILES)),
            RestoreWhitelistRule(path=f"{Path(self.exe).stem}_Data/Managed",
                                 folders=["UnityModManager"]),
        ]

    @property
    def excluded_loose_filenames(self) -> set[str]:
        names = set(super().excluded_loose_filenames)
        return names | _BUNDLED_UMM_FILES if self.umm_builtin else names

    @property
    def wine_dll_overrides(self) -> dict[str, str]:
        return {} if self.umm_builtin else {"winhttp": "native,builtin"}

    @property
    def frameworks(self) -> dict[str, str]:
        return {} if self.umm_builtin else {_UMM_LABEL: "UnityModManager.dll"}

    def framework_installed(self, label: str) -> bool | None:
        if label != _UMM_LABEL or self.umm_builtin:
            return None
        game = self.get_game_path()
        if game is None:
            return None
        return (game / f"{Path(self.exe).stem}_Data" / "Managed"
                / "UnityModManager" / "UnityModManager.dll").is_file()

    # -- staging ------------------------------------------------------------

    @property
    def mod_staging_requires_subdir(self) -> bool:
        return True

    @property
    def mod_staging_wrap_signals(self) -> tuple[set[str], set[str]]:
        return (set(_MANIFEST_NAMES), set())

    @property
    def mod_staging_already_structured_markers(self) -> set[str]:
        return set(_MANIFEST_NAMES)

    @staticmethod
    def mod_staging_wrap_subdir_name(mod_dir: Path) -> str:
        """Folder name for wrapping a flat mod: the UMM ``Id`` when usable.

        UMM's own installer extracts a root-level ``Info.json`` archive to
        ``Mods/<Id>/``; matching it keeps the folder (and the settings UMM mods
        save inside it) stable across versions.  Anything else keeps the
        staging folder's name.
        """
        manifest = _manifest(mod_dir, UMM_MANIFEST)
        if manifest is None:
            return mod_dir.name
        try:
            info = json.loads(manifest.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return mod_dir.name
        mod_id = info.get("Id") if isinstance(info, dict) else None
        if not isinstance(mod_id, str):
            return mod_dir.name
        mod_id = mod_id.strip()
        if (not mod_id or mod_id in {".", ".."} or "\\" in mod_id
                or Path(mod_id).name != mod_id
                or mod_id.casefold() in _RESERVED_FOLDERS):
            return mod_dir.name
        return mod_id

    @property
    def additional_install_logic(self) -> list:
        return [self._flatten_bundle_wrapper]

    @staticmethod
    def _flatten_bundle_wrapper(mod_dir: Path, _name: str, log_fn) -> bool:
        """Lift ``Wrapper/<Mod>/<manifest>`` archives up one level."""
        children = list(mod_dir.iterdir())
        folders = [child for child in children if child.is_dir()]
        if len(folders) != 1 or _has_manifest(mod_dir):
            return False
        wrapper = folders[0]
        if _has_manifest(wrapper) or wrapper.name.casefold() in {
                "mods", "unitymodmanager", _MODIFICATIONS.casefold()}:
            return False
        if not any(_has_manifest(child) for child in wrapper.iterdir()
                   if child.is_dir()):
            return False
        contents = list(wrapper.iterdir())
        if any((mod_dir / child.name).exists() for child in contents):
            return False
        for child in contents:
            child.rename(mod_dir / child.name)
        wrapper.rmdir()
        log_fn(f"Owlcat: removed archive wrapper '{wrapper.name}/'.")
        return True

    def _move_templates(self, mod_dir: Path, log_fn) -> bool:
        """Move top-level Template mod folders under ``Modifications/``."""
        if not self.supports_template_mods:
            return False
        moved = False
        for unit in sorted(mod_dir.iterdir()):
            if (not unit.is_dir() or unit.is_symlink()
                    or unit.name.casefold() == _MODIFICATIONS.casefold()
                    or classify_mod_dir(unit) != "template"):
                continue
            target = mod_dir / _MODIFICATIONS / unit.name
            if target.exists():
                continue
            target.parent.mkdir(exist_ok=True)
            unit.rename(target)
            log_fn(f"Owlcat: filed Template mod '{unit.name}' under {_MODIFICATIONS}/.")
            moved = True
        return moved

    @staticmethod
    def _file_portraits(mod_dir: Path, log_fn) -> bool:
        """Move portrait folders under ``Portraits/``; loose images get a folder."""
        found = _portrait_dirs(mod_dir)
        if not found:
            return False
        portraits = find_child(mod_dir, _PORTRAITS) or mod_dir / _PORTRAITS
        moved = False
        for folder in found:
            if folder.parent == portraits:
                continue
            target = portraits / (mod_dir.name if folder == mod_dir else folder.name)
            if target.exists():
                log_fn(f"Owlcat: portrait '{target.name}' already exists in "
                       f"{mod_dir.name}; left '{folder.relative_to(mod_dir)}' in place.")
                continue
            if folder == mod_dir:
                target.mkdir(parents=True)
                for image in mod_dir.iterdir():
                    if image.is_file() and image.name.casefold() in _PORTRAIT_FILES:
                        image.rename(target / image.name)
            else:
                target.parent.mkdir(exist_ok=True)
                old_parent = folder.parent
                folder.rename(target)
                while old_parent != mod_dir and not any(old_parent.iterdir()):
                    old_parent.rmdir()
                    old_parent = old_parent.parent
            moved = True
        if moved:
            log_fn(f"Owlcat: filed portraits in '{mod_dir.name}' under {_PORTRAITS}/.")
        return moved

    def prepare_mod_staging(self, staging: Path, log_fn) -> bool:
        if not staging.is_dir():
            return False
        from Utils.mods.install import wrap_flat_mod_dir
        from Utils.deployment.vortex import preserve_sources

        changed = False
        for mod_dir in staging.iterdir():
            if not mod_dir.is_dir() or mod_dir.is_symlink():
                continue
            flattened = self._flatten_bundle_wrapper(mod_dir, mod_dir.name, log_fn)
            wrapped = wrap_flat_mod_dir(
                mod_dir, set(_MANIFEST_NAMES), set(), set(_MANIFEST_NAMES),
                self.mod_staging_wrap_subdir_name)
            with preserve_sources(mod_dir):
                filed = self._move_templates(mod_dir, log_fn)
                portraits = self._file_portraits(mod_dir, log_fn)
            changed = flattened or wrapped or filed or portraits or changed
        return changed

    # -- mod inspection -----------------------------------------------------

    def _profile_dir(self) -> Path:
        return self._active_profile_dir or (
            self.get_profile_root() / "profiles" / "default")

    def _enabled_mod_dirs(self) -> list[Path]:
        """Staged folders of enabled mods, lowest priority first."""
        modlist = self._profile_dir() / "modlist.txt"
        if not modlist.is_file():
            return []
        staging = self.get_effective_mod_staging_path()
        return [staging / entry.name for entry in reversed(read_modlist(modlist))
                if entry.enabled and not entry.is_separator
                and (staging / entry.name).is_dir()]

    @staticmethod
    def _units(mod_dir: Path) -> list[Path]:
        units = [mod_dir] if _has_manifest(mod_dir) else []
        parents = [mod_dir] + [find_child(mod_dir, name) for name in (
            _MODIFICATIONS, "Mods", "UnityModManager")]
        for parent in parents:
            if parent is not None and parent.is_dir():
                units += [child for child in sorted(parent.iterdir())
                          if child.is_dir() and _has_manifest(child)]
        return units

    def _wanted_template_ids(self) -> list[str]:
        wanted: list[str] = []
        for mod_dir in self._enabled_mod_dirs():
            for unit in self._units(mod_dir):
                if classify_mod_dir(unit) == "template":
                    unique = template_id(unit)
                    if unique and unique.casefold() not in {w.casefold() for w in wanted}:
                        wanted.append(unique)
        return wanted

    # -- deploy hooks -------------------------------------------------------

    def _sync(self, wanted: list[str], log) -> None:
        root = self.locallow_root()
        if root is None:
            message = ("Owlcat mods: game prefix not found; launch the game once, "
                       "then deploy again to enable Template mods.")
            log(message)
            if wanted:
                self.add_deploy_warning(message)
            return
        apply_settings_sync(root / SETTINGS_FILE, wanted,
                            self.get_profile_root() / _SYNC_STATE, log)

    def post_deploy(self, log_fn=None) -> None:
        log = log_fn or (lambda _message: None)
        mods = self._enabled_mod_dirs()
        kinds = {(mod_dir.name, classify_mod_dir(unit))
                 for mod_dir in mods for unit in self._units(mod_dir)}
        if not self.umm_builtin and any(kind == "umm" for _n, kind in kinds) \
                and self.framework_installed(_UMM_LABEL) is False:
            self.add_deploy_warning(
                "Unity Mod Manager isn't installed. Run UMM's installer in this "
                "game's Wine prefix (Doorstop Proxy method), then deploy again.")
        if not self.supports_template_mods:
            for name in sorted({n for n, kind in kinds if kind == "template"}):
                self.add_deploy_warning(
                    f"{self.name} doesn't load Owlcat Template mods; "
                    f"'{name}' won't work.")
        if self.umm_builtin:
            for mod_dir in mods:
                if any((mod_dir / f).is_file() or any(
                        (u / f).is_file() for u in mod_dir.iterdir() if u.is_dir())
                        for f in _BUNDLED_UMM_FILES):
                    self.add_deploy_warning(
                        f"{self.name} ships Unity Mod Manager; "
                        f"{mod_dir.name}'s bundled copy was not deployed.")
        if self.supports_template_mods:
            self._sync(self._wanted_template_ids(), log)

    def restore(self, log_fn=None, progress_fn=None) -> None:
        super().restore(log_fn=log_fn, progress_fn=progress_fn)
        if self.supports_template_mods and self.locallow_root() is not None:
            self._sync([], log_fn or (lambda _message: None))

    def post_clean_game_folder(self, log_fn=None) -> None:
        super().post_clean_game_folder(log_fn)
        if self.supports_template_mods and self.locallow_root() is not None:
            self._sync([], log_fn or (lambda _message: None))
