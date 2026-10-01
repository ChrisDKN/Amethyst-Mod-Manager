from __future__ import annotations

import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from Utils.wizards.payload import PayloadInstallError


WIZARD_CLASSES = (
    "wizards.sseedit.SSEEditWizard", "wizards.sseedit.SSEEditQACWizard",
    "wizards.sseedit.XEditDiscordWizard", "wizards.sseedit.XEditDiscordQACWizard",
)
NEXUS_URL = "https://www.nexusmods.com/skyrimspecialedition/mods/164?tab=files&file_id=495506"
NEXUS_FILE_ID = 495506


def _name(value):
    if (not isinstance(value, str) or not value.strip() or value.strip() != value
            or value in (".", "..") or any(char in value for char in "/\\:")
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise PayloadInstallError("invalid_xedit_options", "An xEdit application or executable name is unsafe.")
    return value


def options(*, xedit_exe=None, nexus_url=None, nexus_file_id=None, app_dir=None,
            display_name=None, qac=False, discord=False, discord_mode=None, **_extra):
    base = _name(xedit_exe or "SSEEdit.exe")
    if not base.lower().endswith(".exe") or not isinstance(qac, bool) or not isinstance(discord, bool):
        raise PayloadInstallError("invalid_xedit_options", "Choose a declared Windows xEdit build.")
    exe = base[:-4] + "QuickAutoClean.exe" if qac and not discord else base
    directory = _name(app_dir or base.removesuffix(".exe"))
    label = display_name or base.removesuffix(".exe")
    if not isinstance(label, str) or not label.strip():
        raise PayloadInstallError("invalid_xedit_options", "The xEdit display name is missing.")
    if nexus_file_id is None and nexus_url is None:
        nexus_file_id = NEXUS_FILE_ID
    if isinstance(nexus_file_id, bool) or (nexus_file_id is not None and
            (not isinstance(nexus_file_id, int) or nexus_file_id < 0)):
        raise PayloadInstallError("invalid_xedit_options", "The pinned Nexus file is invalid.")
    file_id = 0 if discord else nexus_file_id or 0
    url = nexus_url or NEXUS_URL
    source = {"kind": "manual", "url": None if discord else url}
    if file_id:
        parsed = urlsplit(url)
        parts = parsed.path.strip("/").split("/")
        if (parsed.scheme != "https" or parsed.hostname not in ("nexusmods.com", "www.nexusmods.com")
                or len(parts) != 3 or parts[1] != "mods" or not parts[2].isdigit() or int(parts[2]) <= 0):
            raise PayloadInstallError("invalid_xedit_options", "The pinned Nexus source is invalid.")
        source = {"kind": "nexus", "url": url, "domain": parts[0],
                  "mod_id": int(parts[2]), "file_id": file_id,
                  "download_method": "nexus.download.start"}
    if discord and (not isinstance(discord_mode, str) or not discord_mode.isalnum()):
        raise PayloadInstallError("invalid_xedit_options", "The multi-game build needs its declared game mode.")
    return {"label": label, "app_dir": directory, "base_exe": base, "exe_name": exe,
            "optional_64bit_exe": None if discord else str(Path("Optional") / (Path(base).stem + "64.exe")),
            "qac": qac, "discord": discord, "discord_mode": discord_mode if discord else None,
            "archive_keywords": ["xedit" if discord else label.lower()], "source": source}


def tool_options(tool):
    if tool.dialog_class_path not in WIZARD_CLASSES:
        raise PayloadInstallError("xedit_unavailable", "Choose a built-in xEdit tool.")
    values = dict(tool.extra)
    values["qac"] = tool.dialog_class_path.endswith("QACWizard")
    values["discord"] = tool.dialog_class_path in WIZARD_CLASSES[2:]
    return options(**values)


def installation_target(game, choice):
    from Utils.bethesda.xedit import applications_dir

    path = applications_dir(game, choice["app_dir"])
    return path.parent.parent.resolve() / "Applications" / path.name


@contextmanager
def prepare_xedit_payload(archive, choice, *, log_fn=lambda _m: None, checkpoint=lambda: None):
    from Utils.bethesda.xedit import flatten_subdirs
    from Utils.wizards.payload import PreparedPayload, prepare_payload

    with tempfile.TemporaryDirectory(prefix="amethyst-xedit-payload-") as raw:
        with prepare_payload(archive, strip_top_dir=False, log_fn=log_fn,
                             checkpoint=checkpoint) as extracted:
            root = Path(raw) / "payload"
            shutil.move(str(extracted.root), str(root))
            checkpoint()
            flatten_subdirs(root, choice["exe_name"])
            checkpoint()
            if not (root / choice["exe_name"]).is_file():
                raise PayloadInstallError("xedit_executable_missing", "The archive does not contain the selected xEdit executable.",
                                          {"exe_name": choice["exe_name"]})
            files = tuple(sorted(path.relative_to(root) for path in root.rglob("*") if path.is_file()))
            folded = [str(path).casefold() for path in files]
            if len(set(folded)) != len(folded):
                raise PayloadInstallError("unsafe_tool_payload", "The xEdit payload has paths differing only in case.")
            payload = PreparedPayload(Path(archive), root, files, extracted.archive_identity)
            payload.check_archive()
            yield payload


def install_xedit_payload(game, payload, choice, *, checkpoint=lambda: None,
                          before_write=lambda: None, log_fn=lambda _m: None):
    from Utils.deployment.locking import game_mutation_lock
    from Utils.operations import acquire_game_operation
    from Utils.wizards.payload import _install_files

    target = installation_target(game, choice)
    if payload.archive.resolve().is_relative_to(target):
        raise PayloadInstallError("tool_archive_in_destination", "Move the archive outside the xEdit application folder.")
    with acquire_game_operation(game, "tools.xedit.install"):
        with game_mutation_lock(game):
            checkpoint()
            payload.check_archive()
            before_write()
            if installation_target(game, choice) != target:
                raise PayloadInstallError("tool_destination_changed", "The xEdit application destination changed.")
            journal = _install_files(payload, target, checkpoint)
            journal.finish(log_fn)
    log_fn(f"xEdit: installed {len(payload.files)} file(s) into {target}.")
    return target, len(payload.files)


def install_xedit_archive(game, archive, choice, *, log_fn=lambda _m: None):
    with prepare_xedit_payload(archive, choice, log_fn=log_fn) as payload:
        target, count = install_xedit_payload(game, payload, choice, log_fn=log_fn)
    return target / choice["exe_name"], count
