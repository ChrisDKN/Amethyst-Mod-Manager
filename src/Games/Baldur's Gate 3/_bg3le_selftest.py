"""Self-test for bg3le support (the native Linux build's Script Extender).

Run: PYTHONPATH=src python3 "src/Games/Baldur's Gate 3/_bg3le_selftest.py" -v
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("bg3le_runtime_bg3", _HERE / "bg3le_runtime.py")
rt = importlib.util.module_from_spec(_spec)
sys.modules["bg3le_runtime_bg3"] = rt
_spec.loader.exec_module(rt)


class _Env(unittest.TestCase):
    """A fake ~/.local/share with or without a bg3le install."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data = Path(self._tmp.name)
        patcher = mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(self.data),
                                               "HOST_XDG_DATA_HOME": ""})
        patcher.start()
        self.addCleanup(patcher.stop)
        home = mock.patch.object(Path, "home", return_value=self.data / "home")
        home.start()
        self.addCleanup(home.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def install(self):
        lib = self.data / "bg3le" / "lib" / "libbg3le.so"
        lib.parent.mkdir(parents=True)
        lib.write_bytes(b"\x7fELF")


class DetectionTest(_Env):
    def test_not_installed(self):
        self.assertIsNone(rt.install_dir())
        self.assertFalse(rt.is_installed())

    def test_installed(self):
        self.install()
        self.assertEqual(rt.install_dir(), self.data / "bg3le")
        self.assertEqual(rt.library_path(), self.data / "bg3le" / "lib" / "libbg3le.so")


class LaunchOptionTest(_Env):
    def test_loads_bg3le(self):
        self.assertTrue(rt.loads_bg3le('"/x/bg3le/bin/bg3le-launch" %command%'))
        self.assertFalse(rt.loads_bg3le("%command% --skip-launcher"))
        self.assertFalse(rt.loads_bg3le(""))

    def _problem(self, manager="", steam=""):
        with mock.patch("Utils.executables.launch.load_launch_options", return_value=manager), \
             mock.patch("Utils.executables.launch.game_exe_key", return_value="bg3"), \
             mock.patch("Utils.executables.launch.effective_steam_id", return_value="1086940"), \
             mock.patch("Utils.launchers.steam.steam_launch_options", return_value=steam):
            return rt.launch_problem(object())

    def test_no_problem_without_install(self):
        self.assertIsNone(self._problem(steam=""))

    def test_steam_option_missing(self):
        self.install()
        self.assertIn("Steam Launch Options", self._problem(steam="%command%"))

    def test_steam_option_present(self):
        self.install()
        self.assertIsNone(self._problem(steam=rt.wrapper_option()))

    def test_manager_options_replace_steams(self):
        self.install()
        self.assertIn("Amethyst's Launch Options",
                      self._problem(manager="gamemoderun %command%", steam=rt.wrapper_option()))
        self.assertIsNone(self._problem(manager=rt.wrapper_option()))


class RequirementTest(unittest.TestCase):
    def test_external_provider_satisfies(self):
        from Nexus import nexus_requirements as nr
        from Nexus.nexus_meta import NexusModMeta
        meta = NexusModMeta(mod_id=9162, game_domain="baldursgate3",
                            nexus_requirements="2172:BG3SE")
        nr.register_external_provider("baldursgate3", lambda: set())
        index = nr.RequirementIndex(["MCM"], "baldursgate3")
        index.refresh({"MCM": meta})
        self.assertEqual([mid for mid, _ in index.missing["MCM"]], [2172])

        nr.register_external_provider("baldursgate3", lambda: {rt.BG3SE_NEXUS_ID})
        index = nr.RequirementIndex(["MCM"], "baldursgate3")
        index.refresh({"MCM": meta})
        self.assertEqual(index.missing["MCM"], [])
        self.assertEqual(nr.externally_provided("skyrimspecialedition"), set())


class FrameworksTest(unittest.TestCase):
    def test_hook_answers_for_framework(self):
        from Utils.games.frameworks import STATE_INSTALLED, STATE_MISSING, detect_frameworks_snapshot

        class Snapshot:
            def framework_winners(self):
                return []

            def framework_basenames(self, _names):
                return set()

        class Game:
            frameworks = {"Script Extender (bg3le)": "lib/libbg3le.so"}
            installed = True

            def get_game_path(self):
                return None

            def get_effective_root_folder_path(self):
                return None

            def framework_installed(self, label):
                return self.installed

        game = Game()
        self.assertEqual(detect_frameworks_snapshot(game, Snapshot(), None)[0].state, STATE_INSTALLED)
        game.installed = False
        self.assertEqual(detect_frameworks_snapshot(game, Snapshot(), None)[0].state, STATE_MISSING)


class ReplacementTest(unittest.TestCase):
    def test_bg3se_replaced_on_native_only(self):
        spec = importlib.util.spec_from_file_location("bg3_handler_selftest", _HERE / "baldurs_gate_3.py")
        handler = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(handler)
        game = handler.BaldursGate3.__new__(handler.BaldursGate3)
        game._runtime_mode = "native"
        self.assertEqual(game.requirement_replacement(rt.BG3SE_NEXUS_ID)["wizard"], "install_se_bg3le")
        self.assertIsNone(game.requirement_replacement(9162))
        game._runtime_mode = "proton"
        self.assertIsNone(game.requirement_replacement(rt.BG3SE_NEXUS_ID))


class ExtractTest(unittest.TestCase):
    def test_finds_installer_and_refuses_escapes(self):
        with tempfile.TemporaryDirectory() as tmp:
            good = Path(tmp) / "good.zip"
            with zipfile.ZipFile(good, "w") as z:
                z.writestr("bg3le-v0.3.0-linux-x86_64/install.py", "")
            self.assertEqual(rt._extract(good, Path(tmp) / "a"),
                             Path(tmp) / "a" / "bg3le-v0.3.0-linux-x86_64")
            bad = Path(tmp) / "bad.zip"
            with zipfile.ZipFile(bad, "w") as z:
                z.writestr("../escape.py", "")
            with self.assertRaises(ValueError):
                rt._extract(bad, Path(tmp) / "b")


if __name__ == "__main__":
    unittest.main()
