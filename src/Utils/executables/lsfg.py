"""Install and detect the upstream LSFG-VK Vulkan layer."""

from __future__ import annotations

import re
import shutil
import tarfile
import tempfile
import urllib.parse
import urllib.request
import uuid
from pathlib import Path, PurePosixPath

_BUILDS_URL = "https://builds.lsfg-vk.dev/"
_RELEASE_RE = re.compile(
    r'href=["\'](?P<name>lsfg-vk-(?P<version>\d+\.\d+\.\d+)\.tar\.xz)["\']')
_REQUIRED = (
    Path("bin/lsfg-vk-cli"),
    Path("lib/liblsfg-vk-layer.so"),
    Path("share/vulkan/implicit_layer.d/")
    / "VkLayer_LSFGVK_frame_generation.json",
)


def installed_prefix() -> Path | None:
    local = Path.home() / ".local"
    if all((local / rel).is_file() for rel in _REQUIRED):
        return local
    cli = shutil.which("lsfg-vk-cli")
    if cli:
        return Path(cli).resolve().parent.parent
    prefixes = (Path("/usr/local"), Path("/usr"))
    return next((prefix for prefix in prefixes
                 if all((prefix / rel).is_file() for rel in _REQUIRED)), None)


def is_installed() -> bool:
    return installed_prefix() is not None


def _latest_release() -> tuple[str, str]:
    from Utils.ca_bundle import get_ssl_context

    request = urllib.request.Request(
        _BUILDS_URL, headers={"User-Agent": "AmethystModManager/1.0"})
    with urllib.request.urlopen(
            request, timeout=20, context=get_ssl_context()) as response:
        page = response.read().decode("utf-8", errors="replace")
    releases = []
    for match in _RELEASE_RE.finditer(page):
        version = match.group("version")
        releases.append((tuple(map(int, version.split("."))), version,
                         match.group("name")))
    if not releases:
        raise RuntimeError("the upstream build page listed no stable release")
    _key, version, name = max(releases)
    return version, urllib.parse.urljoin(_BUILDS_URL, name)


def _extract_archive(archive: Path, destination: Path) -> None:
    with tarfile.open(archive, "r:xz") as source:
        for member in source.getmembers():
            relative = PurePosixPath(member.name)
            parts = tuple(part for part in relative.parts if part not in ("", "."))
            if not parts:
                continue
            if relative.is_absolute() or ".." in parts:
                raise RuntimeError(f"unsafe path in release archive: {member.name}")
            target = destination.joinpath(*parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                raise RuntimeError(
                    f"unsupported entry in release archive: {member.name}")
            incoming = source.extractfile(member)
            if incoming is None:
                raise RuntimeError(f"could not read {member.name} from archive")
            target.parent.mkdir(parents=True, exist_ok=True)
            with incoming, target.open("wb") as output:
                shutil.copyfileobj(incoming, output)
            target.chmod(member.mode & 0o777)


def _install_payload(payload: Path, prefix: Path) -> None:
    missing = [str(rel) for rel in _REQUIRED if not (payload / rel).is_file()]
    if missing:
        raise RuntimeError(
            "release archive is missing required files: " + ", ".join(missing))

    prefix.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
            prefix="amethyst-lsfg-backup-", dir=prefix) as backup_dir:
        backup = Path(backup_dir)
        changed: list[tuple[Path, Path | None]] = []
        try:
            for source in sorted(path for path in payload.rglob("*")
                                 if path.is_file()):
                relative = source.relative_to(payload)
                target = prefix / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                saved = None
                if target.exists() or target.is_symlink():
                    if not target.is_file():
                        raise RuntimeError(
                            f"installation target is not a file: {target}")
                    saved = backup / relative
                    saved.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(target, saved)
                temporary = target.with_name(
                    f".{target.name}.amethyst-new-{uuid.uuid4().hex[:8]}")
                try:
                    shutil.copy2(source, temporary)
                    temporary.replace(target)
                finally:
                    temporary.unlink(missing_ok=True)
                changed.append((target, saved))
        except BaseException:
            for target, saved in reversed(changed):
                if saved is None:
                    target.unlink(missing_ok=True)
                else:
                    shutil.copy2(saved, target)
            raise


def install_latest(log_fn=None) -> tuple[bool, str]:
    log = log_fn or (lambda _message: None)
    try:
        version, url = _latest_release()
        log(f"LSFG-VK: downloading stable release {version}.")
        with tempfile.TemporaryDirectory(prefix="amethyst-lsfg-") as tmp_dir:
            tmp = Path(tmp_dir)
            archive = tmp / f"lsfg-vk-{version}.tar.xz"
            from Utils.ca_bundle import download_file
            download_file(url, archive, timeout=90)
            payload = tmp / "payload"
            payload.mkdir()
            _extract_archive(archive, payload)
            _install_payload(payload, Path.home() / ".local")
        if not is_installed():
            raise RuntimeError("required Vulkan-layer files are missing after setup")
        log(f"LSFG-VK: installed {version} to {Path.home() / '.local'}.")
        return True, version
    except Exception as exc:
        log(f"LSFG-VK: setup failed: {exc}")
        return False, str(exc)
