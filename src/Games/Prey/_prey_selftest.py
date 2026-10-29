"""Self-test for Prey (2017) file routing.

Run: PYTHONPATH=src python3 src/Games/Prey/_prey_selftest.py -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
import zipfile
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
        self.assertEqual(_routes(self.game, [
            "patch_mooncrash_iconfixes.pak", "Patch_Whiplash_UI.pak", "patch_codes_1111_mc.pak", "patch_codes_1111.pak",
        ]), {
            "patch_mooncrash_iconfixes.pak": "whiplash/gamesdk/precache/patch_mooncrash_iconfixes.pak",
            "Patch_Whiplash_UI.pak": "whiplash/gamesdk/precache/patch_whiplash_ui.pak",
            "patch_codes_1111_mc.pak": "whiplash/gamesdk/precache/patch_codes_1111_mc.pak",
            "patch_codes_1111.pak": "gamesdk/precache/patch_codes_1111.pak",
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

    def test_luma_lands_beside_gog_or_epic_executable(self):
        for store_dir in ("x64-GOG", "x64-Epic"):
            with tempfile.TemporaryDirectory() as tmp:
                exe = Path(tmp) / f"Binaries/Danielle/{store_dir}/Release/Prey.exe"
                exe.parent.mkdir(parents=True)
                exe.write_bytes(b"")
                self.game.set_game_path(Path(tmp))
                self.assertEqual(_routes(self.game, ["dxgi.dll"]),
                                 {"dxgi.dll": f"binaries/danielle/{store_dir.lower()}/release/dxgi.dll"})


def _run_install_hooks(root: Path, mod_name: str = "mod") -> None:
    for hook in Prey2017().additional_install_logic:
        hook(root, mod_name, lambda _m: None)


def _files_under(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


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

    def test_mod_moves_under_its_modinfo_name_and_keeps_manager_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._stage(root, '<Mod modName="ThatGuy.Hackerman" displayName="Hackerman"/>')
            _run_install_hooks(root, "Hackerman - Instant Hacking")
            self.assertEqual(_files_under(root), {
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
                before = _files_under(root)
                _run_install_hooks(root, "broken")
                self.assertEqual(_files_under(root), before, mod_info)

    def test_framework_release_stays_together_in_its_own_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for rel in ("ChairManager.exe", "lua.dll", "ChairloaderPatch/Libs/x.xml",
                        "Examples/AuthorName.ModExample/ModInfo.xml", "meta.ini"):
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                (root / rel).write_text('<Mod modName="AuthorName.ModExample"/>')
            _run_install_hooks(root, "Chairloader")
            staged = _files_under(root)
            self.assertEqual(staged, {
                "meta.ini",
                "Chairloader/ChairManager.exe",
                "Chairloader/lua.dll",
                "Chairloader/ChairloaderPatch/Libs/x.xml",
                "Chairloader/Examples/AuthorName.ModExample/ModInfo.xml",
            })
            self.assertEqual(_routes(Prey2017(), sorted(staged - {"meta.ini"})),
                             dict.fromkeys(sorted(staged - {"meta.ini"})))


class PreyMooncrashPakContent(unittest.TestCase):
    MOONCRASH_ONLY = "Ark/DoomClock.xml"  # FCK TIMER's override; not in the base game
    BASE_ONLY = "Ark/Campaign/Objectives/CargoBay_Objectives.xml"
    SHARED = "Ark/Campaign/KeycodeLibrary.xml"

    def _staged_after_install(self, inner_paths: list[str]) -> set[str]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with zipfile.ZipFile(root / "patch_mod.pak", "w") as pak:
                for path in inner_paths:
                    pak.writestr(path, "<x/>")
            _run_install_hooks(root)
            return _files_under(root)

    def test_pak_overriding_mooncrash_only_files_goes_to_whiplash(self):
        self.assertEqual(self._staged_after_install([self.MOONCRASH_ONLY, self.SHARED]),
                         {"Whiplash/GameSDK/Precache/patch_mod.pak"})

    def test_backslash_paths_are_recognised(self):
        self.assertEqual(self._staged_after_install([self.MOONCRASH_ONLY.replace("/", "\\")]),
                         {"Whiplash/GameSDK/Precache/patch_mod.pak"})

    def test_shared_or_mixed_paks_stay_on_the_base_game_route(self):
        for inner in ([self.SHARED], [self.MOONCRASH_ONLY, self.BASE_ONLY]):
            self.assertEqual(self._staged_after_install(inner), {"patch_mod.pak"}, inner)

    def test_pak_that_is_not_a_zip_is_left_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "patch_mod.pak").write_bytes(b"not a zip")
            _run_install_hooks(root)
            self.assertTrue((root / "patch_mod.pak").is_file())


if __name__ == "__main__":
    unittest.main()
