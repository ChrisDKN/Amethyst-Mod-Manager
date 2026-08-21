"""Hermetic checks for Daggerfall Unity's profile VFS integration.

Run from the repository root::

    PYTHONPATH=src python3 -m Utils.vfs._daggerfall_selftest
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import tempfile
from pathlib import Path
from unittest.mock import patch

from Utils.deploy import LinkMode
from Utils.vfs import has_deployment_state, manifest_path, pending_path


SRC = Path(__file__).resolve().parents[2]


def _load_handler():
    path = SRC / "Games" / "Daggerfall Unity" / "daggerfall_unity.py"
    spec = importlib.util.spec_from_file_location("amm_vfs_test_dfu", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load Daggerfall Unity handler: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


DFU = _load_handler()


class DaggerfallFixture(DFU.DaggerfallUnity):
    """Keep handler settings and paths wholly inside a temporary directory."""

    def __init__(self, root: Path, *, vfs: bool = True):
        self._game_path = root / "game"
        self._prefix_path = None
        self._launch_binary_path = self._game_path / "RenamedPlayer.x86_64"
        self._deploy_mode = LinkMode.HARDLINK
        self._staging_path = root / "manager"
        self._settings = {
            "vfs_enabled": vfs,
            "case_alias_links": False,
            "manage_load_order_in_dfu": False,
        }
        self._game_path.mkdir(parents=True)
        self._staging_path.mkdir(parents=True)
        self.profile = self._staging_path / "profiles" / "test"
        self.profile.mkdir(parents=True)
        self.set_active_profile_dir(self.profile)
        (self.profile / "modlist.txt").write_text(
            "+ExampleMod\n", encoding="utf-8")

    def _load_settings(self) -> dict:
        return dict(self._settings)

    def _save_settings(self, data: dict) -> None:
        self._settings = dict(data)

    def save_paths(self) -> None:
        return None


def _prepare_game(game: DaggerfallFixture, *, portable: bool = True) -> None:
    exe = game.get_game_path() / "RenamedPlayer.x86_64"
    exe.write_text("native player", encoding="utf-8")
    exe.chmod(0o644)

    streaming = game.get_mod_data_path()
    streaming.mkdir(parents=True)
    (streaming / "Vanilla.txt").write_text("vanilla", encoding="utf-8")

    if portable:
        (game.get_game_path() / "Portable.txt").write_text("", encoding="utf-8")
        game_data = (game.get_game_path() / "PortableAppdata" / "Mods" /
                     "GameData")
        game_data.mkdir(parents=True)
        (game_data / "Mods.json").write_text(
            json.dumps([{
                "FileName": "Manual",
                "Title": "Manual Mod",
                "Enabled": True,
                "LoadPriority": 0,
            }]),
            encoding="utf-8",
        )
        physical_settings = game_data / "PhysicalGuid" / "modsettings.json"
        physical_settings.parent.mkdir()
        physical_settings.write_text("physical-setting", encoding="utf-8")
        existing_save = (game.get_game_path() / "PortableAppdata" / "Saves" /
                         "slot" / "existing.sav")
        existing_save.parent.mkdir(parents=True)
        existing_save.write_text("physical-save", encoding="utf-8")


def _prepare_mod(game: DaggerfallFixture, *, include_stash: bool = True) -> None:
    mod = game.get_effective_mod_staging_path() / "ExampleMod" / "Mods"
    mod.mkdir(parents=True)
    (mod / "Example.dfmod").write_bytes(b"test bundle")
    (mod / "Example.dfmod.json").write_text(
        json.dumps({"ModTitle": "Example Mod"}), encoding="utf-8")

    lines = [
        "Mods/Example.dfmod\tExampleMod\n",
        "Mods/Example.dfmod.json\tExampleMod\n",
    ]
    if include_stash:
        stash_file = (game.get_effective_overwrite_path() /
                      "DFU_ModSettings" / "GameData" / "StashedGuid" /
                      "modsettings.json")
        stash_file.parent.mkdir(parents=True)
        stash_file.write_text("stashed-setting", encoding="utf-8")
        lines.append(
            "DFU_ModSettings/GameData/StashedGuid/modsettings.json"
            "\t[Overwrite]\n"
        )
    game.get_effective_filemap_path().write_text("".join(lines), encoding="utf-8")


def _deploy(game: DaggerfallFixture) -> Path:
    with patch("Utils.vfs.overlay._bubblewrap_status",
               return_value=(False, "test")):
        game.deploy(profile="test", mode=LinkMode.HARDLINK)
    payload = json.loads(manifest_path(game, "test").read_text(encoding="utf-8"))
    return Path(payload["view_root"])


def test_private_portable_deploy_launch_and_restore() -> None:
    with tempfile.TemporaryDirectory(prefix="amm_dfu_vfs_") as tmp:
        game = DaggerfallFixture(Path(tmp))
        _prepare_game(game)
        _prepare_mod(game)

        physical_exe = game.get_launch_binary_path()
        physical_mods_json = (game.get_game_path() / "PortableAppdata" /
                              "Mods" / "GameData" / "Mods.json")
        original_mods_json = physical_mods_json.read_text(encoding="utf-8")
        physical_setting = (physical_mods_json.parent / "PhysicalGuid" /
                            "modsettings.json")

        view = _deploy(game)
        view_streaming = view / "DaggerfallUnity_Data" / "StreamingAssets"
        view_mods_json = (view / "PortableAppdata" / "Mods" / "GameData" /
                          "Mods.json")
        view_setting = view_mods_json.parent / "StashedGuid" / "modsettings.json"

        assert (view_streaming / "Mods" / "Example.dfmod").is_file()
        assert not (game.get_mod_data_path() / "Mods" / "Example.dfmod").exists()
        assert not (view_streaming / "DFU_ModSettings").exists()
        assert game._ordered_dfmods("test") == [
            view_streaming / "Mods" / "Example.dfmod"]

        generated = json.loads(view_mods_json.read_text(encoding="utf-8"))
        assert any(entry.get("Title") == "Example Mod" for entry in generated)
        assert physical_mods_json.read_text(encoding="utf-8") == original_mods_json
        assert physical_setting.read_text(encoding="utf-8") == "physical-setting"

        launch = game.get_vfs_launch_exe()
        assert launch == view / "RenamedPlayer.x86_64"
        assert launch.stat().st_mode & stat.S_IXUSR
        assert not physical_exe.stat().st_mode & stat.S_IXUSR
        wrapped = game.wrap_launch_command([str(launch)])
        assert str(view) in wrapped
        assert str(launch) in wrapped
        assert game.get_launch_handoff("test") is None
        assert game.get_steam_launch_string("test") == ""
        try:
            game.get_vfs_passthrough_command([str(physical_exe)])
        except RuntimeError as exc:
            assert "Play button" in str(exc)
        else:
            raise AssertionError("DFU unexpectedly accepted a storefront command")

        view_setting.write_text("changed-in-game", encoding="utf-8")
        extracted = (view / "PortableAppdata" / "Mods" / "ExtractedFiles" /
                     "Example Mod" / "asset.bin")
        extracted.parent.mkdir(parents=True)
        extracted.write_text("runtime", encoding="utf-8")
        existing_save = (view / "PortableAppdata" / "Saves" / "slot" /
                         "existing.sav")
        existing_save.write_text("changed-save", encoding="utf-8")
        save = view / "PortableAppdata" / "Saves" / "slot" / "save.sav"
        save.parent.mkdir(parents=True, exist_ok=True)
        save.write_text("save", encoding="utf-8")

        # Restore must follow deployed state even after the option was toggled.
        game._settings["vfs_enabled"] = False
        game.restore()
        assert not has_deployment_state(game)
        assert not view.exists()
        assert physical_mods_json.read_text(encoding="utf-8") == original_mods_json
        assert physical_setting.read_text(encoding="utf-8") == "physical-setting"
        assert (game.get_game_path() / "PortableAppdata" / "Saves" / "slot" /
                "existing.sav").read_text() == "physical-save"
        stash = game.get_effective_overwrite_path() / "DFU_ModSettings"
        assert (stash / "GameData" / "StashedGuid" /
                "modsettings.json").read_text() == "changed-in-game"
        assert (stash / "ExtractedFiles" / "Example Mod" /
                "asset.bin").read_text() == "runtime"
        root_upper = manifest_path(game, "test").parent / "root-upper"
        assert (root_upper / "PortableAppdata" / "Saves" / "slot" /
                "existing.sav").read_text() == "changed-save"
        assert (root_upper / "PortableAppdata" / "Saves" / "slot" /
                "save.sav").read_text() == "save"

    print("✓ DFU portable metadata, configured native launch and restore isolation")


def test_redeploy_preserves_runtime_state() -> None:
    with tempfile.TemporaryDirectory(prefix="amm_dfu_vfs_redeploy_") as tmp:
        game = DaggerfallFixture(Path(tmp))
        _prepare_game(game)
        _prepare_mod(game)
        first_view = _deploy(game)

        created = (first_view / "PortableAppdata" / "Mods" / "GameData" /
                   "NewGuid" / "modsettings.json")
        created.parent.mkdir(parents=True)
        created.write_text("created-at-runtime", encoding="utf-8")
        existing_save = (first_view / "PortableAppdata" / "Saves" / "slot" /
                         "existing.sav")
        existing_save.write_text("changed-before-redeploy", encoding="utf-8")

        second_view = _deploy(game)
        assert second_view == first_view
        restored = (second_view / "PortableAppdata" / "Mods" / "GameData" /
                    "NewGuid" / "modsettings.json")
        assert restored.read_text(encoding="utf-8") == "created-at-runtime"
        assert (game.get_effective_overwrite_path() / "DFU_ModSettings" /
                "GameData" / "NewGuid" / "modsettings.json").is_file()
        assert (second_view / "PortableAppdata" / "Saves" / "slot" /
                "existing.sav").read_text() == "changed-before-redeploy"
        assert (game.get_game_path() / "PortableAppdata" / "Saves" / "slot" /
                "existing.sav").read_text() == "physical-save"
        game.restore()

    print("✓ DFU VFS-to-VFS rebuild preserves per-mod runtime state")


def test_dfu_owned_load_order_stays_profile_private() -> None:
    with tempfile.TemporaryDirectory(prefix="amm_dfu_vfs_owned_order_") as tmp:
        game = DaggerfallFixture(Path(tmp))
        game._settings["manage_load_order_in_dfu"] = True
        _prepare_game(game)
        _prepare_mod(game)
        physical = (game.get_game_path() / "PortableAppdata" / "Mods" /
                    "GameData" / "Mods.json")
        original = physical.read_text(encoding="utf-8")

        view = _deploy(game)
        private = (view / "PortableAppdata" / "Mods" / "GameData" /
                   "Mods.json")
        private.write_text('[{"Title": "Changed in DFU"}]', encoding="utf-8")
        game.restore()

        assert physical.read_text(encoding="utf-8") == original
        root_upper = manifest_path(game, "test").parent / "root-upper"
        assert json.loads((root_upper / "PortableAppdata" / "Mods" /
                           "GameData" / "Mods.json").read_text())[0][
                               "Title"] == "Changed in DFU"

    print("✓ DFU-owned portable load order persists only in profile storage")


def test_cross_filesystem_symlink_fallback_is_detached() -> None:
    with tempfile.TemporaryDirectory(prefix="amm_dfu_vfs_crossfs_") as tmp:
        game = DaggerfallFixture(Path(tmp))
        _prepare_game(game)
        _prepare_mod(game)

        def symlink_transfer(source: str, target: str, _mode):
            Path(target).parent.mkdir(parents=True, exist_ok=True)
            os.symlink(source, target)
            return LinkMode.SYMLINK, None

        with (
            patch("Utils.vfs.overlay._bubblewrap_status",
                  return_value=(False, "test")),
            patch("Utils.vfs.overlay._do_link_ex",
                  side_effect=symlink_transfer),
        ):
            game.deploy(profile="test", mode=LinkMode.HARDLINK)
        view = json.loads(manifest_path(game, "test").read_text(encoding="utf-8"))
        view = Path(view["view_root"])
        launch = game.get_vfs_launch_exe()
        existing_save = (view / "PortableAppdata" / "Saves" / "slot" /
                         "existing.sav")
        assert launch.is_file() and not launch.is_symlink()
        assert existing_save.is_file() and not existing_save.is_symlink()
        existing_save.write_text("cross-fs-change", encoding="utf-8")
        assert (game.get_game_path() / "PortableAppdata" / "Saves" / "slot" /
                "existing.sav").read_text() == "physical-save"
        game.restore()

    print("✓ DFU cross-filesystem shadow symlinks are made profile-private")


def test_pending_and_physical_coexistence_restore() -> None:
    with tempfile.TemporaryDirectory(prefix="amm_dfu_vfs_pending_") as tmp:
        game = DaggerfallFixture(Path(tmp))
        _prepare_game(game)
        _prepare_mod(game)
        marker = pending_path(game, "test")
        marker.parent.mkdir(parents=True)
        marker.write_text("test\n", encoding="utf-8")
        game.restore()
        assert not marker.exists()
        assert not has_deployment_state(game)

    with tempfile.TemporaryDirectory(prefix="amm_dfu_vfs_coexist_") as tmp:
        game = DaggerfallFixture(Path(tmp))
        _prepare_game(game)
        _prepare_mod(game)
        view = _deploy(game)

        view_setting = (view / "PortableAppdata" / "Mods" / "GameData" /
                        "StashedGuid" / "modsettings.json")
        view_setting.write_text("newer-view-setting", encoding="utf-8")
        physical_setting = (game.get_game_path() / "PortableAppdata" / "Mods" /
                            "GameData" / "StashedGuid" / "modsettings.json")
        physical_setting.parent.mkdir(parents=True, exist_ok=True)
        physical_setting.write_text("stale-physical-setting", encoding="utf-8")

        streaming = game.get_mod_data_path()
        core = streaming.with_name("StreamingAssets_Core")
        streaming.rename(core)
        streaming.mkdir()
        (streaming / "physical-mod.txt").write_text("physical", encoding="utf-8")
        game._settings["vfs_enabled"] = False
        game.restore()

        assert not view.exists()
        assert (streaming / "Vanilla.txt").read_text() == "vanilla"
        assert not (streaming / "physical-mod.txt").exists()
        assert not core.exists()
        assert (game.get_effective_overwrite_path() / "DFU_ModSettings" /
                "GameData" / "StashedGuid" /
                "modsettings.json").read_text() == "newer-view-setting"

    print("✓ DFU interrupted and coexisting physical/VFS restore paths")


def test_failed_runtime_rescue_keeps_view_recoverable() -> None:
    with tempfile.TemporaryDirectory(prefix="amm_dfu_vfs_rescue_") as tmp:
        game = DaggerfallFixture(Path(tmp))
        _prepare_game(game)
        _prepare_mod(game)
        view = _deploy(game)

        runtime = (view / "PortableAppdata" / "Mods" / "GameData" /
                   "RetryGuid" / "modsettings.json")
        runtime.parent.mkdir(parents=True)
        runtime.write_text("must-survive", encoding="utf-8")
        module = DFU._mods_json()
        with patch.object(module, "stash_mod_settings", return_value=0):
            try:
                game.restore()
            except RuntimeError as exc:
                assert "left published" in str(exc)
            else:
                raise AssertionError("DFU cleanup discarded an unstashed runtime file")

        assert has_deployment_state(game)
        assert view.is_dir()
        assert runtime.read_text() == "must-survive"
        game.restore()
        assert not has_deployment_state(game)
        assert (game.get_effective_overwrite_path() / "DFU_ModSettings" /
                "GameData" / "RetryGuid" /
                "modsettings.json").read_text() == "must-survive"

    print("✓ DFU rescue failure retains the published view for retry")


def test_physical_mode_unchanged_and_external_binary_rejected() -> None:
    with tempfile.TemporaryDirectory(prefix="amm_dfu_physical_") as tmp:
        game = DaggerfallFixture(Path(tmp), vfs=False)
        _prepare_game(game)
        _prepare_mod(game, include_stash=False)
        game.deploy(profile="test", mode=LinkMode.HARDLINK)
        assert (game.get_mod_data_path() / "Mods" / "Example.dfmod").is_file()
        assert game.get_mod_data_path().with_name("StreamingAssets_Core").is_dir()
        game.restore()
        assert (game.get_mod_data_path() / "Vanilla.txt").read_text() == "vanilla"

        external = Path(tmp) / "external.x86_64"
        external.write_text("external", encoding="utf-8")
        game._launch_binary_path = external
        game._settings["vfs_enabled"] = True
        assert any("inside the configured" in error
                   for error in game.validate_install())

    print("✓ DFU physical deploy remains intact and external VFS binary is rejected")


def main() -> None:
    assert issubclass(DFU.DaggerfallUnity, DFU.ProfileVFSGameMixin)
    assert "vfs_enabled" in DFU.DaggerfallUnity.profile_overridable_settings
    assert DFU.DaggerfallUnity.vfs_direct_shadow_launch
    test_private_portable_deploy_launch_and_restore()
    test_redeploy_preserves_runtime_state()
    test_dfu_owned_load_order_stays_profile_private()
    test_cross_filesystem_symlink_fallback_is_detached()
    test_pending_and_physical_coexistence_restore()
    test_failed_runtime_rescue_keeps_view_recoverable()
    test_physical_mode_unchanged_and_external_binary_rejected()
    print("All Daggerfall Unity profile-VFS checks passed.")


if __name__ == "__main__":
    main()
