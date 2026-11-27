"""Focused regression tests for collection binary patch application.

Run from the repository root with::

    PYTHONPATH=src python3 -m Utils.collections._selftest -v
"""

from __future__ import annotations

import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from Utils.collections import patches


def _patch_patcher_deps(stack: ExitStack, fake_bsdiff4: object) -> None:
    stack.enter_context(mock.patch.object(patches, "bsdiff4", fake_bsdiff4))
    stack.enter_context(mock.patch.object(patches, "_BSDIFF_AVAILABLE", True))
    if not hasattr(os, "pathconf"):
        stack.enter_context(mock.patch("Utils.atomic_write.filename_limit", return_value=255))


class CollectionPatchCrcTests(unittest.TestCase):
    def test_crc_matches_zero_byte_representations(self) -> None:
        empty_crc = patches._crc32_hex(b"")
        self.assertEqual(empty_crc, "00000000")
        for expected in ("", "0", "00000000", 0):
            with self.subTest(expected=expected):
                self.assertTrue(patches._crc_matches(b"", empty_crc, expected))

        self.assertFalse(patches._crc_matches(b"", empty_crc, "DEADBEEF"))
        self.assertFalse(patches._crc_matches(b"", empty_crc, None))

    def test_crc_matches_rejects_empty_expected_on_non_empty_file(self) -> None:
        non_empty = b"hello"
        actual_crc = patches._crc32_hex(non_empty)
        self.assertFalse(patches._crc_matches(non_empty, actual_crc, ""))
        self.assertFalse(patches._crc_matches(non_empty, actual_crc, "0"))
        self.assertFalse(patches._crc_matches(non_empty, actual_crc, None))
        self.assertFalse(patches._crc_matches(non_empty, actual_crc, "not-hex"))

    def test_crc_matches_unpadded_hex(self) -> None:
        self.assertTrue(patches._crc_matches(b"x", "00A1B2C3", "a1b2c3"))
        self.assertFalse(patches._crc_matches(b"x", "00A1B2C3", "a1b2c4"))

    def test_apply_collection_patches_allows_zero_byte_empty_and_zero_crc(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive_root = root / "archive"
            staging_path = root / "staging"
            mod_name = "Complex Sorter"
            patch_dir = archive_root / "patches" / mod_name / "Rules (Mods)"
            patch_dir.mkdir(parents=True)
            mod_dir = staging_path / mod_name
            rules_dir = mod_dir / "Rules (Mods)"
            rules_dir.mkdir(parents=True)

            target_a = rules_dir / "GhostWire Masks.ini"
            target_b = rules_dir / "Sten.ini"
            target_a.write_bytes(b"")
            target_b.write_bytes(b"")
            (patch_dir / "GhostWire Masks.ini.diff").write_bytes(b"diff-a")
            (patch_dir / "Sten.ini.diff").write_bytes(b"diff-b")

            schema = {
                "mods": [
                    {
                        "name": mod_name,
                        "patches": {
                            "Rules (Mods)\\GhostWire Masks.ini": "",
                            "Rules (Mods)\\Sten.ini": "0",
                        },
                    }
                ]
            }
            fake_bsdiff4 = SimpleNamespace(
                patch=lambda src, diff: src + b"patched:" + diff
            )
            with ExitStack() as stack:
                _patch_patcher_deps(stack, fake_bsdiff4)
                result = patches.apply_collection_patches(
                    archive_root=archive_root,
                    collection_schema=schema,
                    staging_path=staging_path,
                    mod_folder_for=lambda _entry: mod_name,
                )
                reapplied = patches.apply_collection_patches(
                    archive_root=archive_root,
                    collection_schema=schema,
                    staging_path=staging_path,
                    mod_folder_for=lambda _entry: mod_name,
                )

            self.assertEqual(result.applied, 2)
            self.assertEqual(result.crc_mismatch, 0)
            self.assertEqual(result.failed, 0)
            self.assertEqual(target_a.read_bytes(), b"patched:diff-a")
            self.assertEqual(target_b.read_bytes(), b"patched:diff-b")
            self.assertEqual(reapplied.already_applied, 2)
            self.assertEqual(reapplied.applied, 0)

    def test_apply_collection_patches_rejects_empty_crc_for_non_empty_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive_root = root / "archive"
            staging_path = root / "staging"
            mod_name = "Complex Sorter"
            patch_dir = archive_root / "patches" / mod_name
            patch_dir.mkdir(parents=True)
            mod_dir = staging_path / mod_name
            mod_dir.mkdir(parents=True)

            target = mod_dir / "NonEmpty.ini"
            target.write_bytes(b"existing-content")
            (patch_dir / "NonEmpty.ini.diff").write_bytes(b"diff")

            schema = {
                "mods": [
                    {
                        "name": mod_name,
                        "patches": {"NonEmpty.ini": ""},
                    }
                ]
            }
            logs: list[str] = []
            fake_bsdiff4 = SimpleNamespace(patch=lambda _src, _diff: b"replaced")
            with ExitStack() as stack:
                _patch_patcher_deps(stack, fake_bsdiff4)
                result = patches.apply_collection_patches(
                    archive_root=archive_root,
                    collection_schema=schema,
                    staging_path=staging_path,
                    mod_folder_for=lambda _entry: mod_name,
                    log_fn=logs.append,
                )

            self.assertEqual(result.applied, 0)
            self.assertEqual(result.crc_mismatch, 1)
            self.assertEqual(target.read_bytes(), b"existing-content")
            self.assertTrue(any("CRC mismatch" in line for line in logs))


if __name__ == "__main__":
    unittest.main()
