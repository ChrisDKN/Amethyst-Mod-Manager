"""
reader.py — GPAK archive format reader for Mewgenics.

GPAK is used by Mewgenics and The End Is Nigh. Layout:
  - 4 bytes: file count (uint32 LE)
  - For each file:
      - 2 bytes: filename length (uint16 LE)
      - N bytes: filename (UTF-8 or Latin-1)
      - 4 bytes: stored size (uint32 LE) — bytes of this file in the archive
  - File data: concatenated blobs, one per file (each blob may be zlib-compressed).
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path
from typing import NamedTuple


class GpakEntry(NamedTuple):
    """Single file entry in a GPAK archive (directory only)."""
    name: str
    stored_size: int


class GpakReader:
    """Read a GPAK archive: list entries and extract files (with optional zlib)."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._entries: list[GpakEntry] = []
        self._data_start: int = 0

    def open(self) -> None:
        """Parse the GPAK directory. Call before list_entries() or extract()."""
        self._entries.clear()
        with self.path.open("rb") as f:
            (num_files,) = struct.unpack("<I", f.read(4))
            if num_files > 10_000_000:
                raise ValueError(f"GPAK file count {num_files} looks invalid")
            for _ in range(num_files):
                (name_len,) = struct.unpack("<H", f.read(2))
                if name_len > 4096:
                    raise ValueError(f"GPAK filename length {name_len} looks invalid")
                name_bytes = f.read(name_len)
                try:
                    name = name_bytes.decode("utf-8")
                except UnicodeDecodeError:
                    name = name_bytes.decode("latin-1")
                (stored_size,) = struct.unpack("<I", f.read(4))
                self._entries.append(GpakEntry(name=name, stored_size=stored_size))
            self._data_start = f.tell()

    def list_entries(self) -> list[GpakEntry]:
        """Return directory entries (name, stored_size, data_offset). Call open() first."""
        if not self._entries and self._data_start == 0:
            self.open()
        return list(self._entries)

    def read_file(self, index: int, try_zlib: bool = True) -> bytes:
        """Read and optionally decompress one file by index (0-based)."""
        if not self._entries and self._data_start == 0:
            self.open()
        if index < 0 or index >= len(self._entries):
            raise IndexError(f"File index {index} out of range (0..{len(self._entries) - 1})")
        entry = self._entries[index]
        offset = self._data_start + sum(e.stored_size for e in self._entries[:index])
        with self.path.open("rb") as f:
            f.seek(offset)
            raw = f.read(entry.stored_size)
        if try_zlib and len(raw) >= 2:
            # Common zlib headers
            if raw[:2] in (b"\x78\x9c", b"\x78\x01", b"\x78\xda", b"\x78\x5e"):
                try:
                    return zlib.decompress(raw)
                except zlib.error:
                    pass
        return raw

    def extract_all(
        self,
        dest_dir: Path | str,
        try_zlib: bool = True,
        progress_fn=None,
    ) -> list[Path]:
        """Extract all files into dest_dir. Returns list of created paths.
        progress_fn: optional callable(done: int, total: int) called after each file.
        """
        if not self._entries and self._data_start == 0:
            self.open()
        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)
        created: list[Path] = []
        total = len(self._entries)
        if progress_fn and total:
            progress_fn(0, total)
        for i, entry in enumerate(self._entries):
            data = self.read_file(i, try_zlib=try_zlib)
            out_path = dest / entry.name
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(data)
            created.append(out_path)
            if progress_fn:
                progress_fn(i + 1, total)
        return created


def _data_start_and_entries(path: Path) -> tuple[int, list[GpakEntry]]:
    """Parse directory only; return (data_start_offset, entries)."""
    entries: list[GpakEntry] = []
    with path.open("rb") as f:
        (num_files,) = struct.unpack("<I", f.read(4))
        if num_files > 10_000_000:
            raise ValueError(f"GPAK file count {num_files} looks invalid")
        for _ in range(num_files):
            (name_len,) = struct.unpack("<H", f.read(2))
            name_bytes = f.read(name_len)
            try:
                name = name_bytes.decode("utf-8")
            except UnicodeDecodeError:
                name = name_bytes.decode("latin-1")
            (stored_size,) = struct.unpack("<I", f.read(4))
            entries.append(GpakEntry(name=name, stored_size=stored_size))
        data_file_start = f.tell()
    return data_file_start, entries


def list_gpak(path: Path | str) -> list[GpakEntry]:
    """List entries in a GPAK file without holding it open."""
    path = Path(path)
    _, entries = _data_start_and_entries(path)
    return entries


def extract_gpak(
    gpak_path: Path | str,
    dest_dir: Path | str,
    try_zlib: bool = True,
    progress_fn=None,
) -> list[Path]:
    """Extract a GPAK archive to a directory. Returns list of created file paths.
    progress_fn: optional callable(done: int, total: int) called after each file.
    """
    r = GpakReader(gpak_path)
    r.open()
    return r.extract_all(dest_dir, try_zlib=try_zlib, progress_fn=progress_fn)
