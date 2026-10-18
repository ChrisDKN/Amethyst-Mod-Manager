from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from Utils.wizards.payload import PayloadInstallError
from Utils.wizards.applications import installation_target


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


@contextmanager
def prepare_xedit_payload(archive, choice, *, log_fn=lambda _m: None, checkpoint=lambda: None):
    from Utils.wizards.applications import prepare_application_payload

    with prepare_application_payload(archive, choice, log_fn=log_fn, checkpoint=checkpoint, family="xedit") as payload:
        yield payload


def install_xedit_payload(game, payload, choice, *, checkpoint=lambda: None,
                          before_write=lambda: None, log_fn=lambda _m: None):
    from Utils.wizards.applications import install_application_payload

    return install_application_payload(game, payload, choice, checkpoint=checkpoint,
                                       before_write=before_write, log_fn=log_fn, family="xedit")


def install_xedit_archive(game, archive, choice, *, log_fn=lambda _m: None):
    from Utils.wizards.applications import install_application_archive

    return install_application_archive(game, archive, choice, log_fn=log_fn, family="xedit")
