from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


class PayloadInstallError(RuntimeError):
    def __init__(self, name, message, details=None):
        super().__init__(message)
        self.name = name
        self.details = details or {}


def _token(path):
    try:
        value = Path(path).lstat()
    except FileNotFoundError:
        return None
    return (value.st_dev, value.st_ino, value.st_mode, value.st_size,
            value.st_mtime_ns, value.st_ctime_ns)


def archive_token(path):
    value = Path(path).stat()
    if not stat.S_ISREG(value.st_mode):
        raise PayloadInstallError("unsafe_tool_payload", "The archive must be a regular file.")
    return _token(path), (value.st_dev, value.st_ino, value.st_size,
                          value.st_mtime_ns, value.st_ctime_ns)


@dataclass(frozen=True)
class PreparedPayload:
    archive: Path
    root: Path
    files: tuple[Path, ...]
    archive_identity: tuple
    replace_trees: tuple[Path, ...] = ()
    keep_existing: tuple[Path, ...] = ()
    remove_files: tuple[Path, ...] = ()

    def check_archive(self):
        try:
            current = archive_token(self.archive)
        except OSError as exc:
            raise PayloadInstallError("tool_archive_changed", "The source archive is no longer available.") from exc
        if current != self.archive_identity:
            raise PayloadInstallError("tool_archive_changed", "The archive changed during installation.")


def replacement_trees(payload):
    trees = tuple(Path(path) for path in payload.replace_trees)
    for tree in trees:
        if (not tree.parts or tree.is_absolute() or ".." in tree.parts
                or not any(path.is_relative_to(tree) and path != tree for path in payload.files)
                or (payload.root / tree).is_symlink() or not (payload.root / tree).is_dir()
                or any(path.is_relative_to(tree) for path in payload.keep_existing)):
            raise PayloadInstallError("unsafe_tool_payload", "A replacement tree is unsafe or has no payload files.")
    if any(left.is_relative_to(right) or right.is_relative_to(left)
           for index, left in enumerate(trees) for right in trees[index + 1:]):
        raise PayloadInstallError("unsafe_tool_payload", "Replacement trees must not overlap.")
    if any(path not in payload.files for path in payload.keep_existing):
        raise PayloadInstallError("unsafe_tool_payload", "A preserved file is absent from the payload.")
    if any(not path.parts or path.is_absolute() or ".." in path.parts
           or path in payload.files or any(path.is_relative_to(tree) for tree in trees)
           for path in payload.remove_files):
        raise PayloadInstallError("unsafe_tool_payload", "A removed file overlaps the payload or is unsafe.")
    return trees


def tree_identity(root, checkpoint=lambda: None):
    before = _token(root)
    if before is None:
        return None
    digest = hashlib.sha256()
    count = 0
    stack = [root] if stat.S_ISDIR(before[2]) else []
    while stack:
        checkpoint()
        parent = stack.pop()
        for path in sorted(parent.iterdir(), key=lambda item: item.name):
            checkpoint()
            value = _token(path)
            if value is None:
                raise PayloadInstallError("tool_destination_changed", "A directory entry changed during inspection.")
            digest.update(json.dumps([str(path.relative_to(root)), value], ensure_ascii=True).encode())
            count += 1
            if stat.S_ISDIR(value[2]):
                stack.append(path)
    if _token(root) != before:
        raise PayloadInstallError("tool_destination_changed", "A directory changed during inspection.")
    return before, digest.hexdigest(), count


@contextmanager
def prepare_payload(archive, *, log_fn=lambda _m: None, checkpoint=lambda: None,
                    strip_top_dir=True):
    from Utils.wizards.archives import _strip_single_top_dir, extract_to_dir

    archive = Path(archive)
    identity = archive_token(archive)
    checkpoint()
    with tempfile.TemporaryDirectory(prefix="amethyst-tool-payload-") as temporary:
        directory = Path(temporary)
        extract_to_dir(archive, directory, log_fn=log_fn)
        checkpoint()
        files = []
        for current, directories, names in os.walk(directory, followlinks=False):
            checkpoint()
            for name in directories + names:
                path = Path(current) / name
                mode = path.lstat().st_mode
                if stat.S_ISDIR(mode):
                    continue
                if not stat.S_ISREG(mode):
                    raise PayloadInstallError("unsafe_tool_payload", "Tool payloads cannot contain links or special files.",
                                              {"path": str(path.relative_to(directory))})
                files.append(path)
        if not files:
            raise PayloadInstallError("empty_tool_payload", "The archive contains no installable files.")
        source = _strip_single_top_dir(directory) if strip_top_dir else directory
        payload = PreparedPayload(archive, source, tuple(sorted(path.relative_to(source) for path in files)), identity)
        payload.check_archive()
        yield payload


def destination(game, archive, mode, *, mod_fallback_name, nexus_mod_id=0,
                mod_name_override=None):
    from Utils.mods.install_as_mod import derive_mod_name
    from Utils.mods.names import sanitize_mod_folder_name

    name = None
    if mode == "mod":
        staging = game.get_effective_mod_staging_path()
        if staging is None:
            raise PayloadInstallError("tool_destination_unavailable", "Mod staging is not configured.")
        name = mod_name_override or derive_mod_name(Path(archive), fallback=mod_fallback_name)
        if nexus_mod_id and mod_name_override is None:
            from Nexus.nexus_download import _clean_nexus_stem
            name = _clean_nexus_stem(name, str(nexus_mod_id))
        name = sanitize_mod_folder_name(name)
        if (name.casefold() in {"root_folder", "overwrite"}
                or name.casefold().endswith("_separator") or name.casefold().startswith("@group:")):
            raise PayloadInstallError("invalid_tool_mod_name", "The archive name is reserved for manager state.")
        target = Path(staging).resolve() / name
        from Utils.mods.links import require_local_destination
        require_local_destination(game, target)
        label = f"mod folder ({name})"
    elif mode == "root":
        target = Path(game.get_effective_root_folder_path()).resolve()
        label = "Root_Folder (staging)"
    elif mode == "game":
        path = game.get_game_path()
        if path is None:
            raise PayloadInstallError("tool_destination_unavailable", "The game path is not configured.")
        target, label = Path(path).resolve(), "game folder"
    else:
        raise PayloadInstallError("invalid_tool_destination", "Choose game, root, or mod installation.")
    return target, label, name


class _FileJournal:
    def __init__(self):
        self.files = []
        self.directories = []
        self.trees = []

    def mkdir(self, path):
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise PayloadInstallError("unsafe_tool_destination", "A destination directory is a link or file.",
                                      {"path": str(path)})
        if path.is_dir():
            if path != path.parent:
                self.mkdir(path.parent)
            return
        self.mkdir(path.parent)
        path.mkdir()
        self.directories.append((path, _token(path)[:2]))

    def replace(self, source, path):
        from Utils.atomic_write import _tmp_for

        self.mkdir(path.parent)
        before = _token(path)
        if before is not None and not (stat.S_ISREG(before[2]) or stat.S_ISLNK(before[2])):
            raise PayloadInstallError("unsafe_tool_destination", "An output file collides with a directory or special file.",
                                      {"path": str(path)})
        temporary = _tmp_for(path, suffix=".wizard-new")
        backup = _tmp_for(path, suffix=".wizard-old") if before else None
        try:
            shutil.copy2(source, temporary)
            if _token(path) != before:
                raise PayloadInstallError("tool_destination_changed", "A destination file changed while preparing its replacement.")
            record = [path, backup, None]
            if backup is not None:
                path.replace(backup)
            self.files.append(record)
            temporary.replace(path)
            record[2] = _token(path)
        finally:
            temporary.unlink(missing_ok=True)

    def replace_tree(self, source, path, checkpoint):
        from Utils.atomic_write import _tmp_for

        self.mkdir(path.parent)
        before = tree_identity(path, checkpoint)
        if before is not None and not stat.S_ISDIR(before[0][2]):
            raise PayloadInstallError("unsafe_tool_destination", "A replacement directory is a link or file.",
                                      {"path": str(path)})
        temporary = _tmp_for(path, suffix=".wizard-new")
        backup = _tmp_for(path, suffix=".wizard-old") if before else None

        def copy_file(source, target):
            checkpoint()
            return shutil.copy2(source, target)

        try:
            shutil.copytree(source, temporary, copy_function=copy_file)
            checkpoint()
            if tree_identity(path, checkpoint) != before:
                raise PayloadInstallError("tool_destination_changed", "A directory changed while preparing its replacement.")
            record = [path, backup, None, None]
            if backup is not None:
                path.replace(backup)
            self.trees.append(record)
            if backup is not None:
                record[3] = tree_identity(backup)
            checkpoint()
            temporary.replace(path)
            record[2] = tree_identity(path)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)

    def remove(self, path):
        from Utils.atomic_write import _tmp_for

        self.mkdir(path.parent)
        current = _token(path)
        if current is None or not (stat.S_ISREG(current[2]) or stat.S_ISLNK(current[2])):
            raise PayloadInstallError("unsafe_tool_destination", "Only a regular file or link can be removed.",
                                      {"path": str(path)})
        backup = _tmp_for(path, suffix=".wizard-old")
        path.replace(backup)
        self.files.append((path, backup, None))

    def finish(self, log):
        for _path, backup, _identity in self.files:
            if backup is not None:
                try:
                    backup.unlink()
                except OSError as exc:
                    log(f"Wizard: old file retained at {backup}: {exc}")
        for _path, backup, _identity, previous in self.trees:
            if backup is not None:
                try:
                    if tree_identity(backup) != previous:
                        raise OSError("The backup changed after installation.")
                    shutil.rmtree(backup)
                except (OSError, PayloadInstallError) as exc:
                    log(f"Wizard: old directory retained at {backup}: {exc}")

    def rollback(self):
        failures = []
        for path, backup, identity in reversed(self.files):
            try:
                if _token(path) != identity:
                    raise RuntimeError("The output changed during recovery.")
                if backup is not None:
                    backup.replace(path)
                else:
                    path.unlink(missing_ok=True)
            except Exception as exc:
                failures.append({"path": str(path), "backup": str(backup) if backup else None, "reason": str(exc)})
        for path, backup, identity, previous in reversed(self.trees):
            try:
                if tree_identity(path) != identity or (backup is not None and tree_identity(backup) != previous):
                    raise RuntimeError("The directory or its backup changed during recovery.")
                if identity is not None:
                    shutil.rmtree(path)
                if backup is not None:
                    backup.replace(path)
            except Exception as exc:
                failures.append({"path": str(path), "backup": str(backup) if backup else None, "reason": str(exc)})
        for path, identity in reversed(self.directories):
            try:
                current = _token(path)
                if current is not None and current[:2] == identity and not path.is_symlink():
                    path.rmdir()
            except OSError:
                pass
        return failures


def _install_files(payload, target, checkpoint):
    journal = _FileJournal()
    try:
        trees = replacement_trees(payload)
        for relative in trees:
            checkpoint()
            journal.replace_tree(payload.root / relative, target / relative, checkpoint)
        for relative in payload.files:
            checkpoint()
            if any(relative.is_relative_to(tree) for tree in trees):
                continue
            if relative in payload.keep_existing:
                current = _token(target / relative)
                if current is not None and stat.S_ISREG(current[2]):
                    continue
            journal.replace(payload.root / relative, target / relative)
        for relative in payload.remove_files:
            checkpoint()
            if _token(target / relative) is not None:
                journal.remove(target / relative)
        payload.check_archive()
        checkpoint()
    except BaseException as exc:
        failures = journal.rollback()
        if failures:
            raise PayloadInstallError("tool_install_rollback_failed", "Installation failed and some files need recovery.",
                                      {"failures": failures, "state_persisted": True}) from exc
        raise
    return journal


def _owned_tree(root):
    records = {}
    for current, directories, names in os.walk(root, followlinks=False):
        for path in (Path(current), *(Path(current) / name for name in directories + names)):
            value = _token(path)
            if value is None:
                raise RuntimeError("The installed folder changed during recovery.")
            records[str(path.relative_to(root))] = value[:3] if stat.S_ISDIR(value[2]) else value
    return records


def _install_mod(payload, selected, target, name, modlist, log, checkpoint,
                 record_source, root_folder=True):
    from Nexus.nexus_meta import read_meta
    from Utils.atomic_write import _tmp_for
    from Utils.mods.install_as_mod import register_as_mod_neutral
    from Utils.mods.modlist import read_modlist

    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink() or (target.exists() and not target.is_dir()):
        raise PayloadInstallError("unsafe_tool_destination", "The mod destination is a link or file.")
    if any(path.name.casefold() == name.casefold() and path.name != name for path in target.parent.iterdir()):
        raise PayloadInstallError("tool_mod_name_conflict", "A staged folder differs only in letter case.")
    identity = _token(target)
    modlist_identity = _token(modlist)
    temporary = Path(tempfile.mkdtemp(prefix=".wizard-install-", dir=target.parent))
    backup = temporary / "previous"
    modlist_backup = temporary / "modlist.txt"
    staged = temporary / "payload"
    moved, created, registered = False, None, False
    created_tree = None
    try:
        if modlist_identity is not None:
            shutil.copy2(modlist, modlist_backup, follow_symlinks=False)
        shutil.copytree(payload.root, staged)
        checkpoint()
        payload.check_archive()
        if _token(target) != identity or _token(modlist) != modlist_identity:
            raise PayloadInstallError("tool_destination_changed", "The mod or its order changed during extraction.")
        if identity is not None:
            target.rename(backup)
            moved = True
        staged.rename(target)
        created = _token(target)[:2]
        created_tree = _owned_tree(target)
        try:
            registered = True
            register_as_mod_neutral(selected, name, payload.archive if record_source else None,
                                    modlist_path=modlist,
                                    log_fn=log, root_folder=root_folder)
        finally:
            modlist_identity = _token(modlist)
            created_tree["meta.ini"] = _token(target / "meta.ini")
            if created_tree["meta.ini"] is None:
                created_tree.pop("meta.ini")
        metadata = read_meta(target / "meta.ini")
        entry = next((row for row in read_modlist(modlist) if row.name == name), None)
        expected_source = payload.archive.name if record_source else ""
        if metadata.root_folder != root_folder or metadata.installation_file != expected_source or not entry or not entry.enabled:
            raise RuntimeError("Could not verify the managed mod's registration.")
        payload.check_archive()
        checkpoint()
    except BaseException as exc:
        failures = []
        if registered:
            try:
                if _token(modlist) != modlist_identity:
                    raise RuntimeError("Mod order changed during recovery.")
                if not modlist_backup.exists() and not modlist_backup.is_symlink():
                    modlist.unlink(missing_ok=True)
                else:
                    recovered = _tmp_for(modlist, suffix=".wizard-restore")
                    try:
                        shutil.copy2(modlist_backup, recovered, follow_symlinks=False)
                        recovered.replace(modlist)
                    finally:
                        recovered.unlink(missing_ok=True)
            except Exception as error:
                failures.append({"path": str(modlist), "reason": str(error)})
        try:
            if created is not None:
                if target.is_symlink() or _token(target)[:2] != created:
                    raise RuntimeError("The installed folder changed during recovery.")
                if created_tree != _owned_tree(target):
                    raise RuntimeError("Installed files changed during recovery; they were retained.")
                shutil.rmtree(target)
            if moved:
                backup.rename(target)
        except Exception as error:
            failures.append({"path": str(target), "reason": str(error)})
        if failures:
            raise PayloadInstallError("tool_install_rollback_failed", "Installation failed and recovery was incomplete.",
                                      {"backup": str(temporary), "failures": failures, "state_persisted": True}) from exc
        try:
            shutil.rmtree(temporary)
        except OSError as error:
            log(f"Wizard: recovered installation backup retained at {temporary}: {error}")
        raise
    try:
        shutil.rmtree(temporary)
    except OSError as exc:
        log(f"Wizard: previous installation retained at {backup}: {exc}")


def install_prepared_payload(game, payload, mode, *, mod_fallback_name, nexus_mod_id=0,
                             modlist_path=None, restore_first=True, delete_archive=True,
                             log_fn=lambda _m: None, checkpoint=lambda: None,
                             before_write=lambda: None, on_restore_start=lambda: None,
                             after_restore=lambda: None,
                             after_write=lambda: None, strict_index=False,
                             mod_name_override=None, record_source=True, root_folder=True):
    from Utils.deployment.locking import game_mutation_lock
    from Utils.operations import acquire_game_operation

    selected = copy.copy(game)
    profile = getattr(selected, "_active_profile_dir", None)
    profile = (Path(profile) if profile else selected.get_profile_root() / "profiles" / "default").resolve()
    selected.set_active_profile_dir(profile)
    binding = copy.copy(selected)
    target, label, name = destination(selected, payload.archive, mode,
                                      mod_fallback_name=mod_fallback_name, nexus_mod_id=nexus_mod_id,
                                      mod_name_override=mod_name_override)
    if ((mode == "mod" and payload.archive.resolve().is_relative_to(target))
            or payload.archive.parent.resolve() / payload.archive.name in {target / path for path in payload.files}):
        raise PayloadInstallError("tool_archive_in_destination", "Move the source archive outside the installation destination.")
    if mode == "mod":
        selected.get_effective_mod_staging_path = lambda: target.parent
        original_root = Path(selected.get_profile_root()).resolve()
        selected.get_profile_root = lambda: original_root
        selected.get_mod_staging_path = lambda: target.parent
    check_cancel = checkpoint

    def checkpoint():
        check_cancel()
        current, _label, _name = destination(binding, payload.archive, mode,
                                            mod_fallback_name=mod_fallback_name, nexus_mod_id=nexus_mod_id,
                                            mod_name_override=mod_name_override)
        if current != target:
            raise PayloadInstallError("tool_destination_changed", "The installation destination changed.")

    with acquire_game_operation(selected, "tools.payload.install"):
        checkpoint()
        payload.check_archive()
        replacement_trees(payload)
        before_write()
        checkpoint()
        if mode == "game" and restore_first:
            from Utils.deployment.pipeline import _run_restore_pipeline
            on_restore_start()
            log_fn("Wizard: restoring game to vanilla state…")
            if not _run_restore_pipeline(selected, profile.name, log_fn=log_fn):
                raise PayloadInstallError("tool_restore_failed", "Restore failed; the tool was not installed.",
                                          {"restore_attempted": True})
            current, _label, _name = destination(selected, payload.archive, mode,
                                                mod_fallback_name=mod_fallback_name, nexus_mod_id=nexus_mod_id,
                                                mod_name_override=mod_name_override)
            if current != target:
                raise PayloadInstallError("tool_destination_changed", "The configured game changed during restore.")
            after_restore()
        with game_mutation_lock(selected):
            checkpoint()
            payload.check_archive()
            log_fn(f"Wizard: extracting {payload.archive.name} → {target}")
            if mode == "mod":
                modlist = Path(modlist_path) if modlist_path else profile / "modlist.txt"
                modlist = modlist.parent.resolve() / modlist.name
                _install_mod(payload, selected, target, name, modlist, log_fn, checkpoint,
                             record_source, root_folder)
            else:
                journal = _install_files(payload, target, checkpoint)
                journal.finish(log_fn)
            log_fn(f"Wizard: extracted {len(payload.files)} file(s).")
            if name is not None:
                from Utils.mods.install_as_mod import index_installed_mod
                try:
                    index_installed_mod(selected, name, log_fn=log_fn, strict=strict_index)
                except Exception as exc:
                    raise PayloadInstallError("tool_install_index_failed", "The mod was installed but could not be indexed.",
                                              {"mod": name, "path": str(target), "state_persisted": True,
                                               "archive_retained": True}) from exc
            if delete_archive:
                try:
                    payload.check_archive()
                    payload.archive.unlink()
                    log_fn(f"Wizard: deleted {payload.archive.name} from Downloads.")
                except PayloadInstallError as exc:
                    exc.details.update(state_persisted=True, archive_retained=True, path=str(target))
                    raise
                except OSError as exc:
                    log_fn(f"Wizard: could not delete archive: {exc}")
        after_write()
    return label, len(payload.files), name
