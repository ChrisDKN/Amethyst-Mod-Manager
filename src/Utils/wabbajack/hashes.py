from __future__ import annotations

import base64
import ctypes
import ctypes.util
import hashlib
from functools import lru_cache
from pathlib import Path

from .paths import WabbajackError


def hash_bytes(value) -> bytes:
    try:
        if isinstance(value, int):
            return (value & ((1 << 64) - 1)).to_bytes(8, "little")
        text = str(value)
        decoded = base64.b64decode(text + "=" * (-len(text) % 4), validate=True)
        if len(decoded) != 8:
            raise ValueError("not 64 bits")
        return decoded
    except (ValueError, TypeError) as exc:
        raise WabbajackError(f"Invalid archive hash: {value!r}") from exc


def canonical_hash(value) -> str:
    return base64.b64encode(hash_bytes(value)).decode("ascii")


@lru_cache(maxsize=1)
def _native_library():
    name = ctypes.util.find_library("xxhash")
    if not name:
        raise WabbajackError("xxHash64 is unavailable. Install python-xxhash or libxxhash.")
    library = ctypes.CDLL(name)
    library.XXH64_createState.restype = ctypes.c_void_p
    library.XXH64_reset.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
    library.XXH64_update.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
    library.XXH64_digest.argtypes = [ctypes.c_void_p]
    library.XXH64_digest.restype = ctypes.c_uint64
    library.XXH64_freeState.argtypes = [ctypes.c_void_p]
    return library


class XXHash:
    def __init__(self):
        try:
            import xxhash
            self._python = xxhash.xxh64()
            self._state = None
        except ImportError:
            self._python = None
            self._lib = _native_library()
            self._state = self._lib.XXH64_createState()
            if not self._state:
                raise MemoryError("Could not allocate xxHash64 state")
            self._lib.XXH64_reset(self._state, 0)

    def update(self, data: bytes) -> None:
        if self._python is not None:
            self._python.update(data)
        elif self._lib.XXH64_update(self._state, data, len(data)):
            raise WabbajackError("xxHash64 update failed")

    def digest(self) -> str:
        value = (self._python.intdigest() if self._python is not None
                 else self._lib.XXH64_digest(self._state))
        return base64.b64encode(value.to_bytes(8, "little")).decode("ascii")

    def __del__(self):
        if getattr(self, "_state", None):
            self._lib.XXH64_freeState(self._state)
            self._state = None


def file_hash(path: Path, stop=None) -> str:
    h = XXHash()
    with path.open("rb") as stream:
        while data := stream.read(1024 * 1024):
            if stop is not None and stop.is_set():
                raise InterruptedError("Installation stopped")
            h.update(data)
    return h.digest()


def verify_file(path: Path, expected: str, size: int, stop=None) -> bool:
    return (path.is_file() and path.stat().st_size == size
            and file_hash(path, stop) == expected)


def package_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()
