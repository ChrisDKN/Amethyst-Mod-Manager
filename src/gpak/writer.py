"""
writer.py — Pack files into a GPAK archive.

Writes the same format the reader expects: file count, then per-file
(name length, name, stored size), then concatenated file data.
Optionally compresses each file with zlib (default) so the result
matches Mewtator / game expectations.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path
from typing import Iterator


def _iter_files(source_dir: Path) -> Iterator[tuple[Path, str]]:
    """Yield (absolute path, archive name with forward slashes) for each file under source_dir."""
    source = source_dir.resolve()
    for f in sorted(source.rglob("*")):
        if f.is_file():
            try:
                rel = f.relative_to(source)
            except ValueError:
                continue
            name = rel.as_posix()
            yield f, name


def pack_gpak(
    source_dir: Path | str,
    output_path: Path | str,
    *,
    compress: bool = True,
    progress_fn=None,
) -> int:
    """Pack a directory into a GPAK file.

    source_dir: root directory containing files to pack (walked recursively).
    output_path: path to the .gpak file to create.
    compress: if True, compress each file with zlib (default; matches Mewtator).
    progress_fn: optional callable(done: int, total: int) called after each file.

    Returns the number of files written.
    """
    source = Path(source_dir).resolve()
    output = Path(output_path).resolve()
    if not source.is_dir():
        raise NotADirectoryError(f"Source is not a directory: {source}")

    files = list(_iter_files(source))
    if not files:
        raise ValueError(f"No files found under {source}")

    output.parent.mkdir(parents=True, exist_ok=True)
    total = len(files)
    if progress_fn:
        progress_fn(0, total)

    with output.open("wb") as out:
        # Directory: file count
        out.write(struct.pack("<I", len(files)))

        # Collect data and write directory entries (name length, name, stored size)
        data_parts: list[bytes] = []
        for done, (path, name) in enumerate(files, 1):
            raw = path.read_bytes()
            if compress:
                data = zlib.compress(raw, level=9)
            else:
                data = raw
            data_parts.append(data)
            name_bytes = name.encode("utf-8")
            if len(name_bytes) > 4096:
                raise ValueError(f"Archive name too long: {name}")
            out.write(struct.pack("<H", len(name_bytes)))
            out.write(name_bytes)
            out.write(struct.pack("<I", len(data)))
            if progress_fn:
                progress_fn(done, total)

        # File data
        for data in data_parts:
            out.write(data)

    return len(files)
