"""Focused regression tests for profile backup and restore.

Run from the repository root with::

    PYTHONPATH=src python3 -m Utils.profiles._selftest -v
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

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
    at ``/``: should the ``symlinks=True`` fix ever regress, the test fails
    fast and bounded instead of filling the disk. The property under test - the
    link stays a link and its target is never walked - is identical.
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
            self.assertTrue((clone / "mesh.dds").is_file())
            self.assertTrue((clone / "meta.ini").is_file())
            self.assertTrue((clone / "notes.txt").is_file())
            cloned_files = sorted(
                name
                for _dirpath, _dirnames, filenames in os.walk(
                    clone, followlinks=False)
                for name in filenames)
            self.assertEqual(
                cloned_files,
                ["mesh.dds", "meta.ini", "notes.txt", "steamuser.txt"])

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


if __name__ == "__main__":
    unittest.main()
