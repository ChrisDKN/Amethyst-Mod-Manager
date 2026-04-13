"""
Shared path safety utilities used by deploy, filemap, and other modules.
"""

from __future__ import annotations


def has_path_traversal(path_str: str) -> bool:
    """Return True if *path_str* contains a ``..`` path segment.

    Checks individual path segments (split on ``/`` and ``\\``), so filenames
    like ``file..name.ext`` are allowed while ``foo/../bar`` is not.
    Also returns True if the path starts with an absolute ``/`` or ``\\``.
    """
    normalised = path_str.replace("\\", "/")
    if normalised.startswith("/"):
        return True
    return ".." in normalised.split("/")
