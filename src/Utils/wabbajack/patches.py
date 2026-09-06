from __future__ import annotations

import hashlib
import struct
from pathlib import Path

from Utils.atomic_write import atomic_writer
from .hashes import XXHash
from .paths import WabbajackError


def _read(stream, count: int) -> bytes:
    if count < 0 or count > 1024 * 1024:
        raise WabbajackError("Invalid patch field length")
    data = stream.read(count)
    if len(data) != count:
        raise WabbajackError("Truncated Octodiff patch")
    return data


def apply_octodiff(source: Path, patch, target: Path, size: int, expected: str,
                   stop=None) -> str:
    if _read(patch, 9) != b"OCTODELTA" or _read(patch, 1) != b"\x01":
        raise WabbajackError("Unsupported Octodiff header")
    length, shift = 0, 0
    while True:
        value = _read(patch, 1)[0]
        length |= (value & 127) << shift
        if not value & 128:
            break
        shift += 7
        if shift > 28:
            raise WabbajackError("Invalid Octodiff hash name")
    algorithm = _read(patch, length).decode("ascii")
    if algorithm != "SHA1":
        raise WabbajackError(f"Unsupported Octodiff checksum: {algorithm}")
    digest_size = struct.unpack("<i", _read(patch, 4))[0]
    if digest_size != 20:
        raise WabbajackError("Invalid Octodiff SHA1 length")
    checksum = _read(patch, digest_size)
    if _read(patch, 3) != b">>>":
        raise WabbajackError("Invalid Octodiff metadata terminator")
    sha, xx, written = hashlib.sha1(), XXHash(), 0
    source_size = source.stat().st_size
    with source.open("rb") as basis, atomic_writer(target, "wb", encoding=None) as output:
        while command := patch.read(1):
            if stop is not None and stop.is_set():
                raise InterruptedError("Installation stopped")
            if command == b"\x60":
                offset, count = struct.unpack("<qq", _read(patch, 16))
                if offset < 0 or count < 0 or offset + count > source_size:
                    raise WabbajackError("Octodiff copy exceeds source bounds")
                basis.seek(offset)
                incoming = basis
            elif command == b"\x80":
                count = struct.unpack("<q", _read(patch, 8))[0]
                incoming = patch
            else:
                raise WabbajackError(f"Unknown Octodiff command: {command.hex()}")
            if count < 0 or written + count > size:
                raise WabbajackError("Octodiff output exceeds declared size")
            while count:
                if stop is not None and stop.is_set():
                    raise InterruptedError("Installation stopped")
                data = _read(incoming, min(count, 1024 * 1024))
                output.write(data)
                sha.update(data)
                xx.update(data)
                written += len(data)
                count -= len(data)
        if written != size or sha.digest() != checksum or (expected and xx.digest() != expected):
            raise WabbajackError(f"Patched output failed verification: {target.name}")
    return xx.digest()
