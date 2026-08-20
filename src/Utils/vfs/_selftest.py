"""Focused integration checks for Amethyst's Linux profile VFS.

Run directly from the source tree::

    python3 src/Utils/vfs/_selftest.py

The checks require bubblewrap. They verify shadow-tree and legacy-overlay
precedence, runtime-file capture, and that neither construction nor launch
places mod files in the real game directory. The FUSE compatibility test runs
when fuse-overlayfs and /dev/fuse are available.
"""

from __future__ import annotations

import json
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import patch


_SRC_ROOT = Path(__file__).resolve().parents[2]
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from Utils.vfs import (  # noqa: E402
    BACKEND_FUSE,
    BACKEND_KERNEL,
    BACKEND_SHADOW,
    MANIFEST_NAME,
    MANIFEST_VERSION,
    PENDING_NAME,
    RUNTIME_NAME,
    STATE_DIR_NAME,
    build_layers,
    cleanup_deployment,
    deployment_state_profiles,
    finalize_deployment,
    has_deployment_state,
    prefer_virtual_executable,
    wrap_command,
)
from Utils.deploy import (  # noqa: E402
    CustomRule,
    LinkMode,
    RestoreIncompleteError,
    cleanup_custom_deploy_dirs,
    deploy_custom_rules,
    restore_custom_rules,
)
from Utils.quick_configure import (  # noqa: E402
    build_quick_configure_options,
    deploy_mode_change_blocked,
)
from Utils.launch_handoff import build_launch_handoff  # noqa: E402
from Utils.exe_launch import is_game_launch_exe, launch_game  # noqa: E402
from Games.Bethesda.fallout_3 import Fallout_3  # noqa: E402
from Games.BepInEx.BepInEx import (  # noqa: E402
    Subnautica,
    Subnautica_Below_Zero,
)
from Games.ue5_game import UE5Game, UE5Rule  # noqa: E402
from Games.Custom.custom_game import (  # noqa: E402
    RootCustomGame,
    StandardCustomGame,
    Ue5CustomGame,
    make_custom_game,
)

_stardew_spec = importlib.util.spec_from_file_location(
    "amethyst_vfs_selftest_stardew",
    _SRC_ROOT / "Games" / "Stardew Valley" / "Stardew Valley.py",
)
if _stardew_spec is None or _stardew_spec.loader is None:
    raise RuntimeError("Could not load the Stardew Valley game handler.")
_stardew_module = importlib.util.module_from_spec(_stardew_spec)
_stardew_spec.loader.exec_module(_stardew_module)
StardewValley = _stardew_module.StardewValley

_oblivion_spec = importlib.util.spec_from_file_location(
    "amethyst_vfs_selftest_oblivion_remastered",
    _SRC_ROOT / "Games" / "Oblivion Remastered" / "oblivion_remastered.py",
)
if _oblivion_spec is None or _oblivion_spec.loader is None:
    raise RuntimeError("Could not load the Oblivion Remastered game handler.")
_oblivion_module = importlib.util.module_from_spec(_oblivion_spec)
_oblivion_spec.loader.exec_module(_oblivion_module)
OblivionRemastered = _oblivion_module.OblivionRemastered


class _FakeGame:
    mod_folder_strip_prefixes = {"data"}
    custom_routing_rules: list = []
    exe_name = "SkyrimSELauncher.exe"
    exe_name_alts: list[str] = []
    direct_launch_exes = ["SkyrimSE.exe"]
    name = "test game"

    def __init__(self, root: Path):
        self.root = root
        self.game = root / "game"
        self.profiles = root / "profiles-root"
        self.profile = self.profiles / "profiles" / "default"
        self.staging = self.profiles / "mods"
        self.overwrite = self.profiles / "overwrite"
        self.root_folder = self.profiles / "Root_Folder"
        self._active_profile_dir = self.profile
        (self.game / "Data").mkdir(parents=True)
        self.profile.mkdir(parents=True)
        self.staging.mkdir(parents=True)
        self.overwrite.mkdir(parents=True)
        self.root_folder.mkdir(parents=True)

    def get_game_path(self):
        return self.game

    def get_mod_data_path(self):
        return self.game / "Data"

    def get_profile_root(self):
        return self.profiles

    def get_effective_overwrite_path(self):
        return self.overwrite

    def get_effective_root_folder_path(self):
        return self.root_folder

    def get_prefix_path(self):
        return None


class _FakeBethesdaGame(_FakeGame, Fallout_3):
    """Exercise the shared Bethesda handler hooks without user configuration."""

    exe_name = "Fallout3Launcher.exe"
    direct_launch_exes = ["Fallout3.exe"]

    def __init__(self, root: Path):
        _FakeGame.__init__(self, root)
        self._script_extender_swap = True
        self._deploy_mode = LinkMode.HARDLINK
        self._deploy_active = False
        self._settings = {"skyrim_vfs_enabled": True}

    def _load_settings(self) -> dict:
        return dict(self._settings)

    def _save_settings(self, data: dict) -> None:
        self._settings = dict(data)

    def is_configured(self) -> bool:
        return True

    def set_deploy_mode(self, mode: LinkMode) -> None:
        self._deploy_mode = mode

    def get_deploy_active(self) -> bool:
        return self._deploy_active


class _FakeContentGame(_FakeGame):
    """A non-Bethesda-style fixture whose primary mod directory is Content/."""

    def __init__(self, root: Path):
        super().__init__(root)
        (self.game / "Content").mkdir()

    def get_mod_data_path(self):
        return self.game / "Content"


class _FakeHandoffGame:
    name = "Handoff Test"
    game_id = "Handoff_Test"
    native_launch_required = True

    def __init__(self, launcher_key: str, launcher_value: str):
        self.launcher_key = launcher_key
        self.launcher_value = launcher_value

    def get_saved_launcher_id(self, key: str) -> str:
        return self.launcher_value if key == self.launcher_key else ""

    def get_game_path(self) -> Path:
        return Path("/games/handoff-test")


class _FakeSubnauticaGame(_FakeGame, Subnautica):
    """Subnautica fixture using the real BepInEx routing contract."""

    exe_name = "Subnautica.exe"

    def __init__(self, root: Path):
        _FakeGame.__init__(self, root)
        self._game_path = self.game
        self._staging_path = self.profiles
        self._deploy_mode = LinkMode.HARDLINK
        self._settings = {"vfs_enabled": True}

    @property
    def custom_routing_rules(self):
        return Subnautica.custom_routing_rules.fget(self)

    @property
    def mod_folder_strip_prefixes(self):
        return Subnautica.mod_folder_strip_prefixes.fget(self)

    def get_mod_data_path(self):
        # Deliberately absent initially: BepInEx is introduced by the profile.
        return self.game / "BepInEx" / "plugins"

    def _load_settings(self) -> dict:
        return dict(self._settings)


class _FakeStardewGame(_FakeGame, StardewValley):
    """Stardew fixture retaining the real handler's SMAPI deploy rules."""

    exe_name = "StardewValley"

    def __init__(self, root: Path):
        _FakeGame.__init__(self, root)
        self._game_path = self.game
        self._staging_path = self.profiles
        self._deploy_mode = LinkMode.HARDLINK
        self._settings = {"vfs_enabled": True}
        (self.game / "Mods").mkdir()

    @property
    def mod_folder_strip_prefixes(self):
        return {"mods"}

    def get_mod_data_path(self):
        return self.game / "Mods"

    def _load_settings(self) -> dict:
        return dict(self._settings)

    def _save_settings(self, data: dict) -> None:
        self._settings = dict(data)

    def _save_settings(self, data: dict) -> None:
        self._settings = dict(data)

    def is_configured(self) -> bool:
        return True

    def get_deploy_mode(self) -> LinkMode:
        return self._deploy_mode

    def set_deploy_mode(self, mode: LinkMode) -> None:
        self._deploy_mode = mode


class _FakeUE5Game(_FakeGame, UE5Game):
    """UE project nested below an install that also contains Engine/."""

    name = "UE5 VFS Test"
    game_id = "ue5_vfs_test"
    # Deliberately differ from the physical project's casing (Pseudoregalia
    # has this exact shape in the custom definitions).
    exe_name = "testproject/Binaries/Win64/TestProject-Win64-Shipping.exe"
    preferred_launch_exe = "Binaries/Win64/ProfileLoader.exe"
    mod_folder_strip_prefixes: set[str] = set()
    custom_routing_rules: list[CustomRule] = []

    def __init__(self, root: Path):
        self.root = root
        self.install = root / "ue5-install"
        self.game = self.install / "TestProject"
        self.profiles = root / "profiles-root"
        self.profile = self.profiles / "profiles" / "default"
        self.staging = self.profiles / "mods"
        self.overwrite = self.profiles / "overwrite"
        self.root_folder = self.profiles / "Root_Folder"
        self._active_profile_dir = self.profile
        self._game_path = self.game
        self._staging_path = self.profiles
        self._prefix_path = None
        self._deploy_mode = LinkMode.HARDLINK
        self._settings = {"vfs_enabled": True}
        self.game.mkdir(parents=True)
        self.profile.mkdir(parents=True)
        self.staging.mkdir(parents=True)
        self.overwrite.mkdir(parents=True)
        self.root_folder.mkdir(parents=True)

    def get_mod_data_path(self):
        return self.game

    def _load_settings(self) -> dict:
        return dict(self._settings)

    def _save_settings(self, data: dict) -> None:
        self._settings = dict(data)


class _FakeUE5RoutedGame(_FakeUE5Game):
    custom_routing_rules = [
        CustomRule(
            dest="drive_c/users/test/AppData/Local/TestGame",
            filenames=["Engine.ini"],
            flatten=True,
            to_prefix=True,
        ),
    ]

    def __init__(self, root: Path):
        super().__init__(root)
        self.prefix = root / "prefix"
        self.prefix.mkdir()

    def get_prefix_path(self):
        return self.prefix


class _FakeUE5FailingGame(_FakeUE5RoutedGame):
    def _vfs_populate_ue5_layer_files(
        self, _destination: Path, _profile: str, _log_fn,
    ) -> None:
        raise RuntimeError("injected UE5 layer hook failure")


class _FakeUE5ManagedModsGame(_FakeUE5Game):
    """UE5 fixture whose Lua route enables managed UE4SS mods.txt handling."""

    @property
    def _ue5_post_passthrough_rules(self):
        return [
            UE5Rule(
                dest="Binaries/Win64/ue4ss/Mods",
                extensions=[".lua"],
            ),
        ]


class _FakeCustomPaths:
    """Temporary paths/settings while retaining the real custom handlers."""

    def __init__(self, root: Path, definition: dict):
        self.root = root
        self._defn = definition
        self.game = root / "game"
        self.profiles = root / "profiles-root"
        self.profile = self.profiles / "profiles" / "default"
        self.staging = self.profiles / "mods"
        self.overwrite = self.profiles / "overwrite"
        self.root_folder = self.profiles / "Root_Folder"
        self.filemap = self.profiles / "filemap.txt"
        self.prefix = root / "prefix"
        self._game_path = self.game
        self._prefix_path = self.prefix
        self._staging_path = self.profiles
        self._deploy_mode = LinkMode.HARDLINK
        self._active_profile_dir = self.profile
        self._settings = {"vfs_enabled": True}
        for directory in (
            self.game,
            self.profile,
            self.staging,
            self.overwrite,
            self.root_folder,
            self.prefix,
        ):
            directory.mkdir(parents=True)

    def get_profile_root(self) -> Path:
        return self.profiles

    def get_effective_filemap_path(self) -> Path:
        return self.filemap

    def get_effective_mod_staging_path(self) -> Path:
        return self.staging

    def get_effective_overwrite_path(self) -> Path:
        return self.overwrite

    def get_effective_root_folder_path(self) -> Path:
        return self.root_folder

    @property
    def _deploy_state_file(self) -> Path:
        return self.profiles / "deploy_state.json"

    def _load_settings(self) -> dict:
        return dict(self._settings)

    def _save_settings(self, data: dict) -> None:
        self._settings = dict(data)

    def get_deploy_mode(self) -> LinkMode:
        return self._deploy_mode

    def set_deploy_mode(self, mode: LinkMode) -> None:
        self._deploy_mode = mode

    def is_configured(self) -> bool:
        return True


class _FakeStandardCustomGame(_FakeCustomPaths, StandardCustomGame):
    pass


class _FakeRootCustomGame(_FakeCustomPaths, RootCustomGame):
    pass


def _deploy_custom_fixture(game, **kwargs):
    """Deploy without importing optional filemap-index dependencies."""
    mod_files_stub = types.ModuleType("Utils.mod_files")
    mod_files_stub.excluded_raw_by_mod = lambda _profile: {}
    with patch.dict(sys.modules, {"Utils.mod_files": mod_files_stub}):
        return game.deploy(**kwargs)


def _write_manifest(game: _FakeGame) -> Path:
    state = game.profile / STATE_DIR_NAME
    paths = {
        "root_layer": state / "lower" / "root",
        "data_layer": state / "lower" / "data",
        "root_upper": state / "root-upper",
        "data_upper": game.overwrite,
        "root_work": state / "root-work",
        "data_work": state / "data-work",
    }
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    runtime = state / RUNTIME_NAME
    shutil.copyfile(Path(__file__).with_name("runtime.sh"), runtime)
    (state / MANIFEST_NAME).write_text(json.dumps({
        "version": MANIFEST_VERSION,
        "profile": "default",
        "backend": BACKEND_KERNEL,
        "game_root": str(game.game),
        "data_root": str(game.game / "Data"),
        "runtime": str(runtime),
        **{key: str(value) for key, value in paths.items()},
    }), encoding="utf-8")
    return state


def test_nested_overlay() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        game = _FakeGame(Path(tmp))
        (game.game / "root.txt").write_text("vanilla-root")
        (game.game / "Data" / "data.txt").write_text("vanilla-data")
        state = _write_manifest(game)
        (state / "lower" / "root" / "root.txt").write_text("mod-root")
        (state / "lower" / "data" / "data.txt").write_text("mod-data")

        script = (
            f'test "$(cat {game.game / "root.txt"})" = mod-root && '
            f'test "$(cat {game.game / "Data" / "data.txt"})" = mod-data && '
            f'printf changed-root > {game.game / "root.txt"} && '
            f'printf changed-data > {game.game / "Data" / "data.txt"} && '
            f'printf runtime-root > {game.game / "created.txt"} && '
            f'printf runtime-data > {game.game / "Data" / "created.txt"}'
        )
        result = subprocess.run(
            wrap_command(game, ["/bin/sh", "-c", script]),
            text=True, capture_output=True, check=False)
        assert result.returncode == 0, result.stderr
        # Native overlayfs leaves a mode-000 internal work/ entry; a second
        # wrapper construction must clean/recreate it safely.
        repeat = subprocess.run(
            wrap_command(game, ["/bin/true"]),
            text=True, capture_output=True, check=False)
        assert repeat.returncode == 0, repeat.stderr
        assert (game.game / "root.txt").read_text() == "vanilla-root"
        assert (game.game / "Data" / "data.txt").read_text() == "vanilla-data"
        assert (state / "lower" / "root" / "root.txt").read_text() == "mod-root"
        assert (state / "lower" / "data" / "data.txt").read_text() == "mod-data"
        assert (state / "root-upper" / "root.txt").read_text() == "changed-root"
        assert (game.overwrite / "data.txt").read_text() == "changed-data"
        assert not (game.game / "created.txt").exists()
        assert not (game.game / "Data" / "created.txt").exists()
        assert (state / "root-upper" / "created.txt").read_text() == "runtime-root"
        assert (game.overwrite / "created.txt").read_text() == "runtime-data"
    print("✓ nested root/Data overlay and copy-on-write")


def test_fuse_overlay() -> None:
    if (not shutil.which("fuse-overlayfs")
            or not shutil.which("fusermount3")
            or not Path("/dev/fuse").exists()):
        print("- fuse-overlayfs integration skipped (/dev/fuse unavailable)")
        return
    with tempfile.TemporaryDirectory() as tmp:
        game = _FakeGame(Path(tmp))
        (game.game / "root.txt").write_text("vanilla-root")
        (game.game / "Data" / "data.txt").write_text("vanilla-data")
        state = _write_manifest(game)
        (state / "lower" / "root" / "root.txt").write_text("mod-root")
        (state / "lower" / "data" / "data.txt").write_text("mod-data")
        manifest = json.loads((state / MANIFEST_NAME).read_text())
        manifest["backend"] = BACKEND_FUSE
        (state / MANIFEST_NAME).write_text(json.dumps(manifest))

        script = (
            f'test "$(cat {game.game / "root.txt"})" = mod-root && '
            f'test "$(cat {game.game / "Data" / "data.txt"})" = mod-data && '
            f'printf changed-root > {game.game / "root.txt"} && '
            f'printf changed-data > {game.game / "Data" / "data.txt"}'
        )
        result = subprocess.run(
            wrap_command(game, ["/bin/sh", "-c", script]),
            text=True, capture_output=True, check=False)
        assert result.returncode == 0, result.stderr
        assert (game.game / "root.txt").read_text() == "vanilla-root"
        assert (game.game / "Data" / "data.txt").read_text() == "vanilla-data"
        assert (state / "root-upper" / "root.txt").read_text() == "changed-root"
        assert (game.overwrite / "data.txt").read_text() == "changed-data"
        assert not (state / "mount" / "root").is_mount()
        assert not (state / "mount" / "data").is_mount()
    print("✓ fuse-overlayfs runtime, nested binds and cleanup")


def test_layer_build_and_skse_selection() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        game = _FakeGame(Path(tmp))
        game.case_alias_dirs = ["Data", "Data/Meshes", "Data/Data"]
        game.probe_stub_dirs = ["Data/Data"]
        game.case_alias_links = True
        vanilla = game.game / "Data" / "meshes" / "vanilla.txt"
        vanilla.parent.mkdir()
        vanilla.write_text("vanilla")
        staged = game.staging / "Example Mod" / "Data" / "meshes" / "mod.txt"
        staged.parent.mkdir(parents=True)
        staged.write_text("mod")
        staged_loader = game.staging / "SKSE" / "skse64_loader.exe"
        staged_loader.parent.mkdir(parents=True)
        staged_loader.write_text("loader")
        staged_preset = game.staging / "Preset" / "character.jslot"
        staged_preset.parent.mkdir(parents=True)
        staged_preset.write_text("preset")
        game.custom_routing_rules = [
            CustomRule(dest="", filenames=["skse64_loader.exe"],
                       flatten=True, loose_only=True),
            CustomRule(dest="Data/SKSE/Plugins/CharGen/Presets",
                       extensions=[".jslot"], flatten=True),
        ]
        root_data = game.root_folder / "Data" / "meshes" / "mod.txt"
        root_data.parent.mkdir(parents=True)
        root_data.write_text("root-folder-wins")
        overwrite_file = game.overwrite / "meshes" / "overwrite.txt"
        overwrite_file.parent.mkdir(parents=True)
        overwrite_file.write_text("overwrite-wins")
        prior_root_upper = game.profile / STATE_DIR_NAME / "root-upper"
        prior_root_upper.mkdir(parents=True)
        (prior_root_upper / "persistent.log").write_text("persistent-root")
        filemap = game.profiles / "filemap.txt"
        filemap.write_text(
            "meshes/mod.txt\tExample Mod\n"
            "meshes/overwrite.txt\t[Overwrite]\n"
            "skse64_loader.exe\tSKSE\n"
            "character.jslot\tPreset\n")

        counts = build_layers(
            game, profile="default", filemap=filemap, staging=game.staging,
            per_mod_strip={}, per_mod_deploy={}, raw_mods=None,
            excluded_raw=None, root_folder_enabled=True)
        assert counts == (1, 1)
        state = game.profile / STATE_DIR_NAME
        assert (state / "view" / "Data" / "meshes" / "mod.txt").read_text() \
            == "root-folder-wins"
        assert vanilla.read_text() == "vanilla"
        assert not (game.game / "Data" / "meshes" / "mod.txt").exists()
        assert (state / "view" / "skse64_loader.exe").read_text() \
            == "loader"
        assert (state / "view" / "Data" / "SKSE" / "Plugins" / "CharGen"
                / "Presets" / "character.jslot").read_text() == "preset"
        assert (state / "view" / "Data" / "meshes"
                / "overwrite.txt").read_text() == "overwrite-wins"
        assert (state / "view" / "persistent.log").read_text() == \
            "persistent-root"
        assert (state / "view" / "Data" / "meshes"
                / "vanilla.txt").read_text() == "vanilla"
        assert not (game.game / "skse64_loader.exe").exists()

        # The physical Wine lookup optimization must be represented privately
        # too: no aliases/stubs touch the real game, but all spellings resolve
        # through the bound profile-local view.
        manifest = json.loads((state / MANIFEST_NAME).read_text())
        assert manifest["backend"] == BACKEND_SHADOW
        shadow = state / "view"
        shadow_data = shadow / "Data"
        assert (shadow / "data").is_symlink()
        assert (shadow / "DATA").is_symlink()
        assert (shadow_data / "Meshes").is_symlink()
        assert (shadow_data / "MESHES").is_symlink()
        assert (shadow_data / "Data").is_dir()
        assert (shadow_data / "data").is_symlink()
        assert (shadow_data / "DATA").is_symlink()
        assert not (game.game / "data").exists()
        assert not (game.game / "DATA").exists()
        assert not (game.game / "Data" / "Data").exists()

        aliases_visible = subprocess.run(
            wrap_command(game, [
                "/bin/sh", "-c",
                'test -f "$1/data/MESHES/mod.txt" && '
                'test -d "$1/DATA/data"',
                "sh", str(game.game),
            ]),
            text=True, capture_output=True, check=False,
        )
        assert aliases_visible.returncode == 0, aliases_visible.stderr

        vanilla_cmd = [
            "proton", "waitforexitandrun",
            str(game.game / "SkyrimSELauncher.exe"),
        ]
        replaced = prefer_virtual_executable(
            game, vanilla_cmd, "skse64_loader.exe")
        assert replaced[-1] == str(game.game / "skse64_loader.exe")

        finalize_deployment(game)
        runtime_script = (
            'printf root-runtime > "$1/runtime.log" && '
            'printf data-runtime > "$1/Data/runtime.txt"'
        )
        runtime_result = subprocess.run(
            wrap_command(game, [
                "/bin/sh", "-c", runtime_script, "sh", str(game.game),
            ]),
            text=True, capture_output=True, check=False,
        )
        assert runtime_result.returncode == 0, runtime_result.stderr
        assert not (game.game / "runtime.log").exists()
        assert not (game.game / "Data" / "runtime.txt").exists()
        cleanup_deployment(game, preserve_upper=True)
        assert (state / "root-upper" / "runtime.log").read_text() == \
            "root-runtime"
        assert (game.overwrite / "runtime.txt").read_text() == "data-runtime"
        assert not (state / MANIFEST_NAME).exists()
    print("✓ layer build, virtual case aliases/stubs, SKSE selection, cleanup")


def test_shared_bethesda_hooks() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        game = _FakeBethesdaGame(Path(tmp))
        (game.game / game.exe_name).write_text("launcher")
        state = _write_manifest(game)
        (state / "lower" / "root" / "fose_loader.exe").write_text("loader")

        # The original Skyrim-only key migrates on read; the generic key wins
        # after the user changes the shared setting.
        assert game.vfs_enabled
        assert game.vfs_launch_enabled
        assert not game.supports_incremental_deploy
        assert game.get_vfs_launch_exe() == game.game / "fose_loader.exe"
        steam_cmd = ["proton", str(game.game / "Fallout3.exe")]
        replaced = game.get_vfs_steam_command(steam_cmd)
        assert str(game.game / "fose_loader.exe") in replaced

        game.set_vfs_enabled(False)
        assert game._settings["vfs_enabled"] is False
        assert not game.vfs_launch_enabled
        assert game.supports_incremental_deploy
    print("✓ shared Bethesda setting migration and launch hooks")


def test_generic_mod_data_directory() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        game = _FakeContentGame(Path(tmp))
        mod = game.staging / "Example Mod"
        (mod / "assets").mkdir(parents=True)
        (mod / "assets" / "generic.txt").write_text("generic")
        filemap = game.profiles / "filemap.txt"
        filemap.write_text("assets/generic.txt\tExample Mod\n")

        counts = build_layers(
            game, profile="default", filemap=filemap, staging=game.staging,
            per_mod_strip={}, per_mod_deploy={}, raw_mods=None,
            excluded_raw=None, root_folder_enabled=False)
        assert counts == (1, 0)
        state = game.profile / STATE_DIR_NAME
        manifest = json.loads((state / MANIFEST_NAME).read_text())
        assert Path(manifest["data_root"]) == game.game / "Content"
        assert (state / "view" / "Content" / "assets" / "generic.txt").is_file()
        assert not (game.game / "Content" / "assets" / "generic.txt").exists()
        cleanup_deployment(game)
    print("✓ generic primary mod-data directory adapter")


def test_vfs_cleanup_failure_remains_discoverable() -> None:
    """A partial unpublish must retain a profile marker for retry."""
    import Utils.vfs.overlay as vfs_overlay

    with tempfile.TemporaryDirectory() as tmp:
        game = _FakeGame(Path(tmp))
        state = _write_manifest(game)
        lower = state / "lower"
        assert not (state / PENDING_NAME).exists()

        real_remove_tree = vfs_overlay._remove_tree
        failed = False

        def _fail_lower_once(path: Path) -> None:
            nonlocal failed
            if path == lower and not failed:
                failed = True
                raise PermissionError("injected VFS tree removal failure")
            real_remove_tree(path)

        try:
            with patch.object(
                vfs_overlay, "_remove_tree", _fail_lower_once,
            ):
                cleanup_deployment(game)
        except PermissionError as exc:
            assert "injected VFS tree removal failure" in str(exc)
        else:
            raise AssertionError("VFS cleanup tree-removal failure was ignored")

        pending = state / PENDING_NAME
        assert pending.read_text(encoding="utf-8") == "cleanup\n"
        assert lower.is_dir()
        assert has_deployment_state(game)
        assert "default" in deployment_state_profiles(game)

        cleanup_deployment(game)
        assert not lower.exists()
        assert not pending.exists()
        assert not has_deployment_state(game)
        assert "default" not in deployment_state_profiles(game)
    print("✓ failed VFS cleanup remains discoverable until retry succeeds")


def test_standard_custom_shadow_view() -> None:
    definition = {
        "name": "Standard Custom VFS Test",
        "game_id": "standard_custom_vfs_test",
        "exe_name": "bin/StandardGame.exe",
        "deploy_type": "standard",
        "mod_data_path": "Content/Mods",
        "custom_routing_rules": [
            {
                "dest": "",
                "filenames": ["root-loader.dll"],
                "flatten": True,
            },
            {
                "dest": "Content/Mods/Routed",
                "extensions": [".route"],
                "flatten": True,
            },
            {
                "dest": "RoutedOverwrite",
                "filenames": ["routed-overwrite.dll"],
                "flatten": True,
            },
        ],
    }
    with tempfile.TemporaryDirectory() as tmp:
        game = _FakeStandardCustomGame(Path(tmp), definition)
        data = game.get_mod_data_path()
        assert data is not None
        data.mkdir(parents=True)
        launcher = game.game / game.exe_name
        launcher.parent.mkdir(parents=True)
        launcher.write_text("vanilla-launcher")
        (data / "vanilla.txt").write_text("vanilla-data")

        normal = game.staging / "Normal" / "normal.txt"
        root_loader = game.staging / "RootRule" / "root-loader.dll"
        routed = game.staging / "DataRule" / "nested" / "asset.route"
        for source, body in (
            (normal, "normal-mod"),
            (root_loader, "root-loader"),
            (routed, "routed-data"),
        ):
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text(body)
        (game.overwrite / "overwrite-only.txt").write_text("overwrite")
        (game.overwrite / "routed-overwrite.dll").write_text(
            "routed-overwrite")

        root_winner = game.root_folder / "Content" / "Mods" / "normal.txt"
        root_winner.parent.mkdir(parents=True)
        root_winner.write_text("root-folder-wins")
        (game.root_folder / "root-config.ini").write_text("root-config")
        (game.profile / "modlist.txt").write_text(
            "+Normal\n+RootRule\n+DataRule\n", encoding="utf-8")
        game.filemap.write_text(
            "normal.txt\tNormal\n"
            "root-loader.dll\tRootRule\n"
            "nested/asset.route\tDataRule\n"
            "overwrite-only.txt\t[Overwrite]\n"
            "routed-overwrite.dll\t[Overwrite]\n",
            encoding="utf-8",
        )

        _deploy_custom_fixture(game, profile="default")

        state = game.profile / STATE_DIR_NAME
        view = state / "view"
        view_data = view / "Content" / "Mods"
        manifest = json.loads((state / MANIFEST_NAME).read_text())
        assert Path(manifest["game_root"]) == game.game
        assert Path(manifest["data_root"]) == data
        assert (view / "bin" / "StandardGame.exe").read_text() == \
            "vanilla-launcher"
        assert (view_data / "vanilla.txt").read_text() == "vanilla-data"
        assert (view_data / "normal.txt").read_text() == "root-folder-wins"
        assert (view_data / "Routed" / "asset.route").read_text() == \
            "routed-data"
        assert (view_data / "overwrite-only.txt").read_text() == "overwrite"
        assert (view / "root-loader.dll").read_text() == "root-loader"
        assert (view / "root-config.ini").read_text() == "root-config"
        assert (view / "RoutedOverwrite" / "routed-overwrite.dll").read_text() \
            == "routed-overwrite"
        assert not (view_data / "routed-overwrite.dll").exists()
        assert game.get_vfs_launch_exe() == launcher
        assert is_game_launch_exe(game, launcher)

        # Neither the normal layer, custom routes nor Root_Folder may touch
        # the physical installation while VFS is active.
        assert launcher.read_text() == "vanilla-launcher"
        assert (data / "vanilla.txt").read_text() == "vanilla-data"
        assert not (data / "normal.txt").exists()
        assert not (data / "Routed").exists()
        assert not (data / "overwrite-only.txt").exists()
        assert not (data / "routed-overwrite.dll").exists()
        assert not (game.game / "root-loader.dll").exists()
        assert not (game.game / "RoutedOverwrite").exists()
        assert not (game.game / "root-config.ini").exists()
        assert not (game.game / "Content_Core").exists()

        # Restore follows deployed state, not a setting changed afterwards.
        game.set_vfs_enabled(False)
        game.restore()
        assert not (state / MANIFEST_NAME).exists()
        assert not (state / "view").exists()
        assert (data / "vanilla.txt").read_text() == "vanilla-data"
    print("✓ standard custom nested-data VFS routing and launch selection")


def test_root_custom_shadow_view() -> None:
    definition = {
        "name": "Root Custom VFS Test",
        "game_id": "root_custom_vfs_test",
        "exe_name": "Bin/RootGame.exe",
        "deploy_type": "root",
        "mod_data_path": "",
        "custom_routing_rules": [
            {
                "dest": "Loader",
                "filenames": ["loader.dll"],
                "companion_extensions": [".ini"],
                "flatten": True,
            },
        ],
    }
    with tempfile.TemporaryDirectory() as tmp:
        game = _FakeRootCustomGame(Path(tmp), definition)
        launcher = game.game / game.exe_name
        launcher.parent.mkdir(parents=True)
        launcher.write_text("vanilla-launcher")
        physical_collision = game.game / "normal.txt"
        physical_collision.write_text("vanilla-root")

        for source, body in (
            (game.staging / "Normal" / "normal.txt", "normal-mod"),
            (game.staging / "Loader" / "bin" / "loader.dll", "loader"),
            (game.staging / "Loader" / "bin" / "loader.ini", "companion"),
            (game.staging / "RootFlag" / "flagged.txt", "root-flagged"),
        ):
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text(body)
        (game.overwrite / "overwrite-only.cfg").write_text("overwrite")
        (game.root_folder / "root-only.cfg").write_text("root-folder")
        (game.profile / "modlist.txt").write_text(
            "+Normal\n+Loader\n+RootFlag\n", encoding="utf-8")
        game.filemap.write_text(
            "normal.txt\tNormal\n"
            "bin/loader.dll\tLoader\n"
            "bin/loader.ini\tLoader\n"
            "overwrite-only.cfg\t[Overwrite]\n",
            encoding="utf-8",
        )
        (game.filemap.parent / "filemap_root.txt").write_text(
            "flagged.txt\tRootFlag\n", encoding="utf-8")

        _deploy_custom_fixture(game, profile="default")

        state = game.profile / STATE_DIR_NAME
        view = state / "view"
        manifest = json.loads((state / MANIFEST_NAME).read_text())
        assert Path(manifest["game_root"]) == game.game
        assert Path(manifest["data_root"]) == game.game
        assert (view / "normal.txt").read_text() == "normal-mod"
        assert (view / "Loader" / "loader.dll").read_text() == "loader"
        assert (view / "Loader" / "loader.ini").read_text() == "companion"
        assert (view / "root-only.cfg").read_text() == "root-folder"
        assert (view / "flagged.txt").read_text() == "root-flagged"
        assert (view / "overwrite-only.cfg").read_text() == "overwrite"
        assert (view / "Bin" / "RootGame.exe").read_text() == \
            "vanilla-launcher"
        assert game.get_vfs_launch_exe() == launcher
        assert is_game_launch_exe(game, launcher)

        assert physical_collision.read_text() == "vanilla-root"
        assert not (game.game / "Loader").exists()
        assert not (game.game / "root-only.cfg").exists()
        assert not (game.game / "flagged.txt").exists()
        assert not (game.game / "overwrite-only.cfg").exists()
        assert not (game.filemap.parent / "filemap_deployed.txt").exists()

        game.restore()
        assert not (state / MANIFEST_NAME).exists()
        assert physical_collision.read_text() == "vanilla-root"
    print("✓ root custom same-root rules, companions and payload layers")


def test_custom_standard_root_factory_vfs_contract() -> None:
    standard_definition = {
        "name": "Custom Standard VFS Contract",
        "game_id": "custom_standard_vfs_contract",
        "exe_name": "Game.exe",
        "deploy_type": "standard",
        "mod_data_path": "Mods",
    }
    root_definition = {
        "name": "Custom Root VFS Contract",
        "game_id": "custom_root_vfs_contract",
        "exe_name": "Game.exe",
        "deploy_type": "root",
    }
    native_definition = {
        **standard_definition,
        "name": "Custom Native VFS Contract",
        "game_id": "custom_native_vfs_contract",
        "exe_name": "start-game.sh",
    }
    with patch.object(StandardCustomGame, "load_paths", return_value=False):
        standard = make_custom_game(standard_definition)
        root = make_custom_game(root_definition)
        native = make_custom_game(native_definition)
    assert isinstance(standard, StandardCustomGame)
    assert isinstance(root, RootCustomGame)
    for game in (standard, root, native):
        assert game.supports_profile_vfs
        assert "vfs_enabled" in game.profile_overridable_settings
    assert not standard.vfs_direct_shadow_launch
    assert native.vfs_direct_shadow_launch
    print("✓ custom standard/root factory exposes the profile VFS contract")


def test_custom_nondefault_pending_profile_discovery() -> None:
    fixtures = (
        (
            _FakeStandardCustomGame,
            {
                "name": "Standard Pending Discovery Test",
                "game_id": "standard_pending_discovery_test",
                "exe_name": "Game.exe",
                "deploy_type": "standard",
                "mod_data_path": "Mods",
            },
        ),
        (
            _FakeRootCustomGame,
            {
                "name": "Root Pending Discovery Test",
                "game_id": "root_pending_discovery_test",
                "exe_name": "Game.exe",
                "deploy_type": "root",
            },
        ),
    )
    for fixture, definition in fixtures:
        with tempfile.TemporaryDirectory() as tmp:
            game = fixture(Path(tmp), definition)
            data = game.get_mod_data_path()
            assert data is not None
            data.mkdir(parents=True, exist_ok=True)
            failed_profile = game.profiles / "profiles" / "Failed Profile"
            failed_state = failed_profile / STATE_DIR_NAME
            failed_state.mkdir(parents=True)
            (failed_state / "pending").write_text(
                "Failed Profile\n", encoding="utf-8")

            # The selected profile is still default and there is no successful
            # deploy_state record, but the global guard/restore selector must
            # discover the failed non-default build.
            assert game._active_profile_dir == game.profile
            assert game.get_deploy_active()
            assert game.get_last_deployed_profile() == "Failed Profile"

            game.set_active_profile_dir(failed_profile)
            game.set_vfs_enabled(False)
            game.restore()
            assert not (failed_state / "pending").exists()
            assert not game.get_deploy_active()
    print("✓ non-default pending VFS profiles are discoverable and restorable")


def test_custom_missing_prefix_match_fails() -> None:
    definition = {
        "name": "Root Missing Prefix Test",
        "game_id": "root_missing_prefix_test",
        "exe_name": "Game.exe",
        "deploy_type": "root",
        "custom_routing_rules": [
            {
                "dest": "drive_c/users/test/Saves",
                "filenames": ["Settings.ini"],
                "flatten": True,
                "to_prefix": True,
            },
        ],
    }
    with tempfile.TemporaryDirectory() as tmp:
        game = _FakeRootCustomGame(Path(tmp), definition)
        game._prefix_path = None
        (game.game / "Game.exe").write_text("launcher")
        source = game.staging / "PrefixMod" / "Settings.ini"
        source.parent.mkdir(parents=True)
        source.write_text("profile-prefix")
        (game.profile / "modlist.txt").write_text(
            "+PrefixMod\n", encoding="utf-8")
        game.filemap.write_text(
            "Settings.ini\tPrefixMod\n", encoding="utf-8")

        try:
            _deploy_custom_fixture(game, profile="default")
        except RuntimeError as exc:
            message = str(exc).lower()
            assert "no prefix configured" in message
            assert "configure" in message
        else:
            raise AssertionError("matched prefix route deployed without a prefix")

        state = game.profile / STATE_DIR_NAME
        assert (state / "pending").is_file()
        assert not (state / MANIFEST_NAME).exists()
        assert not (game.game / "Settings.ini").exists()
        assert not (state / "view").exists()
        game.set_vfs_enabled(False)
        game.restore()
        assert not (state / "pending").exists()
    print("✓ matched prefix routes fail clearly when no prefix is configured")


def test_custom_rule_global_first_match_partition() -> None:
    """Game and prefix passes must retain one global first-match ordering."""
    game_first_definition = {
        "name": "Custom Rule Game-First Test",
        "game_id": "custom_rule_game_first_test",
        "exe_name": "Game.exe",
        "deploy_type": "standard",
        "mod_data_path": "Mods",
        "custom_routing_rules": [
            {
                "dest": "GameRoute",
                "filenames": ["shared.ini"],
                "flatten": True,
            },
            {
                "dest": "drive_c/users/test/PrefixRoute",
                "filenames": ["shared.ini"],
                "flatten": True,
                "to_prefix": True,
            },
        ],
    }
    with tempfile.TemporaryDirectory() as tmp:
        game = _FakeStandardCustomGame(Path(tmp), game_first_definition)
        game._prefix_path = None
        (game.game / "Mods").mkdir()
        (game.game / "Game.exe").write_text("launcher")
        source = game.staging / "Overlap" / "shared.ini"
        source.parent.mkdir(parents=True)
        source.write_text("game-first")
        (game.profile / "modlist.txt").write_text(
            "+Overlap\n", encoding="utf-8")
        game.filemap.write_text(
            "shared.ini\tOverlap\n", encoding="utf-8")

        # The later prefix rule has no claim, so a missing prefix must not
        # reject this profile or cause a second copy in the normal data layer.
        _deploy_custom_fixture(game, profile="default")
        view = game.profile / STATE_DIR_NAME / "view"
        assert (view / "GameRoute" / "shared.ini").read_text() == \
            "game-first"
        assert not (view / "Mods" / "shared.ini").exists()
        assert not (game.game / "GameRoute").exists()
        game.restore()

    prefix_first_definition = {
        "name": "Custom Rule Prefix-First Test",
        "game_id": "custom_rule_prefix_first_test",
        "exe_name": "Game.exe",
        "deploy_type": "standard",
        "mod_data_path": "Mods",
        "custom_routing_rules": [
            {
                "dest": "drive_c/users/test/PrefixRoute",
                "filenames": ["shared.ini", "bundle.ini"],
                "flatten": True,
                "to_prefix": True,
            },
            {
                "dest": "GameRoute",
                "filenames": ["shared.ini"],
                "flatten": True,
            },
            {
                "dest": "GameRoute",
                "extensions": [".dll"],
                "companion_extensions": [".ini"],
                "flatten": True,
            },
        ],
    }
    with tempfile.TemporaryDirectory() as tmp:
        game = _FakeStandardCustomGame(Path(tmp), prefix_first_definition)
        (game.game / "Mods").mkdir()
        (game.game / "Game.exe").write_text("launcher")
        mod = game.staging / "Overlap"
        mod.mkdir()
        for name, body in (
            ("shared.ini", "profile-shared"),
            ("bundle.dll", "profile-dll"),
            ("bundle.ini", "profile-companion"),
        ):
            (mod / name).write_text(body)
        (game.profile / "modlist.txt").write_text(
            "+Overlap\n", encoding="utf-8")
        game.filemap.write_text(
            "shared.ini\tOverlap\n"
            "bundle.dll\tOverlap\n"
            "bundle.ini\tOverlap\n",
            encoding="utf-8",
        )
        prefix_route = game.prefix / "drive_c" / "users" / "test" \
            / "PrefixRoute"
        prefix_route.mkdir(parents=True)
        (prefix_route / "shared.ini").write_text("vanilla-shared")
        (prefix_route / "bundle.ini").write_text("vanilla-companion")

        _deploy_custom_fixture(game, profile="default")
        view = game.profile / STATE_DIR_NAME / "view"
        assert (prefix_route / "shared.ini").read_text() == "profile-shared"
        assert (prefix_route / "bundle.ini").read_text() == \
            "profile-companion"
        assert (view / "GameRoute" / "bundle.dll").read_text() == \
            "profile-dll"
        # The prefix-first overlap wins once, and its direct primary claim
        # also prevents the later .dll rule's companion pass stealing it.
        assert not (view / "GameRoute" / "shared.ini").exists()
        assert not (view / "GameRoute" / "bundle.ini").exists()
        assert not (view / "Mods" / "shared.ini").exists()
        assert not (view / "Mods" / "bundle.ini").exists()

        game.restore()
        assert (prefix_route / "shared.ini").read_text() == "vanilla-shared"
        assert (prefix_route / "bundle.ini").read_text() == \
            "vanilla-companion"
    print("✓ custom rules preserve global game/prefix first-match ordering")


def test_custom_physical_deploy_modes_unchanged() -> None:
    standard_definition = {
        "name": "Standard Custom Physical Test",
        "game_id": "standard_custom_physical_test",
        "exe_name": "Game.exe",
        "deploy_type": "standard",
        "mod_data_path": "Mods",
    }
    with tempfile.TemporaryDirectory() as tmp:
        game = _FakeStandardCustomGame(Path(tmp), standard_definition)
        game.set_vfs_enabled(False)
        data = game.get_mod_data_path()
        assert data is not None
        data.mkdir()
        (data / "vanilla.txt").write_text("vanilla")
        source = game.staging / "Mod" / "mod.txt"
        source.parent.mkdir(parents=True)
        source.write_text("profile")
        (game.profile / "modlist.txt").write_text(
            "+Mod\n", encoding="utf-8")
        game.filemap.write_text("mod.txt\tMod\n", encoding="utf-8")

        game.deploy(profile="default", mode=LinkMode.HARDLINK)
        assert (data / "mod.txt").read_text() == "profile"
        assert (data / "vanilla.txt").read_text() == "vanilla"
        assert data.with_name("Mods_Core").is_dir()
        assert not (game.profile / STATE_DIR_NAME / MANIFEST_NAME).exists()
        standard_state = game.profile / STATE_DIR_NAME
        standard_state.mkdir()
        (standard_state / "pending").write_text(
            "default\n", encoding="utf-8")
        game.restore()
        assert not (data / "mod.txt").exists()
        assert (data / "vanilla.txt").read_text() == "vanilla"
        assert not data.with_name("Mods_Core").exists()
        assert not (standard_state / "pending").exists()

    root_definition = {
        "name": "Root Custom Physical Test",
        "game_id": "root_custom_physical_test",
        "exe_name": "Game.exe",
        "deploy_type": "root",
    }
    with tempfile.TemporaryDirectory() as tmp:
        game = _FakeRootCustomGame(Path(tmp), root_definition)
        game.set_vfs_enabled(False)
        target = game.game / "shared.txt"
        target.write_text("vanilla")
        source = game.staging / "Mod" / "shared.txt"
        source.parent.mkdir(parents=True)
        source.write_text("profile")
        (game.profile / "modlist.txt").write_text(
            "+Mod\n", encoding="utf-8")
        game.filemap.write_text("shared.txt\tMod\n", encoding="utf-8")

        game.deploy(profile="default", mode=LinkMode.HARDLINK)
        assert target.read_text() == "profile"
        assert (game.filemap.parent / "filemap_deployed.txt").is_file()
        assert not (game.profile / STATE_DIR_NAME / MANIFEST_NAME).exists()
        root_state = game.profile / STATE_DIR_NAME
        root_state.mkdir()
        (root_state / "pending").write_text(
            "default\n", encoding="utf-8")
        game.restore()
        assert target.read_text() == "vanilla"
        assert not (game.filemap.parent / "filemap_deployed.txt").exists()
        assert not (root_state / "pending").exists()
    print("✓ VFS cleanup coexists with standard/root physical restore markers")


def test_custom_pending_prefix_restore_and_traversal_guard() -> None:
    prefix_definition = {
        "name": "Root Custom Prefix Recovery Test",
        "game_id": "root_custom_prefix_recovery_test",
        "exe_name": "Game.exe",
        "deploy_type": "root",
        "custom_routing_rules": [
            {
                "dest": "drive_c/users/test/Saves",
                "filenames": ["Settings.ini"],
                "flatten": True,
                "to_prefix": True,
            },
        ],
    }
    with tempfile.TemporaryDirectory() as tmp:
        game = _FakeRootCustomGame(Path(tmp), prefix_definition)
        (game.game / "Game.exe").write_text("launcher")
        target = game.prefix / "drive_c" / "users" / "test" / "Saves" \
            / "Settings.ini"
        target.parent.mkdir(parents=True)
        target.write_text("vanilla-prefix")
        source = game.staging / "PrefixMod" / "Settings.ini"
        source.parent.mkdir(parents=True)
        source.write_text("profile-prefix")
        (game.profile / "modlist.txt").write_text(
            "+PrefixMod\n", encoding="utf-8")
        game.filemap.write_text(
            "Settings.ini\tPrefixMod\n", encoding="utf-8")

        def _interrupt_after_transfer(_done: int, _total: int) -> None:
            raise RuntimeError("injected custom-prefix interruption")

        state = game.profile / STATE_DIR_NAME
        state.mkdir()
        (state / "pending").write_text("default\n", encoding="utf-8")
        try:
            deploy_custom_rules(
                game.filemap,
                game.game,
                game.staging,
                rules=game.custom_routing_rules,
                mode=LinkMode.HARDLINK,
                progress_fn=_interrupt_after_transfer,
                prefix_root=game.prefix,
            )
        except RuntimeError as exc:
            assert "injected custom-prefix interruption" in str(exc)
        else:
            raise AssertionError("custom-prefix interruption did not propagate")

        assert (state / "pending").is_file()
        assert not (state / MANIFEST_NAME).exists()
        assert target.read_text() == "profile-prefix"
        # The conservative destination journal is published before placement,
        # so a callback interruption after transfer remains fully reversible.
        assert (game.filemap.parent / "custom_rules_deployed.txt").is_file()
        assert (game.filemap.parent / "custom_rules_prefix_backup").is_dir()
        game.set_vfs_enabled(False)
        game.restore()
        assert target.read_text() == "vanilla-prefix"
        assert not (game.filemap.parent / "custom_rules_prefix_backup").exists()
        assert not (state / "pending").exists()

    # Simulate the smaller interruption window after an original was moved to
    # backup but before the planned destination journal was published.
    with tempfile.TemporaryDirectory() as tmp:
        game = _FakeRootCustomGame(Path(tmp), prefix_definition)
        target = game.prefix / "drive_c" / "users" / "test" / "Saves" \
            / "Settings.ini"
        target.parent.mkdir(parents=True)
        target.write_text("vanilla-prefix")
        backup = game.filemap.parent / "custom_rules_prefix_backup" \
            / target.relative_to(game.prefix)
        backup.parent.mkdir(parents=True)
        target.rename(backup)
        state = game.profile / STATE_DIR_NAME
        state.mkdir()
        (state / "pending").write_text("default\n", encoding="utf-8")
        assert not (game.filemap.parent / "custom_rules_deployed.txt").exists()
        game.set_vfs_enabled(False)
        game.restore()
        assert target.read_text() == "vanilla-prefix"
        assert not (game.filemap.parent / "custom_rules_prefix_backup").exists()
        assert not (state / "pending").exists()

    traversal_definition = {
        "name": "Standard Custom Traversal Test",
        "game_id": "standard_custom_traversal_test",
        "exe_name": "Game.exe",
        "deploy_type": "standard",
        "mod_data_path": "Mods",
        "custom_routing_rules": [
            {
                "dest": "../escaped",
                "filenames": ["escape.dll"],
                "flatten": True,
            },
        ],
    }
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        game = _FakeStandardCustomGame(root, traversal_definition)
        (game.game / "Game.exe").write_text("launcher")
        (game.game / "Mods").mkdir()
        source = game.staging / "Escape" / "escape.dll"
        source.parent.mkdir(parents=True)
        source.write_text("escape")
        (game.profile / "modlist.txt").write_text(
            "+Escape\n", encoding="utf-8")
        game.filemap.write_text("escape.dll\tEscape\n", encoding="utf-8")
        try:
            _deploy_custom_fixture(game, profile="default")
        except RuntimeError as exc:
            assert "must remain relative" in str(exc)
        else:
            raise AssertionError("VFS custom-rule traversal was accepted")
        assert not (game.game.parent / "escaped").exists()
        assert (game.profile / STATE_DIR_NAME / "pending").is_file()
        game.set_vfs_enabled(False)
        game.restore()
        assert not (game.profile / STATE_DIR_NAME / "pending").exists()
    print("✓ custom VFS pending-prefix recovery and routing traversal guard")


def test_custom_rule_symlink_restore_and_redeploy_self_heal() -> None:
    definition = {
        "name": "Custom Rule Journal Test",
        "game_id": "custom_rule_journal_test",
        "exe_name": "Game.exe",
        "deploy_type": "root",
        "custom_routing_rules": [
            {
                "dest": "drive_c/users/test/Saves",
                "filenames": ["Settings.ini"],
                "flatten": True,
                "to_prefix": True,
            },
        ],
    }

    # A symlink to a directory is reported by os.walk as a directory entry.
    # Restore must move the link itself back, never traverse or discard it.
    with tempfile.TemporaryDirectory() as tmp:
        game = _FakeRootCustomGame(Path(tmp), definition)
        original_dir = game.prefix / "original-settings"
        original_dir.mkdir()
        target = game.prefix / "drive_c" / "users" / "test" / "Saves" \
            / "Settings.ini"
        target.parent.mkdir(parents=True)
        target.symlink_to(original_dir, target_is_directory=True)
        original_link = os.readlink(target)
        source = game.staging / "PrefixMod" / "Settings.ini"
        source.parent.mkdir(parents=True)
        source.write_text("profile-settings")
        game.filemap.write_text(
            "Settings.ini\tPrefixMod\n", encoding="utf-8")

        deploy_custom_rules(
            game.filemap,
            game.game,
            game.staging,
            rules=game.custom_routing_rules,
            mode=LinkMode.HARDLINK,
            prefix_root=game.prefix,
        )
        assert not target.is_symlink()
        assert target.read_text() == "profile-settings"
        restore_custom_rules(
            game.filemap,
            game.game,
            rules=game.custom_routing_rules,
            prefix_root=game.prefix,
        )
        assert target.is_symlink()
        assert os.readlink(target) == original_link
        assert target.resolve() == original_dir.resolve()

    # A second deploy without an explicit restore must first recover the
    # original from the first journal, then create a fresh backup of it.
    with tempfile.TemporaryDirectory() as tmp:
        game = _FakeRootCustomGame(Path(tmp), definition)
        target = game.prefix / "drive_c" / "users" / "test" / "Saves" \
            / "Settings.ini"
        target.parent.mkdir(parents=True)
        target.write_text("vanilla-prefix")
        source = game.staging / "PrefixMod" / "Settings.ini"
        source.parent.mkdir(parents=True)
        source.write_text("profile-one")
        game.filemap.write_text(
            "Settings.ini\tPrefixMod\n", encoding="utf-8")
        kwargs = {
            "rules": game.custom_routing_rules,
            "mode": LinkMode.HARDLINK,
            "prefix_root": game.prefix,
        }

        deploy_custom_rules(
            game.filemap, game.game, game.staging, **kwargs)
        assert target.read_text() == "profile-one"
        source.unlink()
        source.write_text("profile-two")
        deploy_custom_rules(
            game.filemap, game.game, game.staging, **kwargs)
        assert target.read_text() == "profile-two"
        backup = game.filemap.parent / "custom_rules_prefix_backup" \
            / target.relative_to(game.prefix)
        assert backup.read_text() == "vanilla-prefix"
        restore_custom_rules(
            game.filemap,
            game.game,
            rules=game.custom_routing_rules,
            prefix_root=game.prefix,
        )
        assert target.read_text() == "vanilla-prefix"
        assert not (game.filemap.parent / "custom_rules_deployed.txt").exists()
        assert not (game.filemap.parent / "custom_rules_prefix_backup").exists()
    print("✓ custom-rule symlink recovery and deploy-twice self-heal")


def test_custom_rule_prefix_restore_failure_is_retryable() -> None:
    """A failed prefix unlink must preserve both recovery artifacts."""
    definition = {
        "name": "Custom Rule Prefix Restore Retry Test",
        "game_id": "custom_rule_prefix_restore_retry_test",
        "exe_name": "Game.exe",
        "deploy_type": "root",
        "custom_routing_rules": [
            {
                "dest": "drive_c/users/test/Saves",
                "filenames": ["Settings.ini"],
                "flatten": True,
                "to_prefix": True,
            },
        ],
    }
    with tempfile.TemporaryDirectory() as tmp:
        game = _FakeRootCustomGame(Path(tmp), definition)
        target = game.prefix / "drive_c" / "users" / "test" / "Saves" \
            / "Settings.ini"
        target.parent.mkdir(parents=True)
        target.write_text("vanilla-prefix")
        source = game.staging / "PrefixMod" / "Settings.ini"
        source.parent.mkdir(parents=True)
        source.write_text("profile-prefix")
        game.filemap.write_text(
            "Settings.ini\tPrefixMod\n", encoding="utf-8")

        deploy_custom_rules(
            game.filemap,
            game.game,
            game.staging,
            rules=game.custom_routing_rules,
            mode=LinkMode.HARDLINK,
            prefix_root=game.prefix,
        )
        log_path = game.filemap.parent / "custom_rules_deployed.txt"
        backup_root = game.filemap.parent / "custom_rules_prefix_backup"
        backup = backup_root / target.relative_to(game.prefix)
        assert target.read_text() == "profile-prefix"
        assert backup.read_text() == "vanilla-prefix"

        real_unlink = os.unlink
        failed = False

        def _fail_target_once(path, *args, **kwargs) -> None:
            nonlocal failed
            if Path(path) == target and not failed:
                failed = True
                raise PermissionError("injected prefix unlink failure")
            real_unlink(path, *args, **kwargs)

        try:
            with patch("Utils.deploy_custom_rules.os.unlink", _fail_target_once):
                restore_custom_rules(
                    game.filemap,
                    game.game,
                    rules=game.custom_routing_rules,
                    prefix_root=game.prefix,
                )
        except RuntimeError as exc:
            message = str(exc).lower()
            assert "retained" in message
            assert "another restore attempt" in message
        else:
            raise AssertionError("custom-rule prefix unlink failure was ignored")

        assert target.read_text() == "profile-prefix"
        assert log_path.read_text(encoding="utf-8").splitlines() == [
            str(target),
        ]
        assert backup.read_text() == "vanilla-prefix"

        removed = restore_custom_rules(
            game.filemap,
            game.game,
            rules=game.custom_routing_rules,
            prefix_root=game.prefix,
        )
        assert removed == 1
        assert target.read_text() == "vanilla-prefix"
        assert not log_path.exists()
        assert not backup_root.exists()
    print("✓ custom-rule prefix restore retains recovery state for retry")


def test_external_separator_cleanup_failure_is_retryable() -> None:
    """A failed destination unlink must not consume its original backup."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        profile = root / "profile"
        profile.mkdir()
        target = root / "external" / "nested" / "Settings.ini"
        target.parent.mkdir(parents=True)
        target.write_text("profile-settings")

        log_path = profile / "custom_deploy_log.txt"
        log_path.write_text(str(target) + "\n", encoding="utf-8")
        backup_root = profile / "custom_deploy_backup"
        backup = backup_root / target.relative_to(target.anchor)
        backup.parent.mkdir(parents=True)
        backup.write_text("vanilla-settings")

        real_unlink = Path.unlink
        failed = False

        def _fail_target_once(path: Path, *args, **kwargs) -> None:
            nonlocal failed
            if path == target and not failed:
                failed = True
                raise PermissionError("injected external unlink failure")
            real_unlink(path, *args, **kwargs)

        try:
            with patch.object(Path, "unlink", _fail_target_once):
                cleanup_custom_deploy_dirs(profile, entries=[])
        except RuntimeError as exc:
            message = str(exc).lower()
            assert "retained" in message
            assert "another restore attempt" in message
        else:
            raise AssertionError("external cleanup unlink failure was ignored")

        assert target.read_text() == "profile-settings"
        assert log_path.read_text(encoding="utf-8").splitlines() == [
            str(target),
        ]
        assert backup.read_text() == "vanilla-settings"

        removed = cleanup_custom_deploy_dirs(profile, entries=[])
        assert removed == 1
        assert target.read_text() == "vanilla-settings"
        assert not log_path.exists()
        assert not backup_root.exists()
    print("✓ external separator cleanup retains recovery state for retry")


def test_ue5_nested_project_shadow_view() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        game = _FakeUE5Game(Path(tmp))
        engine_file = game.install / "Engine" / "Binaries" / "ThirdParty" \
            / "engine-runtime.dll"
        engine_file.parent.mkdir(parents=True)
        engine_file.write_text("vanilla-engine")
        launcher = (
            game.game / "bINARIES" / "wIN64"
            / "TestProject-Win64-Shipping.exe"
        )
        launcher.parent.mkdir(parents=True)
        launcher.write_text("vanilla-launcher")
        vanilla_pak = game.game / "Content" / "Paks" / "~mods" \
            / "ProfileBundle.pak"
        vanilla_pak.parent.mkdir(parents=True)
        vanilla_pak.write_text("vanilla-pak")

        bundle = game.staging / "ProfileBundle" / "bundle"
        bundle.mkdir(parents=True)
        for extension in (".pak", ".utoc", ".ucas"):
            (bundle / f"ProfileBundle{extension}").write_text(
                f"profile{extension}")
        ue4ss = game.staging / "ProfileBundle" / "Binaries" / "Win64" \
            / "UE4SS" / "UE4SS.dll"
        ue4ss.parent.mkdir(parents=True)
        ue4ss.write_text("profile-ue4ss")

        # The final filename differs too: Wine accepts this spelling, and the
        # VFS selector must return the actual published path for Proton.
        loader = game.root_folder / "Binaries" / "Win64" \
            / "PROFILELOADER.EXE"
        loader.parent.mkdir(parents=True)
        loader.write_text("profile-loader")
        project_config = game.root_folder / "Config" / "profile.ini"
        project_config.parent.mkdir(parents=True)
        project_config.write_text("profile-config")

        (game.profile / "modlist.txt").write_text(
            "+ProfileBundle\n", encoding="utf-8")
        filemap = game.profiles / "filemap.txt"
        filemap.write_text(
            "bundle/ProfileBundle.pak\tProfileBundle\n"
            "bundle/ProfileBundle.utoc\tProfileBundle\n"
            "bundle/ProfileBundle.ucas\tProfileBundle\n"
            "Binaries/Win64/UE4SS/UE4SS.dll\tProfileBundle\n",
            encoding="utf-8",
        )

        mod_files_stub = types.ModuleType("Utils.mod_files")
        mod_files_stub.excluded_raw_by_mod = lambda _profile: {}
        with patch.dict(sys.modules, {"Utils.mod_files": mod_files_stub}):
            game.deploy(profile="default")

        state = game.profile / STATE_DIR_NAME
        view = state / "view"
        project_view = view / "TestProject"
        manifest = json.loads((state / MANIFEST_NAME).read_text())
        assert Path(manifest["game_root"]) == game.install
        assert Path(manifest["data_root"]) == game.game
        assert game.get_vfs_game_root() == game.install
        assert game.get_vfs_data_root() == game.game

        # The complete outer install is shadowed, not just the nested project.
        assert (view / engine_file.relative_to(game.install)).read_text() == \
            "vanilla-engine"
        assert (view / launcher.relative_to(game.install)).read_text() == \
            "vanilla-launcher"

        pak_root = project_view / "Content" / "Paks" / "~mods"
        for extension in (".pak", ".utoc", ".ucas"):
            assert (pak_root / f"ProfileBundle{extension}").read_text() == \
                f"profile{extension}"
        assert (project_view / "bINARIES" / "wIN64" / "ue4ss"
                / "UE4SS.dll").read_text() == "profile-ue4ss"

        # UE5 Root_Folder is project-relative even though the VFS mount root
        # is the outer install directory.
        assert (project_view / "bINARIES" / "wIN64"
                / "PROFILELOADER.EXE").read_text() == "profile-loader"
        assert (project_view / "Config" / "profile.ini").read_text() == \
            "profile-config"
        assert not (view / "Binaries" / "Win64" / "ProfileLoader.exe").exists()
        assert not (view / "Config" / "profile.ini").exists()

        expected_loader = (
            game.install / "TestProject" / "bINARIES" / "wIN64"
            / "PROFILELOADER.EXE"
        )
        assert game.get_vfs_launch_exe() == expected_loader
        assert is_game_launch_exe(game, expected_loader)
        assert not is_game_launch_exe(
            game, game.install / "TestProject" / "Tools" / "Editor.exe")
        replaced = prefer_virtual_executable(
            game, ["proton", str(launcher)], game.preferred_launch_exe)
        assert replaced[-1] == str(expected_loader)

        # Publishing the profile view must leave every physical install file
        # and directory untouched.
        assert engine_file.read_text() == "vanilla-engine"
        assert launcher.read_text() == "vanilla-launcher"
        assert vanilla_pak.read_text() == "vanilla-pak"
        assert not (game.game / "Content" / "Paks" / "~mods"
                    / "ProfileBundle.utoc").exists()
        assert not (game.game / "Content" / "Paks" / "~mods"
                    / "ProfileBundle.ucas").exists()
        assert not (game.game / "bINARIES" / "wIN64" / "ue4ss").exists()
        assert not (game.game / "bINARIES" / "wIN64"
                    / "PROFILELOADER.EXE").exists()
        assert not (game.game / "Config" / "profile.ini").exists()

        game.restore()
        assert not (state / MANIFEST_NAME).exists()
        assert vanilla_pak.read_text() == "vanilla-pak"
    print("✓ UE5 outer-install/project routing and executable resolution")


def test_custom_ue5_factory_vfs_contract() -> None:
    definition = {
        "name": "Custom UE5 VFS Test",
        "game_id": "custom_ue5_vfs_test",
        "exe_name": "Project/Binaries/Win64/Project.exe",
        "deploy_type": "ue5",
    }
    with patch.object(UE5Game, "load_paths", return_value=False):
        game = make_custom_game(definition)
    assert isinstance(game, Ue5CustomGame)
    assert game.supports_profile_vfs
    assert "vfs_enabled" in game.profile_overridable_settings
    assert game.vfs_root_payload_targets_data
    print("✓ custom UE5 factory exposes the profile VFS contract")


def test_ue5_external_routes_restore_and_failure_rollback() -> None:
    def _prepare(game: _FakeUE5RoutedGame, root: Path):
        launcher = (
            game.game / "Binaries" / "Win64"
            / "TestProject-Win64-Shipping.exe"
        )
        launcher.parent.mkdir(parents=True)
        launcher.write_text("launcher")

        prefix_target = (
            game.prefix / "drive_c" / "users" / "test" / "AppData"
            / "Local" / "TestGame" / "Engine.ini"
        )
        prefix_target.parent.mkdir(parents=True)
        prefix_target.write_text("vanilla-prefix")
        external_root = root / "external-config"
        external_target = external_root / "Settings.json"
        external_root.mkdir()
        external_target.write_text("vanilla-external")

        prefix_source = game.staging / "PrefixMod" / "Engine.ini"
        prefix_source.parent.mkdir(parents=True)
        prefix_source.write_text("profile-prefix")
        external_source = game.staging / "ExternalMod" / "Settings.json"
        external_source.parent.mkdir(parents=True)
        external_source.write_text("profile-external")

        (game.profile / "modlist.txt").write_text(
            "+PrefixMod\n-External_separator\n+ExternalMod\n",
            encoding="utf-8",
        )
        (game.profile / "profile_state.json").write_text(json.dumps({
            "separator_deploy_paths": {
                "External_separator": {
                    "path": str(external_root),
                    "mode": "symlink",
                },
            },
        }), encoding="utf-8")
        (game.profiles / "filemap.txt").write_text(
            "Engine.ini\tPrefixMod\nSettings.json\tExternalMod\n",
            encoding="utf-8",
        )
        return prefix_target, external_target

    mod_files_stub = types.ModuleType("Utils.mod_files")
    mod_files_stub.excluded_raw_by_mod = lambda _profile: {}

    with tempfile.TemporaryDirectory() as tmp:
        game = _FakeUE5RoutedGame(Path(tmp))
        prefix_target, external_target = _prepare(game, Path(tmp))
        with patch.dict(sys.modules, {"Utils.mod_files": mod_files_stub}):
            game.deploy(profile="default")
        assert prefix_target.read_text() == "profile-prefix"
        assert external_target.read_text() == "profile-external"
        assert external_target.is_symlink()
        assert not (game.profiles / "ue5_deployed.txt").exists()
        # Redeploy must first reverse the previous physical side effects; an
        # old mod hardlink must never be mistaken for the vanilla backup.
        with patch.dict(sys.modules, {"Utils.mod_files": mod_files_stub}):
            game.deploy(profile="default")
        assert prefix_target.read_text() == "profile-prefix"
        assert external_target.read_text() == "profile-external"
        assert external_target.is_symlink()
        game.restore()
        assert prefix_target.read_text() == "vanilla-prefix"
        assert external_target.read_text() == "vanilla-external"

    with tempfile.TemporaryDirectory() as tmp:
        game = _FakeUE5FailingGame(Path(tmp))
        prefix_target, external_target = _prepare(game, Path(tmp))
        try:
            with patch.dict(sys.modules, {"Utils.mod_files": mod_files_stub}):
                game.deploy(profile="default")
        except RuntimeError as exc:
            assert "injected UE5 layer hook failure" in str(exc)
        else:
            raise AssertionError("injected UE5 VFS failure did not propagate")
        assert prefix_target.read_text() == "vanilla-prefix"
        assert external_target.read_text() == "vanilla-external"
        assert not (game.profiles / "ue5_deployed.txt").exists()
        game.restore()
        assert prefix_target.read_text() == "vanilla-prefix"
        assert external_target.read_text() == "vanilla-external"

    # A brand-new external file has no vanilla backup to reveal its path.
    # The incremental journal must therefore be durable before placement so
    # an exception between transfer and the final UE5 manifest cannot leak it.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        game = _FakeUE5Game(root)
        launcher = game.install / game.exe_name
        launcher.parent.mkdir(parents=True)
        launcher.write_text("launcher")
        external_root = root / "new-external-config"
        external_root.mkdir()
        external_target = external_root / "NewSettings.json"
        source = game.staging / "ExternalMod" / external_target.name
        source.parent.mkdir(parents=True)
        source.write_text("profile-external")
        (game.profile / "modlist.txt").write_text(
            "-External_separator\n+ExternalMod\n", encoding="utf-8")
        (game.profile / "profile_state.json").write_text(json.dumps({
            "separator_deploy_paths": {
                "External_separator": {
                    "path": str(external_root),
                    "mode": "hardlink",
                },
            },
        }), encoding="utf-8")
        (game.profiles / "filemap.txt").write_text(
            f"{external_target.name}\tExternalMod\n", encoding="utf-8")

        def _interrupt_after_placement(_done: int, _total: int) -> None:
            raise RuntimeError("injected progress callback failure")

        try:
            with patch.dict(sys.modules, {"Utils.mod_files": mod_files_stub}):
                game.deploy(
                    profile="default",
                    progress_fn=_interrupt_after_placement,
                )
        except RuntimeError as exc:
            assert "injected progress callback failure" in str(exc)
        else:
            raise AssertionError("progress callback failure did not propagate")
        assert not external_target.exists()
        game.restore()
        assert not external_target.exists()
    print("✓ UE5 prefix/external restore and failed-build rollback")


def test_ue5_physical_mods_txt_restore() -> None:
    """VFS-specific mods.txt handling must not alter physical deployment."""
    with tempfile.TemporaryDirectory() as tmp:
        game = _FakeUE5ManagedModsGame(Path(tmp))
        game._settings["vfs_enabled"] = False

        mods_dir = game.game / "Binaries" / "Win64" / "ue4ss" / "Mods"
        mods_dir.mkdir(parents=True)
        mods_txt = mods_dir / "mods.txt"
        original = "VanillaBuiltIn : 1\n; preserve this comment\n"
        mods_txt.write_text(original, encoding="utf-8")

        shipped_mods_txt = (
            game.staging / "LuaMod" / "Binaries" / "Win64" / "ue4ss"
            / "Mods" / "mods.txt"
        )
        shipped_mods_txt.parent.mkdir(parents=True)
        shipped_mods_txt.write_text("ModBuiltIn : 1\n", encoding="utf-8")
        main_lua = game.staging / "LuaMod" / "Example" / "Scripts" / "main.lua"
        main_lua.parent.mkdir(parents=True)
        main_lua.write_text("return {}\n", encoding="utf-8")
        (game.profile / "modlist.txt").write_text("+LuaMod\n", encoding="utf-8")
        (game.profiles / "filemap.txt").write_text(
            "Binaries/Win64/ue4ss/Mods/mods.txt\tLuaMod\n"
            "Example/Scripts/main.lua\tLuaMod\n",
            encoding="utf-8",
        )

        game.deploy(profile="default")
        game.restore()
        assert mods_txt.read_text(encoding="utf-8") == original
    print("✓ physical UE5 deploy restores the original UE4SS mods.txt")


def test_oblivion_restore_handles_physical_vfs_coexistence() -> None:
    """A physical UE5 marker must survive VFS-state classification."""
    class _RestoreFixture(OblivionRemastered):
        def __init__(self, root: Path):
            self.physical_manifest = root / "ue5_deployed.txt"
            self.external_manifest = root / "external_deployed.txt"
            self.prefix_context = root / "prefix_context.json"
            self.plugins_removals = 0

        def _ue5_deployed_manifest_path(self) -> Path:
            return self.physical_manifest

        def _vfs_external_manifest_path(
            self, profile: str | None = None,
        ) -> Path:
            return self.external_manifest

        def _vfs_prefix_context_path(
            self, profile: str | None = None,
        ) -> Path:
            return self.prefix_context

        def _remove_plugins_txt_symlink(self, log_fn) -> None:
            self.plugins_removals += 1

    with tempfile.TemporaryDirectory() as tmp:
        game = _RestoreFixture(Path(tmp))
        game.physical_manifest.write_text("physical")
        with (
            patch.object(UE5Game, "restore", return_value=None) as base_restore,
            patch("Utils.vfs.has_deployment_state", return_value=True),
        ):
            game.restore()
        base_restore.assert_called_once()
        assert game.plugins_removals == 1

        # Pure VFS state keeps Plugins.txt private to the view and must not
        # run the physical plugin cleanup path.
        game.physical_manifest.unlink()
        with (
            patch.object(UE5Game, "restore", return_value=None),
            patch("Utils.vfs.has_deployment_state", return_value=True),
        ):
            game.restore()
        assert game.plugins_removals == 1
    print("✓ Oblivion restore distinguishes pure VFS from coexistence")


def test_subnautica_shadow_view() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        game = _FakeSubnauticaGame(Path(tmp))
        (game.game / game.exe_name).write_text("subnautica")

        plugin = game.staging / "MapMod" / "MapMod.dll"
        plugin.parent.mkdir(parents=True)
        plugin.write_text("plugin")
        (plugin.parent / "meta.ini").write_text(
            "[thunderstore]\n"
            "namespace = ExampleAuthor\n"
            "name = MapMod\n"
            "version = 1.0.0\n",
            encoding="utf-8",
        )
        doorstop = game.staging / "BepInExPack" / "winhttp.dll"
        doorstop.parent.mkdir(parents=True)
        doorstop.write_text("doorstop")
        core = game.staging / "BepInExPack" / "core" / "BepInEx.Core.dll"
        core.parent.mkdir(parents=True)
        core.write_text("core")
        staged_save = game.staging / "Saves" / "slot0001" / "gameinfo.json"
        staged_save.parent.mkdir(parents=True)
        staged_save.write_text("profile-save")
        prefix_root = Path(tmp) / "prefix"
        prefix_root.mkdir()
        (prefix_root / "pfx").symlink_to(".", target_is_directory=True)
        external_saves = (
            prefix_root / "pfx" / "drive_c" / "users" / "steamuser"
            / "AppData" / "LocalLow" / "Unknown Worlds" / "Subnautica"
            / "Subnautica"
        )
        original_save = external_saves / "slot0001" / "gameinfo.json"
        original_save.parent.mkdir(parents=True)
        original_save.write_text("original-save")

        (game.profile / "modlist.txt").write_text(
            "+MapMod\n+BepInExPack\n"
            "-External Saves_separator\n+Saves\n",
            encoding="utf-8",
        )
        (game.profile / "profile_state.json").write_text(json.dumps({
            "separator_deploy_paths": {
                "External Saves_separator": {
                    "path": str(external_saves),
                    "mode": "hardlink",
                },
            },
        }), encoding="utf-8")
        filemap = game.profiles / "filemap.txt"
        filemap.write_text(
            "MapMod.dll\tMapMod\n"
            "winhttp.dll\tBepInExPack\n"
            "core/BepInEx.Core.dll\tBepInExPack\n"
            "slot0001/gameinfo.json\tSaves\n",
            encoding="utf-8",
        )
        assert game._vfs_per_mod_subdirs(game.profile, game.staging) == {
            "MapMod": "ExampleAuthor-MapMod",
        }
        mod_files_stub = types.ModuleType("Utils.mod_files")
        mod_files_stub.excluded_raw_by_mod = lambda _profile: {}
        with patch.dict(sys.modules, {"Utils.mod_files": mod_files_stub}):
            game.deploy(profile="default")
        state = game.profile / STATE_DIR_NAME
        view = state / "view"
        assert (view / "Subnautica.exe").read_text() == "subnautica"
        assert (view / "winhttp.dll").read_text() == "doorstop"
        assert (view / "BepInEx" / "core"
                / "BepInEx.Core.dll").read_text() == "core"
        assert (view / "BepInEx" / "plugins" / "ExampleAuthor-MapMod"
                / "MapMod.dll").read_text() == "plugin"
        assert original_save.read_text() == "profile-save"
        assert (game.profiles / "custom_deploy_log.txt").is_file()
        assert (game.profiles / "custom_deploy_backup").is_dir()
        assert not (view / "BepInEx" / "plugins" / "slot0001"
                    / "gameinfo.json").exists()
        assert not (game.game / "BepInEx").exists()
        assert game.get_vfs_launch_exe() == game.game / "Subnautica.exe"

        visible = subprocess.run(
            wrap_command(game, [
                "/bin/sh", "-c",
                'test -f "$1/winhttp.dll" && '
                'test -f "$1/BepInEx/plugins/ExampleAuthor-MapMod/MapMod.dll"',
                "sh", str(game.game),
            ]),
            text=True, capture_output=True, check=False,
        )
        assert visible.returncode == 0, visible.stderr
        game.restore()
        assert not (state / MANIFEST_NAME).exists()
        assert not (game.game / "BepInEx").exists()
        assert original_save.read_text() == "original-save"
        assert not (game.profiles / "custom_deploy_log.txt").exists()
        assert not (game.profiles / "custom_deploy_backup").exists()

        below_zero = Subnautica_Below_Zero.__new__(Subnautica_Below_Zero)
        assert not below_zero.supports_profile_vfs
    print("✓ Subnautica root/BepInEx routing, external saves, and opt-in boundary")


def test_stardew_shadow_view() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        game = _FakeStardewGame(Path(tmp))
        vanilla_launcher = game.game / "StardewValley"
        vanilla_launcher.write_text("#!/bin/sh\nexit 9\n", encoding="utf-8")
        vanilla_launcher.chmod(0o755)
        vanilla_mod = game.game / "Mods" / "vanilla.txt"
        vanilla_mod.write_text("vanilla")

        good_manifest = game.staging / "GoodMod" / "GoodMod" / "manifest.json"
        good_manifest.parent.mkdir(parents=True)
        good_manifest.write_text('{"Name": "Good Mod"}')

        at_manifest = game.staging / "ATPack" / "MyPack" / "manifest.json"
        at_manifest.parent.mkdir(parents=True)
        at_manifest.write_text(json.dumps({
            "ContentPackFor": {
                "UniqueID": "PeacefulEnd.AlternativeTextures",
            },
        }))
        at_texture = (
            game.staging / "ATPack" / "MyPack" / "textures" / "Chair"
            / "Texture.PNG"
        )
        at_texture.parent.mkdir(parents=True)
        at_texture.write_text("texture")

        orphan_config = game.overwrite / "Orphan" / "config.json"
        orphan_config.parent.mkdir(parents=True)
        orphan_config.write_text("orphan")

        (game.profile / "modlist.txt").write_text(
            "+GoodMod\n+ATPack\n", encoding="utf-8")
        filemap = game.profiles / "filemap.txt"
        filemap.write_text(
            "GoodMod/manifest.json\tGoodMod\n"
            "MyPack/manifest.json\tATPack\n"
            "MyPack/textures/Chair/Texture.PNG\tATPack\n"
            "Orphan/config.json\t[Overwrite]\n",
            encoding="utf-8",
        )

        # A staged SMAPI installation supplies this launcher shim. It must win
        # inside the view while the physical vanilla launcher stays untouched.
        staged_launcher = game.root_folder / "StardewValley"
        staged_launcher.write_text(
            "#!/bin/sh\n"
            "test -f Mods/GoodMod/manifest.json && "
            "test -f Mods/MyPack/Textures/Chair/texture.png && "
            "test ! -e Mods/Orphan/config.json\n",
            encoding="utf-8",
        )
        staged_launcher.chmod(0o755)

        mod_files_stub = types.ModuleType("Utils.mod_files")
        mod_files_stub.excluded_raw_by_mod = lambda _profile: {}
        filemap_stub = types.ModuleType("Utils.filemap")
        filemap_stub.OVERWRITE_NAME = "[Overwrite]"
        with patch.dict(sys.modules, {
            "Utils.mod_files": mod_files_stub,
            "Utils.filemap": filemap_stub,
        }):
            game.deploy(profile="default")

        state = game.profile / STATE_DIR_NAME
        view = state / "view"
        assert game.supports_profile_vfs
        assert (view / "Mods" / "GoodMod" / "manifest.json").is_file()
        assert (view / "Mods" / "MyPack" / "Textures" / "Chair"
                / "texture.png").read_text() == "texture"
        assert not (view / "Mods" / "Orphan" / "config.json").exists()
        assert (view / "Mods" / "vanilla.txt").read_text() == "vanilla"
        assert vanilla_launcher.read_text() == "#!/bin/sh\nexit 9\n"
        assert not (game.game / "Mods" / "GoodMod").exists()

        wrapped = game.wrap_launch_command(
            [str(vanilla_launcher)], env=os.environ.copy())
        assert "bwrap" not in [Path(token).name for token in wrapped]
        assert str(view / "StardewValley") in wrapped

        # The shared Play path must recognize the extensionless Linux binary
        # and never hand it to Proton.
        with patch("Utils.exe_launch.spawn_process_watched") as spawn, \
                patch("Utils.exe_launch.launch_exe_via_proton") as proton:
            launch_game(game)
        proton.assert_not_called()
        spawn.assert_called_once()
        launched = spawn.call_args.args[0]
        assert "bwrap" not in [Path(token).name for token in launched]
        assert str(view / "StardewValley") in launched

        result = subprocess.run(
            wrapped,
            cwd=game.game,
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr

        game.restore()
        assert not (state / MANIFEST_NAME).exists()
        assert vanilla_launcher.read_text() == "#!/bin/sh\nexit 9\n"
        assert vanilla_mod.read_text() == "vanilla"
        assert not (game.game / "Mods" / "GoodMod").exists()
    print("✓ Stardew/SMAPI native shadow launch and deploy rules")


def test_vfs_as_deploy_method() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        game = _FakeBethesdaGame(Path(tmp))
        options = build_quick_configure_options(game)
        assert not any(option["key"] == "vfs_enabled" for option in options)
        deploy_method = next(
            option for option in options if option["key"] == "deploy_mode")
        assert deploy_method["value"] == "vfs"
        assert [value for value, _label in deploy_method["choices"]] == [
            "symlink", "hardlink", "vfs",
        ]

        deploy_method["apply"]("symlink")
        assert not game.vfs_enabled
        assert game.get_deploy_mode() is LinkMode.SYMLINK
        deploy_method["apply"]("vfs")
        assert game.vfs_enabled
        assert game.get_deploy_mode() is LinkMode.SYMLINK

        game._deploy_active = True
        assert deploy_mode_change_blocked(game, "hardlink")
        assert not deploy_mode_change_blocked(game, "vfs")
    print("✓ VFS is exposed as a third deploy method")


def test_deploy_pipeline_stops_on_incomplete_restore() -> None:
    """Only the typed recoverable-state error must abort before deploy."""
    filemap_stub = types.ModuleType("Utils.filemap")
    filemap_stub.build_filemap = lambda *_args, **_kwargs: None
    with patch.dict(sys.modules, {"Utils.filemap": filemap_stub}):
        from Utils.deploy_pipeline import run_deploy_pipeline

    class _PipelineGame:
        name = "Restore Pipeline Test"
        restore_before_deploy = True
        root_folder_deploy_enabled = True
        wine_dll_overrides: dict[str, str] = {}
        mod_folder_strip_prefixes: set[str] = set()

        def __init__(self, root: Path, restore_error: RuntimeError):
            self.root = root
            self.game = root / "game"
            self.profiles = root / "profiles-root"
            self.profile = self.profiles / "profiles" / "default"
            self.staging = self.profiles / "mods"
            self.data = self.game / "Data"
            self.filemap = self.profiles / "filemap.txt"
            self.root_folder = root / "missing-root-folder"
            self.restore_error = restore_error
            self.restore_calls = 0
            self.deploy_calls = 0
            self.saved_profile: tuple[str, str] | None = None
            self._active_profile_dir = self.profile
            for directory in (
                self.game, self.profile, self.staging, self.data,
            ):
                directory.mkdir(parents=True, exist_ok=True)
            (self.game / "Game.exe").write_text("launcher")
            self.filemap.write_text("", encoding="utf-8")

        def get_game_path(self) -> Path:
            return self.game

        def get_profile_root(self) -> Path:
            return self.profiles

        def get_last_deployed_profile(self):
            return None

        def set_active_profile_dir(self, path: Path) -> None:
            self._active_profile_dir = Path(path)

        def load_paths(self) -> None:
            return None

        def restore(self, **_kwargs) -> None:
            self.restore_calls += 1
            raise self.restore_error

        def get_effective_root_folder_path(self) -> Path:
            return self.root_folder

        def get_effective_mod_staging_path(self) -> Path:
            return self.staging

        def get_effective_filemap_path(self) -> Path:
            return self.filemap

        def get_mod_data_path(self) -> Path:
            return self.data

        def get_prefix_path(self):
            return None

        def get_deploy_mode(self) -> LinkMode:
            return LinkMode.HARDLINK

        def begin_deferred_runtime_snapshot(self) -> None:
            return None

        def end_deferred_runtime_snapshot(self):
            return False, []

        def deploy(self, **_kwargs) -> None:
            self.deploy_calls += 1

        def save_last_deployed_profile(
            self, profile: str, *, deploy_mode: str,
        ) -> None:
            self.saved_profile = profile, deploy_mode

        def post_deploy(self, **_kwargs) -> None:
            return None

    def _run(game: _PipelineGame) -> tuple[bool, list[str]]:
        messages: list[str] = []
        mod_files_stub = types.ModuleType("Utils.mod_files")
        mod_files_stub.excluded_raw_by_mod = lambda _profile: {}
        with (
            patch.dict(sys.modules, {"Utils.mod_files": mod_files_stub}),
            patch(
                "Utils.profile_groups.materialize_if_group",
                return_value=None,
            ),
            patch(
                "Utils.deploy_pipeline._build_filemap_for_game",
                return_value=None,
            ),
            patch(
                "Utils.flatpak_sandbox.ensure_symlink_target_access",
                return_value=None,
            ),
            patch(
                "Utils.deploy_pipeline.load_per_mod_strip_prefixes",
                return_value={},
            ),
            patch(
                "Utils.deploy_pipeline.deploy_root_flagged_mods",
                return_value=0,
            ),
        ):
            result = run_deploy_pipeline(
                game,
                "default",
                log_fn=messages.append,
                do_backup=False,
            )
        return result, messages

    with tempfile.TemporaryDirectory() as tmp:
        game = _PipelineGame(
            Path(tmp),
            RestoreIncompleteError("managed recovery state remains"),
        )
        result, messages = _run(game)
        assert not result
        assert game.restore_calls == 1
        assert game.deploy_calls == 0
        assert any("Deploy FAILED" in message for message in messages)
        assert not any("continuing" in message for message in messages)
        assert game._active_profile_dir == game.profile

    # Ordinary RuntimeError remains the historical first-deploy path: it is
    # logged, but deployment is still allowed to proceed to completion.
    with tempfile.TemporaryDirectory() as tmp:
        game = _PipelineGame(
            Path(tmp), RuntimeError("nothing has been deployed yet"))
        result, messages = _run(game)
        assert result
        assert game.restore_calls == 1
        assert game.deploy_calls == 1
        assert game.saved_profile == ("default", "HARDLINK")
        assert any(
            "nothing has been deployed yet" in message
            and "continuing" in message
            for message in messages
        )
        assert any("Deploy finished OK" in message for message in messages)
    print("✓ deploy pipeline stops only for incomplete managed restore state")


def test_flatpak_host_wrap() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        game = _FakeGame(Path(tmp))
        state = _write_manifest(game)
        original = [
            "flatpak-spawn", "--host", "--directory=/tmp", "--env=TEST=1",
            "/usr/bin/python3", "/opt/proton/proton", "run", "SkyrimSE.exe",
        ]
        with patch("Utils.vfs.overlay._inside_flatpak", return_value=True), \
                patch("Utils.vfs.overlay._bubblewrap_binary", return_value="bwrap"), \
                patch("Utils.vfs.overlay._bubblewrap_status", return_value=(True, "")), \
                patch("Utils.vfs.overlay.bubblewrap_status", return_value=(True, "")):
            wrapped = wrap_command(game, original)
        assert wrapped[:5] == [
            "flatpak-spawn", "--host", "--directory=/tmp", "--env=TEST=1", "bwrap",
        ]
        assert wrapped.count("flatpak-spawn") == 1
        assert wrapped[-4:] == original[-4:]

        manifest_path = state / MANIFEST_NAME
        manifest = json.loads(manifest_path.read_text())
        view = state / "view"
        (view / "Data").mkdir(parents=True)
        manifest["backend"] = BACKEND_SHADOW
        manifest["view_root"] = str(view)
        manifest_path.write_text(json.dumps(manifest))
        with patch("Utils.vfs.overlay._inside_flatpak", return_value=True), \
                patch("Utils.vfs.overlay._bubblewrap_binary", return_value="bwrap"), \
                patch("Utils.vfs.overlay._bubblewrap_status", return_value=(True, "")):
            shadow_wrapped = wrap_command(game, original)
        assert shadow_wrapped[:5] == [
            "flatpak-spawn", "--host", "--directory=/tmp", "--env=TEST=1", "bwrap",
        ]
        assert shadow_wrapped.count("flatpak-spawn") == 1
        assert shadow_wrapped[-4:] == original[-4:]
        bind_index = shadow_wrapped.index("--bind")
        assert shadow_wrapped[bind_index:bind_index + 3] == [
            "--bind", str(view), str(game.game),
        ]

        manifest["backend"] = BACKEND_FUSE
        manifest_path.write_text(json.dumps(manifest))
        with patch("Utils.vfs.overlay._inside_flatpak", return_value=True), \
                patch("Utils.vfs.overlay._bubblewrap_status", return_value=(True, "")), \
                patch("Utils.vfs.overlay.fuse_overlay_status", return_value=(True, "")):
            fuse_wrapped = wrap_command(game, original)
        assert fuse_wrapped[:5] == [
            "flatpak-spawn", "--host", "--directory=/tmp", "--env=TEST=1",
            "/bin/sh",
        ]
        assert fuse_wrapped.count("flatpak-spawn") == 1
        assert fuse_wrapped[-4:] == original[-4:]
    print("✓ Flatpak host escape remains outside the VFS wrapper")


def test_umu_uses_shadow_directly() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        game = _FakeGame(Path(tmp))
        # Saved game paths may be symlink aliases (notably Steam's historical
        # ~/.steam path), while deployment manifests store canonical roots.
        canonical_game = Path(tmp) / "canonical-game"
        game.game.rename(canonical_game)
        game.game.symlink_to(canonical_game, target_is_directory=True)
        state = _write_manifest(game)
        manifest_path = state / MANIFEST_NAME
        manifest = json.loads(manifest_path.read_text())
        view = state / "view"
        (view / "Data").mkdir(parents=True)
        real_exe = game.game / "Project" / "Binaries" / "Win64" \
            / "SkyrimSELauncher.exe"
        shadow_exe = view / real_exe.relative_to(game.game)
        real_exe.parent.mkdir(parents=True)
        shadow_exe.parent.mkdir(parents=True)
        real_exe.write_text("real")
        shadow_exe.write_text("shadow")
        manifest["backend"] = BACKEND_SHADOW
        manifest["view_root"] = str(view)
        manifest["game_root"] = str(canonical_game.resolve())
        manifest["data_root"] = str((canonical_game / "Data").resolve())
        manifest_path.write_text(json.dumps(manifest))

        # Stand in for umu-run and validate both the rewritten executable and
        # the working directory inherited by its Proton subprocess.
        fake_umu = Path(tmp) / "umu-run"
        fake_umu.write_text(
            '#!/bin/sh\n'
            'test "$PWD" = "$2" && '
            'test "$1" = "$2/SkyrimSELauncher.exe" && '
            'test "$STEAM_COMPAT_INSTALL_PATH" = "$3"\n',
            encoding="utf-8",
        )
        fake_umu.chmod(0o755)
        original = [
            str(fake_umu), str(real_exe), str(shadow_exe.parent), str(view),
        ]
        # Direct UMU shadow launches do not depend on an outer bwrap mount.
        with patch("Utils.vfs.overlay._bubblewrap_status",
                   return_value=(False, "bwrap unavailable")):
            wrapped = wrap_command(game, original)

        assert "bwrap" not in [Path(token).name for token in wrapped]
        assert str(real_exe) not in wrapped
        assert str(shadow_exe) in wrapped
        result = subprocess.run(
            wrapped, text=True, capture_output=True, check=False)
        assert result.returncode == 0, result.stderr
    print("✓ UMU launches the materialized shadow directly")


def test_steam_runtime_uses_shadow_directly() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        game = _FakeGame(Path(tmp))
        canonical_game = Path(tmp) / "canonical-game"
        game.game.rename(canonical_game)
        game.game.symlink_to(canonical_game, target_is_directory=True)
        state = _write_manifest(game)
        manifest_path = state / MANIFEST_NAME
        manifest = json.loads(manifest_path.read_text())
        view = state / "view"
        (view / "Data").mkdir(parents=True)
        real_exe = game.game / "Project" / "Binaries" / "Win64" \
            / "nvse_loader.exe"
        shadow_exe = view / real_exe.relative_to(game.game)
        real_exe.parent.mkdir(parents=True)
        shadow_exe.parent.mkdir(parents=True)
        real_exe.write_text("real")
        shadow_exe.write_text("shadow")
        manifest["backend"] = BACKEND_SHADOW
        manifest["view_root"] = str(view)
        manifest["game_root"] = str(canonical_game.resolve())
        manifest["data_root"] = str((canonical_game / "Data").resolve())
        manifest_path.write_text(json.dumps(manifest))

        runtime_dir = Path(tmp) / "SteamLinuxRuntime_sniper"
        runtime_dir.mkdir()
        fake_runtime = runtime_dir / "_v2-entry-point"
        fake_runtime.write_text(
            '#!/bin/sh\n'
            'test "$PWD" = "$2" && '
            'test "$1" = "$2/nvse_loader.exe" && '
            'test "$STEAM_COMPAT_INSTALL_PATH" = "$3" && '
            'case ":$STEAM_COMPAT_MOUNTS:" in '
            '*:"$3":*) exit 0 ;; *) exit 1 ;; esac\n',
            encoding="utf-8",
        )
        fake_runtime.chmod(0o755)
        original = [
            str(fake_runtime), str(real_exe), str(shadow_exe.parent), str(view),
        ]
        env = os.environ.copy()
        env["STEAM_COMPAT_INSTALL_PATH"] = str(game.game)
        env["STEAM_COMPAT_MOUNTS"] = f"/mods:{game.game}"
        with patch("Utils.vfs.overlay._bubblewrap_status",
                   return_value=(False, "bwrap unavailable")):
            wrapped = wrap_command(game, original, env=env)

        assert "bwrap" not in [Path(token).name for token in wrapped]
        assert str(real_exe) not in wrapped
        assert str(shadow_exe) in wrapped
        assert env["STEAM_COMPAT_INSTALL_PATH"] == str(view)
        assert str(view) in env["STEAM_COMPAT_MOUNTS"].split(":")
        result = subprocess.run(
            wrapped, env=env, text=True, capture_output=True, check=False)
        assert result.returncode == 0, result.stderr

        flatpak_original = [
            "flatpak-spawn", "--host", str(fake_runtime), str(real_exe),
            str(shadow_exe.parent), str(view),
        ]
        with patch("Utils.vfs.overlay._inside_flatpak", return_value=True):
            flatpak_wrapped = wrap_command(game, flatpak_original, env=env)
        assert flatpak_wrapped.count("flatpak-spawn") == 1
        assert flatpak_wrapped[:2] == ["flatpak-spawn", "--host"]
        assert flatpak_wrapped.index("/usr/bin/env") > flatpak_wrapped.index("/bin/sh")
    print("✓ Steam Linux Runtime launches the shadow directly")


def test_launcher_aware_handoffs() -> None:
    cli = ["/usr/bin/python3", "/home/test/Amethyst/src/cli.py"]
    with patch("Utils.config_paths.cli_invocation", return_value=cli):
        heroic_game = _FakeHandoffGame("heroic_app_name", "heroic-id")
        with patch("Utils.launch_handoff._heroic_launch_is_flatpak",
                   return_value=True):
            heroic = build_launch_handoff(heroic_game)
        assert heroic is not None and heroic.launcher_id == "heroic"
        assert [field.label for field in heroic.fields] == [
            "Wrapper executable", "Wrapper arguments",
        ]
        assert heroic.fields[0].value == "/usr/bin/flatpak-spawn"
        assert heroic.fields[1].value.startswith("--host /usr/bin/python3 ")
        assert heroic.fields[1].value.endswith(" launch Handoff_Test --")

        lutris_game = _FakeHandoffGame("lutris_slug", "lutris-id")
        with patch(
            "Utils.lutris_finder.find_lutris_launch_info",
            return_value=("lutris-id", False),
        ):
            lutris = build_launch_handoff(lutris_game)
        assert lutris is not None and lutris.launcher_id == "lutris"
        assert lutris.fields[0].label == "Command prefix"
        assert lutris.fields[0].value.endswith(" launch Handoff_Test --")
        assert "flatpak-spawn" not in lutris.fields[0].value

        faugus_game = _FakeHandoffGame("faugus_gameid", "faugus-id")
        with patch(
            "Utils.faugus_finder.find_faugus_launch_info",
            return_value=("faugus-id", True),
        ):
            faugus = build_launch_handoff(faugus_game)
        assert faugus is not None and faugus.launcher_id == "faugus"
        assert faugus.fields[0].label == "Launch Arguments"
        assert faugus.fields[0].value.startswith(
            "/usr/bin/flatpak-spawn --host ")

        steam_game = _FakeHandoffGame("shortcut_appid", "123456")
        with patch("Utils.flatpak_sandbox.sandbox_app_for_game",
                   return_value=None):
            steam = build_launch_handoff(steam_game)
        assert steam is not None and steam.launcher_id == "steam"
        assert steam.fields[0].value.endswith(" -- %command%")
    print("✓ Steam/Heroic/Lutris/Faugus handoff formats")


def main() -> None:
    if not shutil.which("bwrap"):
        raise SystemExit("SKIP: bubblewrap is not installed")
    test_nested_overlay()
    test_fuse_overlay()
    test_layer_build_and_skse_selection()
    test_shared_bethesda_hooks()
    test_generic_mod_data_directory()
    test_vfs_cleanup_failure_remains_discoverable()
    test_standard_custom_shadow_view()
    test_root_custom_shadow_view()
    test_custom_standard_root_factory_vfs_contract()
    test_custom_nondefault_pending_profile_discovery()
    test_custom_missing_prefix_match_fails()
    test_custom_rule_global_first_match_partition()
    test_custom_physical_deploy_modes_unchanged()
    test_custom_pending_prefix_restore_and_traversal_guard()
    test_custom_rule_symlink_restore_and_redeploy_self_heal()
    test_custom_rule_prefix_restore_failure_is_retryable()
    test_external_separator_cleanup_failure_is_retryable()
    test_ue5_nested_project_shadow_view()
    test_custom_ue5_factory_vfs_contract()
    test_ue5_external_routes_restore_and_failure_rollback()
    test_ue5_physical_mods_txt_restore()
    test_oblivion_restore_handles_physical_vfs_coexistence()
    test_subnautica_shadow_view()
    test_stardew_shadow_view()
    test_vfs_as_deploy_method()
    test_deploy_pipeline_stops_on_incomplete_restore()
    test_flatpak_host_wrap()
    test_umu_uses_shadow_directly()
    test_steam_runtime_uses_shadow_directly()
    test_launcher_aware_handoffs()
    print("All profile VFS self-tests passed.")


if __name__ == "__main__":
    main()
