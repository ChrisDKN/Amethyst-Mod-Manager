"""Focused regression tests for profile backup and restore.

Run from the repository root with::

    PYTHONPATH=src python3 -m Utils.profiles._selftest -v
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from Utils.profiles.backup import create_backup, restore_backup


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


if __name__ == "__main__":
    unittest.main()
