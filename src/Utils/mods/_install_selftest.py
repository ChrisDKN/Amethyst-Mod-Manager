"""Focused regression tests for mod-folder destination validation.

Run from the source tree with::

    PYTHONPATH=src python3 -m Utils.mods._install_selftest -v
"""

from __future__ import annotations

import importlib
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from Utils.mods.install import (
    PreparedInstall,
    UnsafeInstallPath,
    finish_install,
    install_collection_archive,
    prepare_archive,
)


class _Game:
    name = "Install Path Test"
    game_id = "install-path-test"
    nexus_game_domain = "install-path-test"
    supports_bain = False
    mod_supports_bundles = False
    additional_install_logic = []
    mod_staging_requires_subdir = False
    loot_sort_enabled = False
    plugins_use_star_prefix = True

    def __init__(self, staging: Path):
        self.staging = staging

    def set_active_profile_dir(self, _profile_dir) -> None:
        return None

    def load_paths(self) -> None:
        return None

    def get_effective_mod_staging_path(self) -> Path:
        return self.staging


class ModFolderPathTests(unittest.TestCase):
    def _prepared(
        self, root: Path, staging: Path, name: str,
    ) -> PreparedInstall:
        extract = root / "extract"
        extract.mkdir()
        (extract / "payload.txt").write_text("payload", encoding="utf-8")
        return PreparedInstall(
            root / "archive.zip",
            _Game(staging),
            root / "profile",
            name,
            extract,
            extract,
            None,
            None,
        )

    def _finish(self, prepared: PreparedInstall, staging: Path):
        with (
            patch("Utils.mods.copy.resolve_target_staging",
                  return_value=staging),
            patch("Utils.mods.install.stage_file_list", return_value=[
                ("payload.txt", "payload.txt", False),
            ]),
            patch("Utils.mods.install._write_install_meta"),
            patch("Utils.mods.install._update_indexes"),
            patch("Utils.mods.install._add_to_modlist"),
            patch("Utils.mods.install._add_plugins"),
            patch("Utils.mods.install._check_nexus_flags_after_install"),
        ):
            return finish_install(
                prepared, None, log_fn=lambda _message: None,
                interactive=False)

    def _unsafe_destination(
        self, root: Path, staging: Path, name: str,
    ) -> Path:
        if name.startswith("/"):
            return Path(name)
        return staging / name

    def test_unsafe_names_are_rejected_before_external_deletion(self) -> None:
        names = (
            "",
            "../victim",
            "../../victim",
            "a/b",
            "a\\b",
            ".",
            "..",
        )
        for supplied in names:
            with self.subTest(name=supplied), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                staging = root / "profile" / "mods"
                staging.mkdir(parents=True)
                destination = self._unsafe_destination(root, staging, supplied)
                destination.mkdir(parents=True, exist_ok=True)
                sentinel = destination / "SENTINEL"
                sentinel.write_text("KEEP", encoding="utf-8")
                prepared = self._prepared(root, staging, supplied)

                with self.assertRaises(UnsafeInstallPath):
                    self._finish(prepared, staging)

                self.assertEqual(sentinel.read_text(encoding="utf-8"), "KEEP")

    def test_absolute_name_is_rejected_before_external_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            staging = root / "profile" / "mods"
            staging.mkdir(parents=True)
            destination = root / "absolute-victim"
            destination.mkdir()
            sentinel = destination / "SENTINEL"
            sentinel.write_text("KEEP", encoding="utf-8")
            prepared = self._prepared(root, staging, str(destination))

            with self.assertRaises(UnsafeInstallPath):
                self._finish(prepared, staging)

            self.assertEqual(sentinel.read_text(encoding="utf-8"), "KEEP")

    def test_drive_qualified_nul_and_control_names_are_rejected(self) -> None:
        names = (r"C:\victim", "C:/victim", "bad\x00name", "bad\nname")
        for supplied in names:
            with self.subTest(name=supplied), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                staging = root / "profile" / "mods"
                staging.mkdir(parents=True)
                prepared = self._prepared(root, staging, supplied)

                with self.assertRaises(UnsafeInstallPath):
                    self._finish(prepared, staging)

    def test_existing_destination_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            staging = root / "profile" / "mods"
            outside = root / "outside"
            staging.mkdir(parents=True)
            outside.mkdir()
            sentinel = outside / "SENTINEL"
            sentinel.write_text("KEEP", encoding="utf-8")
            (staging / "Linked Mod").symlink_to(outside, target_is_directory=True)
            prepared = self._prepared(root, staging, "Linked Mod")

            with self.assertRaises(UnsafeInstallPath):
                self._finish(prepared, staging)

            self.assertEqual(sentinel.read_text(encoding="utf-8"), "KEEP")

    def test_valid_unicode_spaced_name_installs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            staging = root / "profile" / "mods"
            staging.mkdir(parents=True)
            name = "Épée magique [SE]"
            prepared = self._prepared(root, staging, name)

            result = self._finish(prepared, staging)

            self.assertEqual(result, name)
            self.assertEqual(
                (staging / name / "payload.txt").read_text(encoding="utf-8"),
                "payload",
            )

    def test_prepare_rejects_unsafe_preferred_name_before_extraction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            staging = root / "profile" / "mods"
            staging.mkdir(parents=True)
            archive = root / "archive.zip"
            archive.write_bytes(b"archive")
            game = _Game(staging)

            with (
                patch("Utils.mods.copy.resolve_target_staging",
                      return_value=staging),
                patch(
                    "Utils.mods.install._extract_with_disk_retry",
                    return_value=(False, root / "unused", 0, []),
                ) as extract,
            ):
                with self.assertRaises(UnsafeInstallPath):
                    prepare_archive(
                        str(archive), game, root / "profile",
                        log_fn=lambda _message: None,
                        preferred_name="../victim",
                    )

            extract.assert_not_called()

    def test_collection_install_accepts_normal_unicode_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            staging = root / "profile" / "mods"
            staging.mkdir(parents=True)
            archive = root / "archive.zip"
            archive.write_bytes(b"archive")
            name = "Collection – Über Mod"
            prepared = self._prepared(root, staging, name)

            with (
                patch("Utils.mods.copy.resolve_target_staging",
                      return_value=staging),
                patch("Utils.mods.install.prepare_archive",
                      return_value=prepared) as prepare,
                patch("Utils.mods.install.stage_file_list", return_value=[
                    ("payload.txt", "payload.txt", False),
                ]),
                patch("Utils.mods.install._write_install_meta"),
                patch("Utils.mods.install._update_indexes"),
                patch("Utils.mods.install._add_to_modlist"),
                patch("Utils.mods.install._add_plugins"),
                patch("Utils.mods.install._check_nexus_flags_after_install"),
            ):
                result = install_collection_archive(
                    str(archive), _Game(staging), root / "profile",
                    log_fn=lambda _message: None,
                    preferred_name=name,
                )

            self.assertEqual(result, name)
            self.assertEqual(prepare.call_args.kwargs["preferred_name"], name)
            self.assertEqual(
                (staging / name / "payload.txt").read_text(encoding="utf-8"),
                "payload",
            )

    def test_collection_logical_filename_reaches_safe_destination_guard(self) -> None:
        keyring = types.ModuleType("keyring")
        keyring.get_password = lambda *_args: None
        keyring.set_password = lambda *_args: None
        keyring.delete_password = lambda *_args: None
        with patch.dict(sys.modules, {"keyring": keyring}):
            collection_install = importlib.import_module(
                "Utils.collections.install")
            from Nexus.nexus_download import DownloadResult
            from Utils.collections.install import (
                CollectionInstallCallbacks,
                run_collection_install,
            )

        class _Downloader:
            def close_worker_session(self) -> None:
                return None

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = root / "profile"
            staging = profile / "mods"
            staging.mkdir(parents=True)
            archive = root / "archive.zip"
            archive.write_bytes(b"archive")
            victim = profile / "victim"
            victim.mkdir()
            sentinel = victim / "SENTINEL"
            sentinel.write_text("KEEP", encoding="utf-8")
            game = _Game(staging)
            mod = types.SimpleNamespace(
                file_id=1,
                mod_id=2,
                mod_name="API name",
                file_name="archive.zip",
                size_bytes=1,
                optional=False,
                domain_name="",
                version="",
                mod_author="",
                category_id=0,
                category_name="",
                resolved_file_category="",
                update_policy="exact",
            )
            schema = {
                "mods": [{
                    "name": "Manifest name",
                    "source": {
                        "fileId": 1,
                        "modId": 2,
                        "logicalFilename": "../victim",
                    },
                }],
                "plugins": [],
            }
            captured: list[str] = []

            def synchronous_pipeline(
                mods, _fetch, download, *_args, **_kwargs,
            ) -> None:
                for item in mods:
                    download(item, ("cached", DownloadResult(
                        success=True,
                        file_path=archive,
                        file_name=archive.name,
                        bytes_downloaded=archive.stat().st_size,
                        mod_id=2,
                        file_id=1,
                    )))

            def collection_sink(*_args, preferred_name="", **_kwargs):
                captured.append(preferred_name)
                return self._finish(
                    self._prepared(root, staging, preferred_name), staging)

            probe = types.SimpleNamespace(
                uncompressed_size=0,
                members_inspected=True,
                has_fomod_config=False,
            )
            with (
                patch.object(collection_install, "run_pipelined",
                             side_effect=synchronous_pipeline),
                patch.object(collection_install, "install_collection_archive",
                             side_effect=collection_sink),
                patch.object(collection_install, "probe_archive",
                             return_value=probe),
                patch.object(collection_install,
                             "_prepare_collection_update_policies"),
                patch.object(collection_install, "_write_new_profile_modlist"),
                patch.object(collection_install, "_write_collection_plugins"),
                patch.object(collection_install, "_install_bundled_assets",
                             return_value=(0, 0, [])),
                patch.object(collection_install, "_run_step3b",
                             return_value=([], None)),
                patch.object(collection_install,
                             "load_clear_archive_after_install",
                             return_value=False),
                patch.object(collection_install, "load_keep_fomod_archives",
                             return_value=True),
            ):
                run_collection_install(
                    game=game,
                    api=object(),
                    downloader=_Downloader(),
                    mods=[mod],
                    download_link_path="",
                    profile_dir=profile,
                    old_profile_dir=None,
                    collection_slug="test",
                    collection_schema_cache=schema,
                    with_bundled=False,
                    callbacks=CollectionInstallCallbacks(),
                )

            self.assertEqual(captured, ["../victim"])
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "KEEP")

    def test_interactive_rename_payload_is_validated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            staging = root / "profile" / "mods"
            staging.mkdir(parents=True)
            (staging / "Existing").mkdir()
            victim = staging.parent / "victim"
            victim.mkdir()
            sentinel = victim / "SENTINEL"
            sentinel.write_text("KEEP", encoding="utf-8")
            prepared = self._prepared(root, staging, "Existing")
            actions = iter(("rename:../victim", "replace"))

            with self.assertRaises(UnsafeInstallPath):
                with patch("Utils.mods.copy.resolve_target_staging",
                           return_value=staging):
                    finish_install(
                        prepared,
                        None,
                        log_fn=lambda _message: None,
                        on_exists=lambda *_args: next(actions),
                    )

            self.assertEqual(sentinel.read_text(encoding="utf-8"), "KEEP")


if __name__ == "__main__":
    unittest.main()
