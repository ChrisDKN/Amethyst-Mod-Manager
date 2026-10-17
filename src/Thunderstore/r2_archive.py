from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import stat
from tempfile import TemporaryDirectory
from zipfile import ZipFile


_MANIFEST_LIMIT = 4 * 1024 ** 2
_CONFIG_LIMIT = 256 * 1024 ** 2
_CONFIG_EXTENSIONS = {'.cfg', '.txt', '.json', '.yml', '.yaml', '.ini',
                      '.toml', '.xml', '.conf', '.config', '.properties'}
_EXECUTABLE_EXTENSIONS = {'.dll', '.exe', '.so', '.dylib', '.scr', '.com', '.pif',
                          '.bat', '.cmd', '.ps1', '.vbs', '.vbe', '.js', '.jse',
                          '.wsf', '.wsh', '.hta', '.msi', '.msix', '.sys', '.drv',
                          '.cpl', '.ocx', '.lnk', '.reg', '.inf', '.sh'}


def inspect_archive(archive):
    entries = archive.infolist()
    if len(entries) > 10000:
        raise ValueError('The .r2z archive contains too many files.')
    manifest, configs, skipped, seen, signature = [], [], [], set(), []
    total = 0
    for entry in entries:
        raw = entry.orig_filename.replace('\\', '/')
        path = PurePosixPath(raw)
        if (path.is_absolute() or '..' in path.parts or ':' in raw
                or '\x00' in raw or not path.parts
                or any(part.endswith((' ', '.')) for part in path.parts)):
            raise ValueError(f'Invalid archive path: {entry.filename}')
        if stat.S_IFMT(entry.external_attr >> 16) not in (0, stat.S_IFREG, stat.S_IFDIR):
            raise ValueError(f'Unsupported archive link or special file: {entry.filename}')
        if entry.is_dir():
            continue
        target = path.as_posix()
        if path.parts[0].casefold() == 'config':
            target = '/'.join(('BepInEx', 'config', *path.parts[1:]))
        key = target.casefold()
        if key in seen:
            raise ValueError(f'Duplicate archive destination: {target}')
        seen.add(key)
        signature.append((entry.filename, entry.CRC, entry.file_size, entry.external_attr))
        if path.as_posix() == 'export.r2x':
            manifest.append(entry)
            continue
        if key in {'mods.yml', 'mods.yaml'} or (
                key.startswith('bepinex/plugins/') and path.name.casefold() == 'manifest.json'):
            continue
        extension = path.suffix.casefold()
        if (extension in _EXECUTABLE_EXTENSIONS or
                (extension not in _CONFIG_EXTENSIONS and not key.startswith('bepinex/config/'))):
            skipped.append(entry.filename)
            continue
        total += entry.file_size
        if entry.file_size > 64 * 1024 ** 2 or total > _CONFIG_LIMIT:
            raise ValueError('Bundled configurations exceed the import size limit (64 MiB per file, 256 MiB total).')
        configs.append((entry, target))
    if len(manifest) != 1:
        raise ValueError('The .r2z archive must contain exactly one export.r2x at its root.')
    if manifest[0].file_size > _MANIFEST_LIMIT:
        raise ValueError('The r2x manifest exceeds 4 MiB.')
    with archive.open(manifest[0]) as stream:
        data = stream.read(_MANIFEST_LIMIT + 1)
    if len(data) > _MANIFEST_LIMIT:
        raise ValueError('The r2x manifest exceeds 4 MiB.')
    digest = hashlib.sha256(data + json.dumps(signature).encode()).hexdigest()
    return data, configs, skipped, digest


def restore_configs(source, profile_dir, *, cancel=None, progress=None):
    from Utils.atomic_write import atomic_writer
    from Utils.profiles.state import profile_uses_specific_mods

    profile_dir = Path(profile_dir)
    if profile_dir.is_symlink() or not profile_uses_specific_mods(profile_dir):
        raise ValueError('Bundled configurations require an isolated profile.')
    destination = profile_dir / 'Root_Folder'

    def stopped():
        return cancel is not None and cancel.is_set()

    def check_destination(path):
        for part in (path, *path.parents):
            if part == profile_dir:
                break
            if part.is_symlink():
                raise ValueError(f'Configuration destination is a link: {part}')

    with ZipFile(source['path']) as archive:
        _, configs, _, signature = inspect_archive(archive)
        if signature != source['signature']:
            raise ValueError('The .r2z archive changed after review. Import it again.')
        total = sum(entry.file_size for entry, _ in configs)
        done = 0
        with TemporaryDirectory(prefix='.r2-config-', dir=profile_dir) as temporary:
            staged = Path(temporary)
            for entry, relative in configs:
                target = staged / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(entry) as reader, target.open('wb') as writer:
                    remaining = entry.file_size
                    while True:
                        if stopped():
                            return None
                        chunk = reader.read(min(1024 ** 2, remaining + 1))
                        if not chunk:
                            break
                        remaining -= len(chunk)
                        if remaining < 0:
                            raise ValueError(f'Invalid configuration size: {entry.filename}')
                        writer.write(chunk)
                        done += len(chunk)
                        if progress:
                            progress(done, total)
                    if remaining:
                        raise ValueError(f'Truncated configuration: {entry.filename}')
            for _, relative in configs:
                check_destination(destination / relative)
            for _, relative in configs:
                if stopped():
                    return None
                target = destination / relative
                check_destination(target)
                target.parent.mkdir(parents=True, exist_ok=True)
                with (staged / relative).open('rb') as reader, atomic_writer(target, mode='wb') as writer:
                    while chunk := reader.read(1024 ** 2):
                        writer.write(chunk)
        return len(configs)
