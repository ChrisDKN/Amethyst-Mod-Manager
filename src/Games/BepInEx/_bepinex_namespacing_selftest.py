"""Focused checks for Thunderstore package namespaces in BepInEx deploys.

Run directly from the source tree::

    python3 src/Games/BepInEx/_bepinex_namespacing_selftest.py

The Risk of Thunder preloader deliberately deletes the legacy direct child
``BepInEx/plugins/RoR2BepInExPack``.  These tests ensure an installed
Thunderstore package is deployed below its versionless package ID instead,
without allowing an unsafe metadata value to escape the destination.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_SRC_ROOT = Path(__file__).resolve().parents[2]
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from Games.BepInEx.BepInEx import _thunderstore_plugin_subdirs  # noqa: E402
from Utils.deployment import LinkMode, deploy_filemap  # noqa: E402
from Utils.mods.modlist import parse_modlist_text  # noqa: E402


def _write_source(staging: Path) -> tuple[str, Path]:
    mod_name = "RoR2BepInExPack"
    source = staging / mod_name / mod_name / "Newtonsoft.Json.dll"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"json-net")
    return mod_name, source


def test_metadata_package_id_map() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        mod = root / "RoR2BepInExPack"
        mod.mkdir()
        (mod / "meta.ini").write_text(
            "[thunderstore]\n"
            "namespace = RiskofThunder\n"
            "name = RoR2BepInExPack\n"
            "version = 1.43.0\n",
            encoding="utf-8",
        )
        entries = parse_modlist_text(
            "+RoR2BepInExPack\n"
            "-DisabledPackage\n"
            "-Visual_separator\n"
        )
        got = _thunderstore_plugin_subdirs(root, entries)
        assert got == {
            "RoR2BepInExPack": "RiskofThunder-RoR2BepInExPack",
        }
    print("✓ Thunderstore package-ID map")


def test_namespaced_plugin_deploy() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        staging = root / "mods"
        deploy = root / "game" / "BepInEx" / "plugins"
        state = root / "state"
        staging.mkdir()
        deploy.mkdir(parents=True)
        state.mkdir()
        mod_name, source = _write_source(staging)
        filemap = state / "filemap.txt"
        filemap.write_text(
            f"{mod_name}/Newtonsoft.Json.dll\t{mod_name}\n",
            encoding="utf-8",
        )

        count, placed = deploy_filemap(
            filemap,
            deploy,
            staging,
            mode=LinkMode.SYMLINK,
            per_mod_subdirs={
                mod_name: "RiskofThunder-RoR2BepInExPack",
            },
        )

        destination = (
            deploy / "RiskofThunder-RoR2BepInExPack"
            / mod_name / "Newtonsoft.Json.dll"
        )
        assert count == 1
        assert destination.is_symlink()
        assert destination.resolve() == source
        assert not (deploy / mod_name).exists()
        assert destination.relative_to(deploy).as_posix().lower() in placed
    print("✓ namespaced BepInEx plugin deploy")


def test_explicit_destination_keeps_requested_layout() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        staging = root / "mods"
        deploy = root / "plugins"
        custom = root / "custom"
        state = root / "state"
        staging.mkdir()
        deploy.mkdir()
        custom.mkdir()
        state.mkdir()
        mod_name, source = _write_source(staging)
        filemap = state / "filemap.txt"
        filemap.write_text(
            f"{mod_name}/Newtonsoft.Json.dll\t{mod_name}\n",
            encoding="utf-8",
        )

        count, placed = deploy_filemap(
            filemap,
            deploy,
            staging,
            mode=LinkMode.COPY,
            per_mod_deploy_dirs={mod_name: custom},
            per_mod_subdirs={
                mod_name: "RiskofThunder-RoR2BepInExPack",
            },
        )

        destination = custom / mod_name / "Newtonsoft.Json.dll"
        assert count == 1
        assert destination.read_bytes() == source.read_bytes()
        assert not (custom / "RiskofThunder-RoR2BepInExPack").exists()
        assert destination.relative_to(custom).as_posix().lower() in placed
    print("✓ explicit custom destination left unchanged")


def test_unsafe_namespace_is_ignored() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        staging = root / "mods"
        deploy = root / "plugins"
        state = root / "state"
        staging.mkdir()
        deploy.mkdir()
        state.mkdir()
        mod_name, _source = _write_source(staging)
        filemap = state / "filemap.txt"
        filemap.write_text(
            f"{mod_name}/Newtonsoft.Json.dll\t{mod_name}\n",
            encoding="utf-8",
        )

        count, _placed = deploy_filemap(
            filemap,
            deploy,
            staging,
            mode=LinkMode.SYMLINK,
            per_mod_subdirs={mod_name: "../escape"},
        )

        assert count == 1
        assert (deploy / mod_name / "Newtonsoft.Json.dll").is_symlink()
        assert not (root / "escape").exists()
    print("✓ unsafe namespace rejected")


if __name__ == "__main__":
    test_metadata_package_id_map()
    test_namespaced_plugin_deploy()
    test_explicit_destination_keeps_requested_layout()
    test_unsafe_namespace_is_ignored()
    print("All BepInEx namespacing checks passed.")
