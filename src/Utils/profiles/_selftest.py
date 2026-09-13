"""Focused regression tests for local ``.amethyst`` bundle imports.

Run from the repository root with::

    PYTHONPATH=src python3 -m Utils.profiles._selftest -v
"""

from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from Utils.mods.install import UnsafeInstallPath
from Utils.profiles.export import install_local_bundle, write_amethyst


class LocalBundleImportPathSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.base = Path(self._temporary.name)
        self.managed = self.base / "managed"
        self.profile_dir = self.managed / "profile"
        self.mods_dir = self.managed / "mods"
        self.overwrite_dir = self.managed / "overwrite"
        self.archive = self.base / "profile.amethyst"

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def _write_bundle(self, *members: tuple[str, bytes]) -> None:
        with zipfile.ZipFile(self.archive, "w") as bundle:
            for name, contents in members:
                bundle.writestr(name, contents)

    def _assert_rejected_without_partial_import(
            self, malicious_member: str, outside: Path) -> None:
        outside.parent.mkdir(parents=True, exist_ok=True)
        outside.write_bytes(b"sentinel")
        partial = self.mods_dir / "Partial" / "created.txt"
        self._write_bundle(
            ("mods/Partial/created.txt", b"partial"),
            (malicious_member, b"attacker"),
        )

        with self.assertRaises(UnsafeInstallPath):
            install_local_bundle(
                self.archive,
                self.profile_dir,
                self.mods_dir,
                self.overwrite_dir,
            )

        self.assertEqual(outside.read_bytes(), b"sentinel")
        self.assertFalse(partial.exists())

    def test_rejects_mod_member_escaping_mods_directory(self) -> None:
        self._assert_rejected_without_partial_import(
            "mods/../../outside.txt", self.base / "outside.txt")

    def test_rejects_profile_member_escaping_profile_directory(self) -> None:
        self._assert_rejected_without_partial_import(
            "profile/../../outside.txt", self.base / "outside.txt")

    def test_rejects_overwrite_member_escaping_overwrite_directory(self) -> None:
        self._assert_rejected_without_partial_import(
            "overwrite/../../outside.txt", self.base / "outside.txt")

    def test_rejects_nested_traversal(self) -> None:
        self._assert_rejected_without_partial_import(
            "mods/first/second/../../../../outside.txt",
            self.base / "outside.txt",
        )

    def test_rejects_symlinked_parent_escaping_mods_directory(self) -> None:
        outside_dir = self.base / "outside"
        outside_dir.mkdir()
        sentinel = outside_dir / "sentinel.txt"
        sentinel.write_bytes(b"sentinel")
        self.mods_dir.mkdir(parents=True)
        (self.mods_dir / "linked").symlink_to(
            outside_dir, target_is_directory=True)
        partial = self.mods_dir / "Partial" / "created.txt"
        self._write_bundle(
            ("mods/Partial/created.txt", b"partial"),
            ("mods/linked/sentinel.txt", b"attacker"),
        )

        with self.assertRaises(UnsafeInstallPath):
            install_local_bundle(
                self.archive,
                self.profile_dir,
                self.mods_dir,
                self.overwrite_dir,
            )

        self.assertEqual(sentinel.read_bytes(), b"sentinel")
        self.assertFalse(partial.exists())

    def test_rejects_symlink_destination_escaping_mods_directory(self) -> None:
        outside = self.base / "outside.txt"
        outside.write_bytes(b"sentinel")
        destination_parent = self.mods_dir / "Example"
        destination_parent.mkdir(parents=True)
        (destination_parent / "file.txt").symlink_to(outside)
        partial = self.mods_dir / "Partial" / "created.txt"
        self._write_bundle(
            ("mods/Partial/created.txt", b"partial"),
            ("mods/Example/file.txt", b"attacker"),
        )

        with self.assertRaises(UnsafeInstallPath):
            install_local_bundle(
                self.archive,
                self.profile_dir,
                self.mods_dir,
                self.overwrite_dir,
            )

        self.assertEqual(outside.read_bytes(), b"sentinel")
        self.assertFalse(partial.exists())

    def test_imports_normal_bundle(self) -> None:
        self._write_bundle(
            ("mods/Example/data/file.bin", b"mod"),
            ("overwrite/generated.cfg", b"overwrite"),
            ("profile/modlist.txt", b"+Example\n"),
            ("profile/plugins.txt", b"*example.esm\n"),
        )

        staged = install_local_bundle(
            self.archive,
            self.profile_dir,
            self.mods_dir,
            self.overwrite_dir,
        )

        self.assertEqual(staged, ["Example"])
        self.assertEqual(
            (self.mods_dir / "Example" / "data" / "file.bin").read_bytes(),
            b"mod",
        )
        self.assertEqual(
            (self.overwrite_dir / "generated.cfg").read_bytes(), b"overwrite")
        self.assertEqual(
            (self.profile_dir / "plugins.txt").read_bytes(), b"*example.esm\n")

    def test_imports_bundle_created_by_profile_exporter(self) -> None:
        source = self.base / "source"
        source_staging = source / "mods"
        source_overwrite = source / "overwrite"
        source_profile = source / "profile"
        mod_name = "Exported Möd 日本語"
        (source_staging / mod_name).mkdir(parents=True)
        (source_staging / mod_name / "file with spaces.txt").write_bytes(b"mod")
        source_overwrite.mkdir(parents=True)
        (source_overwrite / "generated.cfg").write_bytes(b"overwrite")
        source_profile.mkdir(parents=True)
        (source_profile / "modlist.txt").write_text(
            f"+{mod_name}\n", encoding="utf-8")
        write_amethyst(
            self.archive,
            {"AmethystManifest": True, "mods": []},
            staging_root=source_staging,
            overwrite_root=source_overwrite,
            profile_dir=source_profile,
            bundle_names=[mod_name],
        )

        staged = install_local_bundle(
            self.archive,
            self.profile_dir,
            self.mods_dir,
            self.overwrite_dir,
        )

        self.assertEqual(staged, [mod_name])
        self.assertEqual(
            (self.mods_dir / mod_name / "file with spaces.txt").read_bytes(),
            b"mod",
        )
        self.assertEqual(
            (self.overwrite_dir / "generated.cfg").read_bytes(), b"overwrite")

    def test_preserves_spaces_unicode_and_linux_backslashes(self) -> None:
        mod_name = "My Möd 日本語"
        self._write_bundle(
            (f"mods/{mod_name}/file with spaces.txt", b"unicode"),
            (f"mods/{mod_name}/literal\\name.txt", b"backslash"),
            (f"profile/modlist.txt", f"+{mod_name}\n".encode("utf-8")),
        )

        staged = install_local_bundle(
            self.archive,
            self.profile_dir,
            self.mods_dir,
            self.overwrite_dir,
        )

        self.assertEqual(staged, [mod_name])
        self.assertEqual(
            (self.mods_dir / mod_name / "file with spaces.txt").read_bytes(),
            b"unicode",
        )
        self.assertEqual(
            (self.mods_dir / mod_name / "literal\\name.txt").read_bytes(),
            b"backslash",
        )

    def test_accepts_repeated_separators_and_dot_components(self) -> None:
        self._write_bundle(
            ("mods/Example//./nested/file.txt", b"contents"),
            ("profile/modlist.txt", b"+Example\n"),
        )

        staged = install_local_bundle(
            self.archive,
            self.profile_dir,
            self.mods_dir,
            self.overwrite_dir,
        )

        self.assertEqual(staged, ["Example"])
        self.assertEqual(
            (self.mods_dir / "Example" / "nested" / "file.txt").read_bytes(),
            b"contents",
        )


if __name__ == "__main__":
    unittest.main()
