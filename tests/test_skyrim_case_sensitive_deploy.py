from __future__ import annotations

import inspect
import pickle
import sys
import tempfile
import types
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

if "msgpack" not in sys.modules:
    msgpack = types.ModuleType("msgpack")
    msgpack.pack = lambda value, stream, use_bin_type=True: pickle.dump(value, stream)
    msgpack.unpack = lambda stream, raw=False: pickle.load(stream)
    sys.modules["msgpack"] = msgpack

from Games.Bethesda.skyrim_se import SkyrimSE
from Utils.deploy_shared import LinkMode
from Utils.deploy_root import deploy_root_flagged_mods, restore_root_folder
from Utils.deploy_standard import deploy_filemap
from Utils.filemap import build_filemap


class SkyrimCaseSensitiveDeployTests(unittest.TestCase):
    def test_empty_destination_uses_lowercase_source_variant(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            data_dir, _filemap = self._deploy_fixture(root)

            self.assertTrue((data_dir / self.third_person_path()).is_symlink())
            self.assertTrue((data_dir / self.first_person_path()).is_symlink())
            self.assertFalse((data_dir / "Meshes").exists())

    def test_existing_lowercase_hierarchy_is_reused(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            existing = root / "Data" / "meshes" / "actors" / "character"
            existing.mkdir(parents=True)

            data_dir, _filemap = self._deploy_fixture(root)

            self.assertTrue((data_dir / self.third_person_path()).is_symlink())
            self.assertTrue((data_dir / self.first_person_path()).is_symlink())
            self.assertFalse((data_dir / "Meshes").exists())

    def test_existing_differently_cased_hierarchy_is_reused(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            mixed_root = root / "Data" / "MeShEs" / "AcToRs" / "ChArAcTeR"
            (mixed_root / "BeHaViOrS").mkdir(parents=True)
            (mixed_root / "_1sTpErSoN" / "BeHaViOrS").mkdir(parents=True)

            data_dir, _filemap = self._deploy_fixture(root)

            self.assertTrue((mixed_root / "BeHaViOrS" / "0_master.hkx").is_symlink())
            self.assertTrue((mixed_root / "_1sTpErSoN" / "BeHaViOrS"
                             / "0_master.hkx").is_symlink())
            self.assertFalse((data_dir / "meshes").exists())
            self.assertFalse((data_dir / "Meshes").exists())

    def test_fixture_filesystem_distinguishes_case_variants(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            upper = root / "Meshes"
            lower = root / "meshes"
            upper.mkdir()
            lower.mkdir()

            self.assertNotEqual(upper.stat().st_ino, lower.stat().st_ino)

    def test_lower_policy_preserves_case_insensitive_file_identity(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _upper_data, upper_filemap = self._deploy_fixture(
                root / "upper", casing="upper")
            _lower_data, lower_filemap = self._deploy_fixture(
                root / "lower", casing="lower")

            upper_identity = self._casefolded_filemap(upper_filemap)
            lower_identity = self._casefolded_filemap(lower_filemap)

            self.assertEqual(lower_identity, upper_identity)
            self.assertIn(self.third_person_path().as_posix().lower(), lower_identity)
            self.assertIn(self.first_person_path().as_posix().lower(), lower_identity)

    def test_mesh_policy_does_not_lowercase_scripts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            profile = root / "profile"
            staging = profile / "mods"
            self._write_mod_file(staging, "Upper Scripts", "Scripts/example.pex")
            self._write_mod_file(staging, "Lower Scripts", "scripts/other.pex")
            filemap = self._build_skyrim_filemap(
                profile, ["Upper Scripts", "Lower Scripts"])

            data_dir = root / "Data"
            deploy_filemap(filemap, data_dir, staging, mode=LinkMode.SYMLINK)

            self.assertTrue((data_dir / "Scripts" / "example.pex").is_symlink())
            self.assertTrue((data_dir / "Scripts" / "other.pex").is_symlink())
            self.assertFalse((data_dir / "scripts").exists())

    def test_normal_and_root_scripts_share_uppercase_destination(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            profile = root / "profile"
            staging = profile / "mods"
            self._write_mod_file(staging, "Upper Scripts", "Scripts/example.pex")
            self._write_mod_file(staging, "Lower Scripts", "scripts/other.pex")
            root_source = self._write_mod_file(
                staging, "Root Package", "Data/Scripts/root-example.pex")
            filemap = self._build_skyrim_filemap(
                profile,
                ["Upper Scripts", "Lower Scripts", "Root Package"],
                root_folder_mods={"Root Package"},
            )

            game_root = root / "game"
            data_dir = game_root / "Data"
            data_dir.mkdir(parents=True)
            deploy_filemap(filemap, data_dir, staging, mode=LinkMode.SYMLINK)
            deploy_root_flagged_mods(
                profile / "filemap_root.txt", game_root, staging,
                mode=LinkMode.SYMLINK, strip_prefixes={"Data"})

            deployed = data_dir / "Scripts" / "root-example.pex"
            self.assertTrue(deployed.is_symlink())
            self.assertEqual(deployed.resolve(), root_source.resolve())
            self.assertFalse(
                (data_dir / "scripts" / "root-example.pex").exists())

    def test_mesh_policy_leaves_other_namespaces_unchanged(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            profile = root / "profile"
            staging = profile / "mods"
            paths = {
                "Textures": "example.dds",
                "SKSE/Plugins": "example.dll",
                "Sound": "example.wav",
                "Interface": "example.swf",
            }
            mods = []
            for authored_dir, filename in paths.items():
                label = authored_dir.replace("/", " ")
                upper_mod = f"Upper {label}"
                lower_mod = f"Lower {label}"
                self._write_mod_file(
                    staging, upper_mod, f"{authored_dir}/{filename}")
                self._write_mod_file(
                    staging, lower_mod,
                    f"{authored_dir.lower()}/other-{filename}")
                mods.extend((upper_mod, lower_mod))
            filemap = self._build_skyrim_filemap(profile, mods)

            data_dir = root / "Data"
            deploy_filemap(filemap, data_dir, staging, mode=LinkMode.SYMLINK)

            for authored_dir, filename in paths.items():
                self.assertTrue((data_dir / authored_dir / filename).is_symlink())
                self.assertFalse((data_dir / authored_dir.lower()).exists())

    def test_deployment_has_no_casefold_duplicate_directories(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            profile = root / "profile"
            staging = profile / "mods"
            self._write_mod_file(
                staging, "Lower Behavior", self.third_person_path().as_posix())
            self._write_mod_file(
                staging, "Upper Mesh",
                "Meshes/Actors/Character/Behaviors/idle.hkx")
            self._write_mod_file(staging, "Upper Scripts", "Scripts/example.pex")
            self._write_mod_file(staging, "Lower Scripts", "scripts/other.pex")
            filemap = self._build_skyrim_filemap(
                profile,
                ["Lower Behavior", "Upper Mesh", "Upper Scripts", "Lower Scripts"],
            )

            data_dir = root / "Data"
            deploy_filemap(filemap, data_dir, staging, mode=LinkMode.SYMLINK)

            directories = [data_dir]
            directories.extend(p for p in data_dir.rglob("*") if p.is_dir())
            for directory in directories:
                children = [
                    p.name.casefold() for p in directory.iterdir() if p.is_dir()
                ]
                self.assertEqual(len(children), len(set(children)), directory)

    def test_case_variant_file_conflict_keeps_one_winner(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            profile = root / "profile"
            staging = profile / "mods"
            winning_source = self._write_mod_file(
                staging, "High Priority",
                "meshes/actors/character/behaviors/0_MASTER.hkx")
            self._write_mod_file(
                staging, "Low Priority",
                "Meshes/Actors/Character/Behaviors/0_master.hkx")
            filemap = self._build_skyrim_filemap(
                profile, ["High Priority", "Low Priority"])

            lines = filemap.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            self.assertTrue(lines[0].endswith("\tHigh Priority"))

            data_dir = root / "Data"
            deploy_filemap(filemap, data_dir, staging, mode=LinkMode.SYMLINK)
            deployed = next(data_dir.rglob("0_MASTER.hkx"))
            self.assertEqual(deployed.resolve(), winning_source.resolve())

    def test_root_restore_handles_reused_directory_casing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            profile = root / "profile"
            staging = profile / "mods"
            root_folder = profile / "Root_Folder"
            root_folder.mkdir(parents=True)
            source = self._write_mod_file(
                staging, "Root Package", "Data/Scripts/root-example.pex")
            self._build_skyrim_filemap(
                profile, ["Root Package"], root_folder_mods={"Root Package"})
            game_root = root / "game"
            reused_dir = game_root / "Data" / "sCrIpTs"
            reused_dir.mkdir(parents=True)

            deploy_root_flagged_mods(
                profile / "filemap_root.txt", game_root, staging,
                mode=LinkMode.SYMLINK, strip_prefixes={"Data"})
            deployed = reused_dir / "root-example.pex"
            self.assertTrue(deployed.is_symlink())
            self.assertEqual(deployed.resolve(), source.resolve())

            restore_root_folder(root_folder, game_root)

            self.assertFalse(deployed.exists())
            self.assertFalse(deployed.is_symlink())

    def _deploy_fixture(self, root: Path, casing: str | None = None):
        profile = root / "profile"
        staging = profile / "mods"
        (profile / "overwrite").mkdir(parents=True)
        lower_mod = staging / "Behavior Output"
        upper_mod = staging / "Uppercase Contributor"
        third_person = lower_mod / self.third_person_path()
        first_person = lower_mod / self.first_person_path()
        upper_contributor = (
            upper_mod / "Meshes" / "Actors" / "Character" / "_1stPerson"
            / "Behaviors" / "idle.hkx"
        )
        third_person.parent.mkdir(parents=True)
        first_person.parent.mkdir(parents=True)
        upper_contributor.parent.mkdir(parents=True)
        third_person.write_bytes(b"third person")
        first_person.write_bytes(b"first person")
        upper_contributor.write_bytes(b"upper contributor")

        filemap = self._build_skyrim_filemap(
            profile, ["Behavior Output", "Uppercase Contributor"], casing=casing)

        data_dir = root / "Data"
        deploy_filemap(filemap, data_dir, staging, mode=LinkMode.SYMLINK)
        return data_dir, filemap

    @staticmethod
    def _write_mod_file(staging: Path, mod_name: str, relative_path: str) -> Path:
        path = staging / mod_name / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(relative_path.encode("utf-8"))
        return path

    @staticmethod
    def _build_skyrim_filemap(
        profile: Path,
        mod_names: list[str],
        *,
        casing: str | None = None,
        root_folder_mods: set[str] | None = None,
    ) -> Path:
        (profile / "overwrite").mkdir(parents=True, exist_ok=True)
        modlist = profile / "modlist.txt"
        modlist.write_text(
            "".join(f"+{name}\n" for name in mod_names), encoding="utf-8")
        filemap = profile / "filemap.txt"
        game = object.__new__(SkyrimSE)
        kwargs = {
            "normalize_folder_case": True,
            "filemap_casing": casing or game.filemap_casing,
            "filemap_casing_pins": game.filemap_casing_pins,
            "root_folder_mods": root_folder_mods,
        }
        prefixes = getattr(game, "filemap_casing_prefixes", None)
        if (prefixes is not None
                and "filemap_casing_prefixes" in inspect.signature(build_filemap).parameters):
            kwargs["filemap_casing_prefixes"] = prefixes
        build_filemap(modlist, profile / "mods", filemap, **kwargs)
        return filemap

    @staticmethod
    def _casefolded_filemap(filemap: Path) -> dict[str, str]:
        return {
            relative_path.lower(): mod_name
            for relative_path, mod_name in (
                line.split("\t", 1)
                for line in filemap.read_text(encoding="utf-8").splitlines()
            )
        }

    @staticmethod
    def third_person_path() -> Path:
        return Path("meshes/actors/character/behaviors/0_master.hkx")

    @staticmethod
    def first_person_path() -> Path:
        return Path("meshes/actors/character/_1stperson/behaviors/0_master.hkx")


if __name__ == "__main__":
    unittest.main()
