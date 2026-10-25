"""Self-test for Prey (2017) file routing.

Run: PYTHONPATH=src python3 src/Games/Prey/_prey_selftest.py -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

# Handlers create their config dirs on import and construction; keep them out of ~/.config.
_CONFIG = tempfile.TemporaryDirectory()
os.environ["XDG_CONFIG_HOME"] = _CONFIG.name

_SRC_ROOT = Path(__file__).resolve().parents[2]
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from Games.Prey.prey import Prey2017  # noqa: E402
from Utils.deployment.custom_rules import compute_routed_destinations  # noqa: E402


def _routes(game: Prey2017, paths: list[str]) -> dict[str, str | None]:
    claims = compute_routed_destinations(paths, game.custom_routing_rules)
    return {p: (claims[p.lower()][0][1].lower() if p.lower() in claims else None)
            for p in paths}


class PreyRouting(unittest.TestCase):
    def setUp(self):
        self.game = Prey2017()

    def test_loose_pak_goes_to_base_game_precache(self):
        self.assertEqual(_routes(self.game, ["patch_prey_iconfixes.pak"]),
                         {"patch_prey_iconfixes.pak": "gamesdk/precache/patch_prey_iconfixes.pak"})

    def test_vanilla_named_files_win_over_precache_rule(self):
        self.assertEqual(_routes(self.game, [
            "Engine.pak", "English_xml.pak", "game.cfg", "Levels/Campaign/EndGame/level.pak",
        ]), {
            "Engine.pak": "engine/engine.pak",
            "English_xml.pak": "localization/english_xml.pak",
            "game.cfg": "gamesdk/game.cfg",
            "Levels/Campaign/EndGame/level.pak": "gamesdk/levels/campaign/endgame/level.pak",
        })

    def test_structured_mods_are_not_rerouted(self):
        paths = [
            "GameSDK/Precache/patch_x.pak",
            "Whiplash/GameSDK/Precache/patch_x.pak",
            "Engine/Engine.pak",
            "Binaries/Danielle/x64/Release/Luma/Global/a.hlsl",
        ]
        self.assertEqual(_routes(self.game, paths), dict.fromkeys(paths))

    def test_luma_lands_beside_steam_executable(self):
        self.assertEqual(_routes(self.game, ["dxgi.dll", "Luma-Prey.addon", "Luma/Global/a.hlsl"]), {
            "dxgi.dll": "binaries/danielle/x64/release/dxgi.dll",
            "Luma-Prey.addon": "binaries/danielle/x64/release/luma-prey.addon",
            "Luma/Global/a.hlsl": "binaries/danielle/x64/release/luma/global/a.hlsl",
        })

    def test_luma_lands_beside_gog_executable(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "Binaries/Danielle/x64-GOG/Release/Prey.exe"
            exe.parent.mkdir(parents=True)
            exe.write_bytes(b"")
            self.game.set_game_path(Path(tmp))
            self.assertEqual(_routes(self.game, ["dxgi.dll"]),
                             {"dxgi.dll": "binaries/danielle/x64-gog/release/dxgi.dll"})


if __name__ == "__main__":
    unittest.main()
