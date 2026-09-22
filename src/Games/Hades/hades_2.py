from __future__ import annotations

import json
import shutil
from pathlib import Path

from Games.Custom.custom_game import RootCustomGame
from Games.Hades.hades import _importer_module
from Utils.deployment import LinkMode


_DEFINITION = {
    "name": "Hades II",
    "game_id": "Hades_II",
    "exe_name": "Ship/Hades2.exe",
    "exe_name_alts": ["Release/Hades2.exe"],
    "deploy_type": "root",
    "mod_data_path": "",
    "steam_id": "1145350",
    "nexus_game_domain": "hades2",
    "thunderstore_community": "hades-ii",
    "auto_install_deps": ["vcredist"],
    "conflict_ignore_filenames": [
        "license", "license.*", "*read*.txt", "*.md", "icon.png",
    ],
    "mod_folder_strip_prefixes_post": ["Content"],
    "mod_required_top_level_folders": ["Content", "Ship", "Release"],
    "mod_auto_strip_until_required": True,
    "mod_install_as_is_if_no_match": True,
    "restore_before_deploy": True,
    "normalize_folder_case": True,
    "filemap_casing": "upper",
    "wine_dll_overrides": {"d3d12": "native,builtin"},
    "custom_routing_rules": [
        {"dest": "Content", "folders": ["Mods"], "flatten": True},
        {
            "dest": "Content/Mods",
            "filenames": ["modfile.txt"],
            "include_siblings": True,
        },
        {
            "dest": "Content",
            "filenames": ["modimporter", "modimporter.py"],
            "flatten": True,
        },
        {"dest": "Content", "folders": ["sjson"], "flatten": True},
        {"dest": "", "folders": ["Content"], "flatten": True},
        {
            "dest": "Content",
            "folders": [
                "Shaders", "Scripts", "Packages", "Movies", "Maps", "GR2",
                "Game", "Fonts", "Audio",
            ],
            "flatten": True,
        },
        {
            "dest": "Content/Scripts",
            "extensions": [".lua"],
            "loose_only": True,
        },
        {
            "dest": "Content/Movies",
            "folders": ["720p", "1080p"],
            "flatten": True,
        },
        {
            "dest": "Content/Movies",
            "extensions": [".bik"],
            "flatten": True,
        },
        {"dest": "Ship", "filenames": ["d3d12.dll"], "flatten": True},
    ],
    "restore_whitelist": [],
    "custom_frameworks": {"Hell2Modding": "Ship/d3d12.dll"},
    "editable": False,
}


class HadesII(RootCustomGame):
    root_deploy_uses_filegraph_destinations = True

    def __init__(self) -> None:
        self._hell2_manifest_cache: dict[str, tuple[int, int, str, str]] = {}
        super().__init__(dict(_DEFINITION))

    @property
    def is_custom(self) -> bool:
        return False

    @property
    def mod_staging_requires_subdir(self) -> bool:
        return True

    @property
    def mod_staging_wrap_signals(self) -> tuple[set[str], set[str]]:
        return ({"modfile.txt"}, set())

    @property
    def mod_staging_already_structured_markers(self) -> set[str]:
        return {"modfile.txt"}

    def mod_staging_wrap_subdir_name(self, mod_dir: Path) -> str | None:
        kind, _guid = self._hell2_package(mod_dir.name, staging=mod_dir.parent)
        return None if kind else mod_dir.name

    def _hell2_package(
        self, mod_name: str, *, staging: Path | None = None,
    ) -> tuple[str, str]:
        root = (staging or self.get_effective_mod_staging_path()) / mod_name
        manifest = root / "manifest.json"
        try:
            stat = manifest.stat()
        except OSError:
            return "", ""
        cache_key = str(manifest)
        cached = self._hell2_manifest_cache.get(cache_key)
        if cached and cached[:2] == (stat.st_mtime_ns, stat.st_size):
            return cached[2], cached[3]
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return "", ""
        namespace = str(payload.get("namespace") or "").strip()
        name = str(payload.get("name") or "").strip()
        guid = f"{namespace}-{name}" if namespace and name else ""
        if (not guid or Path(guid).name != guid or guid in {".", ".."}
                or "/" in guid or "\\" in guid):
            return "", ""
        kind = (
            "loader"
            if guid.casefold() == "hell2modding-hell2modding"
            else "plugin"
        )
        self._hell2_manifest_cache[cache_key] = (
            stat.st_mtime_ns, stat.st_size, kind, guid)
        return kind, guid

    def filegraph_allow_default_path(self, mod_name: str, path: str) -> bool:
        kind, _guid = self._hell2_package(mod_name)
        if kind == "loader":
            filename = path.replace("\\", "/").lower().rsplit("/", 1)[-1]
            return filename == "d3d12.dll"
        return True

    def _resolve_filemap_entries(
        self, entries: list[tuple[str, str]],
    ) -> list[tuple[str, str, str, str]]:
        resolved = []
        for staged_rel, mod_name in entries:
            kind, guid = self._hell2_package(mod_name)
            if not kind:
                continue
            relative = staged_rel.replace("\\", "/").strip("/")
            if kind == "loader":
                if relative.lower().rsplit("/", 1)[-1] == "d3d12.dll":
                    resolved.append(
                        (staged_rel, mod_name, "Ship", "d3d12.dll"))
                continue
            head, separator, tail = relative.partition("/")
            section = head.casefold() if separator else ""
            if section == "plugins":
                destination = f"Ship/ReturnOfModding/plugins/{guid}"
                relative = tail
            elif section == "plugins_data":
                destination = f"Ship/ReturnOfModding/plugins_data/{guid}"
                relative = tail
            elif section == "config":
                destination = f"Ship/ReturnOfModding/config/{guid}"
                relative = tail
            else:
                destination = f"Ship/ReturnOfModding/plugins/{guid}"
            resolved.append((staged_rel, mod_name, destination, relative))
        return resolved

    def _vfs_prepare_filemap(
        self, _filemap: Path, _staging: Path, log_fn=None,
    ) -> set[str]:
        from Utils.filegraph.deploy import legacy_rows
        return {
            relative.replace("\\", "/").lower()
            for relative, owner in legacy_rows()
            if owner == "[Overwrite]"
        }

    def _vfs_populate_data_layer(
        self,
        *,
        destination: Path,
        game_root: Path,
        filemap: Path,
        staging: Path,
        log_fn=None,
        progress_fn=None,
        **_unused,
    ) -> int:
        from Utils.deployment.game_root import deploy_filemap_to_root

        metadata_dir = destination.parent / "hades2-private-metadata"
        metadata_dir.mkdir(parents=True, exist_ok=True)
        try:
            linked, _placed = deploy_filemap_to_root(
                filemap,
                destination,
                staging,
                mode=LinkMode.HARDLINK,
                strip_prefixes=self.mod_folder_strip_prefixes,
                log_fn=log_fn,
                progress_fn=progress_fn,
                state_dir=metadata_dir,
                write_snapshot=False,
                game=self,
                projected_destinations=True,
                projected_root=game_root,
            )
        finally:
            shutil.rmtree(metadata_dir, ignore_errors=True)
        return linked

    def deploy(self, log_fn=None, mode: LinkMode = LinkMode.HARDLINK,
               profile: str = "default", progress_fn=None) -> None:
        super().deploy(log_fn=log_fn, mode=mode, profile=profile,
                       progress_fn=progress_fn)
        if not self.vfs_launch_enabled:
            _importer_module().apply_mod_imports(
                self._game_path, log_fn=log_fn, game="Hades II")

    def _vfs_post_view_build(self, *, view_root: Path, **kwargs) -> None:
        _importer_module().apply_mod_imports(
            view_root, log_fn=kwargs.get("log_fn"), game="Hades II")

    def restore(self, log_fn=None, progress_fn=None) -> None:
        if self._game_path is not None:
            _importer_module().restore_mod_imports(
                self._game_path, log_fn=log_fn)
        super().restore(log_fn=log_fn, progress_fn=progress_fn)

    def post_clean_game_folder(self, log_fn=None) -> None:
        if self._game_path is not None:
            _importer_module().restore_mod_imports(
                self._game_path, log_fn=log_fn)
