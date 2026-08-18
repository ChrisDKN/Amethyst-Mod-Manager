"""Regression coverage for an open Configure tab across registry reloads."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from Utils.game_helpers import _GAMES  # noqa: E402
from Utils.deploy import LinkMode  # noqa: E402
from Games.base_game import BaseGame  # noqa: E402
from gui_qt.configure_game_view import ConfigureGameView  # noqa: E402
from gui_qt.game_state import GameState  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402


class _Game:
    def __init__(self, root: Path, name: str = "Game"):
        self.name = name
        self.root = root
        self.active_profile = None
        self.load_count = 0

    def get_profile_root(self):
        return self.root

    def set_active_profile_dir(self, profile_dir):
        self.active_profile = profile_dir
        self._active_profile_dir = profile_dir

    def load_paths(self):
        self.load_count += 1


class _View:
    """Only the state touched by ConfigureGameView.refresh_for_profile."""

    def __init__(self, game):
        self._game = game
        self._profile_name = None
        self._found_path = Path("/old/game")
        self._found_prefix = Path("/old/prefix")
        self._found_lutris_slug = "old-lutris"
        self._found_heroic_app = "old-heroic"
        self._found_faugus_gameid = "old-faugus"
        self._found_shortcut_appid = "old-shortcut"
        self._install_source = "faugus"
        self._install_explicit = True
        self._install_choices = [{"source": "faugus"}]
        self._scan_gen = 4
        self.prepopulated_game = None
        self.header_game = None

    def _prepopulate(self):
        self.prepopulated_game = self._game

    def _refresh_scope_header(self):
        self.header_game = self._game

    def _activate_profile_scope(self, profile_name=None):
        ConfigureGameView._activate_profile_scope(self, profile_name)


class _ProfilePathGame(BaseGame):
    """Small concrete handler backed entirely by a test directory."""

    def __init__(self, config_dir: Path):
        self._config_dir = config_dir
        self._game_path = None
        self._prefix_path = None
        self._staging_path = None
        self._deploy_mode = LinkMode.SYMLINK
        self.load_paths()

    @property
    def name(self):
        return "Profile Path Test"

    @property
    def game_id(self):
        return "profile_path_test"

    @property
    def exe_name(self):
        return "game.exe"

    @property
    def _paths_file(self):
        return self._config_dir / "paths.json"

    @property
    def _settings_file(self):
        return self._config_dir / "game_settings.json"

    def get_game_path(self):
        return self._game_path

    def get_mod_data_path(self):
        return self._prefix_path / "Mods" if self._prefix_path else None

    def get_mod_staging_path(self):
        root = self._staging_path or (self._config_dir / "staging")
        return root / "mods"

    def get_deploy_mode(self):
        return self._deploy_mode

    def set_deploy_mode(self, mode):
        self._deploy_mode = mode
        self.save_paths()

    def set_staging_path(self, path):
        self._staging_path = Path(path) if path else None
        self.save_paths()


class ConfigureProfileRebindTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._saved_games = dict(_GAMES)
        _GAMES.clear()

    def tearDown(self):
        _GAMES.clear()
        _GAMES.update(self._saved_games)

    def test_registry_replacement_is_rebound_before_next_profile_save(self):
        old_game = _Game(Path("/profiles/game"))
        old_game.set_active_profile_dir(Path("/profiles/game/profiles/test2"))
        view = _View(old_game)

        # Configure Save reloads the registry with a new handler while the tab
        # stays open. The visible profile has since moved to "test".
        replacement = _Game(Path("/profiles/game"))
        _GAMES["Game"] = replacement
        state = GameState()
        state.game_name = "Game"
        state.profile = "test"
        state.reassert_active_profile()
        ConfigureGameView.refresh_for_profile(view, state.game)

        self.assertIs(view._game, replacement)
        self.assertEqual(
            replacement.active_profile,
            Path("/profiles/game/profiles/test"),
        )
        self.assertEqual(replacement.load_count, 2)
        self.assertIs(view.prepopulated_game, replacement)
        self.assertIs(view.header_game, replacement)
        self.assertIsNone(view._found_path)
        self.assertIsNone(view._found_prefix)
        self.assertIsNone(view._found_faugus_gameid)
        self.assertIsNone(view._found_shortcut_appid)
        self.assertIsNone(view._install_source)
        self.assertFalse(view._install_explicit)
        self.assertEqual(view._install_choices, [])
        self.assertEqual(view._scan_gen, 5)

    def test_different_game_is_not_rebound_into_existing_widgets(self):
        old_game = _Game(Path("/profiles/first"), "First")
        other_game = _Game(Path("/profiles/second"), "Second")
        view = _View(old_game)

        ConfigureGameView.refresh_for_profile(view, other_game)

        self.assertIs(view._game, old_game)
        self.assertIsNone(view.prepopulated_game)
        self.assertEqual(view._scan_gen, 4)

    def test_lutris_and_faugus_stay_isolated_across_open_tab_switches(self):
        with tempfile.TemporaryDirectory(prefix="amm-config-profile-") as td:
            root = Path(td)
            config = root / "config"
            staging = root / "staging"
            game_path = root / "game"
            lutris_prefix = root / "lutris-prefix"
            faugus_prefix = root / "faugus-prefix"
            for directory in (config, staging, game_path, lutris_prefix,
                              faugus_prefix):
                directory.mkdir(parents=True)
            (config / "paths.json").write_text(json.dumps({
                "game_path": str(game_path),
                "prefix_path": str(faugus_prefix),
                "deploy_mode": "symlink",
                "staging_path": str(staging),
                "shortcut_appid": "",
                "lutris_slug": "",
                "faugus_gameid": "faugus-id",
            }), encoding="utf-8")
            for profile in ("default", "test", "test2"):
                pdir = staging / "profiles" / profile
                pdir.mkdir(parents=True)
                (pdir / "profile_state.json").write_text(
                    json.dumps({"profile_settings": {}}), encoding="utf-8")

            choices = [
                {"source": "lutris", "path": game_path,
                 "prefix": lutris_prefix, "id": "lutris-id"},
                {"source": "faugus", "path": game_path,
                 "prefix": faugus_prefix, "id": "faugus-id"},
            ]

            def activate(game, profile):
                game.set_active_profile_dir(staging / "profiles" / profile)
                game.load_paths()

            game = _ProfilePathGame(config)
            activate(game, "test2")
            saved = []
            with patch.object(ConfigureGameView, "_start_game_scan"), \
                    patch.object(ConfigureGameView, "_start_prefix_scan"), \
                    patch.object(ConfigureGameView, "_probe_version"), \
                    patch.object(ConfigureGameView, "_install_prefix_deps"), \
                    patch("gui_qt.configure_game_view._game_logo",
                          return_value=None), \
                    patch("gui_qt.configure_game_view._lutris_available",
                          return_value=True), \
                    patch("gui_qt.configure_game_view._faugus_available",
                          return_value=True):
                view = ConfigureGameView(
                    game, on_done=lambda did_save, _removed: saved.append(did_save))

                # test2 -> Lutris, then mimic Configure Save's registry rebuild.
                view._populate_install_choices(choices)
                view._on_install_combo(0)
                view._on_save()
                game = _ProfilePathGame(config)
                activate(game, "test2")
                view.refresh_for_profile(game, "test2")

                # Switch to test in the still-open tab and save Faugus.
                activate(game, "test")
                view.refresh_for_profile(game, "test")
                view._populate_install_choices(choices)
                view._on_install_combo(1)
                view._on_save()
                game = _ProfilePathGame(config)
                activate(game, "test")
                view.refresh_for_profile(game, "test")

                # Switching the same open tab back must restore test2/Lutris.
                activate(game, "test2")
                view.refresh_for_profile(game, "test2")
                view._populate_install_choices(choices)

                self.assertEqual(saved, [True, True])
                self.assertEqual(view._found_prefix, lutris_prefix)
                self.assertEqual(view._prefix_edit.text(), str(lutris_prefix))
                self.assertEqual(view._install_source, "lutris")
                self.assertEqual(
                    view._install_choices[view._install_combo.currentIndex()]["source"],
                    "lutris",
                )
                test2_settings = json.loads(
                    (staging / "profiles" / "test2" / "profile_state.json")
                    .read_text(encoding="utf-8"))["profile_settings"]
                self.assertEqual(test2_settings["prefix_path"], str(lutris_prefix))
                self.assertEqual(test2_settings["lutris_slug"], "lutris-id")
                # Faugus equals the shared default, so this profile may inherit
                # it rather than storing redundant keys. Its effective values
                # must still be Faugus while test2 remains explicitly Lutris.
                test_game = _ProfilePathGame(config)
                activate(test_game, "test")
                self.assertEqual(test_game.get_prefix_path(), faugus_prefix)
                self.assertEqual(
                    test_game.get_saved_launcher_id("faugus_gameid"),
                    "faugus-id",
                )

                view.deleteLater()


if __name__ == "__main__":
    unittest.main()
