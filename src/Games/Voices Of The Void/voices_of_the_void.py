import re
import shutil
from pathlib import Path

from Games.Custom.custom_game import Ue5CustomGame
from Games.base_game import WizardTool
from Thunderstore.thunderstore_meta import read_meta
from Utils.atomic_write import write_atomic_text
from Utils.deployment import LinkMode
from Utils.mods.modlist import read_modlist


_DEPLOYED_PROFILE = "Amethyst"
_SHIMLOADERS = {"Thunderstore-unreal_shimloader", "0xFFF7-votv_shimloader"}


_DEFINITION = {
    "name": "Voices Of The Void",
    "game_id": "Voices_Of_The_Void",
    "exe_name": "WindowsNoEditor/VotV/Binaries/Win64/VotV-Win64-Shipping.exe",
    "exe_name_alts": ["WindowsNoEditor/VotV.exe"],
    "deploy_type": "ue5",
    "mod_data_path": "WindowsNoEditor/VotV",
    "steam_id": "",
    "nexus_game_domain": "",
    "thunderstore_community": "voices-of-the-void",
    "auto_install_deps": ["vcredist"],
    "mod_folder_strip_prefixes": [],
    "conflict_ignore_filenames": ["LICENSE", "*.md", "icon.png", "manifest.json"],
    "restore_before_deploy": True,
    "normalize_folder_case": True,
    "filemap_casing": "upper",
    "wine_dll_overrides": {"dwmapi": "native,builtin"},
    "custom_routing_rules": [
        {"dest": "Content/Paks", "folders": ["LogicMods"], "flatten": True},
        {
            "dest": "Content/Paks/~mods",
            "extensions": [".pak"],
            "companion_extensions": [".ucas", ".utoc"],
            "include_siblings": True,
        },
        {
            "dest": "Binaries/Win64",
            "extensions": [".asi"],
            "companion_extensions": [".ini"],
        },
        {"dest": "Binaries/Win64", "filenames": ["ue4ss-settings.ini"], "flatten": True},
        {"dest": "Binaries/Win64", "folders": ["Mods"], "flatten": True},
        {"dest": "Binaries/Win64/Mods", "folders": ["scripts"], "include_siblings": True},
        {"dest": "Binaries/Win64/Mods", "filenames": ["enabled.txt"], "include_siblings": True},
        {"dest": "Binaries/Win64/Mods", "folders": ["dlls"], "include_siblings": True},
        {"dest": "Binaries/Win64/Mods", "filenames": ["mods.txt"], "flatten": True},
    ],
    "custom_frameworks": {"UE4SS": "Binaries/Win64/dwmapi.dll"},
    "editable": False,
}


class VoicesOfTheVoid(Ue5CustomGame):
    filegraph_routing_revision = 1

    def __init__(self) -> None:
        super().__init__(dict(_DEFINITION))

    @property
    def is_custom(self) -> bool:
        return False

    @property
    def wizard_tools(self) -> list[WizardTool]:
        return self._base_wizard_tools() + [WizardTool(
            id="update_yeetpatch",
            label="Update Game (YeetPatch)",
            description="Download YeetPatch and update Voices of the Void in a terminal.",
            dialog_class_path="wizards_qt.yeetpatch_view.YeetPatchView",
            category="Setup and Installers",
        )]

    def _resolve_ue4ss_mods_dest(self) -> None:
        return None

    def _package_id(self, mod_name: str) -> str:
        meta = read_meta(self.get_effective_mod_staging_path() / mod_name / "meta.ini")
        package_id = meta.package_id or mod_name
        if (package_id in {"", ".", ".."}
                or any(c in package_id for c in "/\\\x00\r\n")):
            raise ValueError(f"Invalid mod folder name: {package_id!r}")
        return package_id

    def mod_deploy_specs(self, profile_dir: Path, staging: Path,
                         mod_names: list[str]) -> dict[str, tuple]:
        root = self.get_game_path()
        if root is None:
            return {}
        # Include package identity in Filegraph's per-mod variant key.
        return {
            name: (root, (meta.package_id,), (), False)
            for name in mod_names
            if (meta := read_meta(staging / name / "meta.ini")).package_id
        }

    @property
    def default_launch_args(self) -> list[str]:
        root = self.get_game_path()
        if root is None:
            return []
        from Utils.wine.paths import to_wine_path
        return [
            value
            for kind in ("mod", "pak", "cfg", "overlay")
            for value in (
                f"--{kind}-dir",
                to_wine_path(root / _DEPLOYED_PROFILE / "shimloader" / kind,
                             self.get_prefix_path()),
            )
        ]

    def default_launch_args_for_exe(self, exe_name: str) -> list[str]:
        if Path(exe_name).name.casefold() not in {"votv.exe", "votv-win64-shipping.exe"}:
            return []
        return self.default_launch_args

    def filegraph_allow_default_path(self, mod_name: str, relative: str) -> bool:
        if mod_name != "[Overwrite]":
            return True
        relative = relative.replace("\\", "/").casefold()
        # Fusion must rebuild its patch and caches for the current selection.
        return not (relative == f"{_DEPLOYED_PROFILE}/mods.yml".casefold()
                    or relative.startswith(tuple(
                        f"{_DEPLOYED_PROFILE}/shimloader/{kind}/nynrahghost-fusion/".casefold()
                        for kind in ("mod", "pak"))))

    def _resolve_filemap_entries(
        self, entries: list[tuple[str, str]],
    ) -> list[tuple[str, str, str, str]]:
        resolved = super()._resolve_filemap_entries(entries)
        routed = []
        packages = {name: self._package_id(name) for name in {name for _, name in entries}
                    if name != "[Overwrite]"}
        repository_mods = {name for name in packages
                           if read_meta(self.get_effective_mod_staging_path()
                                        / name / "meta.ini").package_id}
        for staged_rel, mod_name, dest, final in resolved:
            if dest == self._PREFIX_SKIP_DEST:
                routed.append((staged_rel, mod_name, dest, final))
                continue
            norm = staged_rel.replace("\\", "/")
            if mod_name == "[Overwrite]":
                routed.append((staged_rel, mod_name, "", norm))
                continue
            package_id = packages[mod_name]
            head, sep, tail = norm.partition("/")
            if package_id in _SHIMLOADERS:
                loader_rel = tail if head.casefold() == "ue4ss" else norm
                loader_head, loader_sep, loader_tail = loader_rel.partition("/")
                if loader_sep and loader_head.casefold() == "mods":
                    dest, final = f"{_DEPLOYED_PROFILE}/shimloader/mod", loader_tail
                elif loader_rel.casefold() in {"dwmapi.dll", "ue4ss.dll", "ue4ss-settings.ini"}:
                    dest, final = "Binaries/Win64", Path(loader_rel).name
                else:
                    dest, final = f"{_DEPLOYED_PROFILE}/shimloader/mod/{package_id}", norm
            elif sep and head.casefold() in {"mod", "pak", "cfg", "overlay"}:
                kind = head.casefold()
                dest = f"{_DEPLOYED_PROFILE}/shimloader/{kind}"
                final = tail if kind == "cfg" else f"{package_id}/{tail}"
            elif (mod_name not in repository_mods
                  or norm.casefold().startswith(("binaries/", "content/", "config/"))):
                # Preserve explicit game-relative layouts from manual packages.
                pass
            else:
                dest, final = f"{_DEPLOYED_PROFILE}/shimloader/mod/{package_id}", norm
            routed.append((staged_rel, mod_name, dest, final))
        return self._canonicalize_routed_dir_casing(routed)

    def _write_shimloader_profile(self, root: Path, profile: str, log_fn) -> None:
        import yaml

        profile_dir = self.get_profile_root() / "profiles" / profile
        staging = self.get_effective_mod_staging_path()
        packages = []
        for entry in read_modlist(profile_dir / "modlist.txt"):
            if entry.is_separator or entry.is_group_header:
                continue
            meta = read_meta(staging / entry.name / "meta.ini")
            if not meta.package_id:
                continue
            package_id = self._package_id(entry.name)
            if re.fullmatch(r"[A-Za-z0-9_-]+", package_id) is None:
                raise ValueError(f"Invalid Thunderstore package ID: {package_id!r}")
            version = meta.version.split(".")
            packages.append({
                "manifestVersion": 1,
                "name": package_id,
                "authorName": meta.namespace,
                "displayName": meta.name,
                "versionNumber": dict(zip(("major", "minor", "patch"),
                                          (int(v) if v.isdigit() else 0
                                           for v in (version + ["0"] * 3)[:3]))),
                "enabled": entry.enabled,
            })
        target = root / _DEPLOYED_PROFILE / "mods.yml"
        manifest_path = self._ue5_deployed_manifest_path()
        manifest = manifest_path.read_text(encoding="utf-8").splitlines()
        relative = target.relative_to(root).as_posix()
        if relative not in manifest:
            if target.is_file() and not target.is_symlink():
                backup_root = root if getattr(self, "_vfs_ue5_populating", False) else self._game_path
                backup = backup_root / "Amethyst_vanilla_files" / relative
                if not backup.exists():
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(target, backup)
            manifest.append(relative)
            write_atomic_text(manifest_path, "\n".join(manifest))
        write_atomic_text(target, yaml.safe_dump(packages, sort_keys=False))
        for kind in ("mod", "pak", "cfg", "overlay"):
            (target.parent / "shimloader" / kind).mkdir(parents=True, exist_ok=True)
        log_fn(f"Wrote VotV shimloader profile ({len(packages)} packages).")

    def _vfs_populate_ue5_layer_files(self, destination: Path, profile: str, log_fn) -> None:
        self._write_shimloader_profile(destination, profile, log_fn)

    def deploy(self, log_fn=None, mode: LinkMode = LinkMode.HARDLINK,
               profile: str = "default", progress_fn=None) -> None:
        super().deploy(log_fn=log_fn, mode=mode, profile=profile, progress_fn=progress_fn)
        if self.vfs_launch_enabled:
            return
        self._write_shimloader_profile(self.get_game_path(), profile, log_fn or (lambda _: None))
        from Utils.deployment import _write_deploy_snapshot
        _write_deploy_snapshot(self.get_game_path(), self._ue5_runtime_snapshot_path(), log_fn=log_fn)

    def restore(self, log_fn=None, progress_fn=None) -> None:
        super().restore(log_fn=log_fn, progress_fn=progress_fn)
        root = self.get_game_path()
        if root is not None:
            profile = root / _DEPLOYED_PROFILE
            paths = [profile / "shimloader" / kind
                     for kind in ("mod", "pak", "cfg", "overlay")]
            for path in [*paths, profile / "shimloader", profile]:
                try:
                    path.rmdir()
                except OSError:
                    pass
