from __future__ import annotations

import filecmp
import json
import os
import shutil
import stat
import uuid
import zipfile
from pathlib import Path

APP_DIR = "NewCalifornia"
MOD_NAME = "New California"
EXE_NAME = "FalloutNewCaliforniaBETA231FullInstaller.exe"
PAYLOAD_NAME = "FalloutNewCaliforniaBETA231FullInstaller.nsisbin"
INSTALLER_FILES = (EXE_NAME, PAYLOAD_NAME, "vcredist_x86.exe", "VC_redist.x64.exe")
NEXUS_URL = "https://www.nexusmods.com/newvegas/mods/45138?tab=files"
FILE_ID = 1000060792
KEYWORDS = ["california", "installer"]
INI_NAMES = ("Fallout.ini", "FalloutPrefs.ini", "FalloutCustom.ini")
GAME_FILES = ("FalloutNV.exe", "Fallout_default.ini", "FalloutNV_backup.exe",
              "Data/Music/Special/MainTitle.mp3")


def applications_dir(game) -> Path:
    return game.get_profile_root() / "Applications" / APP_DIR


def find_installer(game) -> Path | None:
    root = applications_dir(game) / "installer"
    exe = root / EXE_NAME
    if (exe.is_file() and (root / PAYLOAD_NAME).is_file()
            and (root / ".complete").is_file()):
        return exe
    return None


def installer_payload_present(game) -> bool:
    root = applications_dir(game) / "installer"
    return root.is_symlink() or any(
        (root / name).exists() or (root / name).is_symlink()
        for name in (*INSTALLER_FILES, ".complete", ".archive-name", ".payload-files.json"))


def extract_installer(game, archive: Path, log_fn) -> Path:
    from Utils.archives.paths import extraction_path

    root = applications_dir(game)
    root.mkdir(parents=True, exist_ok=True)
    dest = root / "installer"
    if (dest.is_symlink() or (dest.exists() and not dest.is_dir())
            or any((dest / name).exists() or (dest / name).is_symlink()
                   for name in (*INSTALLER_FILES, ".complete", ".payload-files.json"))):
        raise RuntimeError(f"Installer cache already exists: {dest}. Move it aside before extracting again.")
    stage = root / f".extract-{uuid.uuid4().hex}"
    stage.mkdir()
    try:
        with zipfile.ZipFile(archive) as package:
            files = [item for item in package.infolist() if not item.is_dir()]
            names = {Path(item.filename).name.casefold() for item in files}
            if not {EXE_NAME.casefold(), PAYLOAD_NAME.casefold()}.issubset(names):
                raise RuntimeError("Select the New California 2.3.1 full installer ZIP, including its .nsisbin payload.")
            required = sum(item.file_size for item in files)
            if shutil.disk_usage(root).free < required + 256 * 1024 * 1024:
                raise RuntimeError("Not enough space to extract the New California installer into Applications.")
            seen = set()
            for item in files:
                target = extraction_path(stage, item.filename)
                key = str(target.relative_to(stage)).casefold()
                if key in seen or stat.S_ISLNK(item.external_attr >> 16):
                    raise RuntimeError("The installer archive contains duplicate paths or symlinks.")
                seen.add(key)
                target.parent.mkdir(parents=True, exist_ok=True)
                log_fn(f"Extracting {target.name}")
                with package.open(item) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output, 1024 * 1024)
        matches = list(stage.rglob(EXE_NAME))
        if len(matches) != 1 or not (matches[0].parent / PAYLOAD_NAME).is_file():
            raise RuntimeError("The installer EXE and .nsisbin must be together in the archive.")
        payload = matches[0].parent
        paths = [str(path.relative_to(payload)) for path in payload.rglob("*") if path.is_file()]
        if any(path in {".archive-name", ".complete", ".payload-files.json"} for path in paths):
            raise RuntimeError("The installer archive contains reserved cache filenames.")
        if dest.exists():
            if any((dest / item.name).exists() or (dest / item.name).is_symlink() for item in payload.iterdir()):
                raise RuntimeError(f"Installer extraction would replace existing files in {dest}.")
            for item in list(payload.iterdir()):
                item.rename(dest / item.name)
        else:
            payload.rename(dest)
        (dest / ".payload-files.json").write_text(json.dumps(paths))
        (dest / ".archive-name").write_text(archive.name)
        (dest / ".complete").touch()
        return dest / EXE_NAME
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def cleanup_installer(game, log_fn) -> None:
    from Utils.archives.paths import extraction_path

    root = applications_dir(game) / "installer"
    if root.is_symlink():
        raise RuntimeError(f"Installer cache is a symlink; remove it manually: {root}")
    if not root.exists():
        return
    manifest = root / ".payload-files.json"
    names = json.loads(manifest.read_text()) if manifest.is_file() else list(INSTALLER_FILES)
    if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
        raise RuntimeError(f"Invalid installer cache manifest: {manifest}")
    paths = [extraction_path(root, name) for name in names]
    (root / ".complete").unlink(missing_ok=True)
    for path in paths:
        path.unlink(missing_ok=True)
    for name in (".archive-name", ".payload-files.json"):
        (root / name).unlink(missing_ok=True)
    directories = {parent for path in paths for parent in path.parents if parent.is_relative_to(root)}
    for directory in sorted(directories, key=lambda path: len(path.parts), reverse=True):
        try:
            directory.rmdir()
        except OSError:
            pass
    log_fn(f"Removed extracted New California installer files from {root}. Downloads, prefixes and recovery backups were kept.")


def validate_profile_name(game, name: str) -> None:
    from Utils.profiles.manage import _validate_name

    _validate_name(name)
    profiles = game.get_profile_root() / "profiles"
    if profiles.is_dir() and any(path.name.casefold() == name.casefold() for path in profiles.iterdir()):
        raise ValueError(f"Profile '{name}' already exists. Choose another name.")
    if name.casefold() == "default":
        raise ValueError("Choose a name other than Default.")


def _ini(directory: Path, name: str) -> Path:
    matches = [p for p in directory.iterdir() if p.name.casefold() == name.casefold()] if directory.is_dir() else []
    if len(matches) > 1:
        raise RuntimeError(f"Conflicting INI casing in {directory}: {name}")
    return matches[0] if matches else directory / name


def _restore_file(original: Path, target: Path, link=None):
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".amethyst-restore-{uuid.uuid4().hex}")
    try:
        if link is not None:
            temporary.symlink_to(link)
        else:
            shutil.copy2(original, temporary)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


class Installation:
    def __init__(self, game, source_profile: str, name: str):
        validate_profile_name(game, name)
        self.game = game
        self.name = name
        self.game_path = game.get_game_path()
        self.prefix_path = game.get_prefix_path()
        if self.game_path is None or self.prefix_path is None:
            raise RuntimeError("Configure the game folder and game prefix first.")
        if not (self.game_path / "FalloutNV.exe").is_file():
            raise RuntimeError("FalloutNV.exe is missing from the configured game folder.")
        self.profile = game.get_profile_root() / "profiles" / name
        self.recovery = applications_dir(game) / "runs" / uuid.uuid4().hex
        self.recovery.mkdir(parents=True)
        self.source_profile = source_profile
        self.vfs_enabled = game.vfs_enabled
        self.ini_target: Path | None = None
        self.ini_backup: Path | None = None
        self.workspace: IniWorkspace | None = None
        self.game_workspaces = []
        self.ini_sources = []
        self.original_game_files = []
        self.game_files_restored = False
        self.phase = "created"
        self._record()

    def _record(self):
        from Utils.atomic_write import write_atomic_text

        write_atomic_text(self.recovery / "recovery.json", json.dumps({
            "profile": str(self.profile), "game_path": str(self.game_path),
            "source_profile": self.source_profile, "phase": self.phase,
            "ini_target": str(self.ini_target) if self.ini_target else None,
            "ini_backup": str(self.ini_backup) if self.ini_backup else None,
            "original_game_files": self.original_game_files,
            "ini_sources": self.ini_sources,
        }, indent=2))

    def snapshot_inis(self):
        source = self.game._mygames_path()
        for directory in self.game._mygames_paths():
            marker = directory / ".amethyst-new-california"
            if marker.is_file():
                raise RuntimeError(f"An earlier New California installation needs recovery first: {marker.read_text()}")
        profile_inis = self.game._profile_ini_dir(self.source_profile)
        dest = self.recovery / "original-inis"
        dest.mkdir()
        for name in INI_NAMES:
            path = _ini(profile_inis, name) if self.game.profile_ini_files else None
            if path is None or not path.is_file():
                path = _ini(source, name) if source else None
            if path is None or not path.is_file():
                if name != "FalloutCustom.ini":
                    raise RuntimeError(f"{name} is missing. Run the New Vegas launcher once to generate the game INIs.")
                continue
            shutil.copy2(path, dest / name)
            self.ini_sources.append({"name": name, "target": str(path.resolve())})
        self.phase = "inis-saved"
        self._record()

    def create_profile(self, log_fn):
        from Utils.profiles.manage import create_profile
        from Utils.profiles.state import merge_profile_settings

        if self.game.get_game_path() != self.game_path or self.game.get_prefix_path() != self.prefix_path:
            raise RuntimeError("The game paths changed. Return to the original profile and reopen the wizard.")
        create_profile(self.game, self.name, profile_specific_mods=True,
                       log_fn=log_fn, mutation_coordinated=True)
        merge_profile_settings(self.profile, {
            "game_path": str(self.game_path), "prefix_path": str(self.prefix_path),
            "vfs_enabled": False, "profile_ini_files": False, "profile_saves": False,
            "auto_deploy": False,
        })
        self.phase = "profile-created"
        self._record()

    def protect_game_inis(self):
        for number, target in enumerate(self.game._mygames_paths()):
            workspace = IniWorkspace(target, self.recovery / f"game-documents-{number}")
            self.game_workspaces.append(workspace)
            workspace.prepare(self.recovery / "original-inis")

    def backup_game_files(self):
        dest = self.recovery / "original-game-files"
        dest.mkdir()
        for name in GAME_FILES:
            target = self.game_path
            for part in Path(name).parts:
                target = _ini(target, part)
            backup = dest / name
            present = target.exists() or target.is_symlink()
            if present:
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, backup)
            self.original_game_files.append({
                "target": str(target), "relative": name, "present": present,
                "link": str(target.readlink()) if target.is_symlink() else None,
            })
        self._record()

    def detach_game_files(self):
        for record in self.original_game_files:
            target = Path(record["target"])
            if target.is_file():
                temporary = target.with_name(f".amethyst-copy-{uuid.uuid4().hex}")
                try:
                    shutil.copy2(target, temporary)
                    os.replace(temporary, target)
                finally:
                    temporary.unlink(missing_ok=True)

    def capture_game_changes(self):
        for record in self.original_game_files:
            if not record["present"] or record["relative"] == "FalloutNV_backup.exe":
                continue
            target = Path(record["target"])
            original = self.recovery / "original-game-files" / record["relative"]
            if target.is_file() and not filecmp.cmp(original, target, shallow=False):
                dest = self.recovery / "replaced-files" / record["relative"]
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, dest)

    def restore_game_files(self):
        if self.game_files_restored:
            return
        errors = []
        for record in self.original_game_files:
            target = Path(record["target"])
            try:
                if record["present"]:
                    original = self.recovery / "original-game-files" / record["relative"]
                    _restore_file(original, target, record["link"])
                elif target.exists() or target.is_symlink():
                    target.unlink()
            except Exception as exc:
                errors.append(str(exc))
        if errors:
            raise RuntimeError("; ".join(errors))
        self.game_files_restored = True

    def restore_game_inis(self):
        errors = []
        for workspace in reversed(self.game_workspaces):
            try:
                workspace.restore()
            except Exception as exc:
                errors.append(str(exc))
        if errors:
            raise RuntimeError("; ".join(errors))

    def restore_source_inis(self):
        if any(workspace.prepared for workspace in self.game_workspaces):
            raise RuntimeError(f"Restore the game documents workspace first. Recovery: {self.recovery}")
        errors = []
        for record in self.ini_sources:
            target = Path(record["target"])
            try:
                if target.is_symlink():
                    raise RuntimeError(f"Original INI destination changed: {target}. Recovery: {self.recovery}")
                original = self.recovery / "original-inis" / record["name"]
                if not target.is_file() or not filecmp.cmp(original, target, shallow=False):
                    _restore_file(original, target)
            except Exception as exc:
                errors.append(str(exc))
        if errors:
            raise RuntimeError("; ".join(errors))

    def cleanup_inis(self):
        errors = []
        for restore in (self.restore_inis, self.restore_game_inis,
                        self.restore_game_files, self.restore_source_inis):
            try:
                restore()
            except Exception as exc:
                errors.append(str(exc))
        if errors:
            raise RuntimeError("; ".join(errors))

    def check_profile(self):
        if (self.game.get_game_path() != self.game_path
                or self.game.get_prefix_path() != self.prefix_path
                or Path(self.game._active_profile_dir) != self.profile):
            raise RuntimeError("The active game or profile changed. Reopen the wizard.")

    def prepare_inis(self, compat_data: Path):
        from Utils.wine.registry import normalize_pfx

        target = normalize_pfx(compat_data) / self.game._MYGAMES_DOCS / self.game._MYGAMES_SUBPATH
        self.workspace = IniWorkspace(target, self.recovery / "tool-documents")
        self.ini_target = target
        self.ini_backup = self.workspace.backup
        self.phase = "preparing-inis"
        self._record()
        self.workspace.prepare(self.recovery / "original-inis")
        self.phase = "installer-running"
        self._record()

    def capture_inis(self):
        dest = self.recovery / "edited-inis"
        dest.mkdir(exist_ok=True)
        for name in INI_NAMES:
            path = _ini(self.ini_target, name)
            if path.is_file() and not path.is_symlink():
                shutil.copy2(path, dest / name)
            elif name != "FalloutCustom.ini":
                raise RuntimeError(f"Installer did not leave a regular {name} file.")

    def restore_inis(self):
        if self.workspace is None:
            return
        self.workspace.restore()
        self.phase = "inis-restored"
        self._record()

    def register_mod(self, archive: Path | None, log_fn):
        from Nexus.nexus_meta import NexusModMeta, write_meta
        from Utils.deployment.shared import OVERWRITE_LOG_NAME
        from Utils.mods.install_as_mod import index_installed_mod
        from Utils.mods.modlist import prepend_mod
        from Utils.plugins import PluginEntry, write_plugins, write_loadorder
        from Utils.plugins.parser import read_masters
        from Utils.profiles.state import merge_profile_settings

        self.check_profile()
        if self.game.get_deploy_active():
            raise RuntimeError("Restore must finish before registering New California.")
        dest = self.profile / "mods" / MOD_NAME
        if dest.exists() or dest.is_symlink():
            raise RuntimeError(f"Mod already exists: {dest}")
        stage = self.recovery / "mod"
        stage.mkdir()
        seen = set()
        for source, prefix in ((self.profile / "Root_Folder", Path()),
                               (self.profile / "overwrite", Path("Data")),
                               (self.recovery / "replaced-files", Path())):
            for path in sorted(source.rglob("*")):
                if path.is_symlink():
                    raise RuntimeError(f"Unexpected symlink in installer output: {path}")
                if not path.is_file() or path.name == OVERWRITE_LOG_NAME:
                    continue
                rel = prefix / path.relative_to(source)
                if rel.parts[0].casefold() == "data":
                    rel = Path("Data", *rel.parts[1:])
                key = str(rel).casefold()
                if key in seen:
                    raise RuntimeError(f"Conflicting installer output: {rel}. Recovery: {self.recovery}")
                seen.add(key)
                target = stage / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(path), str(target))
        data_dirs = [p for p in stage.iterdir() if p.name.casefold() == "data" and p.is_dir()]
        if len(data_dirs) != 1:
            raise RuntimeError(f"Installer Data files were not captured. Recovery: {self.recovery}")
        data = data_dirs[0]
        installed = {p.name.casefold(): p.name for p in data.iterdir() if p.is_file()}
        if "newcalifornia.esm" not in installed or not any(name.endswith(".bsa") for name in installed):
            raise RuntimeError(f"Installation is incomplete; NewCalifornia.esm and BSA files are required. Recovery: {self.recovery}")
        write_meta(stage / "meta.ini", NexusModMeta(
            mod_name=MOD_NAME, game_domain="newvegas", mod_id=45138,
            file_id=FILE_ID, version="2.3.1", installation_file=(archive.name if archive else
                (applications_dir(self.game) / "installer" / ".archive-name").read_text()),
            root_folder=True,
        ))
        ini_dir = self.game._profile_ini_dir(self.name)
        ini_dir.mkdir(exist_ok=True)
        for path in (self.recovery / "edited-inis").iterdir():
            shutil.copy2(path, ini_dir / path.name)
        base_data = self.game.get_vanilla_plugins_path()
        vanilla = {p.name.casefold(): p.name for p in base_data.iterdir() if p.is_file()}
        order = [vanilla[name.casefold()] for name in self.game.primary_plugin_order if name.casefold() in vanilla]
        plugins = [p.name for p in sorted(data.iterdir()) if p.is_file() and p.suffix.casefold() in {".esm", ".esp"}]
        preferred = ["newcalifornia.esm", "newcalifornia dlc control.esp",
                     "newcalifornia courier stash control.esp"]
        plugins.sort(key=lambda name: (
            Path(name).suffix.casefold() != ".esm",
            preferred.index(name.casefold()) if name.casefold() in preferred else len(preferred),
            name.casefold()))
        order.extend(name for name in plugins if name.casefold() not in {n.casefold() for n in order})
        stage.rename(dest)
        data = dest / data.relative_to(stage)
        try:
            index_installed_mod(self.game, MOD_NAME, log_fn=log_fn, strict=True)
            enabled = {name.casefold() for name in order if name not in plugins}
            entries = []
            for name in order:
                missing = [master for master in read_masters(data / name)
                           if master.casefold() not in enabled] if name in plugins else []
                if not missing:
                    enabled.add(name.casefold())
                else:
                    log_fn(f"Left {name} disabled: missing masters {', '.join(missing)}")
                entries.append(PluginEntry(name, not missing))
            write_plugins(self.profile / "plugins.txt", entries, star_prefix=False)
            write_loadorder(self.profile / "loadorder.txt", entries)
            merge_profile_settings(self.profile, {"profile_ini_files": True, "vfs_enabled": self.vfs_enabled})
            prepend_mod(self.profile / "modlist.txt", MOD_NAME, enabled=True)
        except Exception:
            from Utils.mods.modlist import ensure_mod_preserving_position
            ensure_mod_preserving_position(self.profile / "modlist.txt", MOD_NAME, enabled=False)
            raise
        self.phase = "complete"
        self._record()
        log_fn(f"Installed {MOD_NAME}. Original INIs and recovery record: {self.recovery}")


class IniWorkspace:
    def __init__(self, target: Path, recovery: Path):
        self.target = target
        self.recovery = recovery
        self.backup = target.with_name(f".amethyst-new-california-{uuid.uuid4().hex}")
        self.prepared = False
        self.had_original = target.exists() or target.is_symlink()
        marker = target / ".amethyst-new-california"
        if marker.is_file() and Path(marker.read_text()).parent != recovery.parent:
            raise RuntimeError(f"An earlier New California installation needs recovery first: {marker.read_text()}")

    def prepare(self, source: Path):
        from Utils.atomic_write import write_atomic_text

        self.recovery.mkdir()
        write_atomic_text(self.recovery / "recovery.json", json.dumps({
            "target": str(self.target), "backup": str(self.backup),
            "had_original": self.had_original,
        }, indent=2))
        self.target.parent.mkdir(parents=True, exist_ok=True)
        if self.had_original:
            self.target.rename(self.backup)
        self.prepared = True
        self.target.mkdir()
        (self.target / ".amethyst-new-california").write_text(str(self.recovery))
        for path in source.iterdir():
            dest = self.target / path.name
            shutil.copy2(path, dest)
            dest.chmod(dest.stat().st_mode | stat.S_IWUSR)

    def restore(self):
        if not self.prepared:
            return
        if self.target.exists() or self.target.is_symlink():
            marker = self.target / ".amethyst-new-california"
            if self.target.is_symlink() or not marker.is_file() or marker.read_text() != str(self.recovery):
                raise RuntimeError(f"INI workspace changed; originals remain at {self.backup}. Recovery: {self.recovery}")
            shutil.move(str(self.target), str(self.recovery / "edited-documents"))
        if self.had_original:
            self.backup.rename(self.target)
        self.prepared = False
