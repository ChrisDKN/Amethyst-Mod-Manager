"""Tree cloning that preserves symlinks and never propagates extended attributes.

Both properties matter when the source is mod content, and ``shutil.copytree``
gets each of them wrong by default:

* **Symlinks must be preserved, not followed.** A mod folder can carry a
  Wine/Proton prefix whose ``pfx/dosdevices/z: -> /`` (and ``c: -> ../drive_c``)
  entries are part of the prefix runtime state. ``copytree``'s default
  (``symlinks=False``) dereferences them, so cloning such a mod recursively
  copied the ENTIRE host filesystem - observed on a real NixOS machine as 1.45M
  files / 58 GB for a mod whose real content was 477 files / 61.8 MB.
* **Extended attributes must not be copied.** On bcachefs every directory
  carries an internal ``bcachefs.casefold`` attribute, and ``setxattr()`` on a
  destination directory that already has children fails with ``ENOTEMPTY`` (the
  case-insensitive index must be built on an empty directory). ``copytree`` ends
  each directory recursion with ``copystat`` -> ``_copyxattr``, so that purely
  cosmetic metadata failure is appended to ``errors`` and the whole clone is
  reported as failed - which aborts a profile conversion. Extended attributes
  are meaningless for mod content anyway.

``shutil.copyfile`` + ``shutil.copymode`` are used instead of ``copy2``:
``copy2`` -> ``copystat`` -> ``_copyxattr`` would reintroduce the xattr hazard
for files. ``copymode`` does not touch xattrs.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path


def clone_tree_hardlinked(src: Path, dst: Path, *,
                          copy_exts: "frozenset[str]" = frozenset(),
                          link_files: bool = True) -> None:
    """Clone *src* into *dst*: symlinks preserved, xattrs never copied.

    Regular files are hardlinked (near-instant, zero extra disk) unless their
    extension is in *copy_exts* (real copies - for files the app rewrites in
    place, hardlinking would leak later edits between the two trees) or
    *link_files* is False (real copies throughout, e.g. when a caller needs the
    clone to be fully independent of the source). ``os.link`` failing (cross-FS,
    or a filesystem that does not support links) falls back to a real copy.

    Directory modes/mtimes are intentionally not propagated, so a read-only
    source directory cannot make the clone unwritable.

    Any error other than a failed hardlink propagates: callers that clone mod
    content treat a failed clone as FATAL because a partially cloned tree would
    silently lose files. Do NOT add a bare ``except OSError: continue`` around
    the per-entry work - that is exactly the silent hole this contract forbids.
    """
    stack = [(Path(src), Path(dst))]
    while stack:
        s_dir, d_dir = stack.pop()
        os.makedirs(d_dir, exist_ok=True)
        with os.scandir(s_dir) as it:
            for entry in it:
                s = Path(entry.path)
                d = d_dir / entry.name
                if entry.is_symlink():
                    os.symlink(os.readlink(s), d)
                elif entry.is_dir(follow_symlinks=False):
                    stack.append((s, d))
                elif (not link_files
                      or os.path.splitext(entry.name)[1].lower() in copy_exts):
                    shutil.copyfile(s, d)
                    shutil.copymode(s, d)
                else:
                    try:
                        os.link(s, d)
                    except OSError:
                        # cross-FS or link-unsupported: fall back to a copy.
                        # ONLY this fallback may swallow OSError.
                        shutil.copyfile(s, d)
                        shutil.copymode(s, d)
