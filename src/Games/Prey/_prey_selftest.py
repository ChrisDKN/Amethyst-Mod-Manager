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

    def test_mooncrash_named_paks_go_to_whiplash_precache(self):
        self.assertEqual(_routes(self.game, ["patch_mooncrash_iconfixes.pak", "Patch_Whiplash_UI.pak"]), {
            "patch_mooncrash_iconfixes.pak": "whiplash/gamesdk/precache/patch_mooncrash_iconfixes.pak",
            "Patch_Whiplash_UI.pak": "whiplash/gamesdk/precache/patch_whiplash_ui.pak",
        })

    def test_reshade_preset_lands_beside_executable(self):
        self.assertEqual(_routes(self.game, ["ReShadePreset.ini", "reshade-shaders/Shaders/qUINT_mxao.fx"]), {
            "ReShadePreset.ini": "binaries/danielle/x64/release/reshadepreset.ini",
            "reshade-shaders/Shaders/qUINT_mxao.fx": "binaries/danielle/x64/release/reshade-shaders/shaders/quint_mxao.fx",
        })

    def test_shader_cache_goes_to_saved_games_in_prefix(self):
        path = "Shaders/Cache/D3D11/CGPShaders/ArkSimpleGlass@GlassPS.fxcb"
        claims = compute_routed_destinations([path], self.game.custom_routing_rules)
        self.assertEqual(claims[path.lower()], [(True, f"drive_c/users/steamuser/Saved Games/Arkane Studios/Prey/{path}")])

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


class PreyChairloaderInstall(unittest.TestCase):
    def _stage(self, root: Path, mod_info: str) -> None:
        for rel, data in {
            "ModInfo.xml": mod_info,
            "Data/Ark/Items/ArkItems.xml": "<x/>",
            "Hackerman.dll": "dll",
            "meta.ini": "[General]",
        }.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(data)

    def _staged_files(self, root: Path) -> set[str]:
        return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}

    def test_mod_moves_under_its_modinfo_name_and_keeps_manager_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._stage(root, '<Mod modName="ThatGuy.Hackerman" displayName="Hackerman"/>')
            for hook in Prey2017().additional_install_logic:
                hook(root, "Hackerman - Instant Hacking", lambda _m: None)
            self.assertEqual(self._staged_files(root), {
                "meta.ini",
                "Mods/ThatGuy.Hackerman/ModInfo.xml",
                "Mods/ThatGuy.Hackerman/Data/Ark/Items/ArkItems.xml",
                "Mods/ThatGuy.Hackerman/Hackerman.dll",
            })

    def test_mod_without_a_valid_modname_is_left_in_place(self):
        for mod_info in ('<Mod modName="has space"/>', "<Mod", "<Mod/>"):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self._stage(root, mod_info)
                before = self._staged_files(root)
                for hook in Prey2017().additional_install_logic:
                    hook(root, "broken", lambda _m: None)
                self.assertEqual(self._staged_files(root), before, mod_info)



if __name__ == "__main__":
    unittest.main()
