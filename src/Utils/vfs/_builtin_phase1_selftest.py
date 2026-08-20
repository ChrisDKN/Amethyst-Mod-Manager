"""Focused profile-VFS checks for the straightforward built-in handlers.

Run directly from the repository root::

    PYTHONPATH=src python3 -m Utils.vfs._builtin_phase1_selftest
"""

from __future__ import annotations

import importlib.util
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from Utils.deploy import LinkMode
from Utils.vfs import (
    ProfileVFSGameMixin,
    has_deployment_state,
    manifest_path,
    pending_path,
)


SRC = Path(__file__).resolve().parents[2]


def _load_module(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, SRC / relative)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load test module: {relative}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


STS = _load_module(
    "amm_vfs_test_slay2", "Games/Slay The Spire 2/slay_the_spire_2.py")
RDR = _load_module(
    "amm_vfs_test_rdr2",
    "Games/Red Dead Redemption 2/red_dead_redemption_2.py",
)
BANNERLORD = _load_module(
    "amm_vfs_test_bannerlord",
    "Games/Mount & Blade II Bannerlord/mount_and_blade_2_bannerlord.py",
)
KCD = _load_module(
    "amm_vfs_test_kcd",
    "Games/Kingdom Come Deliverance II/kingdom_come_deliverance_2.py",
)

from Games.RE_Engine_LooseLoading.monster_hunter_rise import (  # noqa: E402
    MonsterHunterRise,
)
from Games.RE_Engine_LooseLoading.monster_hunter_wilds import (  # noqa: E402
    MonsterHunterWilds,
)
from Games.RE_Engine_LooseLoading.pragmata import Pragmata  # noqa: E402
from Games.RE_Engine_LooseLoading.resident_evil_requiem import (  # noqa: E402
    ResidentEvilRequiem,
)


class _FixtureMixin:
    """Keep handler state wholly inside one temporary test directory."""

    def __init__(self, root: Path):
        self._game_path = root / "game"
        self._prefix_path = None
        self._deploy_mode = LinkMode.HARDLINK
        self._staging_path = root / "manager"
        self._settings = {"vfs_enabled": True, "case_alias_links": False}
        self._game_path.mkdir(parents=True)
        self._staging_path.mkdir(parents=True)
        self.profile = self._staging_path / "profiles" / "test"
        self.profile.mkdir(parents=True)
        self.set_active_profile_dir(self.profile)
        (self.profile / "modlist.txt").write_text("+ExampleMod\n", encoding="utf-8")

    def _load_settings(self) -> dict:
        return dict(self._settings)

    def _save_settings(self, data: dict) -> None:
        self._settings = dict(data)

    def save_paths(self) -> None:
        return None


def _fixture(base):
    return type(f"VFS{base.__name__}Fixture", (_FixtureMixin, base), {})


def _write_payload(game, entries: dict[str, str]) -> None:
    staging_mod = game.get_effective_mod_staging_path() / "ExampleMod"
    for relative, content in entries.items():
        target = staging_mod / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    filemap = game.get_effective_filemap_path()
    filemap.parent.mkdir(parents=True, exist_ok=True)
    filemap.write_text(
        "".join(f"{relative}\tExampleMod\n" for relative in entries),
        encoding="utf-8",
    )


def _deploy_and_view(game) -> Path:
    with patch("Utils.vfs.overlay._bubblewrap_status", return_value=(False, "test")):
        game.deploy(profile="test", mode=LinkMode.HARDLINK)
    payload = json.loads(manifest_path(game, "test").read_text(encoding="utf-8"))
    return Path(payload["view_root"])


def _assert_contract(base) -> None:
    assert issubclass(base, ProfileVFSGameMixin)
    assert "vfs_enabled" in base.profile_overridable_settings


def test_standard_handlers() -> None:
    cases = (
        (STS.SlayTheSpire2, "mods", {"Example.txt": "slay"}),
        (RDR.RedDeadRedemption2, "lml", {
            "example/file.ymt": "rdr-data",
            "dinput8.dll": "rdr-root",
        }),
        (BANNERLORD.MountAndBlade2Bannerlord, "Modules", {
            "Example/SubModule.xml": "bannerlord",
        }),
        (KCD.KingdomComeDeliverance2, "mods", {
            "Example/mod.manifest": "kcd2",
        }),
        (KCD.KingdomComeDeliverance, "mods", {
            "Example/mod.manifest": "kcd1",
        }),
    )
    for base, data_rel, entries in cases:
        _assert_contract(base)
        with tempfile.TemporaryDirectory(prefix="amm_builtin_vfs_") as tmp:
            game = _fixture(base)(Path(tmp))
            exe = game.get_game_path() / game.exe_name
            exe.parent.mkdir(parents=True, exist_ok=True)
            exe.write_text("vanilla executable", encoding="utf-8")
            _write_payload(game, entries)

            view = _deploy_and_view(game)
            for relative, content in entries.items():
                expected = (view / relative
                            if relative == "dinput8.dll"
                            else view / data_rel / relative)
                assert expected.read_text(encoding="utf-8") == content
                real = (game.get_game_path() / relative
                        if relative == "dinput8.dll"
                        else game.get_game_path() / data_rel / relative)
                assert not real.exists()

            game.restore()
            assert not has_deployment_state(game)
            assert not view.exists()

    assert STS.SlayTheSpire2.vfs_direct_shadow_launch
    print("✓ Slay 2, RDR2, Bannerlord and KCD1/2 standard VFS contracts")


def test_standard_physical_vfs_coexistence() -> None:
    with tempfile.TemporaryDirectory(prefix="amm_builtin_vfs_coexist_") as tmp:
        game = _fixture(BANNERLORD.MountAndBlade2Bannerlord)(Path(tmp))
        exe = game.get_game_path() / game.exe_name
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_text("exe", encoding="utf-8")
        modules = game.get_mod_data_path()
        modules.mkdir(parents=True)
        (modules / "vanilla.txt").write_text("vanilla", encoding="utf-8")
        _write_payload(game, {"Example/SubModule.xml": "virtual"})
        view = _deploy_and_view(game)

        core = modules.with_name("Modules_Core")
        modules.rename(core)
        modules.mkdir()
        (modules / "physical.txt").write_text("physical", encoding="utf-8")

        game.restore()
        assert not view.exists()
        assert (modules / "vanilla.txt").read_text(encoding="utf-8") == "vanilla"
        assert not (modules / "physical.txt").exists()
        assert not core.exists()

    print("✓ standard physical + VFS coexistence restore")


def test_pending_restore() -> None:
    with tempfile.TemporaryDirectory(prefix="amm_builtin_vfs_pending_") as tmp:
        game = _fixture(STS.SlayTheSpire2)(Path(tmp))
        pending = pending_path(game, "test")
        pending.parent.mkdir(parents=True)
        pending.write_text("test\n", encoding="utf-8")

        game.restore()
        assert not has_deployment_state(game)
        assert not pending.exists()

    print("✓ interrupted built-in VFS deploy is recoverable")


def test_re_loose_loading_family() -> None:
    for subclass in (MonsterHunterRise, MonsterHunterWilds, Pragmata):
        _assert_contract(subclass)
        assert issubclass(subclass, ResidentEvilRequiem)

    with tempfile.TemporaryDirectory(prefix="amm_re_loose_vfs_") as tmp:
        game = _fixture(ResidentEvilRequiem)(Path(tmp))
        exe = game.get_game_path() / game.exe_name
        exe.write_text("exe", encoding="utf-8")
        entries = {
            "natives/STM/example.bin": "loose",
            "dinput8.dll": "loader",
            "Example.pak": "pak",
            "Example.lua": "lua",
        }
        _write_payload(game, entries)
        view = _deploy_and_view(game)

        assert (view / "natives/STM/example.bin").read_text() == "loose"
        assert (view / "dinput8.dll").read_text() == "loader"
        assert (view / "pak_mods/Example.pak").read_text() == "pak"
        assert (view / "reframework/autorun/Example.lua").read_text() == "lua"
        assert not (game.get_game_path() / "natives/STM/example.bin").exists()

        # Simulate a physical root deploy left beside the private profile view.
        game._settings["vfs_enabled"] = False
        game.deploy(profile="test", mode=LinkMode.HARDLINK)
        assert (game.get_game_path() / "natives/STM/example.bin").is_file()
        game._settings["vfs_enabled"] = True

        game.restore()
        assert not view.exists()
        assert not has_deployment_state(game)
        assert not (game.get_game_path() / "natives/STM/example.bin").exists()

    print("✓ RE loose-loading root VFS routing and coexistence restore")


def main() -> None:
    test_standard_handlers()
    test_standard_physical_vfs_coexistence()
    test_pending_restore()
    test_re_loose_loading_family()
    print("All straightforward built-in VFS checks passed.")


if __name__ == "__main__":
    main()
