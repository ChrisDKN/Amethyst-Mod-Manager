from __future__ import annotations

import hashlib
import importlib
import shutil
import stat
import tempfile
from pathlib import Path


PATCHERS = {
    "wizards.fnv_4gb_patch.Fnv4GbPatchWizard": "Utils.bethesda.fnv4gb",
    "wizards.oblivion_4gb_patch.Oblivion4GbPatchWizard": "Utils.bethesda.oblivion4gb",
}
MAX_EXE_BYTES = 256 * 1024 * 1024


class PatchError(RuntimeError):
    def __init__(self, name, message, details=None):
        super().__init__(message)
        self.name = name
        self.details = details or {}


def _stat_identity(info):
    return [info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns]


def patcher_for_game(game):
    from Utils.wizards.plugins import get_all_wizard_tools

    modules = {PATCHERS[tool.dialog_class_path]
               for tool in get_all_wizard_tools(game, include_plugins=False)
               if tool.dialog_class_path in PATCHERS}
    if len(modules) != 1:
        raise PatchError("patch4gb_unavailable", "This game has no supported 4GB patch wizard.")
    return importlib.import_module(modules.pop())


def file_identity(path, checkpoint=lambda: None, *, allow_link=False):
    from Utils.wizards.payload import _token

    before = _token(path)
    if before is None:
        return None
    if not stat.S_ISREG(before[2]) and not (allow_link and stat.S_ISLNK(before[2])):
        raise PatchError("unsafe_patch4gb_file", "The executable or backup is not a regular file.",
                         {"path": str(path)})
    try:
        target = path.stat()
        if not stat.S_ISREG(target.st_mode) or not 0 < target.st_size <= MAX_EXE_BYTES:
            raise PatchError("unsafe_patch4gb_file", "The executable or backup is empty or too large.",
                             {"path": str(path)})
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                checkpoint()
                digest.update(chunk)
        after = path.stat()
        if before != _token(path) or _stat_identity(target) != _stat_identity(after):
            raise PatchError("stale_patch4gb_plan", "An executable or backup changed while being read.")
        return {"identity": before, "target_identity": _stat_identity(target),
                "sha256": digest.hexdigest(), "size_bytes": target.st_size,
                "symlink": stat.S_ISLNK(before[2])}
    except OSError as exc:
        raise PatchError("patch4gb_file_unavailable", "Could not read an executable or backup.",
                         {"path": str(path), "reason": str(exc)}) from exc


def inspect_files(patcher, root, checkpoint=lambda: None, *, allow_link=False):
    from Utils.wizards.payload import _token

    exe, backup = root / patcher.EXE_NAME, root / patcher.BACKUP_NAME
    states = [file_identity(exe, checkpoint, allow_link=allow_link),
              file_identity(backup, checkpoint)]
    checkpoint()
    info = patcher.inspect_exe(root)
    if any((value["identity"] if value is not None else None) != _token(path)
           for path, value in zip((exe, backup), states)):
        raise PatchError("stale_patch4gb_plan", "An executable or backup changed during inspection.")
    if any(value is not None and value["target_identity"] != _stat_identity(path.stat())
           for path, value in zip((exe, backup), states)):
        raise PatchError("stale_patch4gb_plan", "An executable or backup changed during inspection.")
    return info, states


def mutate_files(patcher, root, action, *, checkpoint=lambda: None,
                 before_write=lambda: None, log=lambda _message: None,
                 on_published=lambda: None):
    from Utils.wizards.payload import _FileJournal

    root = Path(root).resolve(strict=True)
    info, states = inspect_files(patcher, root, checkpoint)
    if action == "apply" and info["state"] != "patchable":
        raise PatchError("patch4gb_not_patchable", "The executable cannot be patched in its current state.",
                         {"state": info["state"]})
    if action == "restore" and states[1] is None:
        raise PatchError("patch4gb_backup_missing", "The executable backup is unavailable.")
    if action not in ("apply", "restore"):
        raise ValueError("Unknown 4GB patch action")
    journal = _FileJournal()
    with tempfile.TemporaryDirectory(prefix="amethyst-patch4gb-") as raw:
        prepared = Path(raw)
        source_name = patcher.EXE_NAME if action == "apply" else patcher.BACKUP_NAME
        source, target = root / source_name, prepared / source_name
        with source.open("rb") as incoming, target.open("xb") as output:
            while chunk := incoming.read(1024 * 1024):
                checkpoint()
                output.write(chunk)
        shutil.copystat(source, target)
        checkpoint()
        variant = patcher.apply_4gb_patch(prepared) if action == "apply" else patcher.restore_backup(prepared)
        checkpoint()
        if states != [file_identity(root / patcher.EXE_NAME, checkpoint),
                      file_identity(root / patcher.BACKUP_NAME, checkpoint)]:
            raise PatchError("stale_patch4gb_plan", "The executable or backup changed during preparation.")
        before_write()
        try:
            if action == "apply" and states[1] is None:
                journal.replace(prepared / patcher.BACKUP_NAME, root / patcher.BACKUP_NAME)
            checkpoint()
            if states[0] != file_identity(root / patcher.EXE_NAME, checkpoint):
                raise PatchError("stale_patch4gb_plan", "The executable changed before publication.")
            journal.replace(prepared / patcher.EXE_NAME, root / patcher.EXE_NAME)
            checkpoint()
            if action == "restore":
                if states[1] != file_identity(root / patcher.BACKUP_NAME, checkpoint):
                    raise PatchError("stale_patch4gb_plan", "The backup changed before removal.")
                journal.remove(root / patcher.BACKUP_NAME)
            checkpoint()
        except BaseException as exc:
            failures = journal.rollback()
            if failures:
                raise PatchError("patch4gb_rollback_failed", "Executable recovery was incomplete.",
                                 {"failures": failures, "state_persisted": True}) from exc
            raise
        on_published()
        journal.finish(log)
    return variant
