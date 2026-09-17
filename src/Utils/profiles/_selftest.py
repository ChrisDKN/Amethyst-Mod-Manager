"""Focused regression tests for profile backup, restore, and mod cloning.

Run from the repository root with::

    PYTHONPATH=src python3 -m Utils.profiles._selftest -v
"""

from __future__ import annotations

import errno
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from Utils.fs.clone import clone_tree_hardlinked
from Utils.profiles.backup import create_backup, restore_backup
from Utils.profiles.convert import _COPY_EXTS, _clone_tree


class ProfileBackupTests(unittest.TestCase):
    def test_restore_includes_plugins_and_loadorder(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            profile_dir = Path(temporary) / "profile"
            profile_dir.mkdir()
            plugins = profile_dir / "plugins.txt"
            loadorder = profile_dir / "loadorder.txt"
            plugins.write_text("*Original.esp\n", encoding="utf-8")
            loadorder.write_text("Original.esp\n", encoding="utf-8")

            create_backup(profile_dir)
            backup_dir = next((profile_dir / "backups").iterdir())
            plugins.write_text("*Changed.esp\n", encoding="utf-8")
            loadorder.write_text("Changed.esp\n", encoding="utf-8")

            restore_backup(profile_dir, backup_dir)

            self.assertEqual(
                plugins.read_text(encoding="utf-8"), "*Original.esp\n")
            self.assertEqual(
                loadorder.read_text(encoding="utf-8"), "Original.esp\n")

    def test_restore_old_backup_leaves_current_loadorder_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            profile_dir = Path(temporary) / "profile"
            backup_dir = Path(temporary) / "old-backup"
            profile_dir.mkdir()
            backup_dir.mkdir()
            (profile_dir / "plugins.txt").write_text(
                "*Current.esp\n", encoding="utf-8")
            (profile_dir / "loadorder.txt").write_text(
                "Current.esp\n", encoding="utf-8")
            (backup_dir / "plugins.txt").write_text(
                "*Original.esp\n", encoding="utf-8")

            restore_backup(profile_dir, backup_dir)

            self.assertEqual(
                (profile_dir / "plugins.txt").read_text(encoding="utf-8"),
                "*Original.esp\n",
            )
            self.assertEqual(
                (profile_dir / "loadorder.txt").read_text(encoding="utf-8"),
                "Current.esp\n",
            )


def _file_manifest(root: Path) -> list[str]:
    """Every regular file under *root*, as sorted POSIX relative paths.

    ``followlinks=False`` so the check itself never descends a symlink.
    """
    return sorted(
        p.relative_to(root).as_posix()
        for dirpath, _dirnames, filenames in os.walk(root, followlinks=False)
        for name in filenames
        for p in (Path(dirpath) / name,)
    )


class ConvertCloneSymlinkTests(unittest.TestCase):
    """_clone_tree must PRESERVE symlinks, never follow them out of the mod.

    A tool launch can create a Wine/Proton prefix inside a mod folder; every
    such prefix contains ``pfx/dosdevices/z: -> /`` and ``c: -> ../drive_c``.
    ``shutil.copytree``'s default (``symlinks=False``) dereferences those, so
    cloning such a mod recursively copied the ENTIRE host filesystem - observed
    on a real NixOS machine as 1.45M files and 58 GB of ``.convert-tmp-`` data
    for a mod whose real content was 477 files / 61.8 MB.

    The ``z:`` link below points at a small sandbox "host root" that carries
    the same top-level names (``nix``/``home``/``usr``/…) instead of literally
    at ``/``: should the walker ever regress to following links, the test fails
    fast and bounded instead of filling the disk. The property under test - the
    link stays a link and its target is never walked - is identical. (Do NOT
    point the fixture at ``/``: a regression would then run away.)
    """

    def _make_mod_with_prefix(self, tmp: Path) -> tuple[Path, Path]:
        """Fixture mod folder with a Wine-prefix-shaped tree.

        Returns ``(mod_root, fake_host_root)`` where ``fake_host_root`` is the
        sandbox the ``z:`` drive maps to.
        """
        mod = tmp / "Pandora Behaviour Engine"
        prefix = mod / "prefix_Proton 9.0" / "pfx"
        (prefix / "drive_c" / "users").mkdir(parents=True)
        (prefix / "drive_c" / "users" / "steamuser.txt").write_text(
            "c:\n", encoding="utf-8")
        (prefix / "dosdevices").mkdir()

        fake_host = tmp / "fake-host-root"
        for name in ("nix", "home", "usr", "etc", "var"):
            (fake_host / name).mkdir(parents=True)
        (fake_host / "nix" / "store-sentinel.txt").write_text(
            "must never be copied\n", encoding="utf-8")

        os.symlink(str(fake_host), prefix / "dosdevices" / "z:")
        os.symlink("../drive_c", prefix / "dosdevices" / "c:")

        # Bulk asset (hardlinked) and in-place-editable files (real copies).
        (mod / "mesh.dds").write_bytes(b"DDS " * 64)
        (mod / "meta.ini").write_text("[General]\n", encoding="utf-8")
        (mod / "notes.txt").write_text("hello\n", encoding="utf-8")
        return mod, fake_host

    def test_clone_preserves_symlinks_and_never_follows_them(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            tmp = Path(temporary)
            mod, fake_host = self._make_mod_with_prefix(tmp)
            clone = tmp / "clone"

            _clone_tree(mod, clone)

            dosdevices = clone / "prefix_Proton 9.0" / "pfx" / "dosdevices"
            z = dosdevices / "z:"
            c = dosdevices / "c:"
            self.assertTrue(z.is_symlink(),
                            "dosdevices/z: was materialised, not preserved")
            self.assertEqual(os.readlink(z), str(fake_host))
            self.assertTrue(c.is_symlink(),
                            "dosdevices/c: was materialised, not preserved")
            self.assertEqual(os.readlink(c), "../drive_c")

            # Nothing from the link target (the host root / Nix store) leaked
            # into the clone: none of the host top-level names exist as REAL
            # entries anywhere under the clone. os.walk(followlinks=False)
            # keeps this check itself from descending through the link.
            leaked: list[str] = []
            host_names = {"nix", "home", "usr", "etc", "var",
                          "store-sentinel.txt", "fake-host-root"}
            for dirpath, dirnames, filenames in os.walk(clone,
                                                        followlinks=False):
                for name in list(dirnames) + filenames:
                    if name in host_names:
                        leaked.append(os.path.join(dirpath, name))
            self.assertEqual(leaked, [],
                             f"symlink target content leaked into clone: {leaked}")

            # Only the fixture's own entries were cloned.
            self.assertEqual(
                _file_manifest(clone),
                ["mesh.dds", "meta.ini", "notes.txt",
                 "prefix_Proton 9.0/pfx/drive_c/users/steamuser.txt"])

    def test_copy_exts_real_copies_bulk_assets_hardlinked(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            tmp = Path(temporary)
            mod = tmp / "mod"
            mod.mkdir()
            (mod / "meta.ini").write_text("[General]\n", encoding="utf-8")
            (mod / "notes.txt").write_text("hello\n", encoding="utf-8")
            (mod / "mesh.dds").write_bytes(b"DDS " * 64)
            clone = tmp / "clone"

            _clone_tree(mod, clone)

            self.assertIn(".ini", _COPY_EXTS)
            self.assertIn(".txt", _COPY_EXTS)
            self.assertNotIn(".dds", _COPY_EXTS)
            for name in ("meta.ini", "notes.txt"):
                src_stat = os.stat(mod / name)
                dst_stat = os.stat(clone / name)
                self.assertEqual(dst_stat.st_nlink, 1,
                                 f"{name} should be a real copy")
                self.assertNotEqual(dst_stat.st_ino, src_stat.st_ino)
            bulk = os.stat(clone / "mesh.dds")
            self.assertEqual(bulk.st_nlink, 2,
                             "mesh.dds should be hardlinked to the source")
            self.assertEqual(bulk.st_ino, os.stat(mod / "mesh.dds").st_ino)


class CloneXattrHazardTests(unittest.TestCase):
    """Regression tests for the bcachefs ``bcachefs.casefold`` clone abort.

    On bcachefs every directory carries an internal ``bcachefs.casefold``
    xattr; ``setxattr()`` refuses it on a destination directory that already
    has children (the case-insensitive index must be built on an empty
    directory), returning ``ENOTEMPTY``. ``shutil.copytree`` ends each
    directory recursion with ``copystat`` -> ``_copyxattr``, so that cosmetic
    metadata failure is reported as a clone failure and aborted the whole
    conversion. The walker must never copy xattrs.
    """

    @staticmethod
    def _simulate_bcachefs() -> "list[mock._patch]":
        """Patch the three xattr primitives to model a bcachefs directory."""
        def _listxattr(path, *args, **kwargs):
            return ["bcachefs.casefold"]

        def _getxattr(path, name, *args, **kwargs):
            return b"1"

        def _setxattr(path, name, value, *args, **kwargs):
            raise OSError(errno.ENOTEMPTY, "Directory not empty")

        return [
            mock.patch("os.listxattr", _listxattr, create=True),
            mock.patch("os.getxattr", _getxattr, create=True),
            mock.patch("os.setxattr", _setxattr, create=True),
        ]

    def test_clone_survives_bcachefs_casefold_enotempty(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            tmp = Path(temporary)
            mod = tmp / "mod"
            (mod / "sub").mkdir(parents=True)
            (mod / "sub" / "data.dds").write_bytes(b"DDS " * 8)
            (mod / "meta.ini").write_text("[General]\n", encoding="utf-8")

            patches = self._simulate_bcachefs()
            with patches[0], patches[1], patches[2]:
                # Sanity: with the same simulated filesystem, copytree DOES
                # abort - this is the reported bug, so this test would have
                # caught it. Small local fixture, symlinks=True: bounded.
                with self.assertRaises((shutil.Error, OSError)):
                    shutil.copytree(
                        mod, tmp / "copytree-attempt", symlinks=True)

                # ...and the walker must not care in the slightest.
                clone = tmp / "clone"
                _clone_tree(mod, clone)

            self.assertEqual(
                _file_manifest(clone), ["meta.ini", "sub/data.dds"])
            self.assertEqual(
                (clone / "sub" / "data.dds").read_bytes(), b"DDS " * 8)

    def test_clone_does_not_propagate_directory_xattrs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            tmp = Path(temporary)
            mod = tmp / "mod"
            (mod / "sub").mkdir(parents=True)
            (mod / "sub" / "data.dds").write_bytes(b"DDS " * 8)
            attr = "user.amethyst.selftest"
            try:
                os.setxattr(mod / "sub", attr, b"sentinel")
            except OSError as exc:
                self.skipTest(
                    f"filesystem does not support user.* xattrs: {exc}")
            self.assertIn(attr, os.listxattr(mod / "sub"))

            _clone_tree(mod, tmp / "clone")

            # Copytree would have replicated this; mod metadata must not be.
            self.assertNotIn(attr, os.listxattr(tmp / "clone" / "sub"))
            # The source is untouched (sanity).
            self.assertIn(attr, os.listxattr(mod / "sub"))


class CloneErrorPropagationTests(unittest.TestCase):
    """A clone failure must RAISE, never silently skip an entry.

    The caller (profile conversion) treats a failed clone as FATAL and rolls
    back precisely because a partially cloned tree would silently lose mods.
    Only the hardlink -> copy fallback may catch ``OSError``.
    """

    def test_copy_exts_error_propagates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            tmp = Path(temporary)
            mod = tmp / "mod"
            mod.mkdir()
            (mod / "meta.ini").write_text("[General]\n", encoding="utf-8")

            def _boom(*args, **kwargs):
                raise OSError(errno.EIO, "simulated read error")

            with mock.patch("shutil.copyfile", _boom):
                with self.assertRaises(OSError):
                    _clone_tree(mod, tmp / "clone")

    def test_hardlink_fallback_error_propagates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            tmp = Path(temporary)
            mod = tmp / "mod"
            mod.mkdir()
            (mod / "mesh.dds").write_bytes(b"DDS " * 8)

            with mock.patch("os.link",
                            side_effect=OSError(errno.EXDEV, "cross-device")), \
                 mock.patch("shutil.copyfile",
                            side_effect=OSError(errno.EIO, "copy failed")):
                with self.assertRaises(OSError):
                    _clone_tree(mod, tmp / "clone")

    def test_hardlink_failure_falls_back_to_copy(self) -> None:
        """Only the failed hardlink itself may be swallowed."""
        with tempfile.TemporaryDirectory() as temporary:
            tmp = Path(temporary)
            mod = tmp / "mod"
            mod.mkdir()
            (mod / "mesh.dds").write_bytes(b"DDS " * 8)

            with mock.patch("os.link",
                            side_effect=OSError(errno.EXDEV, "cross-device")):
                _clone_tree(mod, tmp / "clone")

            copied = os.stat(tmp / "clone" / "mesh.dds")
            self.assertEqual(copied.st_nlink, 1)
            self.assertEqual(
                (tmp / "clone" / "mesh.dds").read_bytes(), b"DDS " * 8)


class SharedCloneHelperTests(unittest.TestCase):
    """Direct coverage of Utils.fs.clone.clone_tree_hardlinked options."""

    def test_link_files_false_real_copies_everything(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            tmp = Path(temporary)
            mod = tmp / "mod"
            mod.mkdir()
            (mod / "bulk.dds").write_bytes(b"DDS " * 8)
            # Bounded sandbox target, NOT ``/``: a walker regression would run
            # away if this test followed links.
            sandbox = tmp / "sandbox"
            sandbox.mkdir()
            (sandbox / "sentinel.txt").write_text("nope\n", encoding="utf-8")
            os.symlink(str(sandbox), mod / "sandbox-link")

            clone_tree_hardlinked(mod, tmp / "clone", link_files=False)

            self.assertEqual(
                os.stat(tmp / "clone" / "bulk.dds").st_nlink, 1)
            self.assertTrue((tmp / "clone" / "sandbox-link").is_symlink())
            # The walker never follows the link.
            self.assertNotIn("sentinel.txt", _file_manifest(tmp / "clone"))


if __name__ == "__main__":
    unittest.main()
