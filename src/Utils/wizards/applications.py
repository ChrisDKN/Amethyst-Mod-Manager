from __future__ import annotations

import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path

from .payload import PayloadInstallError


def installation_target(game, choice):
    from Utils.bethesda.xedit import applications_dir

    path = applications_dir(game, choice["app_dir"])
    return path.parent.parent.resolve() / "Applications" / path.name


@contextmanager
def prepare_application_payload(archive, choice, *, log_fn=lambda _m: None,
                                checkpoint=lambda: None, family="application"):
    from Utils.bethesda.xedit import flatten_subdirs
    from Utils.wizards.payload import PreparedPayload, prepare_payload

    with tempfile.TemporaryDirectory(prefix="amethyst-application-payload-") as raw:
        with prepare_payload(archive, strip_top_dir=False, log_fn=log_fn,
                             checkpoint=checkpoint) as extracted:
            root = Path(raw) / "payload"
            shutil.move(str(extracted.root), str(root))
            checkpoint()
            flatten_subdirs(root, choice["exe_name"])
            checkpoint()
            if not (root / choice["exe_name"]).is_file():
                raise PayloadInstallError(f"{family}_executable_missing", "The archive does not contain the selected application executable.",
                                          {"exe_name": choice["exe_name"]})
            files = tuple(sorted(path.relative_to(root) for path in root.rglob("*") if path.is_file()))
            folded = [str(path).casefold() for path in files]
            if len(set(folded)) != len(folded):
                raise PayloadInstallError("unsafe_tool_payload", "The application payload has paths differing only in case.")
            if family == "synthesis":
                from Utils.bethesda.synthesis import validate_installation_payload
                validate_installation_payload(files)
            payload = PreparedPayload(Path(archive), root, files, extracted.archive_identity)
            payload.check_archive()
            yield payload


def install_application_payload(game, payload, choice, *, checkpoint=lambda: None,
                                before_write=lambda: None, log_fn=lambda _m: None, family="application", cancel=None):
    from Utils.deployment.locking import game_mutation_lock
    from Utils.operations import acquire_game_operation
    from Utils.profiles.state import read_only_profile_state
    from contextlib import ExitStack
    from Utils.wizards.payload import _install_files

    target = installation_target(game, choice)
    if family == "synthesis":
        from Utils.bethesda.synthesis import installation_guard, validate_installation_payload, validate_installation_target
        validate_installation_target(game, target)
        validate_installation_payload(payload.files)
    if family == "lod":
        game_path = game.get_game_path()
        if game_path is not None and target.is_relative_to(Path(game_path).resolve()):
            raise PayloadInstallError("lod_installation_in_game", "Configure staging outside the game directory before installing LOD tools.")
    if payload.archive.resolve().is_relative_to(target):
        raise PayloadInstallError("tool_archive_in_destination", "Move the archive outside the application folder.")
    with ExitStack() as locks:
        with read_only_profile_state():
            locks.enter_context(acquire_game_operation(game, f"tools.{family}.install"))
            locks.enter_context(game_mutation_lock(game))
        if family == "synthesis":
            locks.enter_context(installation_guard(target, cancel))
        checkpoint()
        payload.check_archive()
        before_write()
        if installation_target(game, choice) != target:
            raise PayloadInstallError("tool_destination_changed", "The application destination changed.")
        journal = _install_files(payload, target, checkpoint)
        journal.finish(log_fn)
    log_fn(f"{choice['label']}: installed {len(payload.files)} file(s) into {target}.")
    return target, len(payload.files)


def install_application_archive(game, archive, choice, *, log_fn=lambda _m: None,
                                checkpoint=lambda: None, family="application", cancel=None):
    with prepare_application_payload(archive, choice, log_fn=log_fn,
                                     checkpoint=checkpoint, family=family) as payload:
        target, count = install_application_payload(game, payload, choice, log_fn=log_fn,
                                                   checkpoint=checkpoint, family=family, cancel=cancel)
    return target / choice["exe_name"], count
