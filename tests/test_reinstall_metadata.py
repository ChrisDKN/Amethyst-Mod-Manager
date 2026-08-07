from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

nexus_package = types.ModuleType("Nexus")
nexus_package.__path__ = [str(REPO_ROOT / "src" / "Nexus")]
sys.modules.setdefault("Nexus", nexus_package)

from Nexus import nexus_meta
from Utils.mod_install import stage_file_list


class ReinstallMetadataTests(unittest.TestCase):
    def test_root_collection_layout_survives_reinstall(self):
        installed = nexus_meta.NexusModMeta(
            game_domain="examplegame",
            mod_id=42,
            file_id=314,
            version="1.2.3",
            nexus_name="Example Root Package",
            root_folder=True,
            from_collection="example-collection",
            from_collection_bundled=True,
            from_collection_patched=True,
            has_update=True,
            latest_file_id=999,
        )
        refreshed = nexus_meta.build_meta_from_download(
            game_domain="examplegame",
            mod_id=42,
            file_id=314,
            archive_name="example-root-package.zip",
        )

        reinstall_meta = nexus_meta.merge_reinstall_metadata(refreshed, installed)

        self.assertTrue(reinstall_meta.root_folder)
        self.assertEqual(reinstall_meta.from_collection, "example-collection")
        self.assertFalse(reinstall_meta.from_collection_bundled)
        self.assertFalse(reinstall_meta.from_collection_patched)
        self.assertFalse(reinstall_meta.has_update)
        self.assertEqual(reinstall_meta.latest_file_id, 0)

        with tempfile.TemporaryDirectory() as temp_dir:
            extract_root = Path(temp_dir)
            package_root = extract_root / "ExampleRootMod"
            scripts_dir = package_root / "Data" / "Scripts"
            scripts_dir.mkdir(parents=True)
            (package_root / "loader.exe").write_bytes(b"loader")
            (package_root / "runtime.dll").write_bytes(b"runtime")
            (scripts_dir / "example.pex").write_bytes(b"script")

            staged = stage_file_list(
                self._skyrim_install_rules(),
                str(extract_root),
                is_root_install=reinstall_meta.root_folder,
            )

        destinations = {destination for _source, destination, is_folder in staged
                        if not is_folder}
        self.assertEqual(destinations, {
            "loader.exe",
            "runtime.dll",
            "Data/Scripts/example.pex",
        })
        self.assertNotIn("Scripts/example.pex", destinations)

    def test_local_archive_reinstall_reuses_only_stable_package_identity(self):
        installed = nexus_meta.NexusModMeta(
            game_domain="examplegame",
            mod_id=42,
            file_id=314,
            version="1.2.3",
            nexus_name="Example Root Package",
            root_folder=True,
            from_collection="example-collection",
            endorsed=True,
            ignore_update=True,
            ignored_version="2.0.0",
            missing_requirements="7:Example Requirement",
        )

        reinstall_meta = nexus_meta.merge_reinstall_metadata(None, installed)

        self.assertEqual(reinstall_meta.game_domain, "examplegame")
        self.assertEqual(reinstall_meta.mod_id, 42)
        self.assertEqual(reinstall_meta.file_id, 314)
        self.assertEqual(reinstall_meta.version, "1.2.3")
        self.assertEqual(reinstall_meta.nexus_name, "Example Root Package")
        self.assertTrue(reinstall_meta.root_folder)
        self.assertEqual(reinstall_meta.from_collection, "example-collection")
        self.assertFalse(reinstall_meta.endorsed)
        self.assertFalse(reinstall_meta.ignore_update)
        self.assertEqual(reinstall_meta.ignored_version, "")
        self.assertEqual(reinstall_meta.missing_requirements, "")

    @staticmethod
    def _skyrim_install_rules():
        return SimpleNamespace(
            mod_folder_strip_prefixes={"data"},
            mod_required_top_level_folders={"data", "scripts"},
            mod_install_prefix="",
            mod_required_file_types={".esp", ".esm", ".esl", ".exe", ".dll"},
            mod_auto_strip_until_required=True,
            mod_install_as_is_if_no_match=False,
            plugin_extensions=[".esp", ".esm", ".esl"],
            archive_extensions=[".bsa"],
            mod_folder_strip_prefixes_post={"data"},
        )


if __name__ == "__main__":
    unittest.main()
