"""
baldurs_gate_3.py
Game handler for Baldur's Gate 3.

Mod structure:
  Mods install into the Larian AppData Mods folder.  Two layouts are
  supported:
    - Proton prefix:  <prefix>/drive_c/users/steamuser/AppData/Local/
                      Larian Studios/Baldur's Gate 3/Mods/
    - Native Linux:   ~/.local/share/Larian Studios/Baldur's Gate 3/Mods/
  The configured runtime explicitly selects the Proton or native Linux root.
  Staged mods live in Profiles/Baldur's Gate 3/mods/

  Custom routing rules send loose-mod folders (Generated/, Public/, Mods/,
  etc.) to the game's Data directory and bin/ files to the install root;
  .pak files are excluded from the Mods-folder routing rule and instead
  deploy flat at the top of the Larian AppData Mods folder, the only place
  the game loads them from. Unclaimed non-pak files remain in staging.

  After deploying .pak files, modsettings.lsx is generated automatically so
  BG3 recognises the installed mods.  Mod load order follows the modlist
  priority, with dependencies topologically sorted to appear before the
  mods that require them.  Adventure (custom campaign) mods replace the
  GustavX campaign entry; pure override paks stay out of the load order.
"""

import hashlib
import json
import os
import uuid
from pathlib import Path

from Games.base_game import BaseGame, WizardTool
from Utils.deployment import (
    CustomRule, LinkMode, deploy_filemap, deploy_core, move_to_core, restore_data_core,
    deploy_custom_rules, load_per_mod_strip_prefixes,
    load_separator_deploy_paths, expand_separator_deploy_paths,
    expand_separator_link_modes, expand_separator_raw_deploy,
    cleanup_custom_deploy_dirs, restore_custom_rules,
)
from Utils.mods.modlist import read_modlist
from Utils.config_paths import get_profiles_dir
from Utils.bg3.modsettings import write_modsettings
from Utils.atomic_write import write_atomic, write_atomic_text

_PROFILES_DIR = get_profiles_dir()

# Path inside the Proton prefix where the Larian data folder lives
_PREFIX_LARIAN_SUBPATH = Path(
    "drive_c/users/steamuser/AppData/Local/Larian Studios/Baldur's Gate 3"
)

# Native Linux (Steam Deck) build stores its data here
_NATIVE_LARIAN_ROOT = (
    Path.home() / ".local/share/Larian Studios/Baldur's Gate 3"
)

# Subpaths within the Larian root
_MODS_REL = Path("Mods")
_MODSETTINGS_REL = Path("PlayerProfiles/Public/modsettings.lsx")
_MODSETTINGS_BACKUP = "bg3_modsettings_original.lsx"
_MODSETTINGS_STATE = "bg3_modsettings_state.json"

_COLLECTION_DATA_TYPES = {"bg3-loose", "bg3-replacer"}
_COLLECTION_NO_LOAD_ORDER_TYPES = {
    "bg3-lslib-divine-tool", "bg3-bg3se", "bg3-replacer", "bg3-loose",
    "dinput",
}


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _replace_with_symlink(path: Path, link_target: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.amethyst-{uuid.uuid4().hex}")
    try:
        temporary.symlink_to(link_target)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_modsettings_state(profile_dir: Path) -> dict | None:
    try:
        data = json.loads(
            (profile_dir / _MODSETTINGS_STATE).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) and data.get("version") == 1 else None
    except (OSError, ValueError):
        return None


def _backup_modsettings(
    profile_dir: Path, modsettings: Path, log_fn,
) -> Path | None:
    state_path = profile_dir / _MODSETTINGS_STATE
    backup_path = profile_dir / _MODSETTINGS_BACKUP
    state = _read_modsettings_state(profile_dir)
    if state is not None:
        if state.get("had_original") and not backup_path.is_file():
            raise RuntimeError(
                "BG3 modsettings restore state exists but its original backup is missing.")
        if (state.get("had_original")
                and state.get("original_sha256")
                and _digest(backup_path.read_bytes())
                != state["original_sha256"]):
            raise RuntimeError(
                "BG3 modsettings original backup failed its integrity check.")
        return backup_path if state.get("had_original") else None
    if state_path.exists():
        raise RuntimeError(
            "BG3 modsettings restore state is unreadable; refusing to replace it.")

    if modsettings.is_symlink() and not modsettings.is_file():
        raise RuntimeError(
            "BG3 modsettings.lsx is a dangling symlink; refusing to replace it.")
    original_symlink = os.readlink(modsettings) if modsettings.is_symlink() else ""
    original = modsettings.read_bytes() if modsettings.is_file() else None
    if original is not None:
        write_atomic(backup_path, original)
    state = {
        "version": 1,
        "target": str(modsettings),
        "had_original": original is not None,
        "original_symlink": original_symlink,
        "original_sha256": _digest(original) if original is not None else "",
        "generated_sha256": "",
    }
    write_atomic_text(state_path, json.dumps(state, indent=2))
    log_fn("  Preserved the existing modsettings.lsx for exact restore.")
    return backup_path if original is not None else None


def _record_generated_modsettings(profile_dir: Path, modsettings: Path) -> None:
    state = _read_modsettings_state(profile_dir)
    if state is None or not modsettings.is_file():
        return
    state["generated_sha256"] = _digest(modsettings.read_bytes())
    write_atomic_text(
        profile_dir / _MODSETTINGS_STATE, json.dumps(state, indent=2))


def _restore_modsettings(profile_dir: Path, fallback: Path | None, log_fn) -> bool:
    state = _read_modsettings_state(profile_dir)
    if state is None:
        if (profile_dir / _MODSETTINGS_STATE).exists():
            log_fn("  WARN: BG3 modsettings restore state is unreadable; "
                   "managed files were retained.")
        return False
    target = Path(state.get("target") or fallback or "")
    suffix = _MODSETTINGS_REL.parts
    if not target.parts or tuple(target.parts[-len(suffix):]) != suffix:
        log_fn("  WARN: invalid BG3 modsettings restore target; backup retained.")
        return False
    if (fallback is None
            or os.path.abspath(os.fspath(target))
            != os.path.abspath(os.fspath(fallback))):
        log_fn("  WARN: BG3 modsettings restore target does not match the "
               "selected runtime; backup retained.")
        return False

    backup_path = profile_dir / _MODSETTINGS_BACKUP
    generated_hash = state.get("generated_sha256") or ""
    original_hash = state.get("original_sha256") or ""
    original: bytes | None = None
    if state.get("had_original"):
        try:
            original = backup_path.read_bytes()
        except OSError:
            log_fn("  WARN: BG3 modsettings backup is missing or unreadable; "
                   "restore remains retryable.")
            return False
        if original_hash and _digest(original) != original_hash:
            log_fn("  WARN: BG3 modsettings backup failed its integrity check; "
                   "restore remains retryable.")
            return False

    current = target.read_bytes() if target.is_file() else None
    if (current is not None
            and (not generated_hash or _digest(current) != generated_hash)
            and _digest(current) != original_hash):
        recovery = profile_dir / "bg3_modsettings_runtime.lsx"
        index = 1
        while recovery.exists():
            recovery = profile_dir / f"bg3_modsettings_runtime.{index}.lsx"
            index += 1
        write_atomic(recovery, current)
        log_fn(f"  Preserved runtime-modified modsettings at {recovery}.")

    try:
        if state.get("had_original"):
            original_symlink = state.get("original_symlink") or ""
            if original_symlink:
                _replace_with_symlink(target, original_symlink)
            else:
                write_atomic(target, original if original is not None else b"")
            log_fn("  Restored the original modsettings.lsx.")
        elif target.exists() or target.is_symlink():
            target.unlink()
            log_fn("  Removed the manager-generated modsettings.lsx.")
        state_path = profile_dir / _MODSETTINGS_STATE
        state_path.unlink(missing_ok=True)
        backup_path.unlink(missing_ok=True)
        return True
    except OSError as exc:
        log_fn(f"  WARN: could not restore BG3 modsettings: {exc}")
        return False


def _suppress_launcher_mod_warnings(larian_root: Path, log_fn=None) -> None:
    """Mark the Larian launcher's mod/data warnings as already shown.

    The launcher otherwise prompts about third-party mods on every start and
    can deactivate them.  Only the warning flags are touched (not telemetry),
    and only if the launcher has run before (preferences.json exists).
    """
    _log = log_fn or (lambda _: None)
    prefs = larian_root.parent / "Launcher" / "Settings" / "preferences.json"
    if not prefs.is_file():
        return
    desired = {
        "ModsWarningShown": True,
        "DataWarningShown": True,
        "DisplayFilesValidationMsg": False,
        "DisplayModsDetectedMsg": False,
    }
    try:
        data = json.loads(prefs.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            return
        if all(data.get(k) == v for k, v in desired.items()):
            return
        data.update(desired)
        write_atomic_text(prefs, json.dumps(data, indent=4))
        _log(f"  Suppressed Larian launcher mod warnings ({prefs}).")
    except (OSError, ValueError) as exc:
        _log(f"  Note: could not update launcher preferences: {exc}")


class BaldursGate3(BaseGame):

    # patch_version is a configured option, so make it per-profile (stored as a
    # paths.json extra via _load/_save_paths_extra).
    profile_overridable_paths_extras = ("patch_version", "runtime_mode")

    # Unlike an ordinary subfolder-deploy game, BG3 has two meaningful output
    # roots: loose/root-routed files go into the install while normal packages
    # go into Larian's per-user Mods folder. Show both in the Data tab.
    data_tab_include_game_root = True
    data_tab_game_root_label = "<root>"
    data_tab_data_root_label = "Larian Mods"

    def __init__(self):
        self._game_path: Path | None = None
        self._prefix_path: Path | None = None
        self._deploy_mode: LinkMode = LinkMode.HARDLINK
        self._staging_path: Path | None = None
        self._patch_version: int = 8
        self._runtime_mode: str = "auto"
        self.load_paths()

    # -----------------------------------------------------------------------
    # Identity
    # -----------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "Baldur's Gate 3"

    @property
    def game_id(self) -> str:
        return "baldurs_gate_3"

    @property
    def data_tab_title(self) -> str:
        return "Mod destinations"

    @property
    def exe_name(self) -> str:
        return "bin/bg3.exe"

    @property
    def exe_name_alts(self) -> list[str]:
        # Native Linux build ships a bare ELF binary at bin/bg3
        return ["bin/bg3_dx11.exe", "bin/bg3"]

    @property
    def configure_exe_names(self) -> list[str]:
        return ["bin/bg3", "bin/bg3.exe", "bin/bg3_dx11.exe"]

    @property
    def preferred_launch_exe(self) -> str:
        return "bin/bg3" if self._runtime_mode == "native" else ""

    @property
    def steam_id(self) -> str:
        return "1086940"

    @property
    def nexus_game_domain(self) -> str:
        return "baldursgate3"

    @property
    def wizard_tools(self) -> list[WizardTool]:
        return self._base_wizard_tools() + [
            WizardTool(
                id="modio_api_key",
                label="mod.io API Key",
                description="Enter a mod.io key to enable update checks for "
                            "manually-installed mod.io mods.",
                dialog_class_path="wizards.modio_settings.ModioSettingsWizard",
                category="Update tracking",
            ),
        ]

    @property
    def mod_required_top_level_folders(self) -> set[str]:
        return {"data", "bin", "generated", "public", "video", "mods",
                "nativemods"}
    
    @property
    def mod_required_file_types(self) -> set[str]:
        return {".pak"}
    
    @property
    def mod_auto_strip_until_required(self) -> bool:
        return True
    
    @property
    def mod_install_as_is_if_no_match(self) -> bool:
        return True

    @property
    def custom_routing_rules(self) -> list:
        return [
            CustomRule(dest="Data", folders=["Generated"], flatten=True),
            CustomRule(dest="Data", folders=["Public"], flatten=True),
            CustomRule(dest="Data", folders=["Video"], flatten=True),
            # Loose files under Mods/ (unpacked mods) go to game Data/Mods,
            # but .pak files under Mods/ must reach the Larian AppData Mods
            # folder via the normal deploy (a common Nexus packaging layout).
            CustomRule(dest="Data", folders=["Mods"], flatten=True,
                       exclude_extensions=[".pak"]),
            CustomRule(dest="Data", folders=["Cursors"], flatten=True),
            CustomRule(dest="bin", folders=["NativeMods"], flatten=True),
            CustomRule(dest="bin",
                       filenames=["DWrite.dll", "ScriptExtenderSettings.json"],
                       flatten=True),
            CustomRule(dest="", folders=["bin"], flatten=True),
            CustomRule(dest="", folders=["Data"], flatten=True),
        ]

    def _collection_install_types(
        self, profile_dir: Path, staging: Path, mod_names: list[str],
    ) -> dict[str, str]:
        from Nexus.nexus_meta import read_meta

        manifest_types: dict[int, str] = {}
        manifest_named_types: dict[str, str] = {}
        collection_json = profile_dir / "collection.json"
        if collection_json.is_file():
            try:
                payload = json.loads(collection_json.read_text(encoding="utf-8"))
                for item in payload.get("mods") or ():
                    if not isinstance(item, dict):
                        continue
                    source = item.get("source") or {}
                    if not isinstance(source, dict):
                        source = {}
                    details = item.get("details") or {}
                    if not isinstance(details, dict):
                        details = {}
                    file_id = source.get("fileId")
                    install_type = str(details.get("type") or "").strip().lower()
                    if file_id is not None and install_type:
                        try:
                            manifest_types[int(file_id)] = install_type
                        except (TypeError, ValueError):
                            pass
                    if install_type:
                        for value in (
                                item.get("name"), source.get("fileExpression"),
                                source.get("logicalFilename")):
                            if value:
                                manifest_named_types[
                                    str(value).strip().casefold()] = install_type
            except (AttributeError, OSError, TypeError, json.JSONDecodeError):
                pass

        result: dict[str, str] = {}
        for name in mod_names:
            try:
                meta = read_meta(staging / name / "meta.ini")
            except Exception:
                continue
            install_type = (meta.collection_install_type or "").strip().lower()
            if not install_type and meta.collection_source_file_id:
                install_type = manifest_types.get(meta.collection_source_file_id, "")
            if not install_type:
                for value in (meta.installation_file, meta.nexus_name, name):
                    install_type = manifest_named_types.get(
                        str(value or "").strip().casefold(), "")
                    if install_type:
                        break
            if install_type:
                result[name] = install_type
        return result

    def mod_deploy_specs(
        self, profile_dir: Path, staging: Path, mod_names: list[str],
    ) -> dict[str, tuple[Path, tuple[str, ...], tuple[str, ...], bool]]:
        if self._game_path is None:
            return {}
        types = self._collection_install_types(profile_dir, staging, mod_names)
        specs: dict[str, tuple[Path, tuple[str, ...], tuple[str, ...], bool]] = {}
        for name, install_type in types.items():
            if install_type in _COLLECTION_DATA_TYPES:
                specs[name] = (self._game_path / "Data", ("Data",), (), False)
            elif install_type == "bg3-bg3se":
                specs[name] = (
                    self._game_path / "bin", ("bin",), (".dll",), True)
        return specs

    def filegraph_allow_default_path(self, _mod_name: str, path: str) -> bool:
        normalized = path.replace("\\", "/").lower()
        filename = normalized.rsplit("/", 1)[-1]
        if filename.endswith(".pak") or filename in {
                "dwrite.dll", "scriptextendersettings.json"}:
            return True
        routed_folders = {
            "generated", "public", "video", "mods", "cursors", "nativemods",
            "bin", "data",
        }
        return any(part in routed_folders for part in normalized.split("/")[:-1])

    def _deployed_larian_paks(
        self, mods_dir: Path, placed: set[str],
    ) -> tuple[set[tuple[str, str]], set[str]]:
        from Utils.filegraph.deploy import absolute_destination, entries

        deployed: set[tuple[str, str]] = set()
        names: set[str] = set()
        mods_key = str(mods_dir.resolve(strict=False)).casefold()
        for entry in entries():
            destination = absolute_destination(self, entry)
            if destination is None or destination.suffix.lower() != ".pak":
                continue
            if str(destination.parent.resolve(strict=False)).casefold() != mods_key:
                continue
            if destination.name.casefold() not in placed:
                continue
            source = (entry.source_display or os.fsdecode(entry.source_rel))
            deployed.add((entry.mod_name,
                          source.replace("\\", "/").casefold()))
            names.add(destination.name.casefold())
        return deployed, names
    
    @property
    def plugin_extensions(self) -> list[str]:
        return []

    @property
    def has_override_pak_tab(self) -> bool:
        return True

    @property
    def conflict_ignore_filenames(self) -> set[str]:
        return {"info.json","*.txt"}
    
    @property
    def frameworks(self) -> dict[str, str]:
        if self._runtime_mode == "native":
            return {}
        return {
                "Script Extender": "bin/DWrite.dll",
                "Native Mod Loader":"bin/bink2w64_original.dll"
            }

    @property
    def wine_dll_overrides(self) -> dict[str, str]:
        return ({"DWrite": "native,builtin"}
                if self._runtime_mode != "native" else {})

    @property
    def pak_uuid_conflicts(self) -> bool:
        return True

    def runtime_snapshot_exclude_dirs(self) -> set[str] | None:
        # Custom rules route loose mods into Data/ (undone via restore_custom_rules)
        # and the .pak Mods folder lives outside the game root, so only capture
        # runtime-generated files sitting outside Data/.
        return {"Data"}

    # -----------------------------------------------------------------------
    # Paths
    # -----------------------------------------------------------------------

    def get_game_path(self) -> Path | None:
        return self._game_path

    def _larian_root(self) -> Path | None:
        """Return the Larian data root for the selected runtime."""
        if self._runtime_mode == "proton":
            return (self._prefix_path / _PREFIX_LARIAN_SUBPATH
                    if self._prefix_path is not None else None)
        if self._runtime_mode == "native":
            return _NATIVE_LARIAN_ROOT if _NATIVE_LARIAN_ROOT.is_dir() else None
        if self._prefix_path is not None:
            return self._prefix_path / _PREFIX_LARIAN_SUBPATH
        if _NATIVE_LARIAN_ROOT.is_dir():
            return _NATIVE_LARIAN_ROOT
        return None

    def get_mod_data_path(self) -> Path | None:
        """Mods deploy into the Larian AppData Mods folder (prefix or native)."""
        root = self._larian_root()
        return root / _MODS_REL if root is not None else None

    def get_mod_staging_path(self) -> Path:
        if self._staging_path is not None:
            return self._staging_path / "mods"
        return _PROFILES_DIR / self.name / "mods"

    def get_hardlink_deploy_targets(self) -> list[tuple[str, "Path | None"]]:
        if self._runtime_mode == "native":
            data_target: Path | None = _NATIVE_LARIAN_ROOT
            label = "Larian data (native Linux)"
        else:
            data_target = self._prefix_path
            label = "Proton prefix"
        return [
            ("Game directory", self._game_path),
            (label, data_target),
        ]

    # -----------------------------------------------------------------------
    # Configuration persistence
    # -----------------------------------------------------------------------

    def load_paths(self) -> bool:
        self._runtime_mode = "auto"
        loaded = super().load_paths()
        if self._runtime_mode == "auto":
            self._runtime_mode = self._infer_runtime_mode()
        if self._runtime_mode == "native":
            self._prefix_path = None
            self._prefix_path_cleared = True
        return loaded

    def _load_paths_extra(self, data: dict) -> None:
        try:
            pv = int(data.get("patch_version", 8))
        except (TypeError, ValueError):
            pv = 8
        self._patch_version = pv if pv in (6, 7, 8) else 8
        runtime = str(data.get("runtime_mode") or "").strip().lower()
        self._runtime_mode = runtime if runtime in ("native", "proton") else "auto"

    def _save_paths_extra(self) -> dict:
        return {
            "patch_version": self._patch_version,
            "runtime_mode": self._runtime_mode,
        }

    def _infer_runtime_mode(self, game_path: Path | None = None) -> str:
        path = Path(game_path) if game_path is not None else self._game_path
        native = bool(path and (path / "bin/bg3").is_file())
        windows = bool(path and (
            (path / "bin/bg3.exe").is_file()
            or (path / "bin/bg3_dx11.exe").is_file()))
        if native and not windows:
            return "native"
        if windows and not native:
            return "proton"
        if native and windows and self._runtime_mode in ("native", "proton"):
            return self._runtime_mode
        if self._prefix_path is not None:
            return "proton"
        return "native" if native and _NATIVE_LARIAN_ROOT.is_dir() else "proton"

    def get_runtime_mode(self) -> str:
        return self._runtime_mode

    def set_runtime_mode(self, mode: str) -> None:
        normalized = str(mode).strip().lower()
        if normalized not in ("native", "proton"):
            raise ValueError(f"Unsupported BG3 runtime: {mode}")
        self._runtime_mode = normalized
        if normalized == "native":
            self._prefix_path = None
            self._prefix_path_cleared = True
        elif self._prefix_path is None:
            self._prefix_path_cleared = False
        self.save_paths()

    def _find_prefix_for_load(self) -> "Path | None":
        if self._runtime_mode == "native":
            return None
        return super()._find_prefix_for_load()

    def set_game_path(self, path: "Path | str | None") -> None:
        self._game_path = Path(path) if path else None
        if self._game_path is not None:
            self._runtime_mode = self._infer_runtime_mode(self._game_path)
            if self._runtime_mode == "native":
                self._prefix_path = None
                self._prefix_path_cleared = True
            elif self._prefix_path is None:
                self._prefix_path_cleared = False
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
        if path:
            self._runtime_mode = "proton"
        super().set_prefix_path(path)

    def clear_prefix_path(self) -> None:
        self._runtime_mode = (
            "native" if self._game_path is not None
            and (self._game_path / "bin/bg3").is_file()
            else "proton"
        )
        self._prefix_path = None
        self._prefix_path_cleared = True
        self.save_paths()

    def deployment_preflight_error(self) -> str | None:
        if self._runtime_mode == "native":
            if self._game_path is None or not (self._game_path / "bin/bg3").is_file():
                return "Native Linux runtime selected, but bin/bg3 was not found."
            if not _NATIVE_LARIAN_ROOT.is_dir():
                return ("Run the native Linux build once so its Larian data folder "
                        f"exists at {_NATIVE_LARIAN_ROOT}.")
            return None
        if self._prefix_path is None:
            return "Windows/Proton runtime selected, but no Proton prefix is configured."
        if self._game_path is None or not any(
                (self._game_path / rel).is_file()
                for rel in ("bin/bg3.exe", "bin/bg3_dx11.exe")):
            return "Windows/Proton runtime selected, but no BG3 Windows executable was found."
        return None

    def get_patch_version(self) -> int:
        return self._patch_version

    def set_patch_version(self, version: int) -> None:
        if version not in (6, 7, 8):
            version = 8
        self._patch_version = version
        self.save_paths()

    # -----------------------------------------------------------------------
    # Deployment
    # -----------------------------------------------------------------------

    def deploy(self, log_fn=None, mode: LinkMode = LinkMode.HARDLINK,
               profile: str = "default", progress_fn=None) -> None:
        """Deploy staged BG3 mods into the selected runtime's destinations.

        Workflow:
          1. Move everything currently in the Mods folder → Mods_Core/
          2. Hard-link every .pak listed in filemap.txt into the Mods folder
          3. Hard-link vanilla .pak files from Mods_Core/ for anything not
             provided by a mod
          4. Generate modsettings.lsx so BG3 recognises the installed mods
        (Root Folder deployment is handled by the GUI after this returns.)
        """
        _log = log_fn or (lambda _: None)

        larian_root = self._larian_root()
        if larian_root is None:
            raise RuntimeError(
                "No Larian data folder found. Configure the Proton prefix, "
                f"or install the native Linux build so {_NATIVE_LARIAN_ROOT} exists."
            )

        mods_dir = larian_root / _MODS_REL
        filemap  = self.get_effective_filemap_path()
        staging  = self.get_effective_mod_staging_path()
        modlist  = self.get_profile_root() / "profiles" / profile / "modlist.txt"

        # Tell the user exactly where mods are being deployed. The Larian data
        # folder lives either inside the Proton prefix or in the native Linux
        # build's ~/.local/share - diagnosing "mods not loading" almost always
        # starts with confirming which one we targeted.
        if self._prefix_path is not None:
            _log(f"Deploy target: Proton prefix ({self._prefix_path})")
        else:
            _log("Deploy target: native Linux build")
        _log(f"  Larian data root: {larian_root}")
        _log(f"  Mods folder:      {mods_dir}")
        _log(f"  Game path:        {self._game_path or '(not set)'}")
        _log(f"  Staging:          {staging}")
        _log(f"  Deploy mode:      {mode.name}")

        mods_dir.mkdir(parents=True, exist_ok=True)

        from Utils.filegraph.deploy import input_ready
        if not input_ready():
            raise RuntimeError(
                f"filemap.txt not found: {filemap}\n"
                "Run 'Build Filemap' before deploying."
            )

        profile_dir = self.get_profile_root() / "profiles" / profile
        modsettings = larian_root / _MODSETTINGS_REL
        preserved_modsettings = _backup_modsettings(
            profile_dir, modsettings, _log)
        per_mod_strip = load_per_mod_strip_prefixes(profile_dir)

        # Separator overrides - loaded from the real profile_dir and passed
        # explicitly so shared-staging layouts get the right link modes.
        _sep_deploy = load_separator_deploy_paths(profile_dir)
        _entries = read_modlist(profile_dir / "modlist.txt")
        _install_types = self._collection_install_types(
            profile_dir, staging,
            [entry.name for entry in _entries if not entry.is_separator])
        _handler_deploy = self.mod_deploy_specs(
            profile_dir, staging,
            [entry.name for entry in _entries if not entry.is_separator])
        _separator_deploy = expand_separator_deploy_paths(
            _sep_deploy, _entries) if _sep_deploy else {}
        for name in _separator_deploy:
            _handler_deploy.pop(name, None)
        per_mod_deploy = {
            name: spec[0] for name, spec in _handler_deploy.items()
        }
        per_mod_deploy.update(_separator_deploy)
        per_mod_deploy = per_mod_deploy or None
        per_mod_modes = expand_separator_link_modes(_sep_deploy, _entries) or None
        per_mod_raw = expand_separator_raw_deploy(_sep_deploy, _entries)
        per_mod_raw = per_mod_raw or None

        custom_rules = self.custom_routing_rules
        custom_exclude: set[str] = set()
        if custom_rules and self._game_path:
            _log("Step 1a: Routing bin/ and generated/ files via custom rules ...")
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
            _log(f"  Routed {len(custom_exclude)} file(s) to Data/.")

        _log("Step 1: Moving Mods/ → Mods_Core/ ...")
        move_to_core(mods_dir, log_fn=_log)
        _log("  Backed up existing files → Mods_Core/.")

        _log(f"Step 2: Transferring mod .pak files into Mods/ ({mode.name}) ...")
        linked_mod, placed = deploy_filemap(filemap, mods_dir, staging,
                                            mode=mode,
                                            strip_prefixes=self.mod_folder_strip_prefixes,
                                            per_mod_strip_prefixes=per_mod_strip,
                                            per_mod_deploy_dirs=per_mod_deploy,
                                            per_mod_link_modes=per_mod_modes,
                                            log_fn=_log,
                                            progress_fn=progress_fn,
                                            exclude=custom_exclude or None,
                                            core_dir=mods_dir.parent / (mods_dir.name + "_Core"),
                                            # The game only loads .pak files at
                                            # the top level of the Mods folder.
                                            flatten_extensions={".pak"})
        _log(f"  Transferred {linked_mod} mod file(s).")

        _log("Step 3: Filling gaps with vanilla files from Mods_Core/ ...")
        linked_core = deploy_core(mods_dir, placed, mode=mode, log_fn=_log)
        _log(f"  Transferred {linked_core} vanilla file(s).")

        _log(f"Step 4: Generating modsettings.lsx → {modsettings}")
        if not modsettings.parent.is_dir():
            _log(f"  Note: profile folder {modsettings.parent} does not exist "
                 "yet - creating it (game may not have generated a profile).")
        game_data = self._game_path / "Data" if self._game_path else None
        # If this profile was created from a collection, the manifest's
        # loadOrder array drives the pak ordering. Curators interleave paks
        # from different mods (e.g. load-order divider packs whose 30+ entries
        # span the full LO), which the default folder-walk order destroys.
        manifest_lo = None
        collection_json = profile_dir / "collection.json"
        if collection_json.is_file():
            try:
                cj = json.loads(collection_json.read_text(encoding="utf-8"))
                lo = cj.get("loadOrder")
                if isinstance(lo, list) and lo:
                    manifest_lo = lo
                    _log(f"  Using collection manifest load order ({len(lo)} entries).")
            except (OSError, json.JSONDecodeError) as exc:
                _log(f"  Warning: could not read collection.json: {exc}")
        se_dll = (self._game_path / "bin" / "DWrite.dll"
                  if self._game_path else None)
        if se_dll is not None and not se_dll.is_file():
            alt = se_dll.with_name("dwrite.dll")
            if alt.is_file():
                se_dll = alt
        deployed_paks, managed_pak_names = self._deployed_larian_paks(
            mods_dir, placed)
        excluded_mods = {
            name for name, install_type in _install_types.items()
            if install_type in _COLLECTION_NO_LOAD_ORDER_TYPES
        }
        mod_count = write_modsettings(modsettings, modlist, staging,
                                      log_fn=_log,
                                      game_data_path=game_data,
                                      patch_version=self._patch_version,
                                      manifest_load_order=manifest_lo,
                                      script_extender_dll=se_dll,
                                      script_extender_supported=(
                                          self._runtime_mode != "native"),
                                      overwrite_root=self.get_effective_overwrite_path(),
                                      excluded_mods=excluded_mods,
                                      deployed_paks=deployed_paks,
                                      preserved_modsettings=preserved_modsettings,
                                      preserved_pak_root=mods_dir.parent / "Mods_Core",
                                      managed_pak_names=managed_pak_names)
        _record_generated_modsettings(profile_dir, modsettings)

        _suppress_launcher_mod_warnings(larian_root, log_fn=_log)

        # Snapshot the game root so restore() can sweep any runtime-generated
        # files (outside Data/) into Root_Folder/ and preserve them. Deferred
        # by the pipeline so the snapshot lands after Root_Folder files deploy.
        self.snapshot_root_for_runtime_capture(log_fn=_log)

        _log(
            f"Deploy complete. "
            f"{linked_mod} mod + {linked_core} vanilla "
            f"= {linked_mod + linked_core} total file(s) in {mods_dir}. "
            f"modsettings.lsx ({mod_count} mod(s)) at {modsettings}."
        )

    def restore(self, log_fn=None, progress_fn=None) -> None:
        """Remove deployed mods and restore the vanilla Mods folder."""
        _log = log_fn or (lambda _: None)

        larian_root = self._larian_root()
        if larian_root is None:
            raise RuntimeError(
                "No Larian data folder found. Configure the Proton prefix, "
                f"or install the native Linux build so {_NATIVE_LARIAN_ROOT} exists."
            )

        mods_dir = larian_root / _MODS_REL

        # Undo custom-routed files (bin/ and generated/ → game root / Data/)
        if self._game_path:
            custom_rules = self.custom_routing_rules
            if custom_rules:
                _log("Restore: removing custom-routed files (bin/, generated/) ...")
                restore_custom_rules(
                    self.get_effective_filemap_path(),
                    self._game_path,
                    rules=custom_rules,
                    log_fn=_log,
                )

        _profile_dir = self._active_profile_dir
        _entries = read_modlist(_profile_dir / "modlist.txt") if _profile_dir else []
        cleanup_custom_deploy_dirs(_profile_dir, _entries, log_fn=_log, game=self)

        _log("Restore: clearing Mods/ and moving Mods_Core/ back ...")
        restored = restore_data_core(
            mods_dir, overwrite_dir=self.get_effective_overwrite_path(),
            log_fn=_log, game=self, profile_dir=self._active_profile_dir)
        _log(f"  Restored {restored} file(s). Mods_Core/ removed.")

        _log("Restore: restoring the original modsettings.lsx ...")
        modsettings = larian_root / _MODSETTINGS_REL
        profile_dir = self._active_profile_dir
        if profile_dir is not None:
            profile_dir = Path(profile_dir)
            restored_settings = _restore_modsettings(
                profile_dir, modsettings, _log)
            if (not restored_settings
                    and not (profile_dir / _MODSETTINGS_STATE).exists()):
                _log("  No manager-owned modsettings backup needed restoration.")

        # Sweep runtime-generated files (outside Data/) into Root_Folder/ so they
        # re-deploy next time instead of being clobbered by the vanilla restore.
        moved = self.capture_runtime_files_to_root_folder(log_fn=_log)
        if moved:
            _log(f"  Moved {moved} runtime file(s) to Root_Folder/.")

        _log("Restore complete.")

    def post_clean_game_folder(self, log_fn=None) -> None:
        """Restore manager-owned modsettings state after cleaning."""
        larian_root = self._larian_root()
        if larian_root is None:
            return
        profile_dir = self._active_profile_dir
        if profile_dir is not None:
            _restore_modsettings(
                Path(profile_dir), larian_root / _MODSETTINGS_REL,
                log_fn or (lambda _: None))
