"""Focused checks for the Owlcat (Pathfinder / Rogue Trader) handlers.

Run directly from the source tree::

    python3 src/Games/Owlcat/_owlcat_selftest.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

# Handlers create their config dirs on construction; keep them out of ~/.config.
_CONFIG = tempfile.TemporaryDirectory()
os.environ["XDG_CONFIG_HOME"] = _CONFIG.name

_SRC_ROOT = Path(__file__).resolve().parents[2]
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from Games.Owlcat.owlcat_base import (  # noqa: E402
    TEMPLATE_MANIFEST, UMM_MANIFEST, apply_settings_sync, classify_mod_dir,
    locallow_rel, sync_enabled, template_id,
)

_RT_DIR = "Warhammer 40000 Rogue Trader"


def test_locallow_rel_variants() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        prefix = Path(tmp) / "a"
        (prefix / "drive_c" / "users" / "steamuser").mkdir(parents=True)
        assert locallow_rel(prefix, _RT_DIR) == (
            "drive_c/users/steamuser/AppData/LocalLow/Owlcat Games/" + _RT_DIR)

        user = os.environ.get("USER") or "someone"
        os.environ["USER"] = user
        prefix2 = Path(tmp) / "b"
        (prefix2 / "pfx" / "drive_c" / "users" / user).mkdir(parents=True)
        assert locallow_rel(prefix2, _RT_DIR) == (
            f"pfx/drive_c/users/{user}/AppData/LocalLow/Owlcat Games/{_RT_DIR}")

        prefix3 = Path(tmp) / "c"
        (prefix3 / "drive_c" / "users").mkdir(parents=True)
        assert locallow_rel(prefix3, _RT_DIR) == (
            "drive_c/users/steamuser/AppData/LocalLow/Owlcat Games/" + _RT_DIR)

        assert locallow_rel(None, _RT_DIR) is None
        empty = Path(tmp) / "d"
        empty.mkdir()
        assert locallow_rel(empty, _RT_DIR) is None
    print("✓ locallow_rel variants")


def test_classify() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        umm = root / "umm"
        umm.mkdir()
        (umm / UMM_MANIFEST).write_text("{}")
        both = root / "both"
        both.mkdir()
        (both / UMM_MANIFEST).write_text("{}")
        (both / TEMPLATE_MANIFEST).write_text("{}")
        tmpl = root / "tmpl"
        tmpl.mkdir()
        (tmpl / TEMPLATE_MANIFEST).write_text("{}")
        neither = root / "neither"
        neither.mkdir()
        (neither / "readme.txt").write_text("")
        assert classify_mod_dir(umm) == "umm"
        assert classify_mod_dir(both) == "umm"
        assert classify_mod_dir(tmpl) == "template"
        assert classify_mod_dir(neither) is None
    print("✓ classify_mod_dir")


def test_template_id() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        unit = Path(tmp) / "mymod-1.2"
        unit.mkdir()
        (unit / TEMPLATE_MANIFEST).write_text(json.dumps({"UniqueName": "MyMod"}))
        assert template_id(unit) == "MyMod"
        (unit / TEMPLATE_MANIFEST).write_bytes(
            b"\xef\xbb\xbf" + json.dumps({"UniqueName": "BomMod"}).encode())
        assert template_id(unit) == "BomMod"
        (unit / TEMPLATE_MANIFEST).write_text("{not json")
        assert template_id(unit) is None
        (unit / TEMPLATE_MANIFEST).write_text(json.dumps({"UniqueName": "  "}))
        assert template_id(unit) is None
    print("✓ template_id")


def test_sync_adds_and_removes_managed() -> None:
    result, state = sync_enabled([], ["A"], {})
    assert result == ["A"]
    assert state["managed"] == ["A"]
    result, state = sync_enabled(result, [], state)
    assert result == []
    assert state["managed"] == []
    print("✓ sync adds and removes managed ids")


def test_sync_keeps_unmanaged_order() -> None:
    result, _state = sync_enabled(["X", "Y"], ["A"], {})
    assert result == ["X", "Y", "A"]
    print("✓ sync keeps unmanaged entries in order")


def test_sync_user_owned_survives_disable() -> None:
    result, state = sync_enabled(["toybox"], ["ToyBox"], {})
    assert result == ["toybox"]
    assert [v.casefold() for v in state["user_owned"]] == ["toybox"]
    assert state["managed"] == []
    result, state = sync_enabled(result, [], state)
    assert result == ["toybox"]
    print("✓ user-added entry survives disable")


def test_apply_preserves_other_keys() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        settings = Path(tmp) / "settings.json"
        settings.write_text(json.dumps(
            {"$id": "1", "SourceDirectories": [], "EnabledModifications": []}))
        logs: list[str] = []
        assert apply_settings_sync(settings, ["A"], Path(tmp) / "state.json", logs.append)
        data = json.loads(settings.read_text())
        assert data == {"$id": "1", "SourceDirectories": [], "EnabledModifications": ["A"]}
    print("✓ apply preserves other keys")


def test_apply_invalid_json_untouched() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        settings = Path(tmp) / "settings.json"
        settings.write_bytes(b"{not json")
        logs: list[str] = []
        assert not apply_settings_sync(settings, ["A"], Path(tmp) / "state.json", logs.append)
        assert settings.read_bytes() == b"{not json"
        assert any(str(settings) in line for line in logs)
        settings.write_text(json.dumps({"EnabledModifications": "oops"}))
        before = settings.read_bytes()
        assert not apply_settings_sync(settings, ["A"], Path(tmp) / "state.json", logs.append)
        assert settings.read_bytes() == before
    print("✓ apply leaves invalid settings untouched")


def test_apply_creates_missing_file() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        settings = Path(tmp) / "LocalLow" / "settings.json"
        state = Path(tmp) / "state.json"
        assert apply_settings_sync(settings, ["A"], state, lambda _m: None)
        assert json.loads(settings.read_text()) == {"EnabledModifications": ["A"]}
        assert json.loads(state.read_text())["managed"] == ["A"]
    print("✓ apply creates missing settings file")


def _games():
    from Games.Owlcat.pathfinder_kingmaker import PathfinderKingmaker
    from Games.Owlcat.pathfinder_wotr import PathfinderWotR
    from Games.Owlcat.rogue_trader import RogueTrader
    return PathfinderKingmaker(), PathfinderWotR(), RogueTrader()


def _point(game, root: Path, *, prefix: bool = True) -> None:
    game._game_path = root / "game"
    game._game_path.mkdir(parents=True, exist_ok=True)
    if prefix:
        game._prefix_path = root / "pfx"
        (game._prefix_path / "drive_c" / "users" / "steamuser").mkdir(
            parents=True, exist_ok=True)
    else:
        game._prefix_path = None


def test_identity() -> None:
    km, wotr, rt = _games()
    assert (km.game_id, km.exe_name, km.steam_id, km.nexus_game_domain) == (
        "pathfinder_kingmaker", "Kingmaker.exe", "640820", "pathfinderkingmaker")
    assert (wotr.game_id, wotr.exe_name, wotr.steam_id, wotr.nexus_game_domain) == (
        "pathfinder_wotr", "Wrath.exe", "1184370", "pathfinderwrathoftherighteous")
    assert (rt.game_id, rt.exe_name, rt.steam_id, rt.nexus_game_domain) == (
        "rogue_trader", "WH40KRT.exe", "2186680", "warhammer40kroguetrader")
    print("✓ identity")


def test_data_paths() -> None:
    _km, wotr, rt = _games()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _point(wotr, root)
        _point(rt, root)
        assert wotr.get_mod_data_path() == root / "game" / "Mods"
        assert rt.get_mod_data_path() == (
            root / "pfx" / "drive_c/users/steamuser/AppData/LocalLow/Owlcat Games"
            / _RT_DIR / "UnityModManager")
        _point(rt, root, prefix=False)
        assert rt.get_mod_data_path() is None
    print("✓ data paths")


def test_routing_rules() -> None:
    from Utils.deployment.custom_rules import compute_routed_destinations
    km, wotr, rt = _games()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for game in (km, wotr, rt):
            _point(game, root)
        assert km.custom_routing_rules == []
        paths = ["Modifications/B/OwlcatModificationManifest.json",
                 "Modifications/B/Assemblies/b.dll", "A/Info.json", "A/a.dll"]
        for game, base in ((wotr, "Pathfinder Wrath Of The Righteous"), (rt, _RT_DIR)):
            claims = compute_routed_destinations(paths, game.custom_routing_rules)
            dest = (f"drive_c/users/steamuser/AppData/LocalLow/Owlcat Games/{base}"
                    "/Modifications/B/Assemblies/b.dll").lower()
            got = [(to_prefix, d.lower())
                   for to_prefix, d in claims["modifications/b/assemblies/b.dll"]]
            assert got == [(True, dest)], got
            assert "a/info.json" not in claims and "a/a.dll" not in claims
        _point(rt, root, prefix=False)
        assert rt.custom_routing_rules == []
    print("✓ routing rules")


def test_prepare_staging_restructures() -> None:
    _km, wotr, _rt = _games()
    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp) / "staging"
        flat_umm = staging / "FlatUMM"
        flat_umm.mkdir(parents=True)
        (flat_umm / UMM_MANIFEST).write_text("{}")
        (flat_umm / "Mod.dll").write_text("")
        flat_tmpl = staging / "FlatTmpl"
        flat_tmpl.mkdir()
        (flat_tmpl / TEMPLATE_MANIFEST).write_text("{}")
        nested = staging / "Nested" / "Inner"
        nested.mkdir(parents=True)
        (nested / TEMPLATE_MANIFEST).write_text("{}")
        dual = staging / "Dual" / "Dual"
        dual.mkdir(parents=True)
        (dual / UMM_MANIFEST).write_text("{}")
        (dual / TEMPLATE_MANIFEST).write_text("{}")
        assert wotr.prepare_mod_staging(staging, lambda _m: None)
        assert (flat_umm / "FlatUMM" / UMM_MANIFEST).is_file()
        assert (flat_umm / "FlatUMM" / "Mod.dll").is_file()
        assert (flat_tmpl / "Modifications" / "FlatTmpl" / TEMPLATE_MANIFEST).is_file()
        assert (staging / "Nested" / "Modifications" / "Inner" / TEMPLATE_MANIFEST).is_file()
        assert (dual / UMM_MANIFEST).is_file()
        assert not wotr.prepare_mod_staging(staging, lambda _m: None)
    print("✓ staging restructure")


def test_rt_excludes_bundled_umm() -> None:
    km, wotr, rt = _games()
    assert {"winhttp.dll", "doorstop_config.ini"} <= rt.excluded_loose_filenames
    assert "winhttp.dll" not in wotr.excluded_loose_filenames
    assert rt.wine_dll_overrides == {}
    assert wotr.wine_dll_overrides == {"winhttp": "native,builtin"}
    assert km.wine_dll_overrides == {"winhttp": "native,builtin"}
    print("✓ Rogue Trader excludes bundled UMM")


def test_umm_detection() -> None:
    _km, wotr, rt = _games()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _point(wotr, root)
        assert "Unity Mod Manager" in wotr.frameworks
        assert wotr.framework_installed("Unity Mod Manager") is False
        marker = root / "game" / "Wrath_Data" / "Managed" / "UnityModManager"
        marker.mkdir(parents=True)
        (marker / "UnityModManager.dll").write_text("")
        assert wotr.framework_installed("Unity Mod Manager") is True
        assert rt.frameworks == {}
    print("✓ UMM detection")


def test_discovered() -> None:
    from Utils.games import discovery
    games = discovery.discover_games()
    for name in ("Pathfinder: Kingmaker", "Pathfinder: Wrath of the Righteous",
                 "Warhammer 40,000: Rogue Trader"):
        assert name in games, name
    failures = [f for f in discovery.get_load_failures() if "Owlcat" in str(f)]
    assert not failures, failures
    print("✓ discovered")


def _profile(game, root: Path, lines: list[str]) -> Path:
    staging = root / "Profiles" / "mods"
    profile = root / "Profiles" / "profiles" / "default"
    profile.mkdir(parents=True, exist_ok=True)
    (profile / "modlist.txt").write_text("\n".join(lines) + "\n")
    game._staging_path = root / "Profiles"  # custom staging root holds mods/ + profiles/
    game._active_profile_dir = profile
    staging.mkdir(parents=True, exist_ok=True)
    return staging


def test_post_deploy_syncs_enabled_templates() -> None:
    _km, wotr, _rt = _games()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _point(wotr, root)
        staging = _profile(wotr, root, ["+TmplMod", "-OffMod", "+UmmMod"])
        unit = staging / "TmplMod" / "Modifications" / "Tm"
        unit.mkdir(parents=True)
        (unit / TEMPLATE_MANIFEST).write_text(json.dumps({"UniqueName": "TmplUnique"}))
        off = staging / "OffMod" / "Modifications" / "Off"
        off.mkdir(parents=True)
        (off / TEMPLATE_MANIFEST).write_text(json.dumps({"UniqueName": "OffUnique"}))
        umm = staging / "UmmMod" / "Umm"
        umm.mkdir(parents=True)
        (umm / UMM_MANIFEST).write_text("{}")
        wotr.pop_deploy_warnings()
        wotr.post_deploy(lambda _m: None)
        settings = (wotr.locallow_root() / "OwlcatModificationManagerSettings.json")
        assert json.loads(settings.read_text())["EnabledModifications"] == ["TmplUnique"]
        warnings = wotr.pop_deploy_warnings()
        assert any("Unity Mod Manager isn't installed" in w for w in warnings), warnings
        wotr.post_clean_game_folder(lambda _m: None)
        assert json.loads(settings.read_text())["EnabledModifications"] == []
    print("✓ post_deploy syncs enabled Template mods")


def test_post_deploy_without_prefix() -> None:
    _km, wotr, _rt = _games()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _point(wotr, root, prefix=False)
        _profile(wotr, root, [])
        logs: list[str] = []
        wotr.post_deploy(logs.append)
        assert any("launch the game once" in line for line in logs), logs
    print("✓ post_deploy without prefix")


def test_restore_whitelist_protects_umm() -> None:
    _km, wotr, rt = _games()
    match = wotr.restore_whitelist_matcher()
    assert match is not None
    for rel in ("winhttp.dll", "doorstop_config.ini",
                "wrath_data/managed/unitymodmanager/unitymodmanager.dll"):
        assert match(rel), rel
    assert not match("wrath_data/managed/assembly-csharp.dll")
    assert rt.restore_whitelist_matcher() is None
    print("✓ restore whitelist protects UMM")


def test_no_prefix_keeps_templates_out_of_mods() -> None:
    _km, wotr, _rt = _games()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _point(wotr, root, prefix=False)
        assert "modifications" in wotr.filemap_exclude_dirs
        _point(wotr, root)
        assert "modifications" not in wotr.filemap_exclude_dirs
    print("✓ no prefix keeps Template mods out of Mods/")


def test_sync_reclaims_user_removed_entry() -> None:
    _result, state = sync_enabled(["Foo"], ["Foo"], {})
    assert [v.casefold() for v in state["user_owned"]] == ["foo"]
    result, state = sync_enabled([], ["Foo"], state)  # user removed it by hand
    assert result == ["Foo"] and state["managed"] == ["Foo"]
    result, state = sync_enabled(result, [], state)
    assert result == []
    print("✓ user-removed entry becomes managed again")


def test_flat_umm_wraps_into_id_folder() -> None:
    _km, wotr, _rt = _games()
    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp)
        cases = {
            "ToyBox-1.6.3": ({"Id": "ToyBox"}, "ToyBox"),
            "NoId-1.0": ({"DisplayName": "x"}, "NoId-1.0"),
            "Escape-1.0": ({"Id": "../evil"}, "Escape-1.0"),
            "Slash-1.0": ({"Id": "a\\b"}, "Slash-1.0"),
            "Dot-1.0": ({"Id": ".."}, "Dot-1.0"),
        }
        for folder, (info, _expected) in cases.items():
            mod = staging / folder
            mod.mkdir()
            (mod / UMM_MANIFEST).write_bytes(b"\xef\xbb\xbf" + json.dumps(info).encode())
            (mod / "Mod.dll").write_text("")
            assert wotr.mod_staging_wrap_subdir_name(mod) == _expected, folder
        tmpl = staging / "Tmpl-2.0"
        tmpl.mkdir()
        (tmpl / TEMPLATE_MANIFEST).write_text(json.dumps({"UniqueName": "T"}))
        assert wotr.mod_staging_wrap_subdir_name(tmpl) == "Tmpl-2.0"
        assert wotr.prepare_mod_staging(staging, lambda _m: None)
        assert (staging / "ToyBox-1.6.3" / "ToyBox" / UMM_MANIFEST).is_file()
        assert (staging / "ToyBox-1.6.3" / "ToyBox" / "Mod.dll").is_file()
        assert (staging / "NoId-1.0" / "NoId-1.0" / UMM_MANIFEST).is_file()
        assert (staging / "Escape-1.0" / "Escape-1.0" / UMM_MANIFEST).is_file()
        assert (staging / "Tmpl-2.0" / "Modifications" / "Tmpl-2.0" / TEMPLATE_MANIFEST).is_file()
    print("✓ flat UMM mods wrap into their Id folder")


if __name__ == "__main__":
    test_locallow_rel_variants()
    test_classify()
    test_template_id()
    test_sync_adds_and_removes_managed()
    test_sync_keeps_unmanaged_order()
    test_sync_user_owned_survives_disable()
    test_apply_preserves_other_keys()
    test_apply_invalid_json_untouched()
    test_apply_creates_missing_file()
    test_identity()
    test_data_paths()
    test_routing_rules()
    test_prepare_staging_restructures()
    test_rt_excludes_bundled_umm()
    test_umm_detection()
    test_discovered()
    test_post_deploy_syncs_enabled_templates()
    test_post_deploy_without_prefix()
    test_restore_whitelist_protects_umm()
    test_no_prefix_keeps_templates_out_of_mods()
    test_sync_reclaims_user_removed_entry()
    test_flat_umm_wraps_into_id_folder()
    print("All Owlcat checks passed.")
