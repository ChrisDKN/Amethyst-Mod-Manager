"""Focused profile-VFS checks for the straightforward built-in handlers.

Run directly from the repository root::

    PYTHONPATH=src python3 -m Utils.vfs._builtin_phase1_selftest
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import struct
import tempfile
from pathlib import Path
from unittest.mock import patch

from Utils.deployment import LinkMode
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
from Games.RE_Engine_Invalidation.resident_evil_2 import (  # noqa: E402
    ResidentEvil2,
)
from Games.RE_Engine_Invalidation.resident_evil_village import (  # noqa: E402
    ResidentEvilVillage,
)
from Utils.re_engine.pak import (  # noqa: E402
    hash_filepath,
    patch_pak_file as _real_patch_pak_file,
)


class _FixtureMixin:
    """Keep handler state wholly inside one temporary test directory."""

    def __init__(self, root: Path):
        self._game_path = root / "game"
        self._prefix_path = None
        self._deploy_mode = LinkMode.HARDLINK
        self._staging_path = root / "manager"
        self._beta_branch_cache = None
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


def _write_test_re_pak(path: Path, relative_paths: list[str]) -> list[bytes]:
    """Create a minimal v4 RE PAK entry table for invalidation tests."""
    originals: list[bytes] = []
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        handle.write(struct.pack(
            "<IBBHII", 0x414B504B, 4, 0, 0, len(relative_paths), 0))
        for relative in relative_paths:
            hashes = hash_filepath(relative)
            original = struct.pack("<II", *hashes)
            originals.append(original)
            handle.write(original + (b"\x00" * 40))
    return originals


def _pak_hash_bytes(path: Path, index: int = 0) -> bytes:
    with path.open("rb") as handle:
        handle.seek(16 + index * 48)
        return handle.read(8)


def _converted_tex(_source: Path, target: Path, **_kwargs) -> bool:
    target.write_bytes(b"converted-tex34")
    return True


def _copy_materialized_base(src: str, dst: str, _mode):
    """Exercise the VFS hardlink/symlink fallback-to-copy path."""
    shutil.copy2(src, dst)
    return LinkMode.COPY, None


def test_re_invalidation_hybrid_vfs() -> None:
    """Loose files stay private while physical PAK hashes remain journaled."""
    _assert_contract(ResidentEvilVillage)
    _assert_contract(ResidentEvil2)
    with tempfile.TemporaryDirectory(prefix="amm_re_invalidation_vfs_") as tmp:
        game = _fixture(ResidentEvil2)(Path(tmp))
        game._settings["case_alias_links"] = False
        (game.get_game_path() / game.exe_name).write_text(
            "exe", encoding="utf-8")
        final_relative = "natives/stm/example.tex.34"
        overwrite_relative = "natives/stm/override.tex.34"
        pak = game.get_game_path() / "re_chunk_000.pak"
        originals = _write_test_re_pak(
            pak, [final_relative, overwrite_relative])
        _write_payload(game, {
            "natives/x64/example.tex.10": "source-tex10",
        })
        overwrite = (
            game.get_effective_mod_staging_path().parent / "overwrite"
            / "natives" / "x64" / "override.tex.10"
        )
        overwrite.parent.mkdir(parents=True)
        overwrite.write_text("overwrite-tex10", encoding="utf-8")
        with game.get_effective_filemap_path().open(
            "a", encoding="utf-8",
        ) as handle:
            handle.write("natives/x64/override.tex.10\t[Overwrite]\n")
        logs: list[str] = []

        with (
            patch("Utils.vfs.overlay._bubblewrap_status",
                  return_value=(False, "test")),
            patch(
                "Games.RE_Engine_Invalidation.resident_evil_village."
                "tex_needs_conversion",
                return_value=True,
            ),
            patch(
                "Games.RE_Engine_Invalidation.resident_evil_village."
                "convert_tex_v10_to_v34",
                side_effect=_converted_tex,
            ),
            patch(
                "Utils.vfs.overlay._do_link_ex",
                side_effect=_copy_materialized_base,
            ),
        ):
            game.deploy(
                profile="test", mode=LinkMode.HARDLINK,
                log_fn=logs.append)

        payload = json.loads(
            manifest_path(game, "test").read_text(encoding="utf-8"))
        view = Path(payload["view_root"])
        private_tex = view / "natives" / "STM" / "example.tex.34"
        assert private_tex.read_bytes() == b"converted-tex34"
        assert (view / "natives" / "STM" / "override.tex.34").read_bytes() \
            == b"converted-tex34"
        assert not (view / "natives" / "x64" / "override.tex.10").exists()
        assert not (game.get_game_path() / "natives").exists()
        assert _pak_hash_bytes(pak) == b"\x00" * 8
        assert _pak_hash_bytes(pak, 1) == b"\x00" * 8
        view_pak = view / pak.name
        assert view_pak.stat().st_ino != pak.stat().st_ino
        assert _pak_hash_bytes(view_pak) == b"\x00" * 8
        assert _pak_hash_bytes(view_pak, 1) == b"\x00" * 8
        assert (game.profile / "pak_patches"
                / "re_chunk_000.pak.json").is_file()
        assert (game.get_game_path() / ".mm_pak_restore.json").is_file()
        assert any("required PAK invalidation is physical" in line
                   for line in logs)

        # Restore follows published state rather than the current setting.
        game._settings["vfs_enabled"] = False
        game.restore(log_fn=logs.append)
        assert not view.exists()
        assert not has_deployment_state(game)
        assert _pak_hash_bytes(pak) == originals[0]
        assert _pak_hash_bytes(pak, 1) == originals[1]
        assert not (game.profile / "pak_patches").exists()
        assert (game.get_game_path() / ".mm_pak_restore.json").is_file()
        assert not list(game.profile.glob("mm_tex_*"))

    print("✓ RE invalidation uses private loose files plus physical PAK journal")


def test_re_invalidation_physical_mode_unchanged() -> None:
    with tempfile.TemporaryDirectory(prefix="amm_re_invalidation_physical_") as tmp:
        game = _fixture(ResidentEvil2)(Path(tmp))
        game._settings["vfs_enabled"] = False
        (game.get_game_path() / game.exe_name).write_text(
            "exe", encoding="utf-8")
        final_relative = "natives/stm/example.tex.34"
        pak = game.get_game_path() / "re_chunk_000.pak"
        original = _write_test_re_pak(pak, [final_relative])[0]
        _write_payload(game, {
            "natives/x64/example.tex.10": "source-tex10",
        })
        with (
            patch(
                "Games.RE_Engine_Invalidation.resident_evil_village."
                "tex_needs_conversion",
                return_value=True,
            ),
            patch(
                "Games.RE_Engine_Invalidation.resident_evil_village."
                "convert_tex_v10_to_v34",
                side_effect=_converted_tex,
            ),
        ):
            game.deploy(profile="test", mode=LinkMode.HARDLINK)

        physical_tex = (
            game.get_game_path() / "natives" / "STM" / "example.tex.34")
        assert physical_tex.read_bytes() == b"converted-tex34"
        assert _pak_hash_bytes(pak) == b"\x00" * 8
        game.restore()
        assert not physical_tex.exists()
        assert _pak_hash_bytes(pak) == original

    print("✓ RE invalidation physical deployment behavior is unchanged")


def test_re_invalidation_failed_patch_rolls_back_hybrid_view() -> None:
    with tempfile.TemporaryDirectory(prefix="amm_re_invalidation_failure_") as tmp:
        game = _fixture(ResidentEvilVillage)(Path(tmp))
        (game.get_game_path() / game.exe_name).write_text(
            "exe", encoding="utf-8")
        relative = "natives/stm/failure.bin"
        patch_pak = (
            game.get_game_path() / "re_chunk_000.pak.patch_001.pak")
        main_pak = game.get_game_path() / "re_chunk_000.pak"
        patch_original = _write_test_re_pak(patch_pak, [relative])[0]
        main_original = _write_test_re_pak(main_pak, [relative])[0]
        _write_payload(game, {relative: "loose"})

        calls = [0]

        def _fail_second(pak_path, hashes, backup_path, log_fn=None):
            calls[0] += 1
            if calls[0] == 1:
                return _real_patch_pak_file(
                    pak_path, hashes, backup_path, log_fn=log_fn)
            raise OSError("injected second-PAK failure")

        with (
            patch("Utils.vfs.overlay._bubblewrap_status",
                  return_value=(False, "test")),
            patch(
                "Games.RE_Engine_Invalidation.resident_evil_village."
                "patch_pak_file",
                side_effect=_fail_second,
            ),
        ):
            try:
                game.deploy(profile="test", mode=LinkMode.HARDLINK)
            except OSError as exc:
                assert "second-PAK failure" in str(exc)
            else:
                raise AssertionError("injected PAK failure was ignored")

        assert _pak_hash_bytes(patch_pak) == patch_original
        assert _pak_hash_bytes(main_pak) == main_original
        assert not has_deployment_state(game)
        assert not manifest_path(game, "test").exists()
        assert not (game.get_game_path() / "natives").exists()
        assert not (game.profile / "pak_patches").exists()

    print("✓ failed hybrid PAK patch restores PAKs and unpublishes loose view")


def main() -> None:
    test_standard_handlers()
    test_standard_physical_vfs_coexistence()
    test_pending_restore()
    test_re_loose_loading_family()
    test_re_invalidation_hybrid_vfs()
    test_re_invalidation_physical_mode_unchanged()
    test_re_invalidation_failed_patch_rolls_back_hybrid_view()
    print("All straightforward built-in VFS checks passed.")


if __name__ == "__main__":
    main()
