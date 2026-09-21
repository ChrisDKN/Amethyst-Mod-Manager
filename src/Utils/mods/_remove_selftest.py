"""Focused regression tests for safe mod removal.

Run from the source tree with::

    PYTHONPATH=src python3 -m Utils.mods._remove_selftest -v
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from Utils.mods.remove import remove_mods


class _Game:
    plugin_extensions = []

    def __init__(self, game_dir: Path, staging: Path, *, active: bool = True):
        self.game_dir = game_dir
        self.staging = staging
        self.active = active

    def get_game_path(self) -> Path:
        return self.game_dir

    def get_deploy_active(self) -> bool:
        return self.active

    def get_effective_mod_staging_path(self) -> Path:
        return self.staging


class _Profile:
    def __init__(self, entries):
        self.entries = list(entries)
        self.forgotten: list[str] = []

    def deployed_entries(self):
        return tuple(self.entries)

    def forget_deployed_mods(self, names) -> int:
        self.forgotten.extend(names)
        keys = {name.lower() for name in names}
        before = len(self.entries)
        self.entries = [e for e in self.entries if e.mod_key not in keys]
        return before - len(self.entries)


class _Library:
    def __init__(self, profile: _Profile):
        self.profile = profile
        self.removed: list[str] = []

    def open_profile(self, _profile_dir):
        return self.profile

    def remove_mod(self, name: str) -> bool:
        self.removed.append(name)
        return True


class ModRemovalTests(unittest.TestCase):
    def _fixture(self, root: Path, *, active: bool = True, deployed: bool = True):
        profile_dir = root / "profile"
        staging = profile_dir / "mods"
        source = staging / "Example Mod" / "payload.txt"
        destination = root / "game" / "Data" / "payload.txt"
        source.parent.mkdir(parents=True)
        destination.parent.mkdir(parents=True)
        source.write_text("ORIGINAL", encoding="utf-8")
        if deployed:
            shutil.copy2(source, destination)
        entry = types.SimpleNamespace(
            target="game",
            destination="Data/payload.txt",
            mod_name="Example Mod",
            mod_key="example mod",
            source_display="payload.txt",
        )
        profile = _Profile([entry])
        library = _Library(profile)
        game = _Game(root / "game", staging, active=active)
        return profile_dir, staging, source, destination, profile, library, game

    def _remove(self, profile_dir, staging, library, game):
        service = types.ModuleType("Utils.filegraph.service")
        service.FileGraphService = types.SimpleNamespace(
            open_library=lambda *_args, **_kwargs: library)
        with patch.dict(sys.modules, {"Utils.filegraph.service": service}):
            remove_mods(
                game,
                profile_dir,
                ["Example Mod"],
                staging_root=staging,
            )

    def test_successful_undeploy_removes_all_owned_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            profile_dir, staging, _source, destination, profile, library, game = fixture

            self._remove(profile_dir, staging, library, game)

            self.assertFalse(destination.exists())
            self.assertFalse((staging / "Example Mod").exists())
            self.assertEqual(profile.entries, [])
            self.assertEqual(profile.forgotten, ["Example Mod"])
            self.assertEqual(library.removed, ["Example Mod"])

    def test_unlink_failure_preserves_source_and_catalog_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            profile_dir, staging, source, destination, profile, library, game = fixture
            original_unlink = Path.unlink

            def fail_destination(path, *args, **kwargs):
                if path == destination:
                    raise PermissionError("injected unlink failure")
                return original_unlink(path, *args, **kwargs)

            with (
                patch.object(Path, "unlink", fail_destination),
                patch("Utils.mods.remove._remove_plugins_for_mods") as plugins,
            ):
                with self.assertRaises(RuntimeError):
                    self._remove(profile_dir, staging, library, game)

            plugins.assert_not_called()
            self.assertTrue(destination.exists())
            self.assertEqual(destination.read_text(encoding="utf-8"), "ORIGINAL")
            self.assertTrue(source.exists())
            self.assertEqual(len(profile.entries), 1)
            self.assertEqual(profile.forgotten, [])
            self.assertEqual(library.removed, [])

    def test_modified_deployed_copy_preserves_source_and_catalog_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            profile_dir, staging, source, destination, profile, library, game = fixture
            destination.write_text("USER MODIFIED COPY", encoding="utf-8")

            with self.assertRaises(RuntimeError):
                self._remove(profile_dir, staging, library, game)

            self.assertEqual(
                destination.read_text(encoding="utf-8"), "USER MODIFIED COPY")
            self.assertTrue(source.exists())
            self.assertEqual(len(profile.entries), 1)
            self.assertEqual(profile.forgotten, [])
            self.assertEqual(library.removed, [])

    def test_inactive_deployment_skips_undeploy_and_removes_mod(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp), active=False)
            profile_dir, staging, _source, destination, profile, library, game = fixture

            self._remove(profile_dir, staging, library, game)

            self.assertTrue(destination.exists())
            self.assertFalse((staging / "Example Mod").exists())
            self.assertEqual(profile.entries, [])
            self.assertEqual(profile.forgotten, ["Example Mod"])
            self.assertEqual(library.removed, ["Example Mod"])

    def test_missing_destination_is_already_reconciled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp), deployed=False)
            profile_dir, staging, _source, destination, profile, library, game = fixture

            self._remove(profile_dir, staging, library, game)

            self.assertFalse(destination.exists())
            self.assertFalse((staging / "Example Mod").exists())
            self.assertEqual(profile.entries, [])
            self.assertEqual(profile.forgotten, ["Example Mod"])
            self.assertEqual(library.removed, ["Example Mod"])


if __name__ == "__main__":
    unittest.main()
